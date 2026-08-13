"""
Discovery Agent
=================
The LLM-in-the-loop half of the system: given a natural-language goal and a
starting page, run an observe -> decide -> act loop until the goal is met or a
stopping condition fires, then distill the successful transcript into a
schema.CapabilityArtifact.

Two closely related but distinct things happen here and they're kept separate on
purpose:

  1. The live transcript -- every observation, every LLM decision (with its
     stated reasoning), every action result. This is verbose, includes the raw
     accessibility-tree dump per step, and is only used for debugging/evidence
     (Section 3.5). It is NOT the artifact.

  2. The distilled artifact -- built AFTER the goal is confirmed reached, by
     asking the LLM to look back over its own successful transcript and emit the
     structured, typed capability described in schema.py. This step is what turns
     "a transcript of one lucky run" into "a reviewable, replayable contract" --
     the LLM is asked to name stable parameters, choose locator fallbacks, and
     write robustness notes, not just log what it clicked.

The loop uses Claude's tool-calling: at each step the model sees a text
rendering of the current accessibility-tree observation and must call exactly
one tool representing the next action (or a "goal_complete" / "stuck" tool to
end the loop). This keeps the model's action space closed and auditable instead
of parsing prose for what it "meant."
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Page
from anthropic import Anthropic

from agent.schema import (
    Action, ActionType, TargetElement, Locator, LocatorStrategy,
    Checkpoint, CheckpointType, Step, CapabilityArtifact, ArtifactMetadata,
    InputParam, OutputField, ParamType, DeclaredOutcome, OutcomeCategory,
)
from agent.perception import observe
from agent.executor import ActionExecutor, GuardrailBlocked, ElementNotResolved
from agent.guardrails import AllowlistPolicy, RiskGate, classify_risk, redact

MODEL = "claude-sonnet-4-5"
MAX_STEPS = 25

SYSTEM_PROMPT = """You are a careful back-office banking operator's assistant. You are driving a REAL legacy \
banking console via a browser to accomplish a stated goal. This is a mock/test system with fake data \
(no real customers, no real money) but you must act EXACTLY as if it were production -- deliberately, \
verifying results, and never guessing at values you have not observed.

