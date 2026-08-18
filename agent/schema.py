"""
Capability Artifact Schema
===========================
This is the contract between:
  - the discovery run (an LLM figuring out a flow for the first time), which EMITS one,
  - the replay engine (deterministic, no LLM), which CONSUMES one to execute in production,
  - a human reviewer, who needs to understand what it does without reading code,
  - a calling AI agent, which needs typed inputs/outputs to invoke it as a tool.

Design principles (see REPORT.md section 2 for the full rationale):

1. Steps are decoupled from the raw model transcript. The LLM's chain-of-thought,
   screenshots, and retries during discovery are DISCARDED from the artifact -- only the
   distilled, confirmed sequence of actions survives. This keeps the artifact small,
   reviewable, and free of anything an LLM said that isn't actually load-bearing.

2. Every locator has a PRIMARY strategy and ordered FALLBACKS. Legacy apps have no
   test IDs, so "one selector, hope it works" is not viable. Each step records how the
   element was found and what else would have worked, so replay can degrade gracefully
   instead of hard-failing the moment one signal disappears (e.g. a label's exact text
   changes but its table position doesn't).

3. Checkpoints are assertions, not hopes. Every step declares what must be true
   after it runs (a URL pattern, a text fragment present, an element existing) so replay
   can tell "the click worked" apart from "the click silently did nothing," which is the
   single most common way naive replay bots fail silently in production.

4. Inputs/outputs are typed and named, independent of step order. An agent calling
   this capability sees a clean function signature (member_id: str) -> (savings_balance:
   float), not "step 3's typed value."

5. Outcomes are a closed, explicit taxonomy (see Outcome below), not a boolean. The
   brief is explicit that "no such member" is a legitimate answer, not a crash --  so the
   artifact declares, per step and overall, which business outcomes are EXPECTED and
   distinguishes them at the schema level from RECOVERABLE conditions and HARD failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    """
    How an element is targeted. Ordered roughly from most to least robust for the
    legacy-app case the brief describes -- see REPORT.md for the full argument.
    """
    TEST_ID = "test_id"                 # data-testid -- best case, rare in legacy apps
    ARIA_ROLE_NAME = "aria_role_name"   # accessibility tree: role + accessible name
    LABEL_TEXT = "label_text"           # associated <label> / preceding cell text
    CSS_SELECTOR = "css_selector"       # structural CSS path
    TEXT_CONTENT = "text_content"       # exact visible text match (buttons/links)
    ELEMENT_ID = "element_id"           # raw #id, when present but not a real test id
    XPATH = "xpath"                     # last resort, most brittle to layout change


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    frame: Optional[str] = Field(
        default=None,
        description="Name/URL-fragment of the frame this locator lives in, for frameset apps. None = main document.",
    )
    description: str = Field(
        description="Human-readable description of the target, for reviewers, e.g. 'Member ID search box'."
    )


class TargetElement(BaseModel):
    """An element to act on, with a primary locator and ordered fallbacks."""
    primary: Locator
    fallbacks: list[Locator] = Field(
        default_factory=list,
        description="Tried in order if primary fails to resolve to exactly one element.",
    )
    robustness_note: str = Field(
        description="Why this locator strategy was chosen for this element -- what makes it likely to survive minor UI change, and what would break it."
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT = "assert"


class Action(BaseModel):
    type: ActionType
    target: Optional[TargetElement] = Field(
        default=None, description="Element acted on. Not required for NAVIGATE."
    )
    value_template: Optional[str] = Field(
        default=None,
        description="Static text or a {param_name} template resolved from typed inputs at replay time. Used by FILL, SELECT, NAVIGATE(url).",
    )
    extract_as: Optional[str] = Field(
        default=None,
        description="If ActionType.EXTRACT, the output field name this value is bound to (must match an entry in ArtifactSchema.outputs).",
    )
    timeout_ms: int = Field(default=8000, description="Max wait for this action's target/condition.")


# ---------------------------------------------------------------------------
# Checkpoints -- assertions that confirm an action actually took effect
# ---------------------------------------------------------------------------

class CheckpointType(str, Enum):
    URL_MATCHES = "url_matches"          # substring or regex against current URL
    TEXT_PRESENT = "text_present"        # a text fragment appears somewhere in (frame) content
    TEXT_ABSENT = "text_absent"          # a text fragment does NOT appear (e.g. no error banner)
    ELEMENT_VISIBLE = "element_visible"  # a TargetElement resolves and is visible


class Checkpoint(BaseModel):
    type: CheckpointType
    expectation: str = Field(description="The URL substring / text fragment to check for.")
    target: Optional[TargetElement] = Field(
        default=None, description="Required when type == ELEMENT_VISIBLE."
    )
    frame: Optional[str] = None


# ---------------------------------------------------------------------------
# Outcome taxonomy -- the load-bearing distinction the brief calls out
# ---------------------------------------------------------------------------

class OutcomeCategory(str, Enum):
    SUCCESS = "success"                  # goal achieved, declared outputs available
    BUSINESS_OUTCOME = "business_outcome"  # a named, expected non-success result (e.g. NOT_FOUND)
    RECOVERABLE = "recoverable"          # transient; replay engine retried/handled internally
    HARD_FAILURE = "hard_failure"        # unexpected; replay stops and surfaces for debugging


class DeclaredOutcome(BaseModel):
    """
    A named business outcome this capability can legitimately produce besides success,
    e.g. MEMBER_NOT_FOUND, ACCOUNT_FROZEN. Declared up front so a calling agent's code
    can branch on outcome.name without parsing free-text errors.
    """
    name: str
    category: Literal[OutcomeCategory.BUSINESS_OUTCOME, OutcomeCategory.RECOVERABLE]
    detection: Checkpoint = Field(description="How replay recognizes this outcome occurred.")
    description: str


# ---------------------------------------------------------------------------
# Step -- one recorded unit of the flow
# ---------------------------------------------------------------------------

class Step(BaseModel):
    index: int
    description: str = Field(description="Human-readable summary, e.g. 'Submit member search form'.")
    action: Action
    checkpoint: Optional[Checkpoint] = Field(
        default=None,
        description="Assertion that this step actually took effect. Optional only for pure EXTRACT/ASSERT steps that check something rather than change it.",
    )
    risk: Literal["safe", "risky"] = Field(
        default="safe",
        description="'risky' = irreversible or state-changing in a way that matters (money movement, account creation/closure). Drives guardrail behavior at replay time.",
    )


# ---------------------------------------------------------------------------
# Parameters and outputs -- the typed function signature
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    ENUM = "enum"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    enum_values: Optional[list[str]] = None
    description: str
    example: Optional[str] = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


# ---------------------------------------------------------------------------
# Top-level artifact
# ---------------------------------------------------------------------------

class ArtifactMetadata(BaseModel):
    artifact_id: str
    name: str = Field(description="Short, stable capability name, e.g. 'open_member_subaccount'.")
    version: str = Field(default="1.0.0", description="Semver. Bump on any change to steps/schema.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    surface: str = Field(description="Target application identifier, e.g. 'cu-serv-mock-v1'.")
    tenant_scope: str = Field(
        default="base",
        description="'base' = generic/vendor-default recording; a tenant id = a per-tenant override layer. See REPORT.md section 4.",
    )
    discovery_goal: str = Field(description="The original natural-language goal the LLM was given.")
    approval_state: Literal["draft", "approved"] = "draft"
    author: str = Field(default="llm-discovery-agent")


class CapabilityArtifact(BaseModel):
    """The full agent-invocable capability: contract + recorded flow."""
    metadata: ArtifactMetadata
    inputs: list[InputParam]
    outputs: list[OutputField]
    entry_point: str = Field(description="Starting URL or app entry point for replay.")
    preconditions: list[str] = Field(
        default_factory=list,
        description="Human-readable preconditions replay assumes (e.g. 'operator session already authenticated').",
    )
    steps: list[Step]
    declared_outcomes: list[DeclaredOutcome] = Field(
        default_factory=list,
        description="Named non-success outcomes this capability can legitimately return.",
    )
    success_checkpoint: Checkpoint = Field(
        description="Final assertion that confirms overall goal completion, independent of per-step checkpoints."
    )
    allowlist_domains: list[str] = Field(
        description="Domains/hosts this capability is permitted to navigate to during replay."
    )

    def model_dump_json_pretty(self) -> str:
        return self.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Replay result contract
# ---------------------------------------------------------------------------

class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    ESCALATED = "escalated"
    HARD_FAILURE = "hard_failure"


class ReplayResult(BaseModel):
    status: ReplayStatus
    artifact_id: str
    artifact_version: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome_name: Optional[str] = Field(
        default=None, description="Populated when status == BUSINESS_OUTCOME, matches a DeclaredOutcome.name."
    )
    failed_step_index: Optional[int] = None
    error_detail: Optional[str] = Field(
        default=None,
        description="For HARD_FAILURE: what step, what was expected, what was observed.",
    )
    escalation_id: Optional[str] = None
    started_at: str
    finished_at: str
    evidence_dir: Optional[str] = None


# ---------------------------------------------------------------------------
# Tenant overrides -- multi-tenant reuse (REPORT.md section 4)
# ---------------------------------------------------------------------------
#
# Design: hundreds of tenants running the SAME vendor product should reuse
# ONE base artifact rather than being re-recorded per tenant, because the
# flow shape (which steps, in what order, what they mean) rarely differs --
# what differs is presentation: a relabeled field, a different element id
# after a tenant's template customization, a different button caption. An
# override therefore replaces only the LOCATOR on a specific step, never the
# step's action type, order, or business meaning -- so a tenant override can
# never accidentally turn a safe read into a risky write, and a reviewer
# auditing an override only has to check "does this locator correctly find
# the same kind of thing," not re-verify the whole flow.
#
# Overrides are a SEPARATE, small object rather than being embedded into the
# base artifact, so hundreds of tenant overlays don't bloat the one file every
# tenant shares, and a broken override for one tenant can be reviewed/rolled
# back without touching the base artifact other tenants depend on.

class StepLocatorOverride(BaseModel):
    step_index: int = Field(description="Which step in the BASE artifact this override applies to.")
    target: TargetElement = Field(description="Replacement locator (primary + fallbacks) for this tenant's rendering.")


class CheckpointOverride(BaseModel):
    """Override a text-based checkpoint's expected string (e.g. a relabeled
    field name), for either a specific step's checkpoint or the artifact's
    overall success_checkpoint."""
    step_index: Optional[int] = Field(
        default=None, description="Step whose checkpoint to override; None means the artifact's success_checkpoint."
    )
    expectation: str = Field(description="Replacement expected text for this tenant's rendering.")


class TenantOverride(BaseModel):
    """
    A tenant-scoped overlay on top of a 'base' CapabilityArtifact. Applying an
    override never changes step order, action types, risk classification, or
    the input/output contract -- only WHICH ELEMENT a step targets and WHAT
    TEXT a checkpoint expects, which is exactly the axis that varies between
    tenants running the same underlying vendor product with different
    branding/configuration.
    """
    tenant_id: str
    base_artifact_id: str = Field(description="artifact_id of the base CapabilityArtifact this overlay applies to.")
    base_artifact_name: str = Field(description="name of the base artifact, for human-readable review.")
    entry_point_override: Optional[str] = Field(
        default=None, description="Replacement entry_point URL/host for this tenant, if it differs from base."
    )
    locator_overrides: list[StepLocatorOverride] = Field(default_factory=list)
    checkpoint_overrides: list[CheckpointOverride] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: str = Field(default="", description="Human-readable reason for the override, for reviewers.")


def apply_tenant_override(artifact: "CapabilityArtifact", override: TenantOverride) -> "CapabilityArtifact":
    """
    Produce a new CapabilityArtifact with the tenant's locator/checkpoint
    overrides applied on top of the base. Returns a new object rather than
    mutating -- the base artifact in artifacts_store/ must stay exactly what
    every other tenant still replays against.
    """
    if override.base_artifact_id != artifact.metadata.artifact_id:
        raise ValueError(
            f"Override targets base artifact {override.base_artifact_id}, "
            f"but was applied to {artifact.metadata.artifact_id}."
        )

    new_steps = list(artifact.steps)
    locator_by_step = {o.step_index: o.target for o in override.locator_overrides}
    checkpoint_by_step = {o.step_index: o.expectation for o in override.checkpoint_overrides if o.step_index is not None}

    for i, step in enumerate(new_steps):
        new_action = step.action
        new_checkpoint = step.checkpoint
        if step.index in locator_by_step:
            new_action = Action(
                type=step.action.type, target=locator_by_step[step.index],
                value_template=step.action.value_template, extract_as=step.action.extract_as,
                timeout_ms=step.action.timeout_ms,
            )
        # The first NAVIGATE step's value_template is what replay actually
        # navigates to -- it must track entry_point_override, or replay keeps
        # going to the base tenant's host regardless of what entry_point says
        # on the returned artifact object. This was a real bug: entry_point
        # and step 0's literal URL can drift independently since they're
        # stored in two different places.
        if step.action.type == ActionType.NAVIGATE and override.entry_point_override and step.index == 0:
            new_action = Action(
                type=new_action.type, target=new_action.target,
                value_template=override.entry_point_override,
                extract_as=new_action.extract_as, timeout_ms=new_action.timeout_ms,
            )
        if step.index in checkpoint_by_step and step.checkpoint is not None:
            new_checkpoint = Checkpoint(
                type=step.checkpoint.type, expectation=checkpoint_by_step[step.index],
                target=step.checkpoint.target, frame=step.checkpoint.frame,
            )
        if new_action is not step.action or new_checkpoint is not step.checkpoint:
            new_steps[i] = Step(index=step.index, description=step.description,
                                 action=new_action, checkpoint=new_checkpoint, risk=step.risk)

    new_success_checkpoint = artifact.success_checkpoint
    overall_override = next((o for o in override.checkpoint_overrides if o.step_index is None), None)
    if overall_override:
        new_success_checkpoint = Checkpoint(
            type=artifact.success_checkpoint.type, expectation=overall_override.expectation,
            target=artifact.success_checkpoint.target, frame=artifact.success_checkpoint.frame,
        )

    new_metadata = artifact.metadata.model_copy(update={"tenant_scope": override.tenant_id})

    return CapabilityArtifact(
        metadata=new_metadata,
        inputs=artifact.inputs,
        outputs=artifact.outputs,
        entry_point=override.entry_point_override or artifact.entry_point,
        preconditions=artifact.preconditions,
        steps=new_steps,
        declared_outcomes=artifact.declared_outcomes,
        success_checkpoint=new_success_checkpoint,
        allowlist_domains=artifact.allowlist_domains,
    )