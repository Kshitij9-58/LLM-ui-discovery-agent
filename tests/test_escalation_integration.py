"""
Integration test for the human escalation / handoff mechanism (Section 3.6).

This is the test that actually proves the central claim in REPORT.md section 5:
that a paused session can be picked up by a separate process (the operator
console) and driven WITHOUT a fresh login, because what's handed off is the
live session's storage_state, not just a URL to revisit.

The test runs three phases in three separate Playwright browser instances
(closing the browser between phases, not just the page) to genuinely simulate
process-boundary handoff rather than reusing one in-memory browser object:

  1. AUTOMATION: logs in, navigates partway into a flow, hits something it
     can't safely handle alone (a frozen account), and escalates -- pausing by
     closing its own browser entirely.
  2. OPERATOR: a fresh browser loads ONLY the saved session_state_path (no
     credentials, no login call) and must land on an already-authenticated
     page. This is the crux of the test -- if this fails, the "same session"
     claim is false. The operator then performs a manual recovery action and
     hands back.
  3. AUTOMATION (resumed): a fresh browser loads the (possibly-updated)
     session_state_path and must still be authenticated, proving continuity
     survived the round trip.
"""
import os
import shutil

import pytest
import requests
from playwright.sync_api import sync_playwright

from agent.escalation import InterventionStore, ControlState

BASE_URL = "http://127.0.0.1:5055"
EVIDENCE_DIR = "evidence/interventions_test"


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


@pytest.fixture
def clean_store():
    shutil.rmtree(EVIDENCE_DIR, ignore_errors=True)
    requests.post(f"{BASE_URL}/admin/reset")
    yield InterventionStore(EVIDENCE_DIR)
    shutil.rmtree(EVIDENCE_DIR, ignore_errors=True)


def test_escalation_creates_intervention_with_full_context(clean_store):
    store = clean_store
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/login")
        page.fill("#fld_username", "operator1")
        page.fill("#fld_password", "test-password")
        page.click("input[type=submit]")
        page.goto(f"{BASE_URL}/member/99999")  # the frozen test account

        req = store.create(
            capability_or_goal="open_member_subaccount for member 99999",
            step_index=3,
            page=page,
            reason="Member account is FROZEN; cannot safely proceed without a human decision.",
            context=context,
            evidence_dir=EVIDENCE_DIR,
        )
        browser.close()

    assert req.control_state == ControlState.PENDING_HUMAN
    assert req.current_url.endswith("/member/99999")
    assert "FROZEN" in req.reason
    assert os.path.exists(req.screenshot_path)
    assert os.path.exists(req.session_state_path)


def test_operator_resumes_paused_session_without_fresh_login(clean_store):
    """The core claim: loading the paused session's storage_state authenticates
    a brand-new browser context with no login call at all."""
    store = clean_store

    # phase 1: automation escalates and "disappears" (browser fully closed)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/login")
        page.fill("#fld_username", "operator1")
        page.fill("#fld_password", "test-password")
        page.click("input[type=submit]")
        page.goto(f"{BASE_URL}/member/99999")

        req = store.create(
            capability_or_goal="open_member_subaccount for member 99999",
            step_index=3, page=page,
            reason="Member account is FROZEN.",
            context=context, evidence_dir=EVIDENCE_DIR,
        )
        intervention_id = req.intervention_id
        browser.close()

    # phase 2: a genuinely separate browser instance loads ONLY the saved
    # storage_state -- no credentials, no /login navigation, no form fill.
    req = store.take_control(intervention_id, operator_name="human_operator_jane")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=req.session_state_path)
        page = context.new_page()
        page.goto(req.current_url)

        body_text = page.evaluate("() => document.body.innerText")
        # THE ASSERTION THAT MATTERS: we're on the protected member page,
        # not bounced to a login form, despite never having authenticated
        # in this browser instance.
        assert "ACCOUNT FROZEN" in body_text
        assert "Sign In" not in body_text

        # operator performs a manual recovery action
        page.goto(f"{BASE_URL}/search")
        page.fill("#fld_memberid", "12345")
        page.click("input[type=submit]")
        store.record_operator_action(intervention_id, "looked up member 12345 instead of frozen 99999")

        req = store.resolve_and_hand_back(intervention_id, context)
        browser.close()

    assert req.control_state == ControlState.RESUMING
    assert len(req.operator_actions_log) >= 2  # take_control + at least one recorded action

    # phase 3: automation resumes -- another fresh browser, still authenticated
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=req.session_state_path)
        page = context.new_page()
        page.goto(f"{BASE_URL}/member/12345")
        body_text = page.evaluate("() => document.body.innerText")
        assert "Savings Balance" in body_text
        assert "Sign In" not in body_text
        browser.close()

    store.mark_resumed(intervention_id)
    final = store.load(intervention_id)
    assert final.control_state == ControlState.AUTOMATION


def test_operator_actions_are_logged_with_timestamps(clean_store):
    store = clean_store
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE_URL}/login")
        page.fill("#fld_username", "operator1")
        page.fill("#fld_password", "test-password")
        page.click("input[type=submit]")

        req = store.create(
            capability_or_goal="test", step_index=0, page=page,
            reason="test escalation", context=context, evidence_dir=EVIDENCE_DIR,
        )
        browser.close()

    store.take_control(req.intervention_id, operator_name="jane")
    store.record_operator_action(req.intervention_id, "did thing one")
    store.record_operator_action(req.intervention_id, "did thing two")
    final = store.load(req.intervention_id)

    assert len(final.operator_actions_log) == 3  # take_control + 2 actions
    assert all(line.startswith("[20") for line in final.operator_actions_log)  # ISO timestamp prefix
    assert "jane" in final.operator_actions_log[0]
    assert "did thing one" in final.operator_actions_log[1]
    assert "did thing two" in final.operator_actions_log[2]