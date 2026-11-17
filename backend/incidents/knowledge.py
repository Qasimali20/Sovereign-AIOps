"""Runbook retrieval (RAG) over versioned Markdown in knowledge/runbooks/.

Documents are split into sections (`## ` headings). Each section is a chunk with a stable id
(`<file>#<slug>`) and the file's content hash as its version, so every excerpt an agent saw is
traceable to an exact document revision. Documents with `always: true` front matter are platform
facts included in every prompt; the rest compete on relevance.

Retrieval: dense similarity with the fleet's embedding model (through the gateway, alias `embed`),
then the reranker (`rerank`) orders the candidates. Vectors are cached on disk keyed by chunk text
hash, so only edited sections are re-embedded. If the models are unreachable the module degrades to
keyword overlap and says so (`method`), rather than failing the incident workflow.

Retrieved text is documentation, not telemetry; callers must keep it separate from evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass

import httpx

from backend.core import config

DOCS = config.ROOT / "knowledge" / "runbooks"
INDEX = config.ROOT / "data" / "knowledge_index.json"
TIMEOUT_S = 20.0
_lock = threading.Lock()
_state: dict = {"sig": None, "chunks": [], "vectors": {}, "fail_at": 0.0}
RETRY_S = 60.0


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    text: str
    version: str
    always: bool

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.title}\n{self.text}".encode()).hexdigest()[:16]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse(name: str, raw: str) -> list[Chunk]:
    """Pure: one Markdown document -> its section chunks."""
    version = hashlib.sha256(raw.encode()).hexdigest()[:10]
    always = False
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    if m:
        always = bool(re.search(r"^always:\s*true\s*$", m.group(1), re.M))
        raw = raw[m.end():]
    doc_title = (re.search(r"^# (.+)$", raw, re.M) or [None, name])[1].strip()
    out = []
    for sec in re.split(r"^## ", raw, flags=re.M)[1:]:
        head, _, body = sec.partition("\n")
        body = " ".join(body.split())
        if body:
            out.append(Chunk(f"{name}#{_slug(head)}", f"{doc_title}: {head.strip()}", body, version, always))
    return out


def load_chunks() -> list[Chunk]:
    return [c for f in sorted(DOCS.glob("*.md")) for c in parse(f.stem, f.read_text())]


# ---------------------------------------------------------------- model calls (through the gateway)
def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{config.GATEWAY_URL}/v1/{path}", json=body, timeout=TIMEOUT_S,
                   headers={"Authorization": f"Bearer {config.GATEWAY_KEY}"} if config.GATEWAY_KEY else {})
    r.raise_for_status()
    return r.json()


def _embed(texts: list[str]) -> list[list[float]]:
    data = _post("embeddings", {"model": "embed", "input": texts})["data"]
    return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]


def _rerank(query: str, docs: list[str]) -> list[float]:
    res = _post("rerank", {"model": "rerank", "query": query, "documents": docs})["results"]
    scores = [0.0] * len(docs)
    for r in res:
        scores[r["index"]] = r["relevance_score"]
    return scores


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_WORD = re.compile(r"[a-z0-9]{3,}")


def keyword_score(query: str, text: str) -> float:
    """Pure fallback: share of distinct query terms present in the text."""
    q = set(_WORD.findall(query.lower()))
    return len(q & set(_WORD.findall(text.lower()))) / len(q) if q else 0.0


# ---------------------------------------------------------------- index
def _refresh() -> None:
    """Reload documents when any file changed; embed only sections whose text is new."""
    files = sorted(DOCS.glob("*.md"))
    sig = tuple((f.name, f.stat().st_mtime_ns) for f in files)
    if sig == _state["sig"] or (sig == _state.get("tried") and time.time() - _state["fail_at"] < RETRY_S):
        return
    chunks = load_chunks()
    vectors = _state["vectors"] or (json.loads(INDEX.read_text()) if INDEX.exists() else {})
    missing = [c for c in chunks if not c.always and c.key not in vectors]
    if missing:
        try:
            for c, v in zip(missing, _embed([f"{c.title}\n{c.text}" for c in missing])):
                vectors[c.key] = v
            live = {c.key for c in chunks}
            vectors = {k: v for k, v in vectors.items() if k in live}
            INDEX.parent.mkdir(parents=True, exist_ok=True)
            INDEX.write_text(json.dumps(vectors))
        except Exception:              # keyword fallback until the embedding model is reachable
            _state.update(fail_at=time.time(), tried=sig, chunks=chunks, vectors=vectors)
            return
    _state.update(sig=sig, chunks=chunks, vectors=vectors)


def retrieve(query: str, k: int = 4, candidates: int = 10) -> dict:
    """Relevant runbook sections for `query`: always-on platform facts plus the top-k by relevance."""
    with _lock:
        _refresh()
        chunks, vectors = _state["chunks"], _state["vectors"]
    pool = [c for c in chunks if not c.always]
    method = "dense+rerank"
    try:
        if any(c.key not in vectors for c in pool) or time.time() - _state["fail_at"] < RETRY_S:
            raise RuntimeError("embedding model unavailable or index incomplete")
        qv = _embed([query])[0]
        top = sorted(pool, key=lambda c: _cos(qv, vectors[c.key]), reverse=True)[:candidates]
        try:
            scores = _rerank(query, [f"{c.title}\n{c.text}" for c in top])
        except Exception:
            method = "dense"
            scores = [_cos(qv, vectors[c.key]) for c in top]
        ranked = sorted(zip(top, scores), key=lambda t: t[1], reverse=True)[:k]
    except Exception:
        method = "keyword"
        ranked = sorted(((c, keyword_score(query, f"{c.title} {c.text}")) for c in pool),
                        key=lambda t: t[1], reverse=True)[:k]
    picked = [(c, None) for c in chunks if c.always] + ranked
    return {"query": query, "method": method,
            "chunks": [{"id": c.id, "title": c.title, "version": c.version, "always": c.always,
                        "score": None if s is None else round(float(s), 4), "text": c.text} for c, s in picked]}


def query_for(incident: dict) -> str:
    """The retrieval query for an incident: what happened, in the detector's own words."""
    d = incident.get("detail") or {}
    extra = " ".join(str(d[k]) for k in ("guidance", "meaning", "classification") if d.get(k))
    return f"{incident['kind'].replace('_', ' ')}: {incident['title']} {extra}".strip()


def block(retrieved: dict) -> str:
    """Prompt section. Clearly documentation, with ids the agent can cite."""
    lines = [f"[doc:{c['id']} v{c['version']}] {c['title']}\n{c['text']}" for c in retrieved["chunks"]]
    return "<runbooks>\n" + "\n\n".join(lines) + "\n</runbooks>"
