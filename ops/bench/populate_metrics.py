"""Light, mixed traffic to every model on the fleet, so the dashboards show real serving metrics.

    .venv/bin/python ops/bench/populate_metrics.py ops/bench/.loadkey [minutes=10]

One client per model, each with a pause between requests: enough to fill the
latency, throughput, cache and speech charts, far from saturating anything. Requests go through the gateway, so
its own metrics (and ASR real-time factor) fill too; PaddleOCR is reached over SSH because it listens on localhost.
Node-3's qwen-embed / qwen-rerank belong to another project and are not called.

Guard: every 5 s the hottest GPU and CPU are read live from the console's fleet view; at 70 °C every client pauses
until all are below 63 °C. With no reading it pauses rather than run blind.
Ctrl-C stops early and still prints the summary.
"""
from __future__ import annotations

import asyncio
import base64
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

HERE = Path(__file__).parent
GW = "http://127.0.0.1:8790/v1"
FLEET = "http://127.0.0.1:5170/api/fleet"     # the console's live view of every node's Spark Monitor (cached 4 s)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.config import NODE_BY_NAME  # noqa: E402  fleet addresses come from .env
OCR_NODE = f"tmc@{NODE_BY_NAME['Node-1'].lan_ip}"
TEMP_LIMIT = 70.0          # pause every client at this GPU or CPU temperature...
RESUME_AT = 63.0           # ...and resume once everything is back below this.
# Read live, every 5 s: Prometheus temperatures are up to 20 s old, and a node's CPU has been seen to climb 6 °C in
# that time, so a guard reading them overshot the 75 °C limit.
REF_TEXT = "The inference cluster recovered after the gateway failed over to the second replica. Latency is back to normal."

PROMPTS = [
    "Explain in three sentences why GPU memory fills up when serving a large language model.",
    "Write a short incident summary: a TTS server stopped producing audio while its GPU sat idle.",
    "List four signals that show an inference server is healthy, one line each.",
    "Summarise the difference between time to first token and time per output token.",
    "Draft a two-line status update for a planned maintenance window on a GPU cluster.",
    "What does a prefix cache hit mean for a chat model? Answer briefly.",
    "Give three tips for reducing queue time on a busy model server.",
    "Describe a safe rollback plan for a model server upgrade in five short steps.",
]
LONG_PROMPTS = [            # answers of several hundred tokens: the GPU writes for seconds, not a burst
    "Write a detailed runbook (about 400 words) for diagnosing a model server whose GPU is idle while requests queue.",
    "Explain, in about 400 words, how KV cache, prefix caching and batching affect latency and throughput on an LLM server.",
    "Write a 400-word post-incident review of a GPU firmware hang on one node of a four-node inference cluster.",
    "Describe, in about 400 words, how to roll out a new model version across four GPU nodes without downtime.",
]
SPEECH = ["Node four is serving speech.", "The nightly backup finished on time.", "Two approvals are waiting for review.",
          "All four Sparks are online and healthy.", "The gateway moved traffic to the second replica."]
DOCS = ["Xid 119: GSP firmware timeout on the GPU", "Disk usage on the root volume is at 91 percent",
        "TTS server idle while requests are queued", "Gateway failover to the second replica completed",
        "SSH brute force attempts from one address", "Prometheus target down for two minutes",
        "Model server restarted after an out-of-memory error", "Backup restore test passed"]

stats: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])        # name -> [ok, failed, seconds]
stop = asyncio.Event()
cool = asyncio.Event()                                            # set while temperatures allow traffic
cool.set()


async def run(name: str, pause: float, call) -> None:
    while not stop.is_set():
        await cool.wait()
        if stop.is_set():
            break
        t = time.time()
        try:
            ok = await call()
        except Exception:
            ok = False
        s = stats[name]
        s[0 if ok else 1] += 1
        s[2] += time.time() - t
        try:
            await asyncio.wait_for(stop.wait(), pause * random.uniform(0.7, 1.3))
        except asyncio.TimeoutError:
            pass


