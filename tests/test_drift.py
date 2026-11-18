"""Configuration drift: parsing, secret masking, comparison, baseline and acceptance, recovery, fleet consistency."""
import json

import pytest

from backend.core import store
from backend.incidents import detector, runtime
from backend.monitoring import drift

UNIT = """# /etc/systemd/system/vllm-tts.service
[Service]
Environment=HF_TOKEN=hf_abcdef123456
Environment="OPENAI_API_KEY=sk-123"
Environment=PATH=/usr/bin
ExecStart=/x/vllm serve tts --gpu-memory-utilization {mem}"""


def raw(mem="0.3", enabled="enabled", image="sha256:aaa", keys="SHA256:k1", extra_unit=""):
    s, u = drift._SPLIT, drift._UNIT_SPLIT
    cont = "\t".join(json.dumps(x) for x in ["/qwen-embed", "vllm/vllm-openai:v1", image, "unless-stopped", ["serve", "m"],
                                               {"8000/tcp": [{"HostPort": "8103"}]}, [{"Source": "/models", "Destination": "/m"}],
                                               ["HF_TOKEN=secret", "MODE=x"]])
    once = "\t".join(json.dumps(x) for x in ["/scratch", "alpine", "sha256:b", "no", [], {}, [], []])
    return (f"kernel=6.17.0-1026-nvidia\nos=24.04\nnvidia_driver=580.159.03\ndocker=29.2.1\ncuda=13.0\n{s}\n"
            f"sshd_config=abc\nauthorized_keys={keys}\nsudoers_d=README\n{s}\n"
            f"{u} system vllm-tts.service {enabled}\n{UNIT.format(mem=mem)}\n{u} system snapd.service enabled\n[Unit]\n"
            f"{u} user aiops.service enabled\n[Service]\nExecStart=uvicorn{extra_unit}\n{s}\n{cont}\n{once}\n")


def test_parse_masks_secrets_and_keeps_only_configuration():
    items = drift.parse(raw())
    unit = items["unit:vllm-tts.service"]["content"]
    assert "hf_abcdef" not in unit and "sk-123" not in unit and "HF_TOKEN=<masked>" in unit and "PATH=/usr/bin" in unit
    assert "unit:snapd.service" not in items and "unit:user/aiops.service" in items          # OS units out, user units in
    c = json.loads(items["container:qwen-embed"]["content"])
    assert "secret" not in items["container:qwen-embed"]["content"] and c["env"][0].startswith("HF_TOKEN=#")
    assert "container:scratch" not in items                                                   # one-off container
    assert items["host:kernel"]["value"] == "6.17.0-1026-nvidia"


def test_compare_reports_changes_with_a_diff():
    a, b = drift.parse(raw()), drift.parse(raw(mem="0.15", enabled="disabled", image="sha256:bbb", keys="SHA256:k1 SHA256:k2"))
    ch = {c["key"]: c for c in drift.compare(a, b)}
    assert set(ch) == {"unit:vllm-tts.service", "container:qwen-embed", "security:authorized_keys"}
    assert "-ExecStart=/x/vllm serve tts --gpu-memory-utilization 0.3" in ch["unit:vllm-tts.service"]["diff"]
    assert "+# enabled: disabled" in ch["unit:vllm-tts.service"]["diff"]
    removed = drift.compare(a, {k: v for k, v in a.items() if k != "unit:user/aiops.service"})
    assert [(c["key"], c["change"]) for c in removed] == [("unit:user/aiops.service", "removed")]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(drift, "BASELINE_PATH", tmp_path / "drift.json")
    store.init()


def test_first_reading_is_the_baseline_and_acceptance_is_audited(db):
    a, b = drift.parse(raw()), drift.parse(raw(mem="0.15"))
    assert drift.ensure_baseline("Node-4", a) and not drift.ensure_baseline("Node-4", b)
    assert (drift.BASELINE_PATH.stat().st_mode & 0o777) == 0o600
    assert drift.accept("Node-4", b, ["unit:vllm-tts.service"], "human:alice", "TTS lowered so Qwen3.6 fits") == ["unit:vllm-tts.service"]
    assert drift.compare(drift.load_baseline()["Node-4"]["items"], b) == []
    ev = [e for e in store.audit_log(limit=10) if e["event"] == "drift_accepted"]
    assert ev and ev[0]["data"]["reason"] == "TTS lowered so Qwen3.6 fits" and ev[0]["data"]["before"].startswith("enabled")


def test_drift_incident_clears_only_when_the_item_matches_again():
    inc = {"kind": "config_drift", "node": "Node-4", "resource": "unit:vllm-tts.service", "fingerprint": "f"}
    det = detector.DetectorState()
    assert runtime.condition_cleared(inc, det, {}, set(), drifted={"Node-4": None}) is None        # not read: unknown
    assert runtime.condition_cleared(inc, det, {}, set(), drifted={"Node-4": {"unit:vllm-tts.service"}}) is None
    assert "matches the baseline" in runtime.condition_cleared(inc, det, {}, set(), drifted={"Node-4": set()})


def test_fleet_consistency_lists_only_disagreements():
    a = {"ok": True, "items": drift.parse(raw())}
    b = {"ok": True, "items": drift.parse(raw().replace("6.17.0-1026", "7.0.0-1019"))}
    assert drift.fleet_consistency({"Node-1": a, "Node-4": b}) == {
        "host:kernel": {"Node-1": "6.17.0-1026-nvidia", "Node-4": "7.0.0-1019-nvidia"}}


from tests.test_api import H, client, login  # noqa: E402,F401  (client: the API fixture)


def test_drift_api_viewers_read_only_admins_accept(client, tmp_path, monkeypatch):
    monkeypatch.setattr(drift, "BASELINE_PATH", tmp_path / "drift.json")
    items = drift.parse(raw(mem="0.15"))
    drift.ensure_baseline("Node-4", drift.parse(raw()))
    monkeypatch.setitem(runtime.runtime.drift, "Node-4", {"ok": True, "checked": 1.0, "items": items})
    monkeypatch.setitem(runtime.runtime.drift_changes, "Node-4", drift.compare(drift.load_baseline()["Node-4"]["items"], items))
    login(client, "vera")
    assert [x["key"] for x in client.get("/api/drift").json()["nodes"]["Node-4"]["changes"]] == ["unit:vllm-tts.service"]
    body = {"node": "Node-4", "keys": ["unit:vllm-tts.service"], "reason": "planned"}
    assert client.post("/api/drift/accept", json=body, headers=H).status_code == 403
    login(client, "root")
    r = client.post("/api/drift/accept", json=body, headers=H)
    assert r.status_code == 200 and r.json() == {"accepted": ["unit:vllm-tts.service"], "remaining": []}
