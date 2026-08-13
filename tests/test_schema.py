import pytest
from pydantic import ValidationError

from agent.schema import (
    CapabilityArtifact, ArtifactMetadata, InputParam, OutputField, ParamType,
    Step, Action, ActionType, TargetElement, Locator, LocatorStrategy,
    Checkpoint, CheckpointType, DeclaredOutcome, OutcomeCategory,
)


def _minimal_artifact() -> CapabilityArtifact:
    step = Step(
        index=0, description="Navigate",
        action=Action(type=ActionType.NAVIGATE, value_template="http://127.0.0.1:5055/search"),
        checkpoint=Checkpoint(type=CheckpointType.URL_MATCHES, expectation="/search"),
        risk="safe",
    )
    return CapabilityArtifact(
        metadata=ArtifactMetadata(artifact_id="t1", name="test_cap", surface="mock", discovery_goal="test goal"),
        inputs=[InputParam(name="x", type=ParamType.STRING, description="x")],
        outputs=[OutputField(name="y", type=ParamType.STRING, description="y")],
        entry_point="http://127.0.0.1:5055/search",
        steps=[step],
        declared_outcomes=[],
        success_checkpoint=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="ok"),
        allowlist_domains=["127.0.0.1"],
    )


def test_artifact_round_trips_through_json():
    artifact = _minimal_artifact()
    raw = artifact.model_dump_json_pretty()
    restored = CapabilityArtifact.model_validate_json(raw)
    assert restored.metadata.name == "test_cap"
    assert restored.steps[0].action.type == ActionType.NAVIGATE
    assert restored.success_checkpoint.expectation == "ok"


def test_artifact_defaults_to_draft_approval():
    artifact = _minimal_artifact()
    assert artifact.metadata.approval_state == "draft"


def test_target_element_requires_primary_locator():
    with pytest.raises(ValidationError):
        TargetElement(fallbacks=[], robustness_note="missing primary")  # type: ignore[call-arg]


def test_declared_outcome_category_restricted_to_business_or_recoverable():
    # SUCCESS and HARD_FAILURE are not valid categories for a DeclaredOutcome --
    # those are terminal replay statuses, not things a step declares up front.
    with pytest.raises(ValidationError):
        DeclaredOutcome(
            name="X", category=OutcomeCategory.HARD_FAILURE,  # type: ignore[arg-type]
            detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="x"),
            description="bad",
        )


def test_step_risk_defaults_to_safe():
    step = Step(index=0, description="d", action=Action(type=ActionType.CLICK))
    assert step.risk == "safe"
