"""Independent verification: decides recovery from fresh telemetry, never from
the executor's return code."""
from __future__ import annotations

import time

from backend.incidents import detector
from backend.incidents.tools import check_endpoint_health, inspect_container


def verify_lab_action(proposal: dict) -> tuple[str, dict]:
    """Containment in the lab is verified by reading the lab state back, not by trusting the executor."""
    from backend.security import lab
    s = lab.state()
    target = proposal["target"]["resource_id"]
    key = {"block_ip_lab": "blocked_ips", "disable_lab_account": "disabled_accounts",
           "isolate_lab_endpoint": "isolated_endpoints", "quarantine_lab_file": "quarantined_files"}[proposal["operation"]]
    present = target in s[key]
    return ("resolved" if present else "not_resolved"), {"checks": [{"t": time.time(), "lab_state": key, "target": target,
                                                                      "present": present}]}


# Kinds a single fresh detector scan can decide. Sustained rules (CPU, GPU stall, latency) need consecutive scans,
# "model disappeared" needs to have seen it before, unreachability needs failure counts, and service/security
# rules are not in the detector: re-scanning for those would always look clear, so they are not verifiable here.
VERIFIABLE_BY_SCAN = {"disk_usage", "gpu_temperature", "gpu_unavailable", "high_memory",
                      "model_load_stalled", "model_queue_stuck", "model_engine_down"}


def verify_incident(incident: dict, wait_s: int = 30, interval_s: int = 3) -> tuple[str, dict]:
    kind, resource = incident["kind"], incident["resource"]
    if kind != "service_unhealthy" and kind not in VERIFIABLE_BY_SCAN:
        return "not_verifiable", {"reason": f"no independent check can confirm recovery for {kind!r}; a person must confirm"}
    deadline = time.time() + wait_s
    checks = []
    while True:
        if kind == "service_unhealthy":
            h = check_endpoint_health(resource)["data"]
            c = inspect_container(resource)["data"]
            checks.append({"t": time.time(), "health": h, "container": c})
            ok = h["ok"] and c.get("running")
        else:
            # Re-run the detector and see whether the same fingerprint still fires.
            alerts = detector.scan(detector.DetectorState())
            still = [a.fingerprint for a in alerts if a.fingerprint == incident["fingerprint"]]
            checks.append({"t": time.time(), "still_firing": bool(still)})
            ok = not still
        if ok:
            # require a second consecutive good reading
            time.sleep(interval_s)
            if kind == "service_unhealthy":
                h2 = check_endpoint_health(resource)["data"]
                checks.append({"t": time.time(), "health": h2})
                ok = h2["ok"]
            if ok:
                return "resolved", {"checks": checks}
        if time.time() >= deadline:
            return "not_resolved", {"checks": checks}
        time.sleep(interval_s)
