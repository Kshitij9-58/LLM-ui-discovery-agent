"""
Human-in-the-Loop Escalation & Handoff
=========================================
Section 3.6 of the brief calls out a specific seam: automation must be able to
PAUSE, CEDE CONTROL of the SAME live session (not spin up a fresh one), let a
human perform manual steps, then RESUME on that same session -- with a record
of who is (or should be) in control at any moment, and evidence preserved
across the handoff.

Scope, per the brief: a full real-time co-browsing console is explicitly out of
scope. What's real here:

  - IntervionRequest: a structured record of WHY the system stopped, carrying
    enough context (goal/capability, step index, current URL, screenshot path,
    reason) for a human to act on without reading code or logs.
  - SessionControlState: an explicit state machine (AUTOMATION -> a human takes
    it -> HUMAN -> hand back -> AUTOMATION) persisted alongside the live
    Playwright BrowserContext's storage_state, so "the same session" survives
    the handoff even across process boundaries -- the automation process can
    pause, a separate operator-facing process can load the SAME cookies/storage
    and drive the SAME server-side session, and control can be handed back.
  - A minimal operator surface: `operator_console.py` is a tiny Flask app that
    lets a human view the intervention context and either (a) take manual
    actions via its own Playwright page loaded with the paused session's
    storage_state, then (b) mark the intervention resolved, which the
    automation process polls for and resumes on.

What's mocked / simplified, and why (see REPORT.md section 5 for the full
argument): there's no live pixel-streaming/co-browsing (that's the explicitly
out-of-scope "full real-time" console) -- instead the handoff unit is the
*session's storage_state* (cookies + localStorage), which is the thing that
actually needs to transfer for "operate the same session" to be true for a
cookie-authenticated web app. For a desktop app or an app using non-cookie
session tokens, the same pattern generalizes to "hand off whatever credential/
session artifact the surface uses" (documented as the design answer, not built).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from playwright.sync_api import BrowserContext


class ControlState(str, Enum):
    AUTOMATION = "automation"
    PENDING_HUMAN = "pending_human"   # escalated, waiting for an operator to pick it up
    HUMAN = "human"                    # an operator has taken control
    RESUMING = "resuming"              # operator marked done, automation about to resume


@dataclass
class InterventionRequest:
    intervention_id: str
    capability_or_goal: str
    step_index: Optional[int]
    current_url: str
    reason: str
    screenshot_path: Optional[str]
    created_at: str
    control_state: ControlState = ControlState.PENDING_HUMAN
    session_state_path: Optional[str] = None   # where the paused session's storage_state was dumped
    operator_actions_log: list[str] = field(default_factory=list)
    resolved_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["control_state"] = self.control_state.value
        return d


class InterventionStore:
    """
    File-backed store for intervention requests, acting as the coordination point
    between the (paused) automation process and the operator console process.
    A real system would use a proper queue/DB; a JSON file per intervention is the
    right-sized mock for a single-machine demo -- the STATE MACHINE and the
    session hand-off mechanism are the things being tested here, not the
    storage technology (see Ground Rules: don't reward scaling infrastructure).
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _path(self, intervention_id: str) -> str:
        return os.path.join(self.base_dir, f"{intervention_id}.json")

    def create(self, capability_or_goal: str, step_index: Optional[int], page, reason: str,
               context: BrowserContext, evidence_dir: str) -> InterventionRequest:
        intervention_id = str(uuid.uuid4())[:8]
        os.makedirs(evidence_dir, exist_ok=True)

        screenshot_path = os.path.join(evidence_dir, f"escalation_{intervention_id}.png")
        try:
            page.screenshot(path=screenshot_path)
        except Exception:
            screenshot_path = None

        session_state_path = os.path.join(evidence_dir, f"session_state_{intervention_id}.json")
        context.storage_state(path=session_state_path)

        req = InterventionRequest(
            intervention_id=intervention_id,
            capability_or_goal=capability_or_goal,
            step_index=step_index,
            current_url=page.url,
            reason=reason,
            screenshot_path=screenshot_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            control_state=ControlState.PENDING_HUMAN,
            session_state_path=session_state_path,
        )
        self.save(req)
        return req

    def save(self, req: InterventionRequest) -> None:
        with open(self._path(req.intervention_id), "w") as f:
            json.dump(req.to_dict(), f, indent=2)

    def load(self, intervention_id: str) -> Optional[InterventionRequest]:
        path = self._path(intervention_id)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            d = json.load(f)
        d["control_state"] = ControlState(d["control_state"])
        return InterventionRequest(**d)

    def take_control(self, intervention_id: str, operator_name: str = "operator") -> InterventionRequest:
        req = self.load(intervention_id)
        if req is None:
            raise ValueError("Unknown intervention id")
        req.control_state = ControlState.HUMAN
        req.operator_actions_log.append(f"[{datetime.now(timezone.utc).isoformat()}] {operator_name} took control")
        self.save(req)
        return req

    def record_operator_action(self, intervention_id: str, action_description: str) -> InterventionRequest:
        req = self.load(intervention_id)
        if req is None:
            raise ValueError("Unknown intervention id")
        req.operator_actions_log.append(f"[{datetime.now(timezone.utc).isoformat()}] {action_description}")
        self.save(req)
        return req

    def resolve_and_hand_back(self, intervention_id: str, context: BrowserContext) -> InterventionRequest:
        """Operator is done: snapshot the (possibly-modified) session state again,
        so automation resumes with whatever the human just did (e.g. re-authenticated,
        dismissed a dialog, corrected a field) reflected in the session."""
        req = self.load(intervention_id)
        if req is None:
            raise ValueError("Unknown intervention id")
        context.storage_state(path=req.session_state_path)
        req.control_state = ControlState.RESUMING
        req.resolved_at = datetime.now(timezone.utc).isoformat()
        req.operator_actions_log.append(f"[{req.resolved_at}] operator marked resolved, handing control back to automation")
        self.save(req)
        return req

    def mark_resumed(self, intervention_id: str) -> InterventionRequest:
        req = self.load(intervention_id)
        req.control_state = ControlState.AUTOMATION
        self.save(req)
        return req

    def wait_for_resolution(self, intervention_id: str, poll_interval: float = 1.0, timeout: float = 600.0) -> InterventionRequest:
        """Automation-side blocking wait: poll until the operator has resolved and
        handed control back. In a real system this would be an async callback/webhook;
        polling a file is the right-sized mock for the same coordination contract."""
        start = time.time()
        while time.time() - start < timeout:
            req = self.load(intervention_id)
            if req and req.control_state == ControlState.RESUMING:
                return req
            time.sleep(poll_interval)
        raise TimeoutError(f"Intervention {intervention_id} was not resolved within {timeout}s")
