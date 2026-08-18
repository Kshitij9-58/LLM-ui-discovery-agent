"""
Regression tests for ActionExecutor's _select_option_smart() helper.

Context (see REPORT.md bug log): a live discovery run against the
"open new sub-account" flow showed the LLM proposing five different
casings/formats for the same intended selection on the "Account Type"
dropdown -- "Youth Savings", "YOUTH_SAVINGS", "youth_savings" -- because
Playwright's select_option(str) matches by the <option> `value` attribute
ONLY, not by label and not case-insensitively. One of those guesses
("youth_savings", lowercase) timed out entirely rather than failing fast,
and all five attempts were recorded into the discovery artifact, bloating
an 8-step capability into 11 steps with three of them being redundant/
failed guesses at the same select.

_select_option_smart() fixes this by inspecting the real <option>
value/label pairs on the page and resolving by: exact value -> exact
label (case-insensitive) -> normalized match. These tests exercise all
resolution paths plus the failure case, against a live Playwright page
and the mock bank's real "Account Type" <select>.

Auth/fixture pattern (login via #fld_username/#fld_password, /admin/reset,
requests-based liveness check) mirrors test_replay_integration.py so this
file behaves consistently with the rest of the integration suite.
"""
import pytest
import requests
from playwright.sync_api import sync_playwright

from agent.executor import _select_option_smart

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


@pytest.fixture
def account_type_select(authenticated_page):
    """
    Navigates to the "open new sub-account" form on an authenticated session
    and returns a Playwright Locator for the real "Account Type" <select>
    element, so tests exercise _select_option_smart() against actual DOM
    options rather than a stub.
    """
    page = authenticated_page
    page.goto(f"{BASE_URL}/search")
    # Use element_id locators, matching the pattern test_replay_integration.py
    # already relies on for this same form -- the table-layout HTML here has
    # no <label for> / aria-label on these fields, so Playwright's
    # get_by_role(..., name=...) cannot reliably resolve them by accessible
    # name and will time out waiting for a match that never resolves.
    page.fill("#fld_memberid", "12345")
    page.click("input[name=\"do_search\"]")
    page.click("text=Open New Sub-Account")
    return page.locator("#fld_acct_type")


def test_selects_by_exact_value_match(account_type_select):
    """The straightforward case: requested value matches an option's value attribute exactly."""
    actual = _select_option_smart(account_type_select, "YOUTH_SAVINGS", timeout_ms=8000)
    assert actual == "YOUTH_SAVINGS"


def test_selects_by_exact_label_case_insensitive(account_type_select):
    """
    The bug's primary case: the LLM proposes the human-readable label
    ("Youth Savings") rather than the underlying value ("YOUTH_SAVINGS").
    Playwright's raw select_option(str) would fail this silently (0 options
    matched) since it only matches by value.
    """
    actual = _select_option_smart(account_type_select, "Youth Savings", timeout_ms=8000)
    assert actual == "YOUTH_SAVINGS"


def test_selects_by_normalized_lowercase_match(account_type_select):
    """
    The specific guess that timed out during the live discovery run:
    lowercase with underscore ("youth_savings"), matching neither the
    option's value nor its label verbatim.
    """
    actual = _select_option_smart(account_type_select, "youth_savings", timeout_ms=8000)
    assert actual == "YOUTH_SAVINGS"


def test_selects_by_normalized_match_ignoring_spacing_and_case(account_type_select):
    """Belt-and-suspenders: hyphens/extra whitespace/mixed case should still normalize-match."""
    actual = _select_option_smart(account_type_select, "  youth-savings  ", timeout_ms=8000)
    assert actual == "YOUTH_SAVINGS"


def test_raises_with_available_options_when_nothing_matches(account_type_select):
    """
    On a genuine mismatch, the error should surface the real on-page options
    rather than a bare Playwright timeout -- this is what let the original
    bug's failure (step 9 in the pre-fix transcript) go unnoticed as a bare
    'UNEXPECTED ERROR: Timeout 8000ms exceeded' with no indication of what
    was actually available to select.
    """
    with pytest.raises(ValueError) as exc_info:
        _select_option_smart(account_type_select, "Certificate of Deposit", timeout_ms=8000)
    message = str(exc_info.value)
    assert "No option matching" in message
    assert "YOUTH_SAVINGS" in message  # confirms real options are listed, not a bare timeout