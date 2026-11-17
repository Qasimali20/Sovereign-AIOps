"""Runbook knowledge base: parsing, versioning, fallback, and the prompt boundary."""
from backend.incidents import graph, knowledge

DOC = """---
always: true
---
# Title

## First section
Some text
over lines.

## Second
More.
"""


def test_parse_sections_ids_versions():
    a, b = knowledge.parse("doc", DOC)
    assert a.id == "doc#first-section" and a.text == "Some text over lines." and a.always
    assert b.title == "Title: Second"
    assert a.version == knowledge.parse("doc", DOC)[0].version
    assert a.version != knowledge.parse("doc", DOC + "x")[0].version          # any edit changes the version


def test_repository_runbooks_parse_and_cover_every_detector_kind():
    chunks = knowledge.load_chunks()
    ids = {c.id for c in chunks}
    assert len(ids) == len(chunks)                                            # ids unique
    text = " ".join(c.text for c in chunks)
    for kind in ("model_server_missing", "model_latency_high", "model_queue_stuck", "gpu_stalled", "high_memory",
                 "disk_usage", "high_cpu", "node_unreachable", "node_rebooted"):
        assert kind in text, kind


def test_keyword_fallback_ranks_relevant_text():
    assert knowledge.keyword_score("disk usage high", "disk_usage fires at 85% disk") > \
        knowledge.keyword_score("disk usage high", "ssh password spraying")


def test_runbooks_are_a_separate_block_not_evidence():
    inc = {"id": "INC-1", "title": "t", "kind": "high_cpu", "node": "Node-1", "resource": "r"}
    rb = {"query": "q", "method": "keyword", "chunks": [{"id": "x#y", "version": "1a2b", "title": "T", "text": "body",
                                                          "always": False, "score": 0.5}]}
    p = graph.diagnosis_prompt(inc, [], rb)
    assert "<runbooks>" in p and "[doc:x#y v1a2b]" in p
    assert p.index("<runbooks>") > p.index("<evidence>")
    assert "<runbooks>" not in graph.diagnosis_prompt(inc, [], None)
