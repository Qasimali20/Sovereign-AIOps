"""Live failover test: Qwen3-TTS (Node-4) goes down, the gateway's `tts` group must move traffic to OmniVoice (Node-1),
then move it back once Qwen3-TTS is healthy again.

Node-4 constraint: Qwen3-TTS only fits in GPU memory if it loads before Whisper (boot order). So the outage stops both:
Whisper's port is held by a placeholder so systemd cannot bring Whisper back early; Qwen3-TTS restarts by itself
(Restart=on-failure); when it is healthy the port is released and Whisper restarts. Everything runs as `tmc`, no sudo.

Usage: python ops/bench/failover_tts.py <gateway-key-file> <ref.wav> [out.json]
"""
from __future__ import annotations

import base64
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.config import NODE_BY_NAME  # noqa: E402  fleet addresses come from .env
NODE4 = f"tmc@{NODE_BY_NAME['Node-4'].lan_ip}"
GATEWAY = "http://127.0.0.1:8790/v1/audio/speech"
REF_TEXT = "The inference cluster recovered after the gateway failed over to the second replica. Latency is back to normal."
LINES = ["Node four is serving speech.", "The nightly backup finished.", "Two approvals are waiting.",
         "Latency is back to normal.", "The rollback restored version one."]

events: list[tuple[float, str]] = []
rows: list[dict] = []
stop = threading.Event()


def ev(msg: str) -> None:
    events.append((time.time(), msg))
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ssh(cmd: str, timeout: float = 60) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", NODE4, cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def health(port: int) -> bool:
    return ssh(f"curl -s -m3 -o /dev/null -w '%{{http_code}}' localhost:{port}/health", 15) == "200"


def client(key: str, ref: str) -> None:
    i = 0
    with httpx.Client(timeout=120, headers={"Authorization": f"Bearer {key}"}) as c:
        while not stop.is_set():
            i += 1
            body = {"model": "tts", "task_type": "Base", "input": LINES[i % len(LINES)], "ref_audio": "data:audio/wav;base64," + ref,
                    "ref_text": REF_TEXT, "response_format": "wav"}
            t = time.time()
            row = {"t": t}
            try:
                r = c.post(GATEWAY, json=body)
                row |= {"status": r.status_code, "model": r.headers.get("x-aiops-model"), "node": r.headers.get("x-aiops-node"),
                        "bytes": len(r.content)}
            except Exception as e:
                row |= {"status": 0, "error": type(e).__name__}
            row["latency"] = time.time() - t
            rows.append(row)
            time.sleep(max(0.0, 2.0 - row["latency"]))


def wait_for(cond, what: str, limit: float, every: float = 3) -> float | None:
    end = time.time() + limit
    while time.time() < end:
        if cond():
            ev(what)
            return time.time()
        time.sleep(every)
    ev(f"TIMEOUT waiting for: {what}")
    return None


def served_by(model_part: str, since: float) -> bool:
    return any(r["t"] >= since and r.get("status") == 200 and model_part in (r.get("model") or "") for r in rows)


def main() -> None:
    key = open(sys.argv[1]).read().strip()
    ref = base64.b64encode(open(sys.argv[2], "rb").read()).decode()
    out = sys.argv[3] if len(sys.argv) > 3 else "failover_tts.json"

    assert health(8091) and health(8001), "Qwen3-TTS and Whisper must be healthy before the test"
    th = threading.Thread(target=client, args=(key, ref), daemon=True)
    th.start()
    ev("baseline: client running against the tts group")
    time.sleep(30)

    # Outage. Hold Whisper's port first so its automatic restart fails fast without taking GPU memory.
    ssh("nohup python3 -c \"import socket,time,os\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "for i in range(100):\n    try: s.bind(('0.0.0.0',8001)); break\n    except OSError: time.sleep(0.2)\n"
        "s.listen(1); open('/tmp/hold8001.pid','w').write(str(os.getpid())); time.sleep(1800)\" >/dev/null 2>&1 &")
    ssh("kill -KILL $(systemctl show -p MainPID --value vllm-whisper)")
    t_kill = time.time()
    ssh("kill -KILL $(systemctl show -p MainPID --value vllm-tts)")
    ev("FAULT: Qwen3-TTS killed (Whisper stopped, port held)")

    t_failover = wait_for(lambda: served_by("OmniVoice", t_kill), "traffic served by OmniVoice on Node-1", 120, 1)
    t_healthy = wait_for(lambda: health(8091), "Qwen3-TTS healthy again (systemd restart)", 900, 5)
    t_back = wait_for(lambda: served_by("Qwen3-TTS", t_healthy or time.time()), "traffic back on Qwen3-TTS", 180, 1) if t_healthy else None

    # Restore Whisper: release the port, end its stuck start-up check so systemd starts it again.
    ssh("kill $(cat /tmp/hold8001.pid) 2>/dev/null; rm -f /tmp/hold8001.pid; "
        "cp=$(systemctl show -p ControlPID --value vllm-whisper); [ \"$cp\" != 0 ] && kill $cp; true")
    t_whisper = wait_for(lambda: health(8001), "Whisper healthy again", 600, 5)
    time.sleep(20)
    stop.set()
    th.join(timeout=130)

    before = [r for r in rows if r["t"] < t_kill]
    during = [r for r in rows if t_kill <= r["t"] < (t_back or time.time())]
    after = [r for r in rows if t_back and r["t"] >= t_back]
    failed = [r for r in during if r.get("status") != 200]

    def lat(rs):
        v = sorted(r["latency"] for r in rs if r.get("status") == 200)
        return {"n": len(v), "p50": round(statistics.median(v), 2) if v else None,
                "p95": round(v[int(0.95 * (len(v) - 1))], 2) if v else None}
    summary = {
        "requests": len(rows),
        "failed_total": sum(r.get("status") != 200 for r in rows),
        "failed_during_outage": len(failed),
        "failure_kinds": sorted({f"{r.get('status')} {r.get('error', '')}".strip() for r in failed}),
        "seconds_to_first_failover_response": round(t_failover - t_kill, 1) if t_failover else None,
        "seconds_qwen3_tts_down": round(t_healthy - t_kill, 1) if t_healthy else None,
        "seconds_to_traffic_back": round(t_back - t_kill, 1) if t_back else None,
        "seconds_whisper_down": round(t_whisper - t_kill, 1) if t_whisper else None,
        "served_by_during_outage": {m: sum(1 for r in during if r.get("model") == m) for m in {r.get("model") for r in during}},
        "latency_before": lat(before), "latency_during": lat(during), "latency_after": lat(after),
    }
    json.dump({"summary": summary, "events": events, "rows": rows}, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
