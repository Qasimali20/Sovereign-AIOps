from backend.incidents import graph


def test_no_new_evidence_goes_to_plan():
    assert graph.after_gather({"new_evidence": 0}) == "plan"
    assert graph.after_gather({}) == "plan"


def test_new_evidence_rediagnoses():
    assert graph.after_gather({"new_evidence": 2}) == "diagnose"


def test_diagnosis_loop_is_bounded():
    d = {"more_evidence": [{"tool": "get_node_metrics", "target": "Node-2"}]}
    assert graph.after_diagnose({"diagnosis": d, "attempts": 1}) == "gather_more"
    assert graph.after_diagnose({"diagnosis": d, "attempts": 2}) == "plan"
    assert graph.after_diagnose({"diagnosis": None}) == "report"


def test_already_have_matches_tool_and_target():
    ev = [{"tool": "inspect_container", "target": "aiops-test-svc"}]
    assert graph._already_have({"tool": "inspect_container", "target": "aiops-test-svc"}, ev)
    assert not graph._already_have({"tool": "get_container_logs", "target": "aiops-test-svc"}, ev)


def test_citation_only_denial_gets_one_corrected_plan():
    cite = ["evidence refs not recorded for this incident: ['EV-bad']"]
    assert graph.after_policy({"policy_decision": "deny", "policy_reasons": cite, "plan_attempts": 1}) == "replan"
    assert graph.after_policy({"policy_decision": "deny", "policy_reasons": cite, "plan_attempts": 2}) == "report"   # once only


def test_other_denials_are_final():
    other = ["operation not allowed on this resource"]
    assert graph.after_policy({"policy_decision": "deny", "policy_reasons": other, "plan_attempts": 1}) == "report"
    mixed = ["evidence refs not recorded for this incident: ['EV-x']", "target not registered"]
    assert graph.after_policy({"policy_decision": "deny", "policy_reasons": mixed, "plan_attempts": 1}) == "report"
    assert graph.after_policy({"policy_decision": "requires_approval", "plan_attempts": 1}) == "request_approval"
