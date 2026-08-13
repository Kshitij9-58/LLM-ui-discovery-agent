"""
Deterministic Replay Engine
==============================
Given a saved CapabilityArtifact and a dict of input parameters, execute the
recorded steps against a live page WITHOUT any LLM in the decision loop --
this is the production path an AI agent triggers to invoke the capability
(Section 3.3).

Error/outcome handling is the load-bearing part of this module (see REPORT.md
section 3). Every step's result is classified into exactly one of:

  SUCCESS            -- goal reached, success_checkpoint holds, outputs returned.
  BUSINESS_OUTCOME    -- a declared_outcome's detection checkpoint matched (e.g.
                         "NOT FOUND" text present). This is a NORMAL, EXPECTED
                         result the caller needs, not an error -- returned with
                         status=business_outcome and outcome_name set.
  RECOVERABLE          -- a declared_outcome tagged RECOVERABLE matched (e.g.
                         SESSION_TIMEOUT). The engine attempts a bounded,
                         policy-defined recovery (e.g. re-authenticate once,
                         retry the step once) and only surfaces this to the
                         caller if recovery itself fails.
  HARD_FAILURE          -- nothing declared matched: an element could not be
                         resolved via any locator, a checkpoint failed with no
                         matching declared_outcome, a guardrail blocked the
                         step, or an unexpected exception occurred. Replay stops
                         immediately and returns exactly which step, what was
                         expected, and what was observed, for debugging.

One recovery policy is implemented as a concrete, bounded example (session
timeout -> re-login once -> retry the failed step once) rather than a generic
"LLM figures it out" fallback, per the brief's stretch-goal framing: assisted
fallback should be bounded and policy-checked, never open-ended. A second
retry-worthy case (SYSTEM_BUSY / transient) uses a plain wait+retry with no
re-auth. Anything else recoverable-tagged in the artifact but without a
matching hardcoded policy here degrades to hard_failure with a clear message --
silently no-op'ing on an unhandled "recoverable" tag would be worse than failing
loudly.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import Page

from agent.schema import (
    CapabilityArtifact, Step, ActionType, OutcomeCategory,
    ReplayResult, ReplayStatus, Checkpoint,
)
from agent.executor import ActionExecutor, check_checkpoint, GuardrailBlocked, ElementNotResolved
from agent.guardrails import AllowlistPolicy, RiskGate, redact

MAX_RECOVERY_ATTEMPTS = 1


@dataclass
class ReplayContext:
    login_url: Optional[str] = None
    login_username: Optional[str] = None
    login_password: Optional[str] = None


def _render_template(template: Optional[str], params: dict) -> Optional[str]:
    if template is None:
        return None
    try:
        return template.format(**params)
    except KeyError as e:
        raise ValueError(f"Artifact references parameter {e} not present in supplied inputs.")


def _check_declared_outcomes(page: Page, artifact: CapabilityArtifact) -> Optional[tuple[str, str]]:
    """Returns (outcome_name, category) for the first declared outcome whose
    detection checkpoint currently matches, else None."""
    for outcome in artifact.declared_outcomes:
        matched, _detail = check_checkpoint(page, outcome.detection)
        if matched:
            return outcome.name, outcome.category.value
    return None


class ReplayEngine:
    def __init__(
        self,
        page: Page,
        allowlist: AllowlistPolicy,
        artifact_approved: bool,
        evidence_dir: Optional[str] = None,
        recovery_context: Optional[ReplayContext] = None,
    ):
        self.page = page
        self.allowlist = allowlist
        self.risk_gate = RiskGate(mode="replay", artifact_approved=artifact_approved)
        self.executor = ActionExecutor(page, allowlist, self.risk_gate)
        self.evidence_dir = evidence_dir
        self.recovery_context = recovery_context or ReplayContext()
        self._recovery_attempts_used = 0

    def _snapshot_failure_evidence(self, tag: str) -> Optional[str]:
        if not self.evidence_dir:
            return None
        os.makedirs(self.evidence_dir, exist_ok=True)
        path = os.path.join(self.evidence_dir, f"replay_failure_{tag}.png")
        try:
            self.page.screenshot(path=path)
            return path
        except Exception:
            return None

    def _attempt_recovery(self, outcome_name: str, step: Step, params: dict) -> bool:
        """Bounded, policy-defined recovery for specific declared RECOVERABLE outcomes.
        Returns True if recovery succeeded and the caller should retry the step once."""
        if self._recovery_attempts_used >= MAX_RECOVERY_ATTEMPTS:
            return False
        self._recovery_attempts_used += 1

        if outcome_name == "SESSION_TIMEOUT" and self.recovery_context.login_url:
            try:
                self.page.goto(self.recovery_context.login_url, timeout=10000)
                if self.recovery_context.login_username and self.recovery_context.login_password:
                    self.page.fill("#fld_username", self.recovery_context.login_username)
                    self.page.fill("#fld_password", self.recovery_context.login_password)
                    self.page.click("input[type=submit]")
                return True
            except Exception:
                return False

        if outcome_name == "SYSTEM_BUSY":
            time.sleep(1.5)
            return True

        return False

    def _rewind_target_for_recovery(self, outcome_name: str, failed_idx: int, steps: list[Step]) -> int:
        """
        Recovery often needs to re-run more than just the step where the failure was
        DETECTED. A busy/timeout response to a form submission means the whole
        submission -- the form fills AND the click that submitted them -- must be
        redone, not just the click: a fresh page load clears any fields that were
        already filled, so retrying only the click resubmits an empty form.

        Policy: walk backward from the failing step to find the state-changing
        action (CLICK/NAVIGATE) at or before the failure -- that identifies which
        "submission" the failure belongs to -- then continue walking back over any
        immediately preceding FILL/SELECT steps that feed that same submission, so
        the whole input block is redone together. Stops at the first step that
        isn't a FILL/SELECT (a prior CLICK/NAVIGATE/EXTRACT), which marks the start
        of this submission's input block.
        """
        # first, find the state-changing step at/before the failure
        submit_idx = failed_idx
        while submit_idx >= 0 and steps[submit_idx].action.type not in (ActionType.CLICK, ActionType.NAVIGATE):
            submit_idx -= 1
        if submit_idx < 0:
            return failed_idx

        # then walk back further over any FILL/SELECT steps that feed this submission
        rewind_idx = submit_idx
        i = submit_idx - 1
        while i >= 0 and steps[i].action.type in (ActionType.FILL, ActionType.SELECT):
            rewind_idx = i
            i -= 1
        return rewind_idx

    def run(self, artifact: CapabilityArtifact, params: dict) -> ReplayResult:
        started_at = datetime.now(timezone.utc).isoformat()

        # validate required inputs up front -- a hard failure here is a caller bug,
        # surfaced immediately rather than partway through side-effecting steps.
        for p in artifact.inputs:
            if p.required and p.name not in params:
                return ReplayResult(
                    status=ReplayStatus.HARD_FAILURE, artifact_id=artifact.metadata.artifact_id,
                    artifact_version=artifact.metadata.version,
                    error_detail=f"Missing required input parameter '{p.name}'.",
                    started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                )

        outputs: dict = {}
        idx = 0
        steps = artifact.steps
        while idx < len(steps):
            step = steps[idx]
            try:
                resolved_value = _render_template(step.action.value_template, params)

                risk_confirmed = True  # replay's confirmation IS artifact_approved, checked inside risk_gate
                entry, extracted = self.executor.execute(
                    step.action, step_index=step.index, description=step.description,
                    risk=step.risk, risk_confirmed=risk_confirmed, resolved_value=resolved_value,
                )
                if step.action.extract_as and extracted is not None:
                    outputs[step.action.extract_as] = extracted

                if step.checkpoint:
                    ok, detail = check_checkpoint(self.page, step.checkpoint)
                    if not ok:
                        raise CheckpointMismatch(f"Step {step.index} checkpoint failed: expected {step.checkpoint.expectation}, observed: {detail}")

                idx += 1
                continue

            except GuardrailBlocked as e:
                shot = self._snapshot_failure_evidence(f"step{step.index}_guardrail")
                return ReplayResult(
                    status=ReplayStatus.HARD_FAILURE, artifact_id=artifact.metadata.artifact_id,
                    artifact_version=artifact.metadata.version, failed_step_index=step.index,
                    error_detail=f"Guardrail blocked step {step.index} ({step.description}): {e}",
                    started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                    evidence_dir=shot,
                )

            except (ElementNotResolved, CheckpointMismatch, Exception) as e:
                # Before treating this as a hard failure, check whether the CURRENT
                # page state matches any declared outcome -- this is the crux of the
                # "business outcome vs. failure" distinction: an element failing to
                # resolve because the app is showing a "NOT FOUND" screen instead of
                # the expected next screen is not a bug, it's the declared outcome.
                declared = _check_declared_outcomes(self.page, artifact)
                if declared:
                    outcome_name, category = declared
                    if category == OutcomeCategory.RECOVERABLE.value:
                        recovered = self._attempt_recovery(outcome_name, step, params)
                        if recovered:
                            idx = self._rewind_target_for_recovery(outcome_name, step.index, steps)
                            continue  # retry from the rewind point, not necessarily the failed step
                        shot = self._snapshot_failure_evidence(f"step{step.index}_{outcome_name}_unrecovered")
                        return ReplayResult(
                            status=ReplayStatus.HARD_FAILURE, artifact_id=artifact.metadata.artifact_id,
                            artifact_version=artifact.metadata.version, failed_step_index=step.index,
                            error_detail=f"Recoverable condition '{outcome_name}' detected but recovery failed or exhausted retry budget.",
                            started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                            evidence_dir=shot,
                        )
                    else:  # BUSINESS_OUTCOME
                        return ReplayResult(
                            status=ReplayStatus.BUSINESS_OUTCOME, artifact_id=artifact.metadata.artifact_id,
                            artifact_version=artifact.metadata.version, failed_step_index=step.index,
                            outcome_name=outcome_name, outputs=outputs,
                            started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                        )

                shot = self._snapshot_failure_evidence(f"step{step.index}_hardfail")
                return ReplayResult(
                    status=ReplayStatus.HARD_FAILURE, artifact_id=artifact.metadata.artifact_id,
                    artifact_version=artifact.metadata.version, failed_step_index=step.index,
                    error_detail=(
                        f"Step {step.index} ('{step.description}') failed with no matching declared outcome. "
                        f"Expected: {step.action.type.value} to succeed"
                        + (f" and checkpoint '{step.checkpoint.expectation}' to hold" if step.checkpoint else "")
                        + f". Observed error: {redact(str(e))}"
                    ),
                    started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                    evidence_dir=shot,
                )

        # all steps completed -- verify overall success checkpoint independently
        ok, detail = check_checkpoint(self.page, artifact.success_checkpoint)
        if not ok:
            shot = self._snapshot_failure_evidence("final_checkpoint")
            return ReplayResult(
                status=ReplayStatus.HARD_FAILURE, artifact_id=artifact.metadata.artifact_id,
                artifact_version=artifact.metadata.version,
                error_detail=f"All steps executed but final success_checkpoint did not hold. Expected {artifact.success_checkpoint.expectation}, observed: {detail}",
                started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                evidence_dir=shot,
            )

        return ReplayResult(
            status=ReplayStatus.SUCCESS, artifact_id=artifact.metadata.artifact_id,
            artifact_version=artifact.metadata.version, outputs=outputs,
            started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
        )


class CheckpointMismatch(Exception):
    pass
