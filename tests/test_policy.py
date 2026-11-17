import pytest

from backend.incidents import policy


def prop(**kw):
    p = {"operation": "restart_test_service",
         "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"},
         "evidence_refs": ["EV-1"]}
    p.update(kw)
    return p


def test_restart_registered_test_service_requires_approval():
    assert policy.evaluate(prop(), {"EV-1"})[0] == "requires_approval"


def test_unknown_operation_denied():
    assert policy.evaluate(prop(operation="rm_rf"), {"EV-1"})[0] == "deny"


def test_unregistered_target_denied():
    p = prop(target={"resource_type": "test_service", "resource_id": "vllm-qwen36"})
    assert policy.evaluate(p, {"EV-1"})[0] == "deny"


def test_resource_type_mismatch_denied():
    p = prop(target={"resource_type": "production", "resource_id": "aiops-test-svc"})
    assert policy.evaluate(p, {"EV-1"})[0] == "deny"


def test_write_without_evidence_denied():
    assert policy.evaluate(prop(evidence_refs=[]), {"EV-1"})[0] == "deny"


def test_fabricated_evidence_denied():
    assert policy.evaluate(prop(evidence_refs=["EV-999"]), {"EV-1"})[0] == "deny"


def test_escalate_and_none_allowed():
    assert policy.evaluate({"operation": "escalate_to_human"}, set())[0] == "allow"
    assert policy.evaluate({"operation": "none"}, set())[0] == "allow"


@pytest.mark.parametrize("op", ["clear_test_cache", "rollback_test_deployment", "restart_test_service"])
def test_parameterless_test_ops_require_approval_and_refuse_parameters(op):
    assert policy.evaluate(prop(operation=op), {"EV-1"})[0] == "requires_approval"
    assert policy.evaluate(prop(operation=op, parameters={"replicas": "2"}), {"EV-1"})[0] == "deny"


def test_scale_parameter_allowlist():
    ok = lambda params: policy.evaluate(prop(operation="scale_test_service", parameters=params), {"EV-1"})[0]
    assert ok({"replicas": "2"}) == "requires_approval"
    assert ok({"replicas": 3}) == "requires_approval"                 # ints normalised, same allowlist
    assert ok({"replicas": "50"}) == "deny"                           # outside 1..3
    assert ok({}) == "deny"                                           # required parameter missing
    assert ok({"replicas": "2", "image": "evil:latest"}) == "deny"    # unknown parameter


def test_test_ops_never_reach_unregistered_targets():
    for op in policy.TEST_OPS:
        p = prop(operation=op, target={"resource_type": "test_service", "resource_id": "chambers-backend"},
                 parameters={"replicas": "2"} if op == "scale_test_service" else {})
        assert policy.evaluate(p, {"EV-1"})[0] == "deny"


def test_test_service_fix_must_belong_to_the_incident():
    """Regression (live, 2026-11-15): a Node-4 reboot incident proposed rolling back the unrelated test service."""
    prop = {"operation": "rollback_test_deployment", "target": {"resource_type": "test_service", "resource_id": "aiops-test-svc"},
            "parameters": {}, "evidence_refs": ["ev1"]}
    reboot = {"kind": "node_rebooted", "node": "Node-4", "resource": "boot-1794754620", "detail": {}}
    own = {"kind": "service_unhealthy", "node": "Node-2", "resource": "aiops-test-svc", "detail": {}}
    d, why = policy.evaluate(prop, {"ev1"}, reboot)
    assert d == "deny" and "not about" in why[0]
    assert policy.evaluate(prop, {"ev1"}, own)[0] == "requires_approval"
