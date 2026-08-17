"""
Regression test for a real bug found via a live discovery run: the distillation
step (agent.discovery.DiscoveryAgent._distill_artifact) is supposed to replace a
recorded literal value (e.g. "12345") with a template placeholder (e.g.
"{member_id}") for any step the LLM identifies as a caller-supplied parameter.

The original implementation matched the LLM's parameter-naming response back to
the recorded step by STRING-COMPARING free-text descriptions ("step_description").
This is fragile: the LLM has to reproduce that description text byte-for-byte in
its own JSON response, and in a real run it silently didn't, so the match failed,
templating never happened, and the resulting CapabilityArtifact had a HARDCODED
member id baked into what was supposed to be a reusable, parameterized capability
-- replaying it against a different member_id still fetched the original member's
data. This test locks in the fix: candidates are matched by a stable integer
candidate_id instead of by description text, so the templating always applies
correctly regardless of how the LLM phrases anything else in its response.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.discovery import DiscoveryAgent, TranscriptEntry
from agent.schema import Action, ActionType


class _FakeGenAIResponse:
    def __init__(self, text: str):
        self.text = text


def _make_agent_with_recorded_steps():
    """Build a DiscoveryAgent with a fake page/client and a pre-populated
    recorded_steps list mimicking a real member-lookup discovery run, without
    driving a real browser or calling a real LLM for the discovery loop itself --
    only _distill_artifact (the method under test) will run for real."""
    agent = DiscoveryAgent.__new__(DiscoveryAgent)  # bypass __init__ (needs a live page + real client)
    agent.page = MagicMock()
    agent.page.url = "http://127.0.0.1:5055/member/12345"
    agent.client = MagicMock()
    agent.extracted_outputs = {"savings_balance": "$4821.63"}
    agent.allowlist = MagicMock(allowed_hosts=["127.0.0.1"])

    agent.recorded_steps = [
        {
            "description": "Navigate to entry point http://127.0.0.1:5055/search",
            "action": Action(type=ActionType.NAVIGATE, value_template="http://127.0.0.1:5055/search", timeout_ms=8000),
            "resolved_value": "http://127.0.0.1:5055/search",
            "risk": "safe",
        },
        {
            "description": "Fill 'Member ID'",
            "action": Action(type=ActionType.FILL, timeout_ms=8000),
            "resolved_value": "12345",
            "risk": "safe",
            "is_param_candidate": True,
        },
        {
            "description": "Click 'Look Up Member'",
            "action": Action(type=ActionType.CLICK, timeout_ms=8000),
            "resolved_value": None,
            "risk": "safe",
        },
        {
            "description": "Extract 'savings_balance'",
            "action": Action(type=ActionType.EXTRACT, extract_as="savings_balance", timeout_ms=8000),
            "resolved_value": None,
            "risk": "safe",
            "extract_as": "savings_balance",
            "on_page_label": "Savings Balance",
        },
    ]
    return agent


def test_distillation_templates_fill_step_by_candidate_id_not_description_text():
    """The core regression test: even if the LLM's JSON response uses a
    candidate_id correctly (the fixed behavior), the fill step's value_template
    must become '{member_id}', not stay as the literal '12345'."""
    agent = _make_agent_with_recorded_steps()

    fake_llm_json = (
        '{"inputs": [{"candidate_id": 0, "param_name": "member_id", "type": "string", '
        '"description": "Member ID to look up", "example": "12345"}], '
        '"outputs": [{"field_name": "savings_balance", "type": "number", "description": "Savings balance"}], '
        '"capability_name": "get_member_savings_balance"}'
    )

    with patch("agent.discovery._generate_with_retry", return_value=_FakeGenAIResponse(fake_llm_json)):
        artifact = agent._distill_artifact(
            goal="Look up member 12345 and read their current savings balance",
            entry_url="http://127.0.0.1:5055/search",
            transcript=[],
        )

    fill_step = artifact.steps[1]
    assert fill_step.action.type == ActionType.FILL
    # THE ASSERTION THAT MATTERS: templated, not hardcoded.
    assert fill_step.action.value_template == "{member_id}"
    assert fill_step.action.value_template != "12345"

    assert len(artifact.inputs) == 1
    assert artifact.inputs[0].name == "member_id"


def test_distillation_is_robust_to_llm_paraphrasing_elsewhere_in_response():
    """Even if the LLM's 'description' field for the input paraphrases things
    differently than expected, matching still works because it's keyed on the
    numeric candidate_id, not on any free-text field."""
    agent = _make_agent_with_recorded_steps()

    fake_llm_json = (
        '{"inputs": [{"candidate_id": 0, "param_name": "member_id", "type": "string", '
        '"description": "totally different wording the model chose to use here", '
        '"example": "99999"}], '
        '"outputs": [{"field_name": "savings_balance", "type": "number", "description": "x"}], '
        '"capability_name": "whatever_name"}'
    )

    with patch("agent.discovery._generate_with_retry", return_value=_FakeGenAIResponse(fake_llm_json)):
        artifact = agent._distill_artifact(
            goal="Look up member 12345 and read their current savings balance",
            entry_url="http://127.0.0.1:5055/search",
            transcript=[],
        )

    fill_step = artifact.steps[1]
    assert fill_step.action.value_template == "{member_id}"


def test_distillation_leaves_non_candidate_steps_as_literal_values():
    """Steps never marked as parameter candidates (is_param_candidate not set)
    should keep their literal resolved_value, not get incorrectly templated."""
    agent = _make_agent_with_recorded_steps()
    # navigate step has a resolved_value but was never flagged as a param candidate
    fake_llm_json = '{"inputs": [], "outputs": [], "capability_name": "x"}'

    with patch("agent.discovery._generate_with_retry", return_value=_FakeGenAIResponse(fake_llm_json)):
        artifact = agent._distill_artifact(
            goal="test", entry_url="http://127.0.0.1:5055/search", transcript=[],
        )

    navigate_step = artifact.steps[0]
    assert navigate_step.action.value_template == "http://127.0.0.1:5055/search"