def _approver_token() -> str:
    env = HERE.parents[1] / ".env"
    for ln in env.read_text().splitlines():
        if ln.startswith("AIOPS_APPROVER_TOKEN="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("AIOPS_APPROVER_TOKEN not found in .env: the heat guard needs it to read live temperatures")


async def guard(end: float) -> None:
    head = {"x-approver-token": _approver_token(), "X-Requested-With": "aiops"}
    async with httpx.AsyncClient(timeout=5, headers=head) as fc:
        await _guard(fc, end)


async def _guard(fc: httpx.AsyncClient, end: float) -> None:
    while time.time() < end and not stop.is_set():
        try:
            nodes = (await fc.get(FLEET)).json()
            temps = {"gpu": max(((n.get("gpu") or {}).get("temp") or 0) for n in nodes.values()),
                     "cpu": max((n.get("cpu_temp_c") or 0) for n in nodes.values())}
            hottest = max(nodes, key=lambda k: max(((nodes[k].get("gpu") or {}).get("temp") or 0), nodes[k].get("cpu_temp_c") or 0))
            print(f"  {time.strftime('%H:%M:%S')}  hottest GPU {temps['gpu']:.0f} °C, CPU {temps['cpu']:.0f} °C ({hottest})  "
                  + "  ".join(f"{k.split()[0]}:{v[0]}" for k, v in sorted(stats.items())), flush=True)
            hot = max(temps.values())
            if cool.is_set() and hot >= TEMP_LIMIT:
                cool.clear()
                print(f"  {hot:.0f} °C: pausing until everything is below {RESUME_AT:.0f} °C", flush=True)
            elif not cool.is_set() and hot < RESUME_AT:
                cool.set()
                print(f"  {hot:.0f} °C: resuming", flush=True)
        except Exception as e:                    # no reading: pause rather than run blind
            cool.clear()
            print("  guard: no live temperatures, pausing:", e, flush=True)
        await asyncio.sleep(5)
    stop.set()
    cool.set()                                                    # release paused clients so they can exit


async def main() -> None:
    key = Path(sys.argv[1]).read_text().strip()
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    wav = (HERE / "assets" / "ref.wav").read_bytes()
    ref = "data:audio/wav;base64," + base64.b64encode(wav).decode()
    png = base64.b64encode((HERE / "assets" / "ocr-sample.png").read_bytes()).decode()
    ocr_body = ('{"model":"paddleocr-vl-1.6","max_tokens":160,"messages":[{"role":"user","content":[{"type":"image_url",'
                f'"image_url":{{"url":"data:image/png;base64,{png}"}}}},{{"type":"text","text":"OCR:"}}]}}]}}').encode()

    async with httpx.AsyncClient(timeout=180, headers={"Authorization": f"Bearer {key}"}) as c:
        def chat(model: str, max_tokens: int, prompts: list[str] = PROMPTS):
            async def call():
                r = await c.post(f"{GW}/chat/completions", json={"model": model, "max_tokens": max_tokens, "temperature": 0.7,
                                                                 "messages": [{"role": "user", "content": random.choice(prompts)}]})
                return r.status_code == 200
            return call

        def embed(model: str):
            async def call():
                r = await c.post(f"{GW}/embeddings", json={"model": model, "input": random.sample(DOCS, 6)})
                return r.status_code == 200
            return call

        async def rerank():
            r = await c.post(f"{GW}/rerank", json={"model": "qwen3-reranker-4b", "query": random.choice(DOCS), "documents": DOCS})
            return r.status_code == 200

        def asr(model: str):
            async def call():
                r = await c.post(f"{GW}/audio/transcriptions", data={"model": model}, files={"file": ("ref.wav", wav, "audio/wav")})
                return r.status_code == 200
            return call

        def tts(model: str):
            async def call():
                r = await c.post(f"{GW}/audio/speech", json={"model": model, "task_type": "Base", "input": random.choice(SPEECH),
                                                             "ref_audio": ref, "ref_text": REF_TEXT, "response_format": "wav"})
                return r.status_code == 200
            return call

        async def ocr():
            p = await asyncio.create_subprocess_exec(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", OCR_NODE,
                "curl -s -m 120 -o /dev/null -w '%{http_code}' -H 'content-type: application/json' --data-binary @- "
                "localhost:8012/v1/chat/completions", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            out, _ = await p.communicate(ocr_body)
            return out.strip() == b"200"

        clients = [
            # one client (Node-2 runs hot), long answers: steady writing fills the throughput chart without parallel load
            ("chat qwen3.6-35b (Node-2)", 3, chat("qwen3.6-35b-a3b-fp8", 640, LONG_PROMPTS)),
            ("chat qwen-chat (Node-3)", 6, chat("qwen-chat", 128)),
            ("embed qwen3-embedding-4b (Node-2)", 2, embed("qwen3-embedding-4b")),
            ("embed bge-m3 (Node-2)", 3, embed("bge-m3:latest")),
            ("rerank qwen3-reranker-4b (Node-2)", 2, rerank),
            ("ocr paddleocr-vl (Node-1)", 6, ocr),
            # speech heats Node-1's CPU fastest: a longer pause than the other clients
            *[(f"asr {m}", 4, asr(m)) for m in ("qwen3-asr", "whisper-turbo", "whisper-urdu",
                                                 "Qwen/Qwen3-ASR-1.7B", "openai/whisper-large-v3-turbo")],
            ("tts OmniVoice (Node-1)", 5, tts("k2-fsa/OmniVoice")),
            ("tts Qwen3-TTS (Node-4)", 3, tts("Qwen/Qwen3-TTS-12Hz-1.7B-Base")),
        ]
        end = time.time() + minutes * 60
        print(f"{len(clients)} clients for {minutes:g} min; pauses at {TEMP_LIMIT:.0f} °C, resumes below {RESUME_AT:.0f} °C", flush=True)

        async def timer():
            try:
                await asyncio.wait_for(stop.wait(), end - time.time())
            except asyncio.TimeoutError:
                stop.set()
                cool.set()
        await asyncio.gather(timer(), guard(end), *(run(n, p, f) for n, p, f in clients))

    print("\nrequests (ok / failed / mean s):")
    for n, (ok, bad, secs) in sorted(stats.items()):
        print(f"  {n:40s} {ok:5d} / {bad:3d} / {secs / max(1, ok + bad):6.2f}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped early; requests (ok / failed / mean s):")
        for n, (ok, bad, secs) in sorted(stats.items()):
            print(f"  {n:40s} {ok:5d} / {bad:3d} / {secs / max(1, ok + bad):6.2f}")
