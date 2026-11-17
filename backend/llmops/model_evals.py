"""LLMOps quality evaluations: small fixed test sets per model kind, scored without a judge model.

  chat         fixed prompts with checkable answers (arithmetic, exact word, JSON extraction, fact, sorting)
  embeddings   six queries against six documents: is the matching document the nearest?
  rerank       the same queries: is the matching document ranked first?
  transcription a reference recording with a known transcript: 1 - word error rate
  speech       synthesise known sentences, transcribe them back with whisper-turbo: 1 - word error rate
  ocr          a rendered invoice with known text (PaddleOCR, over SSH: it listens on localhost): 1 - character error rate

Every model is scored 0..1. Requests go through the gateway with the platform's own "evals" API key (created on first
use), so they are visible on the Requests page like any client. A run takes about a minute and sends a few requests per
model: light enough to run every few hours. A score well below a model's own recent level raises an incident.
"""
from __future__ import annotations

import base64
import json
import math
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from backend.core import config, store

ROOT = Path(__file__).resolve().parents[2]
KEY_PATH = config.DB_PATH.with_name("evals.key")
ASSETS = ROOT / "ops" / "bench" / "assets"
INTERVAL_S = 6 * 3600
REGRESSION_DROP = 0.25          # below the model's recent median by this much...
REGRESSION_FLOOR = 0.75         # ...and below this absolute score: an incident
OCR_NODE = "Node-1"

SUITES: dict[str, list[str]] = {
    "chat": ["qwen3.6-35b-a3b-fp8", "qwen-chat"],
    "embeddings": ["qwen3-embedding-4b", "bge-m3:latest"],
    "rerank": ["qwen3-reranker-4b"],
    "transcription": ["qwen3-asr", "whisper-turbo", "whisper-urdu", "Qwen/Qwen3-ASR-1.7B", "openai/whisper-large-v3-turbo"],
    "speech": ["k2-fsa/OmniVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"],
    "ocr": ["paddleocr-vl-1.6"],
}
CHAT_CASES = [
    ("arithmetic", "What is 17 multiplied by 23? Reply with the number only.", lambda a: _norm(a) == "391"),
    ("exact word", "Reply with exactly one word: READY", lambda a: _norm(a) == "ready"),
    ("JSON extraction", 'From the sentence "Node-3 GPU reached 81 degrees", extract a JSON object with keys "node" (string) '
     'and "temp_c" (number). Reply with the JSON only.', lambda a: _json_ok(a, {"node": "Node-3", "temp_c": 81})),
    ("fact", "What is the capital of Japan? Reply with one word.", lambda a: _norm(a) == "tokyo"),
    ("sorting", "Sort these numbers in ascending order and reply with them comma-separated, nothing else: 9, 2, 7, 4",
     lambda a: re.sub(r"\s", "", a.strip().rstrip(".")) == "2,4,7,9"),
]
DOCS = ["GPU firmware timeout Xid 119 on the GB10", "The root disk is 91 percent full",
        "SSH brute force attempts from a single address", "The TTS server is idle while requests are queued",
        "The nightly backup and its restore test passed", "The gateway failed over to the second replica"]
QUERIES = [("graphics card driver hang", 0), ("storage almost out of space", 1), ("many failed logins", 2),
           ("speech synthesis stuck", 3), ("did the backup work", 4), ("traffic moved to another server", 5)]
REF_TEXT = "The inference cluster recovered after the gateway failed over to the second replica. Latency is back to normal."
TTS_LINES = ["The nightly backup finished on time.", "All four nodes are online and healthy."]
OCR_TEXT = "INVOICE 2026-1116 Sovereign AI Ops GPU hours: 412 Rate: 1.80 USD Total due: 741.60 USD"

_run_lock = threading.Lock()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


def _json_ok(a: str, want: dict) -> bool:
    m = re.search(r"\{.*\}", a, re.S)
    try:
        got = json.loads(m.group(0)) if m else None
    except ValueError:
        return False
    return isinstance(got, dict) and all(str(got.get(k)).strip() == str(v) or got.get(k) == v for k, v in want.items())


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def _edit(a: list | str, b: list | str) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float:
    r = _words(ref)
    return _edit(r, _words(hyp)) / max(1, len(r))


def cer(ref: str, hyp: str) -> float:
    r = " ".join(ref.split())
    return _edit(r, " ".join(hyp.split())) / max(1, len(r))


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