Rules:
- You act ONLY through the provided tools. Never assume an action succeeded -- the next observation tells you.
- The UI is old and table/frame-based. Read the "Visible interactive elements" list and the page text \
carefully; elements are numbered ONLY for your reference in reasoning, you must refer to them by their \
accessible name / role / text when calling a tool, not by number.
- Before any action that creates, changes, or closes an account or moves money, you MUST explicitly state \
in your reasoning that you intend to perform that specific risky action and why it is necessary for the goal. \
The system will not execute a risky action without that explicit statement.
- If you hit a validation error, a "not found" result, a permission-denied message, or anything that means \
the stated goal cannot proceed as given, that can be a legitimate STOPPING outcome -- call report_business_outcome \
rather than flailing at the UI.
- If you are stuck (repeated failures, an unrecognized screen, ambiguous next step) call request_human_escalation \
rather than guessing.
- Call report_goal_complete only once you have OBSERVED (in the current page text/elements, not from memory) \
clear evidence the goal was achieved.
"""

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a URL directly.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "reasoning": {"type": "string"}},
            "required": ["url", "reasoning"],
        },
    },
    {
        "name": "click",
        "description": "Click an interactive element, identified by its accessible role and name (or visible text) as shown in the observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "accessible_name_or_text": {"type": "string", "description": "The exact name/text of the element to click, as shown in the observation."},
                "role": {"type": "string", "description": "The role shown for that element, e.g. 'button', 'link'."},
                "frame": {"type": "string", "description": "The frame name the element is in, e.g. 'mainframe'. Use 'main' if not in a named frame."},
                "reasoning": {"type": "string", "description": "Why this click, and if this action is risky (creates/changes/closes an account, moves money), explicitly say so and why it's necessary."},
            },
            "required": ["accessible_name_or_text", "role", "frame", "reasoning"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into a text/password input field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "accessible_name_or_text": {"type": "string"},
                "role": {"type": "string"},
                "frame": {"type": "string"},
                "value": {"type": "string"},
                "is_sensitive": {"type": "boolean", "description": "True if this value is a credential/secret and should never be persisted in artifacts/logs."},
                "reasoning": {"type": "string"},
            },
            "required": ["accessible_name_or_text", "role", "frame", "value", "reasoning"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a <select> dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "accessible_name_or_text": {"type": "string"},
                "frame": {"type": "string"},
                "option_value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["accessible_name_or_text", "frame", "option_value", "reasoning"],
        },
    },
    {
        "name": "extract_text",
        "description": "Read the text of an element on the page (e.g. a balance) to use as an output value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "accessible_name_or_text": {"type": "string", "description": "Text near/on the element, or a distinctive nearby label, to locate it."},
                "frame": {"type": "string"},
                "output_name": {"type": "string", "description": "A short snake_case name for what this value represents, e.g. 'savings_balance'."},
                "reasoning": {"type": "string"},
            },
            "required": ["accessible_name_or_text", "frame", "output_name", "reasoning"],
        },
    },
    {
        "name": "report_goal_complete",
        "description": "Call this once you have observed clear evidence the stated goal has been achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {"type": "string", "description": "What you observed on the page that confirms success."},
            },
            "required": ["summary", "evidence"],
        },
    },
    {
        "name": "report_business_outcome",
        "description": "Call this if the app returned a legitimate non-success result (not found, validation error, permission denied) that means the goal cannot proceed further, but this is a normal/expected outcome, not a system failure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "outcome_name": {"type": "string", "description": "SCREAMING_SNAKE_CASE short name, e.g. MEMBER_NOT_FOUND."},
                "detail": {"type": "string"},
            },
            "required": ["outcome_name", "detail"],
        },
    },
    {
        "name": "request_human_escalation",
        "description": "Call this if you are stuck and cannot safely proceed on your own.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


@dataclass
class TranscriptEntry:
    step: int
    observation_text: str
    llm_reasoning: str
    tool_name: str
    tool_input: dict
    execution_detail: str
    success: bool


@dataclass
class DiscoveryResult:
    goal: str
    status: str  # "success" | "business_outcome" | "escalated" | "hard_failure"
    transcript: list[TranscriptEntry] = field(default_factory=list)
    outcome_name: Optional[str] = None
    outputs: dict = field(default_factory=dict)
    artifact: Optional[CapabilityArtifact] = None
    screenshots: list[str] = field(default_factory=list)


def _find_element_for_action(page: Page, name_or_text: str, role: Optional[str], frame: Optional[str]) -> TargetElement:
    """
    Build a TargetElement (primary + fallback locators) for an element the LLM
    described by accessible name/role/frame, by cross-referencing the current
    Observation. This is where "what the LLM said" gets turned into "what the
    replay engine can reliably find again."
    """
    obs = observe(page)
    candidates = [
        e for e in obs.elements
        if (frame is None or e.frame_name == frame or frame == "main")
        and (name_or_text.strip().lower() in (e.accessible_name or "").lower()
             or name_or_text.strip().lower() in (e.text or "").lower())
    ]
    if not candidates:
        # relax frame constraint as a last resort
        candidates = [
            e for e in obs.elements
            if name_or_text.strip().lower() in (e.accessible_name or "").lower()
            or name_or_text.strip().lower() in (e.text or "").lower()
        ]
    if not candidates:
        raise ElementNotResolved(f"No element found matching name/text '{name_or_text}'")

    el = candidates[0]
    fallbacks = []
    if el.element_id:
        fallbacks.append(Locator(strategy=LocatorStrategy.ELEMENT_ID, value=el.element_id, frame=el.frame_name, description=f"by id fallback for '{name_or_text}'"))
    if el.text and el.text != el.accessible_name:
        fallbacks.append(Locator(strategy=LocatorStrategy.TEXT_CONTENT, value=el.text, frame=el.frame_name, description=f"by text fallback for '{name_or_text}'"))
    fallbacks.append(Locator(strategy=LocatorStrategy.CSS_SELECTOR, value=el.css_path, frame=el.frame_name, description=f"by structural css fallback for '{name_or_text}'"))

    primary = Locator(
        strategy=LocatorStrategy.ARIA_ROLE_NAME if el.role else LocatorStrategy.TEXT_CONTENT,
        value=f"{el.role}::{el.accessible_name}" if el.role else el.text,
        frame=el.frame_name,
        description=f"'{name_or_text}' ({el.role})",
    )
    return TargetElement(
        primary=primary,
        fallbacks=fallbacks,
        robustness_note=(
            f"Primary keys on accessibility role+accessible-name, which survives layout/styling changes. "
            f"Fallbacks degrade through element id, then visible text, then structural CSS position -- "
            f"chosen so a change on one axis (e.g. relabeling) doesn't break all locators at once."
        ),
    )


class DiscoveryAgent:
    def __init__(self, page: Page, allowlist: AllowlistPolicy, api_key: Optional[str] = None):
        self.page = page
        self.allowlist = allowlist
        self.risk_gate = RiskGate(mode="discovery")
        self.executor = ActionExecutor(page, allowlist, self.risk_gate)
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.extracted_outputs: dict[str, str] = {}
        self.recorded_steps: list[dict] = []  # raw dicts, converted to Step objects at distillation time

    def run(self, goal: str, entry_url: str, screenshot_dir: Optional[str] = None) -> DiscoveryResult:
        ok, reason = self.allowlist.check_host(entry_url)
        if not ok:
            raise GuardrailBlocked(reason)

        self.page.goto(entry_url, timeout=15000)
        self.recorded_steps.append({
            "description": f"Navigate to entry point {entry_url}",
            "action": Action(type=ActionType.NAVIGATE, timeout_ms=8000),
            "resolved_value": entry_url,
            "risk": "safe",
        })

        messages = [{
            "role": "user",
            "content": f"GOAL: {goal}\n\nYou are starting at {entry_url}. Here is the current page state:\n\n{observe(self.page).to_llm_text()}",
        }]

        transcript: list[TranscriptEntry] = []
        screenshots: list[str] = []

        for step_num in range(1, MAX_STEPS + 1):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            text_blocks = [b.text for b in resp.content if b.type == "text"]
            reasoning = " ".join(text_blocks)
            tool_blocks = [b for b in resp.content if b.type == "tool_use"]

            if not tool_blocks:
                # model produced only text -- nudge it to act
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": "Please proceed by calling exactly one tool."})
                continue

            tool_call = tool_blocks[0]
            messages.append({"role": "assistant", "content": resp.content})

            tool_result_text = ""
            success = True
            terminal_status = None

            try:
                if tool_call.name == "report_goal_complete":
                    terminal_status = "success"
                elif tool_call.name == "report_business_outcome":
                    terminal_status = "business_outcome"
                elif tool_call.name == "request_human_escalation":
                    terminal_status = "escalated"
                else:
                    tool_result_text = self._execute_tool(tool_call, step_num)
            except (GuardrailBlocked, ElementNotResolved) as e:
                success = False
                tool_result_text = f"ERROR: {e}"
            except Exception as e:
                success = False
                tool_result_text = f"UNEXPECTED ERROR: {e}"

            if screenshot_dir:
                try:
                    path = f"{screenshot_dir}/discovery_step_{step_num:02d}.png"
                    self.page.screenshot(path=path)
                    screenshots.append(path)
                except Exception:
                    pass

            obs_text = observe(self.page).to_llm_text()
            transcript.append(TranscriptEntry(
                step=step_num,
                observation_text=obs_text,
                llm_reasoning=reasoning,
                tool_name=tool_call.name,
                tool_input=tool_call.input,
                execution_detail=tool_result_text or (json.dumps(tool_call.input)),
                success=success,
            ))

            if terminal_status == "success":
                artifact = self._distill_artifact(goal, entry_url, transcript)
                return DiscoveryResult(goal=goal, status="success", transcript=transcript,
                                        outputs=dict(self.extracted_outputs), artifact=artifact, screenshots=screenshots)
            if terminal_status == "business_outcome":
                return DiscoveryResult(goal=goal, status="business_outcome", transcript=transcript,
                                        outcome_name=tool_call.input.get("outcome_name"),
                                        outputs=dict(self.extracted_outputs), screenshots=screenshots)
            if terminal_status == "escalated":
                return DiscoveryResult(goal=goal, status="escalated", transcript=transcript, screenshots=screenshots)

            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result_text or "ok"},
                    {"type": "text", "text": f"Current page state:\n\n{obs_text}"},
                ],
            })

        return DiscoveryResult(goal=goal, status="hard_failure", transcript=transcript, screenshots=screenshots)

    def _execute_tool(self, tool_call, step_num: int) -> str:
        name = tool_call.name
        inp = tool_call.input
        reasoning = inp.get("reasoning", "")

        if name == "navigate":
            action = Action(type=ActionType.NAVIGATE, timeout_ms=10000)
            risk = "safe"
            entry, _ = self.executor.execute(action, step_index=step_num, description=f"navigate: {reasoning}",
                                              risk=risk, risk_confirmed=True, resolved_value=inp["url"])
            self.recorded_steps.append({"description": f"Navigate to {inp['url']}", "action": action,
                                         "resolved_value": inp["url"], "risk": risk})
            return "navigated"

        if name == "click":
            target = _find_element_for_action(self.page, inp["accessible_name_or_text"], inp.get("role"), inp.get("frame"))
            risk = classify_risk(inp["accessible_name_or_text"] + " " + reasoning)
            confirmed = risk == "safe" or ("risky" in reasoning.lower() or "irreversible" in reasoning.lower()
                                            or any(k in reasoning.lower() for k in ["sub-account", "account", "create", "open"]))
            action = Action(type=ActionType.CLICK, target=target, timeout_ms=8000)
            entry, _ = self.executor.execute(action, step_index=step_num, description=f"click '{inp['accessible_name_or_text']}': {reasoning}",
                                              risk=risk, risk_confirmed=confirmed)
            self.recorded_steps.append({"description": f"Click '{inp['accessible_name_or_text']}'", "action": action,
                                         "resolved_value": None, "risk": risk})
            return "clicked"

        if name == "fill":
            target = _find_element_for_action(self.page, inp["accessible_name_or_text"], inp.get("role"), inp.get("frame"))
            is_sensitive = inp.get("is_sensitive", False)
            action = Action(type=ActionType.FILL, target=target, timeout_ms=8000)
            entry, _ = self.executor.execute(action, step_index=step_num, description=f"fill '{inp['accessible_name_or_text']}'",
                                              risk="safe", risk_confirmed=True, resolved_value=inp["value"])
            # if this looks like it corresponds to a goal parameter (not a hardcoded credential),
            # record it with a {param} template instead of the literal value, UNLESS sensitive.
            value_for_artifact = "[REDACTED]" if is_sensitive else inp["value"]
            self.recorded_steps.append({"description": f"Fill '{inp['accessible_name_or_text']}'", "action": action,
                                         "resolved_value": value_for_artifact, "risk": "safe",
                                         "is_param_candidate": not is_sensitive})
            return "filled"

        if name == "select_option":
            target = _find_element_for_action(self.page, inp["accessible_name_or_text"], "combobox", inp.get("frame"))
            action = Action(type=ActionType.SELECT, target=target, timeout_ms=8000)
            entry, _ = self.executor.execute(action, step_index=step_num, description=f"select '{inp['accessible_name_or_text']}'",
                                              risk="safe", risk_confirmed=True, resolved_value=inp["option_value"])
            self.recorded_steps.append({"description": f"Select '{inp['option_value']}' in '{inp['accessible_name_or_text']}'",
                                         "action": action, "resolved_value": inp["option_value"], "risk": "safe",
                                         "is_param_candidate": True})
            return "selected"

        if name == "extract_text":
            target = _find_element_for_action(self.page, inp["accessible_name_or_text"], None, inp.get("frame"))
            action = Action(type=ActionType.EXTRACT, target=target, timeout_ms=8000, extract_as=inp["output_name"])
            entry, value = self.executor.execute(action, step_index=step_num, description=f"extract '{inp['output_name']}'",
                                                  risk="safe", risk_confirmed=True)
            self.extracted_outputs[inp["output_name"]] = value
            self.recorded_steps.append({"description": f"Extract '{inp['output_name']}'", "action": action,
                                         "resolved_value": None, "risk": "safe", "extract_as": inp["output_name"]})
            return f"extracted: {redact(value)}"

        raise ValueError(f"Unknown tool {name}")

    def _distill_artifact(self, goal: str, entry_url: str, transcript: list[TranscriptEntry]) -> CapabilityArtifact:
        """
        Ask the LLM to name stable input parameters (from FILL/SELECT values that
        varied per-invocation, e.g. a member ID) vs. leave others as fixed. This is
        a second, focused LLM call over the (small, distilled) recorded_steps list --
        NOT over the full raw transcript -- because by this point we only need
        parameterization judgment, not further UI navigation.
        """
        fill_candidates = [
            s for s in self.recorded_steps
            if s.get("is_param_candidate") and s.get("resolved_value") not in (None, "[REDACTED]")
        ]
        extract_fields = [s["extract_as"] for s in self.recorded_steps if s.get("extract_as")]

        param_prompt = (
            f"You just successfully completed this goal via browser automation: \"{goal}\"\n\n"
            f"Here are the values you typed/selected during the run that could plausibly be caller-supplied "
            f"parameters on future invocations (rather than fixed constants):\n"
            + "\n".join(f"- {s['description']}: value=\"{s['resolved_value']}\"" for s in fill_candidates)
            + f"\n\nAnd here are the values you extracted as outputs: {extract_fields}\n\n"
            "Respond ONLY with JSON (no prose, no markdown fences) in this exact shape:\n"
            '{"inputs": [{"step_description": "...", "param_name": "member_id", "type": "string", '
            '"description": "...", "example": "..."}], '
            '"outputs": [{"field_name": "savings_balance", "type": "number", "description": "..."}], '
            '"capability_name": "short_snake_case_name"}\n'
            "Only include a step as an input parameter if its value plausibly varies per call (e.g. a member ID, "
            "an amount) -- not if it's a fixed UI choice unlikely to vary (e.g. always the same button). "
            "Match output field_name values to the extracted output names given above."
        )
        resp = self.client.messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": param_prompt}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"inputs": [], "outputs": [], "capability_name": "discovered_capability"}

        param_by_step_desc = {p["step_description"]: p for p in parsed.get("inputs", [])}

        steps: list[Step] = []
        for i, s in enumerate(self.recorded_steps):
            action: Action = s["action"]
            value_template = None
            if s.get("resolved_value") is not None:
                match = param_by_step_desc.get(s["description"])
                value_template = f"{{{match['param_name']}}}" if match else s["resolved_value"]

            new_action = Action(
                type=action.type, target=action.target, value_template=value_template,
                extract_as=s.get("extract_as"), timeout_ms=action.timeout_ms,
            )
            checkpoint = None
            if action.type in (ActionType.NAVIGATE, ActionType.CLICK):
                # heuristic checkpoint: after navigation/click, next observation's URL is a decent
                # default checkpoint; refined below for the final step using success_checkpoint.
                checkpoint = None
            steps.append(Step(
                index=i, description=s["description"], action=new_action,
                checkpoint=checkpoint, risk=s.get("risk", "safe"),
            ))

        inputs = [
            InputParam(name=p["param_name"], type=ParamType(p.get("type", "string")),
                       description=p.get("description", ""), example=p.get("example"))
            for p in parsed.get("inputs", [])
        ]
        outputs = [
            OutputField(name=o["field_name"], type=ParamType(o.get("type", "string")), description=o.get("description", ""))
            for o in parsed.get("outputs", [])
        ]

        final_url = self.page.url
        success_checkpoint = Checkpoint(
            type=CheckpointType.URL_MATCHES,
            expectation=final_url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0].rsplit("/", 1)[0] or "/",
        )
        # prefer a text-based checkpoint using the LLM's own stated success evidence if available
        last_entry = transcript[-1] if transcript else None
        if last_entry and last_entry.tool_name == "report_goal_complete":
            evidence = last_entry.tool_input.get("evidence", "")
            if evidence:
                success_checkpoint = Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation=evidence[:80])

        declared_outcomes = [
            DeclaredOutcome(
                name="MEMBER_NOT_FOUND", category=OutcomeCategory.BUSINESS_OUTCOME,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="NOT FOUND"),
                description="The supplied member ID does not exist in the system.",
            ),
            DeclaredOutcome(
                name="VALIDATION_ERROR", category=OutcomeCategory.BUSINESS_OUTCOME,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="VALIDATION ERROR"),
                description="One or more supplied input values failed server-side validation.",
            ),
            DeclaredOutcome(
                name="PERMISSION_DENIED", category=OutcomeCategory.BUSINESS_OUTCOME,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="PERMISSION DENIED"),
                description="The target member/account is restricted (e.g. frozen) and the action is not permitted.",
            ),
            DeclaredOutcome(
                name="SESSION_TIMEOUT", category=OutcomeCategory.RECOVERABLE,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="SESSION TIMEOUT"),
                description="The operator session expired mid-flow; replay should re-authenticate and retry once.",
            ),
            DeclaredOutcome(
                name="SYSTEM_BUSY", category=OutcomeCategory.RECOVERABLE,
                detection=Checkpoint(type=CheckpointType.TEXT_PRESENT, expectation="SYSTEM BUSY"),
                description="A transient backend timeout; replay should retry with backoff before failing hard.",
            ),
        ]

        artifact = CapabilityArtifact(
            metadata=ArtifactMetadata(
                artifact_id=str(uuid.uuid4()),
                name=parsed.get("capability_name", "discovered_capability"),
                surface="cu-serv-mock-v1",
                discovery_goal=goal,
            ),
            inputs=inputs,
            outputs=outputs,
            entry_point=entry_url,
            preconditions=["Operator session must be authenticated (see login capability / precondition)."],
            steps=steps,
            declared_outcomes=declared_outcomes,
            success_checkpoint=success_checkpoint,
            allowlist_domains=self.allowlist.allowed_hosts,
        )
        return artifact
