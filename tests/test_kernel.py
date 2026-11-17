"""GPU driver signal parsing and detection, on lines captured from the real 2026-11-13 Xid 119 hang."""
from backend.core import config
from backend.incidents import detector
from backend.monitoring import kernel

# fleet addresses come from .env (placeholders without it)
N1 = config.NODE_BY_NAME["Node-1"].lan_ip
N4 = config.NODE_BY_NAME["Node-4"].lan_ip

REAL = [
    "1794550552.218046 spark-46ef kernel: NVRM: Xid (PCI:000f:01:00): 119, pid=7770, name=VLLM::EngineCor, Timeout after 45s of waiting for RPC response from GPU0 GSP! Expected function 76 (GSP_RM_CONTROL)",
    "1794550597.219698 spark-46ef kernel: NVRM: Xid (PCI:000f:01:00): 119, pid=2207, name=nvidia-modeset/, Timeout after 45s of waiting for RPC response from GPU0 GSP!",
    "1794550642.264764 spark-46ef kernel: NVRM: Xid (PCI:000f:01:00): 154, GPU recovery action changed from 0x0 (None) to 0x1 (GPU Reset Required)",
    "1794550643.000000 spark-46ef kernel: NVRM: gpuHandleSanityCheckRegReadError_GH100: Possible bad register read",
]


def test_parse_real_lines():
    xs = [x for x in map(kernel.parse_xid, REAL) if x]
    assert [x["code"] for x in xs] == [119, 119, 154]
    assert xs[0]["pid"] == "7770" and xs[0]["process"] == "VLLM::EngineCor" and xs[0]["severity"] == "critical"
    assert xs[2]["pid"] is None and "GPU Reset Required" in xs[2]["message"]


def test_summarize_groups_by_code_critical_first():
    g = kernel.summarize([x for x in map(kernel.parse_xid, REAL) if x])
    assert [(x["code"], x["count"]) for x in g] == [(119, 2), (154, 1)]
    assert g[0]["processes"] == ["VLLM::EngineCor (pid 7770)", "nvidia-modeset/ (pid 2207)"]
    assert "power-off" in g[0]["guidance"]


def test_unknown_xid_is_not_dropped():
    x = kernel.parse_xid("1.0 h kernel: NVRM: Xid (PCI:0): 999, something new")
    assert x["code"] == 999 and x["severity"] == "warning"


def test_kernel_alerts_one_per_code_with_context():
    node = config.NODE_BY_NAME["Node-2"]
    ev = {"xids": [x for x in map(kernel.parse_xid, REAL) if x], "bad_register_reads": 3736, "access": "local"}
    alerts = detector.kernel_alerts(node, ev, [{"name": "qwen3.6", "port": 8001, "started": 1.0}])
    assert [a.fingerprint for a in alerts] == ["gpu_xid:Node-2:xid-119", "gpu_xid:Node-2:xid-154"]
    assert alerts[0].detail["bad_register_reads"] == 3736 and alerts[0].detail["model_servers_loading"]


def test_boot_argument_is_validated():
    import pytest
    with pytest.raises(ValueError):
        kernel._command(0, "0; rm -rf /")


def test_missing_model_tracks_baseline():
    node = config.NODE_BY_NAME["Node-1"]
    seen: set[str] = set()
    up = {"vllm": [{"name": "m", "port": 1, "status": "ready"}]}
    assert detector.missing_model_alerts(node, up, seen, "s") == [] and seen == {"m@1"}
    assert [a.resource for a in detector.missing_model_alerts(node, {"vllm": []}, seen, "s")] == ["m@1"]
    loading = {"vllm": [{"name": "m", "port": 1, "status": "loading"}]}
    assert detector.missing_model_alerts(node, loading, seen, "s") == []


def test_ssh_prefers_lan_then_falls_back_to_tailscale_and_remembers(monkeypatch):
    import subprocess
    calls = []

    def fake_run(argv, **kw):
        target = argv[-2]
        calls.append(target)
        ok = target.endswith("spark-4")               # pretend LAN refuses the key, Tailscale works
        return subprocess.CompletedProcess(argv, 0 if ok else 255, "x", "")

    monkeypatch.setattr(kernel.subprocess, "run", fake_run)
    kernel._preferred.clear()
    acc = kernel.access_for(config.NODE_BY_NAME["Node-4"])
    assert acc.targets == (f"tmc@{N4}", "tmc@spark-4")
    kernel.run(acc, "true")
    assert calls == [f"tmc@{N4}", "tmc@spark-4"]
    calls.clear()
    kernel.run(acc, "true")
    assert calls == ["tmc@spark-4"]                    # the working path is tried first next time


def test_ssh_hung_tailscale_check_falls_through(monkeypatch):
    import subprocess
    calls = []

    def fake_run(argv, **kw):
        target = argv[-2]
        calls.append(target)
        if target.endswith("spark-1"):
            raise subprocess.TimeoutExpired(argv, 12)
        return subprocess.CompletedProcess(argv, 255, "", "Permission denied")

    monkeypatch.setattr(kernel.subprocess, "run", fake_run)
    kernel._preferred.clear()
    r = kernel.run(kernel.access_for(config.NODE_BY_NAME["Node-1"]), "true")
    assert r.returncode == 255 and "re-authentication" in r.stderr
    assert calls == [f"tmc@{N1}", "tmc@spark-1"]
