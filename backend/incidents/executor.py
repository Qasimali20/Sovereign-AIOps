"""Restricted executor. Runs only catalogued operations on registered test
resources, and only when the approval record in the database says approved.
It re-checks everything itself rather than trusting the caller."""
from __future__ import annotations


from backend.core import config, store
from backend.incidents import policy
from backend.core.config import TEST_RESOURCES


class ExecutionRefused(Exception):
    pass


def execute(proposal: dict, approval_id: str) -> dict:
    action_id = proposal["action_id"]
    appr = store.get_approval(approval_id)
    if not appr or appr["action_id"] != action_id or appr["status"] != "approved":
        raise ExecutionRefused("no approved approval record for this action")
    if not appr["approver"] or appr["approver"].lower() in config.NON_HUMAN_ACTORS:
        raise ExecutionRefused("approval must come from a human approver")

    recorded = store.evidence_ids(appr["incident_id"])
    decision, reasons = policy.evaluate(proposal, recorded, store.get_incident(appr["incident_id"]))
    if decision != "requires_approval":
        raise ExecutionRefused(f"policy re-check failed: {decision} {reasons}")

    target = proposal["target"]["resource_id"]
    if proposal["operation"] in policy.LAB_OPS:
        from backend.security import lab
        fn = {"block_ip_lab": lab.block_ip, "disable_lab_account": lab.disable_account,
              "isolate_lab_endpoint": lab.isolate_endpoint, "quarantine_lab_file": lab.quarantine_file}[proposal["operation"]]
        return fn(target, f"human:{appr['approver']}", appr["incident_id"])
    TEST_RESOURCES[target]                                 # registered (policy re-checked above)
    from backend.incidents import testsvc
    by, op = f"human:{appr['approver']}", proposal["operation"]
    if op == "restart_test_service":
        return testsvc.restart(by)
    if op == "clear_test_cache":
        return testsvc.clear_cache(by)
    if op == "rollback_test_deployment":
        return testsvc.rollback(by)
    if op == "scale_test_service":
        return testsvc.scale(int(proposal["parameters"]["replicas"]), by)
    raise ExecutionRefused(f"no executor for {op}")
