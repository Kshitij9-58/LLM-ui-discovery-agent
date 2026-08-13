"""
Integration tests for the replay engine. These require the mock bank app to be
running (see README.md: `python3 mock_bank/app.py`) and a browser installed via
`playwright install chromium`. They exercise the same paths demonstrated in
/evidence/replay -- happy path, business outcome, recoverable error, guardrail
block -- as an automated regression suite rather than one-off manual runs.
"""
import json
import os

import pytest
import requests
from playwright.sync_api import sync_playwright

from agent.schema import (
    CapabilityArtifact, ArtifactMetadata, InputParam, OutputField, ParamType,
    Step, Action, ActionType, TargetElement, Locator, LocatorStrategy,
    Checkpoint, CheckpointType, DeclaredOutcome, OutcomeCategory,
)
from agent.replay import ReplayEngine, ReplayContext, ReplayStatus
from agent.guardrails import AllowlistPolicy

BASE_URL = "http://127.0.0.1:5055"


def _mock_bank_running() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/login", timeout=2)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _mock_bank_running(),
    reason="mock bank app is not running on 127.0.0.1:5055 -- start it with `python3 mock_bank/app.py`",
)


def _lookup_balance_artifact(approval_state: str = "approved") -> CapabilityArtifact:
    steps = [
        Step(
            index=0, description="Navigate to member search",
            action=Action(type=ActionType.NAVIGATE, timeout_ms=8000, value_template=f"{BASE_URL}/search"),
            checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, expectation="/search"),
            risk="safe",
        ),
        Step(
            index=1, description="Fill 'Member ID'",
            action=Action(
                type=ActionType.FILL,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.ELEMENT_ID, value="fld_memberid", description="Member ID field"),
                    fallbacks=[Locator(strategy=LocatorStrategy.LABEL_TEXT, value="Member ID", description="by label")],
                    robustness_note="id primary, label fallback",
                ),
                value_template="{member_id}", timeout_ms=8000,
            ),
            risk="safe",
        ),
        Step(
            index=2, description="Click 'Look Up Member'",
            action=Action(
                type=ActionType.CLICK,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.ARIA_ROLE_NAME, value="button::Look Up Member", description="search submit"),
                    fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Look Up Member", description="by text")],
                    robustness_note="role+name primary, text fallback",
                ),
                timeout_ms=8000,
            ),
            checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="Savings Balance"),
            risk="safe",
        ),
        Step(
            index=3, description="Extract savings balance",
            action=Action(
                type=ActionType.EXTRACT,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.ELEMENT_ID, value="fld_savings_balance", description="balance cell"),
                    fallbacks=[], robustness_note="stable id on this cell",
                ),
                extract_as="savings_balance", timeout_ms=8000,
            ),
            risk="safe",
        ),
    ]
    return CapabilityArtifact(
        metadata=ArtifactMetadata(
            artifact_id="test-lookup", name="lookup_member_balance", surface="cu-serv-mock-v1",
            discovery_goal="look up member and read savings balance", approval_state=approval_state,
        ),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="Member ID", example="12345")],
        outputs=[OutputField(name="savings_balance", type=ParamType.STRING, description="Savings balance")],
        entry_point=f"{BASE_URL}/search",
        steps=steps,
        declared_outcomes=[
            DeclaredOutcome(
                name="MEMBER_NOT_FOUND", category=OutcomeCategory.BUSINESS_OUTCOME,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="NOT FOUND"),
                description="no such member",
            ),
            DeclaredOutcome(
                name="SYSTEM_BUSY", category=OutcomeCategory.RECOVERABLE,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="SYSTEM BUSY"),
                description="transient backend timeout",
            ),
        ],
        success_checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="Savings Balance"),
        allowlist_domains=["127.0.0.1"],
    )


@pytest.fixture
def authenticated_page():
    requests.post(f"{BASE_URL}/admin/reset")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/login")
        page.fill("#fld_username", "operator1")
        page.fill("#fld_password", "test-password")
        page.click("input[type=submit]")
        yield page
        browser.close()


def test_replay_happy_path_extracts_correct_balance(authenticated_page):
    artifact = _lookup_balance_artifact()
    allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    engine = ReplayEngine(authenticated_page, allowlist, artifact_approved=True)
    result = engine.run(artifact, {"member_id": "12345"})
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["savings_balance"] == "$4821.63"


def test_replay_business_outcome_member_not_found(authenticated_page):
    artifact = _lookup_balance_artifact()
    allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    engine = ReplayEngine(authenticated_page, allowlist, artifact_approved=True)
    result = engine.run(artifact, {"member_id": "00000"})
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_name == "MEMBER_NOT_FOUND"


def test_replay_recovers_from_injected_transient_failure(authenticated_page):
    requests.post(f"{BASE_URL}/admin/inject/search_busy")
    artifact = _lookup_balance_artifact()
    allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    engine = ReplayEngine(
        authenticated_page, allowlist, artifact_approved=True,
        recovery_context=ReplayContext(login_url=f"{BASE_URL}/login", login_username="operator1", login_password="test-password"),
    )
    result = engine.run(artifact, {"member_id": "12345"})
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["savings_balance"] == "$4821.63"


def test_replay_missing_required_param_is_hard_failure(authenticated_page):
    artifact = _lookup_balance_artifact()
    allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    engine = ReplayEngine(authenticated_page, allowlist, artifact_approved=True)
    result = engine.run(artifact, {})
    assert result.status == ReplayStatus.HARD_FAILURE
    assert "member_id" in result.error_detail


def test_replay_blocks_risky_action_on_unapproved_artifact(authenticated_page):
    steps = [
        Step(
            index=0, description="Navigate to member detail",
            action=Action(type=ActionType.NAVIGATE, timeout_ms=8000, value_template=f"{BASE_URL}/member/{{member_id}}"),
            checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, expectation="/member/"),
            risk="safe",
        ),
        Step(
            index=1, description="Click 'Open New Sub-Account'",
            action=Action(
                type=ActionType.CLICK,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Open New Sub-Account", description="link"),
                    fallbacks=[], robustness_note="exact text",
                ),
                timeout_ms=8000,
            ),
            risk="risky",
        ),
    ]
    artifact = CapabilityArtifact(
        metadata=ArtifactMetadata(artifact_id="risky-t", name="open_subaccount", surface="cu-serv-mock-v1",
                                   discovery_goal="open sub-account", approval_state="draft"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="member id", example="12345")],
        outputs=[], entry_point=f"{BASE_URL}/member/12345", steps=steps, declared_outcomes=[],
        success_checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="Open New Sub-Account"),
        allowlist_domains=["127.0.0.1"],
    )
    allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
    engine = ReplayEngine(authenticated_page, allowlist, artifact_approved=False)
    result = engine.run(artifact, {"member_id": "12345"})
    assert result.status == ReplayStatus.HARD_FAILURE
    assert "not approved" in result.error_detail