def api_key() -> str:
    """The platform's own gateway key for evaluations, created once and kept beside the database (mode 600)."""
    from backend.llmops.gateway import keys            # here, not at import: the gateway package imports the web layer, which imports this
    if KEY_PATH.exists():
        k = KEY_PATH.read_text().strip()
        if keys.verify(k):
            return k
    k, _ = keys.create("evals", "aiops:model-evals")
    KEY_PATH.write_text(k)
    KEY_PATH.chmod(0o600)
    return k


# ------------------------------------------------------------------ suites: each returns (cases, node)
def _post(c: httpx.Client, path: str, **kw) -> httpx.Response:
    r = c.post(f"{config.GATEWAY_URL}/v1/{path}", **kw)
    r.raise_for_status()
    return r


def eval_chat(c: httpx.Client, model: str):
    cases, node = [], None
    for name, prompt, check in CHAT_CASES:
        r = _post(c, "chat/completions", json={"model": model, "temperature": 0, "max_tokens": 200,
                                                 "messages": [{"role": "user", "content": prompt}]})
        node = r.headers.get("x-aiops-node") or node
        answer = _strip_think(r.json()["choices"][0]["message"].get("content") or "")
        cases.append({"case": name, "ok": bool(check(answer)), "got": answer[:120]})
    return cases, node


def eval_embeddings(c: httpx.Client, model: str):
    r = _post(c, "embeddings", json={"model": model, "input": DOCS + [q for q, _ in QUERIES]})
    vecs = [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    docs, qs = vecs[:len(DOCS)], vecs[len(DOCS):]
    cases = []
    for (q, want), v in zip(QUERIES, qs):
        best = max(range(len(docs)), key=lambda i: _cos(v, docs[i]))
        cases.append({"case": q, "ok": best == want, "got": DOCS[best][:60]})
    return cases, r.headers.get("x-aiops-node")


def eval_rerank(c: httpx.Client, model: str):
    cases, node = [], None
    for q, want in QUERIES:
        r = _post(c, "rerank", json={"model": model, "query": q, "documents": DOCS})
        node = r.headers.get("x-aiops-node") or node
        top = max(r.json()["results"], key=lambda x: x["relevance_score"])["index"]
        cases.append({"case": q, "ok": top == want, "got": DOCS[top][:60]})
    return cases, node


def _transcribe(c: httpx.Client, model: str, wav: bytes) -> tuple[str, str | None]:
    r = _post(c, "audio/transcriptions", data={"model": model}, files={"file": ("eval.wav", wav, "audio/wav")})
    return r.json().get("text", ""), r.headers.get("x-aiops-node")


def eval_transcription(c: httpx.Client, model: str):
    text, node = _transcribe(c, model, (ASSETS / "ref.wav").read_bytes())
    e = wer(REF_TEXT, text)
    return [{"case": "reference recording", "ok": e <= 0.1, "score": max(0.0, 1 - e), "got": text[:120]}], node


def eval_speech(c: httpx.Client, model: str):
    ref = "data:audio/wav;base64," + base64.b64encode((ASSETS / "ref.wav").read_bytes()).decode()
    cases, node = [], None
    for line in TTS_LINES:
        r = _post(c, "audio/speech", json={"model": model, "task_type": "Base", "input": line, "ref_audio": ref,
                                           "ref_text": REF_TEXT, "response_format": "wav"})
        node = r.headers.get("x-aiops-node") or node
        heard, _ = _transcribe(c, "whisper-turbo", r.content)
        e = wer(line, heard)
        cases.append({"case": line, "ok": e <= 0.2, "score": max(0.0, 1 - e), "got": heard[:120]})
    return cases, node


def eval_ocr(c: httpx.Client, model: str):
    png = base64.b64encode((ASSETS / "ocr-sample.png").read_bytes()).decode()
    body = json.dumps({"model": model, "max_tokens": 160, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}, {"type": "text", "text": "OCR:"}]}]})
    node = config.NODE_BY_NAME[OCR_NODE]
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"tmc@{node.lan_ip}",
                          "curl -s -m 120 -H 'content-type: application/json' --data-binary @- localhost:8012/v1/chat/completions"],
                         input=body.encode(), capture_output=True, timeout=150)
    text = json.loads(out.stdout)["choices"][0]["message"]["content"]
    e = cer(OCR_TEXT, text)
    return [{"case": "rendered invoice", "ok": e <= 0.05, "score": max(0.0, 1 - e), "got": " ".join(text.split())[:120]}], OCR_NODE


