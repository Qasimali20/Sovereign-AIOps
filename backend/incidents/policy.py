"""Deterministic policy engine. The model proposes; this code decides.

Decisions:
  allow             no-op actions (none, escalate_to_human)
  requires_approval a write on an approved target, citing recorded evidence
  deny              everything else

Write targets by operation:
  restart_test_service, clear_test_cache, scale_test_service, rollback_test_deployment
                         a registered test resource with that operation allowed
                         (scale_test_service takes exactly one parameter, replicas, 1..3; the others none)
  block_ip_lab           a lab attacker address (documentation range, seen only in the lab)
  disable_lab_account    one of the isolated lab's accounts
Real nodes, real accounts and real addresses are never write targets.
"""
from __future__ import annotations

from backend.core.config import TEST_RESOURCES

READ_ONLY_OPS = {"none", "escalate_to_human"}
WRITE_OPS = {
    # operation: risk class
    "restart_test_service": "disruptive",
    "clear_test_cache": "disruptive (cached data lost)",
    "scale_test_service": "capacity change",
    "rollback_test_deployment": "disruptive (redeploys the previous good release)",
    "block_ip_lab": "containment (lab only)",
    "disable_lab_account": "containment (lab only)",
    "isolate_lab_endpoint": "containment (lab only)",
    "quarantine_lab_file": "containment (lab only)",
}
TEST_OPS = {"restart_test_service", "clear_test_cache", "scale_test_service", "rollback_test_deployment"}
TARGET_TYPE = {**{op: "test_service" for op in TEST_OPS}, "block_ip_lab": "lab_ip", "disable_lab_account": "lab_account",
               "isolate_lab_endpoint": "lab_endpoint", "quarantine_lab_file": "lab_file"}
# Parameter allowlist: operation -> {name: allowed values}. Anything not listed is refused.
PARAMETERS: dict[str, dict[str, tuple[str, ...]]] = {"scale_test_service": {"replicas": ("1", "2", "3")}}
CATALOGUE = READ_ONLY_OPS | set(WRITE_OPS)
SRE_OPS = {"none", "escalate_to_human"} | TEST_OPS
SOC_OPS = {"none", "escalate_to_human", "block_ip_lab", "disable_lab_account", "isolate_lab_endpoint", "quarantine_lab_file"}


def _target_ok(op: str, target: str) -> tuple[bool, str]:
    if op in TEST_OPS:
        res = TEST_RESOURCES.get(target)
        if res is None:
            return False, f"target {target!r} is not a registered test resource"
        if op not in res.allowed_operations:
            return False, f"{op} is not allowed on {target}"
        return True, f"registered test resource {target}"
    from backend.security import lab
    if op == "block_ip_lab":
        return lab.ip_blockable(target)
    if op == "disable_lab_account":
        return lab.account_disableable(target)
    if op == "isolate_lab_endpoint":
        return lab.endpoint_isolatable(target)
    if op == "quarantine_lab_file":
        return lab.file_quarantinable(target)
    return False, f"no target rule for {op}"


LAB_OPS = {"block_ip_lab", "disable_lab_account", "isolate_lab_endpoint", "quarantine_lab_file"}


def incident_subjects(incident: dict | None) -> set[str] | None:
    """What a security incident is about: its source IP or user, plus the accounts involved.
    Containment may only target these, never something merely mentioned in correlated evidence."""
    if not incident or not incident.get("kind", "").startswith("sec_"):
        return None
    d = incident.get("detail") or {}
    return {incident["resource"], *(d.get("users") or []), *([d["user"]] if d.get("user") else []),
            *(d.get(k) for k in ("host", "path", "remote_ip"))} - {None, ""}


def params_ok(op: str, params: dict | None) -> tuple[bool, str]:
    params = {k: str(v) for k, v in (params or {}).items()}
    allowed = PARAMETERS.get(op, {})
    if set(params) != set(allowed):
        return False, f"{op} takes parameters {sorted(allowed) or 'none'}, got {sorted(params) or 'none'}"
    bad = [f"{k}={v!r}" for k, v in params.items() if v not in allowed[k]]
    return (False, f"parameter values not allowed: {bad}") if bad else (True, "")


def evaluate(proposal: dict, known_evidence_ids: set[str], incident: dict | None = None) -> tuple[str, list[str]]:
    op = proposal.get("operation")
    target = (proposal.get("target") or {}).get("resource_id") or ""
    if op not in CATALOGUE:
        return "deny", [f"operation {op!r} is not in the catalogue"]
    if op in READ_ONLY_OPS:
        return "allow", [f"{op} makes no change"]

    if (proposal.get("target") or {}).get("resource_type") != TARGET_TYPE[op]:
        return "deny", ["resource_type does not match the operation"]
    ok, why = _target_ok(op, target)
    if not ok:
        return "deny", [why]
    pok, pwhy = params_ok(op, proposal.get("parameters"))
    if not pok:
        return "deny", [pwhy]
    if op in TEST_OPS and incident is not None and incident.get("resource") != target:
        # a fix must be for the thing this incident is about: a node reboot must not "fix" an unrelated test
        # service just because its errors appeared in the evidence (seen live on 2026-11-15)
        return "deny", [f"this incident concerns {incident.get('resource')!r} on {incident.get('node')}; "
                        f"{op} on {target!r} would change something it is not about"]
    subjects = incident_subjects(incident)
    if op in LAB_OPS and subjects is not None and target not in subjects:
        return "deny", [f"containment must target this incident's subject {sorted(subjects)}, not {target!r}"]
    refs = set(proposal.get("evidence_refs") or [])
    if not refs:
        return "deny", ["write operations need at least one evidence reference"]
    unknown = refs - known_evidence_ids
    if unknown:
        return "deny", [f"evidence refs not recorded for this incident: {sorted(unknown)}"]
    return "requires_approval", [f"{op} on {why} (risk: {WRITE_OPS[op]})",
                                 "human approval is required for every write operation at autonomy level 3"]
