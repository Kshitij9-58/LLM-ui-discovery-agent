from agent.guardrails import (
    AllowlistPolicy, RiskGate, classify_risk, redact, redact_form_value,
)


def test_allowlist_accepts_allowed_host():
    p = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    ok, _ = p.check_host("http://127.0.0.1:5055/search")
    assert ok


def test_allowlist_rejects_disallowed_host():
    p = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    ok, reason = p.check_host("http://evil.example.com/steal")
    assert not ok
    assert "not in the allowlist" in reason


def test_allowlist_action_type_gate():
    p = AllowlistPolicy(allowed_hosts=["127.0.0.1"], allowed_action_types=["navigate", "click"])
    ok, _ = p.check_action_type("click")
    assert ok
    ok2, reason = p.check_action_type("delete_account")
    assert not ok2
    assert "not in the allowed set" in reason


def test_classify_risk_flags_account_actions():
    assert classify_risk("Click Open New Sub-Account button") == "risky"
    assert classify_risk("Transfer funds to savings") == "risky"


def test_classify_risk_leaves_readonly_actions_safe():
    assert classify_risk("Submit member search form") == "safe"
    assert classify_risk("Click Look Up Member button") == "safe"


def test_risk_gate_discovery_requires_confirmation_for_risky():
    gate = RiskGate(mode="discovery")
    allowed, _ = gate.gate("risky", confirmed=False)
    assert not allowed
    allowed2, _ = gate.gate("risky", confirmed=True)
    assert allowed2


def test_risk_gate_discovery_never_blocks_safe_actions():
    gate = RiskGate(mode="discovery")
    allowed, _ = gate.gate("safe", confirmed=False)
    assert allowed


def test_risk_gate_replay_blocks_risky_on_unapproved_artifact():
    gate = RiskGate(mode="replay", artifact_approved=False)
    allowed, reason = gate.gate("risky", confirmed=True)
    assert not allowed
    assert "not approved" in reason


def test_risk_gate_replay_allows_risky_on_approved_artifact():
    gate = RiskGate(mode="replay", artifact_approved=True)
    allowed, _ = gate.gate("risky", confirmed=False)
    assert allowed


def test_redact_masks_password_field():
    out = redact("password=hunter2&member_id=12345")
    assert "hunter2" not in out
    assert "member_id=12345" in out


def test_redact_masks_ssn_shaped_values():
    out = redact("ssn on file: 123-45-6789")
    assert "123-45-6789" not in out


def test_redact_form_value_masks_sensitive_field_names_regardless_of_content():
    assert redact_form_value("password", "shortpw") == "[REDACTED]"
    assert redact_form_value("member_id", "12345") == "12345"
