"""
Integration test for multi-tenant reuse (REPORT.md section 4).

Proves the actual claim, not just the design: ONE base CapabilityArtifact,
discovered/recorded against one tenant's rendering of a vendor product, can
be replayed successfully against a SECOND, differently-configured tenant
running the same underlying product -- by applying a small TenantOverride
overlay rather than re-recording the whole capability.

Requires TWO live mock_bank instances running simultaneously, standing in for
two tenants on the same vendor product with different branding/config:
  - base:      python3 mock_bank/app.py                       (port 5055)
  - northwind: TENANT=northwind PORT=5056 python3 mock_bank/app.py

The two instances render the savings-balance field with different labels and
element ids (see mock_bank/app.py TENANT_CONFIG) -- a real, deliberate
divergence, not a simulated one. The test first confirms the base artifact
genuinely FAILS unmodified against northwind (proving the divergence is
real and matters), then confirms the SAME base artifact succeeds once a
TenantOverride is applied, extracting the correct value for a member the
original discovery run never saw.
"""
import shutil

import pytest
import requests
from playwright.sync_api import sync_playwright

from agent.schema import (
    CapabilityArtifact, ArtifactMetadata, InputParam, OutputField, ParamType,
    Step, Action, ActionType, TargetElement, Locator, LocatorStrategy,
    Checkpoint, CheckpointType, DeclaredOutcome, OutcomeCategory,
    TenantOverride, StepLocatorOverride, CheckpointOverride, apply_tenant_override,
)
from agent.replay import ReplayEngine, ReplayStatus
from agent.guardrails import AllowlistPolicy

BASE_URL = "http://127.0.0.1:5055"
NORTHWIND_URL = "http://127.0.0.1:5056"


def _instance_running(url: str) -> bool:
    try:
        r = requests.get(f"{url}/login", timeout=2)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not (_instance_running(BASE_URL) and _instance_running(NORTHWIND_URL)),
    reason=(
        "requires both mock bank tenant instances running -- "
        "`python3 mock_bank/app.py` (port 5055) AND "
        "`TENANT=northwind PORT=5056 python3 mock_bank/app.py`"
    ),
)


def _base_artifact() -> CapabilityArtifact:
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
                    fallbacks=[], robustness_note="stable id on base",
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
                    primary=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Look Up Member", description="search submit"),
                    fallbacks=[], robustness_note="base tenant button text",
                ),
                timeout_ms=8000,
            ),
            risk="safe",
        ),
        Step(
            index=3, description="Extract savings balance",
            action=Action(
                type=ActionType.EXTRACT,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.ELEMENT_ID, value="fld_savings_balance", description="base tenant balance cell id"),
                    fallbacks=[], robustness_note="base tenant element id",
                ),
                extract_as="savings_balance", timeout_ms=8000,
            ),
            risk="safe",
        ),
    ]
    return CapabilityArtifact(
        metadata=ArtifactMetadata(artifact_id="base-multitenant-test", name="lookup_member_balance",
                                   surface="cu-serv-mock-v1", discovery_goal="test", approval_state="approved"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, description="Member ID", example="12345")],
        outputs=[OutputField(name="savings_balance", type=ParamType.STRING, description="Savings balance")],
        entry_point=f"{BASE_URL}/search",
        steps=steps,
        declared_outcomes=[
            DeclaredOutcome(name="MEMBER_NOT_FOUND", category=OutcomeCategory.BUSINESS_OUTCOME,
                             detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="NOT FOUND"),
                             description="no such member"),
        ],
        success_checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="Savings Balance"),
        allowlist_domains=["127.0.0.1"],
    )


def _northwind_override(base: CapabilityArtifact) -> TenantOverride:
    return TenantOverride(
        tenant_id="northwind",
        base_artifact_id=base.metadata.artifact_id,
        base_artifact_name=base.metadata.name,
        entry_point_override=f"{NORTHWIND_URL}/search",
        locator_overrides=[
            StepLocatorOverride(
                step_index=2,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Search Member Account", description="northwind's relabeled button"),
                    fallbacks=[], robustness_note="northwind button text",
                ),
            ),
            StepLocatorOverride(
                step_index=3,
                target=TargetElement(
                    primary=Locator(strategy=LocatorStrategy.ELEMENT_ID, value="sav_bal_field", description="northwind's balance field id"),
                    fallbacks=[], robustness_note="northwind element id",
                ),
            ),
        ],
        checkpoint_overrides=[CheckpointOverride(step_index=None, expectation="Available Savings")],
        notes="Northwind renames the balance field label/id and search button text.",
    )


def _fresh_authenticated_page(playwright, base_url: str):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base_url}/login")
    page.fill("#fld_username", "operator1")
    page.fill("#fld_password", "test-password")
    page.click("input[type=submit]")
    return browser, page


def test_base_artifact_fails_unmodified_against_northwind():
    """Confirms the tenant divergence is real: without an override, the base
    artifact's locators genuinely don't resolve on northwind's rendering."""
    requests.post(f"{NORTHWIND_URL}/admin/reset")
    base = _base_artifact()
    # point at northwind's host but keep base's ORIGINAL locators (no override)
    base.entry_point = f"{NORTHWIND_URL}/search"
    base.steps[0].action.value_template = f"{NORTHWIND_URL}/search"

    with sync_playwright() as p:
        browser, page = _fresh_authenticated_page(p, NORTHWIND_URL)
        allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
        engine = ReplayEngine(page, allowlist, artifact_approved=True)
        result = engine.run(base, {"member_id": "12345"})
        browser.close()

    assert result.status == ReplayStatus.HARD_FAILURE


def test_tenant_override_makes_base_artifact_work_on_northwind():
    """The actual multi-tenant reuse claim: apply a small override overlay to
    the SAME base artifact and it now succeeds against northwind, extracting
    the correct value for a member the base artifact was never discovered
    against."""
    requests.post(f"{NORTHWIND_URL}/admin/reset")
    base = _base_artifact()
    override = _northwind_override(base)
    tenant_artifact = apply_tenant_override(base, override)

    assert tenant_artifact.metadata.tenant_scope == "northwind"
    assert tenant_artifact.entry_point == f"{NORTHWIND_URL}/search"
    # base artifact itself must be untouched by applying an override
    assert base.metadata.tenant_scope == "base"
    assert base.entry_point == f"{BASE_URL}/search"

    with sync_playwright() as p:
        browser, page = _fresh_authenticated_page(p, NORTHWIND_URL)
        allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
        engine = ReplayEngine(page, allowlist, artifact_approved=True)
        result = engine.run(tenant_artifact, {"member_id": "67890"})
        browser.close()

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["savings_balance"] == "$250.00"


def test_base_artifact_still_works_unmodified_against_base_tenant():
    """Applying an override for one tenant must never affect the base
    artifact's behavior against the tenant it was actually discovered on."""
    requests.post(f"{BASE_URL}/admin/reset")
    base = _base_artifact()

    with sync_playwright() as p:
        browser, page = _fresh_authenticated_page(p, BASE_URL)
        allowlist = AllowlistPolicy(allowed_hosts=["127.0.0.1"])
        engine = ReplayEngine(page, allowlist, artifact_approved=True)
        result = engine.run(base, {"member_id": "12345"})
        browser.close()

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["savings_balance"] == "$4821.63"