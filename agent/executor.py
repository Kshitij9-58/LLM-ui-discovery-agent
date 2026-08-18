"""
Action Executor
==================
The single choke point that turns a schema.Action / TargetElement into real
Playwright calls. Used by BOTH the discovery loop and the replay engine, so
locator-resolution logic and guardrail enforcement can't drift between the two --
what gets recorded during discovery is exactly what replay will do.

Locator resolution strategy (see REPORT.md section 3): try `primary`, and if it
does not resolve to exactly one visible element, walk `fallbacks` in order. This
is what "graceful degradation instead of instant breakage" means in practice --
a locator keyed on exact button text survives a font/spacing change; a locator
keyed on table position survives a text relabel; having both means one axis of
drift doesn't kill the capability.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, Frame, Locator as PWLocator, TimeoutError as PWTimeoutError

from agent.schema import (
    Action, ActionType, TargetElement, Locator, LocatorStrategy,
    Checkpoint, CheckpointType,
)
from agent.perception import resolve_frame
from agent.guardrails import AllowlistPolicy, RiskGate, redact


class GuardrailBlocked(Exception):
    pass


class ElementNotResolved(Exception):
    pass


class CheckpointFailed(Exception):
    pass


@dataclass
class ExecutionLogEntry:
    step_index: Optional[int]
    action_type: str
    description: str
    locator_used: Optional[str]  # which strategy actually resolved it
    success: bool
    detail: str
    timestamp: float


def _pw_locator_for(frame_or_page, loc: Locator) -> PWLocator:
    if loc.strategy == LocatorStrategy.TEST_ID:
        return frame_or_page.get_by_test_id(loc.value)
    if loc.strategy == LocatorStrategy.ARIA_ROLE_NAME:
        role, name = loc.value.split("::", 1)
        return frame_or_page.get_by_role(role, name=name, exact=False)
    if loc.strategy == LocatorStrategy.LABEL_TEXT:
        return frame_or_page.get_by_label(loc.value, exact=False)
    if loc.strategy == LocatorStrategy.TEXT_CONTENT:
        return frame_or_page.get_by_text(loc.value, exact=False)
    if loc.strategy == LocatorStrategy.ELEMENT_ID:
        return frame_or_page.locator(f"#{loc.value}")
    if loc.strategy == LocatorStrategy.CSS_SELECTOR:
        return frame_or_page.locator(loc.value)
    if loc.strategy == LocatorStrategy.XPATH:
        return frame_or_page.locator(f"xpath={loc.value}")
    raise ValueError(f"Unknown locator strategy: {loc.strategy}")


def resolve_target(page: Page, target: TargetElement, timeout_ms: int = 8000) -> tuple[PWLocator, str]:
    """Try primary then fallbacks. Returns (playwright_locator, which_strategy_worked)."""
    candidates = [target.primary] + list(target.fallbacks)
    last_err = None
    for loc in candidates:
        try:
            frame_or_page = resolve_frame(page, loc.frame)
            pw_loc = _pw_locator_for(frame_or_page, loc)
            count = pw_loc.count()
            if count == 1:
                pw_loc.first.wait_for(state="visible", timeout=timeout_ms)
                return pw_loc.first, loc.strategy.value
            elif count > 1:
                # ambiguous match -- not safe to act on, try next strategy
                last_err = f"strategy {loc.strategy.value} matched {count} elements (ambiguous)"
                continue
            else:
                last_err = f"strategy {loc.strategy.value} matched 0 elements"
                continue
        except PWTimeoutError:
            last_err = f"strategy {loc.strategy.value} timed out waiting for visibility"
            continue
        except Exception as e:
            last_err = f"strategy {loc.strategy.value} raised {e}"
            continue
    raise ElementNotResolved(
        f"Could not resolve target '{target.primary.description}' via primary or any of "
        f"{len(target.fallbacks)} fallback(s). Last error: {last_err}"
    )


def _select_option_smart(loc: PWLocator, requested_value: str, timeout_ms: int) -> str:
    """
    Select an <option> by matching `requested_value` against the actual option
    values/labels present on the page, instead of trusting the caller's exact
    casing or format.

    Playwright's select_option(str) matches by the option's `value` attribute
    ONLY -- it is not a fuzzy or label-aware match. An LLM proposing a value
    like "Youth Savings" when the real HTML is <option value="YOUTH_SAVINGS">
    will silently fail to match, or in some cases hang waiting for an option
    that will never appear. This caused a real bug: five near-duplicate SELECT
    steps were recorded into a single discovery artifact because the model
    kept guessing different casings/formats for the same intended selection,
    and one guess ("youth_savings", lowercase) timed out entirely without
    halting the run. See REPORT.md, "Cuts" / bug log.

    Match order:
      1. exact value match (value="...")
      2. exact label match (case-insensitive)
      3. normalized match (strip whitespace/case/underscores/hyphens) on
         either value or label

    Returns the actual `value` attribute that was selected. Raises ValueError
    with the real available options listed if nothing matches, so callers see
    what's actually on the page instead of a bare timeout.
    """
    options = loc.evaluate(
        "el => Array.from(el.options).map(o => ({value: o.value, label: o.label.trim()}))"
    )
    if not options:
        raise ValueError("select element has no <option> children")

    def normalize(s: str) -> str:
        return s.strip().lower().replace("_", "").replace(" ", "").replace("-", "")

    target_norm = normalize(requested_value)

    # 1) exact value match
    for opt in options:
        if opt["value"] == requested_value:
            loc.select_option(value=opt["value"], timeout=timeout_ms)
            return opt["value"]

    # 2) exact label match (case-insensitive)
    for opt in options:
        if opt["label"].lower() == requested_value.strip().lower():
            loc.select_option(value=opt["value"], timeout=timeout_ms)
            return opt["value"]

    # 3) normalized match on either value or label
    for opt in options:
        if normalize(opt["value"]) == target_norm or normalize(opt["label"]) == target_norm:
            loc.select_option(value=opt["value"], timeout=timeout_ms)
            return opt["value"]

    available = ", ".join(f"value={o['value']!r} label={o['label']!r}" for o in options)
    raise ValueError(
        f"No option matching {requested_value!r} found. Available options: {available}"
    )


def check_checkpoint(page: Page, cp: Checkpoint) -> tuple[bool, str]:
    try:
        if cp.type == CheckpointType.URL_MATCHES:
            ok = cp.expectation in page.url
            return ok, f"url={page.url}"
        if cp.type in (CheckpointType.TEXT_PRESENT, CheckpointType.TEXT_ABSENT):
            frame_or_page = resolve_frame(page, cp.frame)
            if frame_or_page is page:
                text = page.evaluate("() => document.body ? document.body.innerText : ''")
                # also scan sub-frames since framesets have no top-level body text
                for fr in page.frames:
                    if fr is not page.main_frame:
                        try:
                            text += "\n" + fr.evaluate("() => document.body ? document.body.innerText : ''")
                        except Exception:
                            pass
            else:
                text = frame_or_page.evaluate("() => document.body ? document.body.innerText : ''")
            present = cp.expectation in text
            if cp.type == CheckpointType.TEXT_PRESENT:
                return present, f"text_present={present}"
            else:
                return (not present), f"text_absent_check, found={present}"
        if cp.type == CheckpointType.ELEMENT_VISIBLE:
            try:
                resolve_target(page, cp.target, timeout_ms=3000)
                return True, "element resolved and visible"
            except ElementNotResolved as e:
                return False, str(e)
    except Exception as e:
        return False, f"checkpoint evaluation error: {e}"
    return False, "unhandled checkpoint type"


class ActionExecutor:
    """
    Executes one schema.Action against a live page, enforcing the allowlist and
    risk gate first. Shared by discovery and replay -- callers differ only in how
    they construct actions (LLM-proposed vs. loaded from a saved artifact) and in
    the RiskGate mode/approval they pass in.
    """

    def __init__(self, page: Page, allowlist: AllowlistPolicy, risk_gate: RiskGate):
        self.page = page
        self.allowlist = allowlist
        self.risk_gate = risk_gate
        self.log: list[ExecutionLogEntry] = []

    def _record(self, step_index, action_type, description, locator_used, success, detail):
        entry = ExecutionLogEntry(
            step_index=step_index,
            action_type=action_type,
            description=redact(description),
            locator_used=locator_used,
            success=success,
            detail=redact(detail),
            timestamp=time.time(),
        )
        self.log.append(entry)
        return entry

    def execute(
        self,
        action: Action,
        step_index: Optional[int] = None,
        description: str = "",
        risk: str = "safe",
        risk_confirmed: bool = False,
        resolved_value: Optional[str] = None,
    ) -> tuple[ExecutionLogEntry, Optional[str]]:
        """Returns (log_entry, extracted_value). extracted_value is None except for EXTRACT actions."""
        extracted_value: Optional[str] = None

        # 1) allowlist: action type
        ok, reason = self.allowlist.check_action_type(action.type.value)
        if not ok:
            self._record(step_index, action.type.value, description, None, False, reason)
            raise GuardrailBlocked(reason)

        # 2) allowlist: destination host, for navigate / form submission targets
        if action.type == ActionType.NAVIGATE and resolved_value:
            ok, reason = self.allowlist.check_host(resolved_value)
            if not ok:
                self._record(step_index, action.type.value, description, None, False, reason)
                raise GuardrailBlocked(reason)

        # 3) risk gate
        ok, reason = self.risk_gate.gate(risk, confirmed=risk_confirmed)
        if not ok:
            self._record(step_index, action.type.value, description, None, False, f"RISK GATE BLOCKED: {reason}")
            raise GuardrailBlocked(reason)

        # 4) actually perform it
        try:
            if action.type == ActionType.NAVIGATE:
                self.page.goto(resolved_value, timeout=action.timeout_ms)
                self._record(step_index, "navigate", description, None, True, f"navigated to {resolved_value}")

            elif action.type == ActionType.CLICK:
                loc, strategy_used = resolve_target(self.page, action.target, action.timeout_ms)
                loc.click(timeout=action.timeout_ms)
                self._record(step_index, "click", description, strategy_used, True, "clicked")

            elif action.type == ActionType.FILL:
                loc, strategy_used = resolve_target(self.page, action.target, action.timeout_ms)
                loc.fill(resolved_value or "", timeout=action.timeout_ms)
                self._record(step_index, "fill", description, strategy_used, True, f"filled value (redacted if sensitive)")

            elif action.type == ActionType.SELECT:
                loc, strategy_used = resolve_target(self.page, action.target, action.timeout_ms)
                actual_value = _select_option_smart(loc, resolved_value or "", action.timeout_ms)
                self._record(
                    step_index, "select", description, strategy_used, True,
                    f"selected {actual_value} (requested: {resolved_value!r})",
                )

            elif action.type == ActionType.WAIT_FOR:
                loc, strategy_used = resolve_target(self.page, action.target, action.timeout_ms)
                self._record(step_index, "wait_for", description, strategy_used, True, "condition met")

            elif action.type == ActionType.EXTRACT:
                loc, strategy_used = resolve_target(self.page, action.target, action.timeout_ms)
                text = loc.inner_text(timeout=action.timeout_ms).strip()
                extracted_value = text
                self._record(step_index, "extract", description, strategy_used, True, f"extracted: {text}")

            elif action.type == ActionType.ASSERT:
                # ASSERT actions carry their condition in the step's checkpoint, not here
                self._record(step_index, "assert", description, None, True, "assert placeholder executed")

            else:
                raise ValueError(f"Unhandled action type {action.type}")

        except GuardrailBlocked:
            raise
        except Exception as e:
            self._record(step_index, action.type.value, description, None, False, f"execution error: {e}")
            raise

        return self.log[-1], extracted_value