RUNNERS = {"chat": eval_chat, "embeddings": eval_embeddings, "rerank": eval_rerank, "transcription": eval_transcription,
           "speech": eval_speech, "ocr": eval_ocr}


def _one(c: httpx.Client, run_id: str, suite: str, model: str) -> dict:
    t0 = time.time()
    try:
        cases, node = RUNNERS[suite](c, model)
        scores = [x.get("score", 1.0 if x["ok"] else 0.0) for x in cases]
        row = {"score": round(sum(scores) / len(scores), 4), "passed": sum(x["ok"] for x in cases), "total": len(cases),
               "node": node, "error": None, "cases": cases}
    except Exception as e:                      # one model failing must not stop the others; recorded as a 0
        row = {"score": 0.0, "passed": 0, "total": 0, "node": None, "error": f"{type(e).__name__}: {str(e)[:200]}", "cases": []}
    row |= {"id": store.new_id("EVAL"), "run_id": run_id, "t": t0, "model": model, "suite": suite,
            "latency_s": round(time.time() - t0, 2)}
    with store.conn() as db:
        db.execute("INSERT INTO model_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (row["id"], run_id, t0, model, suite, row["node"], row["score"], row["passed"], row["total"],
                    row["latency_s"], row["error"], json.dumps(row["cases"])))
    return row


def run_all(by: str = "schedule") -> dict | None:
    """One evaluation run of every model; None if a run is already in progress."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        run_id = store.new_id("EVRUN")
        store.audit(None, by, "model_evals_started", {"run_id": run_id})
        jobs = [(s, m) for s, ms in SUITES.items() for m in ms]
        with httpx.Client(timeout=180, headers={"Authorization": f"Bearer {api_key()}"}) as c, ThreadPoolExecutor(4) as pool:
            rows = list(pool.map(lambda j: _one(c, run_id, *j), jobs))
        summary = {"run_id": run_id, "models": len(rows), "failed": [r["model"] for r in rows if r["error"]],
                   "mean_score": round(sum(r["score"] for r in rows) / len(rows), 4)}
        store.audit(None, by, "model_evals_finished", summary)
        return summary
    finally:
        _run_lock.release()


def running() -> bool:
    return _run_lock.locked()


def last_run_at() -> float | None:
    with store.conn() as c:
        r = c.execute("SELECT MAX(t) FROM model_evals").fetchone()
    return r[0] if r else None


def latest() -> list[dict]:
    """Each model's newest result, with its previous score and recent median for comparison."""
    out = []
    with store.conn() as c:
        for suite, models in SUITES.items():
            for m in models:
                rows = c.execute("SELECT * FROM model_evals WHERE model = ? ORDER BY t DESC LIMIT 6", (m,)).fetchall()
                if not rows:
                    out.append({"model": m, "suite": suite, "score": None})
                    continue
                cur, prev = dict(rows[0]), [r["score"] for r in rows[1:]]
                med = sorted(prev)[len(prev) // 2] if prev else None
                out.append(cur | {"cases": json.loads(cur["cases"] or "[]"), "previous": prev[0] if prev else None,
                                  "baseline": med, "regressed": _regressed(cur["score"], med, cur["error"])})
    return out


def _regressed(score: float, baseline: float | None, error: str | None) -> bool:
    if error:
        return False                      # an unreachable model is the detector's business (model_server_missing etc.)
    return baseline is not None and score < REGRESSION_FLOOR and baseline - score >= REGRESSION_DROP


def history(model: str, days: int = 30) -> list[dict]:
    with store.conn() as c:
        return [dict(r) | {"cases": json.loads(r["cases"] or "[]")} for r in c.execute(
            "SELECT * FROM model_evals WHERE model = ? AND t >= ? ORDER BY t", (model, time.time() - days * 86400))]


def regression_alerts() -> list:
    """Alerts for models whose newest evaluation fell well below their own recent level (raised every scan while true)."""
    from backend.incidents.detector import Alert
    return [Alert("quality_regression", r["node"] or "fleet", r["model"],
                  f"{r['model']} quality dropped: evaluation score {r['score']:.0%} (recently {r['baseline']:.0%})",
                  {"suite": r["suite"], "score": r["score"], "baseline": r["baseline"], "run_id": r["run_id"],
                   "failed_cases": [x for x in r["cases"] if not x["ok"]][:5]})
            for r in latest() if r.get("regressed")]
