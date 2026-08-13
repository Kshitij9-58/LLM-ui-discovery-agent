"""
Safety & Policy Guardrails
============================
Two independent controls, enforced at the SAME choke point (the action executor
used by both discovery and replay) so policy can't be bypassed by one code path:

1. Allowlist -- an explicit, configurable list of hosts and action types the agent
   is permitted to touch. Anything outside it is BLOCKED before execution, not
   after-the-fact logged.

2. Risk classification -- every action is 'safe' (read-only / reversible, e.g.
   navigate, fill a field, click a search button) or 'risky' (state-changing in a
   way that matters -- money movement, account creation/closure, anything with
   real business consequence). Risky actions are handled conservatively:
     - During DISCOVERY (LLM deciding live): risky actions require an explicit
       confirm step recorded in the transcript before the executor will perform
       them -- the LLM must declare intent, not just act.
     - During REPLAY (deterministic): risky actions execute automatically ONLY if
       the artifact's approval_state is 'approved' (a human reviewer signed off on
       the recorded flow). A 'draft' artifact performing a risky action halts and
       escalates instead of running unattended.

This module also owns redaction: anything written to logs/artifacts passes through
`redact()` first, so secrets and full PII never get persisted -- a policy that has
to be structural (one function on the write path) rather than remembered at every
call site, since "someone forgot to redact one log line" is the realistic failure
mode for that kind of rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

@dataclass
class AllowlistPolicy:
    allowed_hosts: list[str]
    allowed_action_types: list[str] = field(
        default_factory=lambda: ["navigate", "click", "fill", "select", "wait_for", "extract", "assert"]
    )

    def check_host(self, url: str) -> tuple[bool, str]:
        host = urlparse(url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return True, ""
        return False, f"Host '{host}' is not in the allowlist {self.allowed_hosts}."

    def check_action_type(self, action_type: str) -> tuple[bool, str]:
        if action_type in self.allowed_action_types:
            return True, ""
        return False, f"Action type '{action_type}' is not in the allowed set {self.allowed_action_types}."


class AllowlistViolation(Exception):
    pass


# ---------------------------------------------------------------------------
# Risk policy
# ---------------------------------------------------------------------------

# Action.description substrings / heuristics an LLM's proposed action can be checked
# against during discovery, in addition to the explicit 'risky' flag steps carry once
# they're part of a recorded artifact. Kept small and explicit rather than "smart" --
# a static, auditable list is the right shape for a safety control.
RISKY_KEYWORDS = [
    "sub-account", "subaccount", "open account", "close account", "delete", "transfer",
    "withdraw", "deposit", "submit payment", "wire", "freeze", "unfreeze",
    "confirm", "approve", "override",
]


def classify_risk(action_description: str) -> str:
    d = action_description.lower()
    return "risky" if any(k in d for k in RISKY_KEYWORDS) else "safe"


class RiskGate:
    """
    Gatekeeper for risky actions. One instance shared by the discovery loop and the
    replay engine so the policy is identical in both places.
    """

    def __init__(self, mode: str, artifact_approved: bool = False):
        """
        mode: 'discovery' or 'replay'.
        artifact_approved: only meaningful in replay mode -- whether the artifact
        being executed has approval_state == 'approved'.
        """
        assert mode in ("discovery", "replay")
        self.mode = mode
        self.artifact_approved = artifact_approved

    def gate(self, risk: str, confirmed: bool) -> tuple[bool, str]:
        """
        Returns (allowed, reason). `confirmed` means: during discovery, the LLM
        explicitly stated intent to perform this specific risky action in its
        reasoning for this step (checked by the caller before invoking gate);
        during replay, it's ignored -- replay uses artifact_approved instead.
        """
        if risk == "safe":
            return True, ""
        if self.mode == "discovery":
            if confirmed:
                return True, ""
            return False, "Risky action requires explicit confirmation in the agent's stated reasoning before it will be executed."
        else:  # replay
            if self.artifact_approved:
                return True, ""
            return False, "Artifact is not approved; risky/irreversible steps require human approval before unattended replay."


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_PATTERNS = [
    (re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*[^\s&"\']+'), r"\1=[REDACTED]"),
    (re.compile(r'(?i)(token|api[_-]?key|secret|authorization)\s*[=:]\s*[^\s&"\']+'), r"\1=[REDACTED]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),                     # SSN-shaped
    (re.compile(r"\b\d{13,19}\b"), "[REDACTED-CARDNUM]"),                          # card-number-shaped
    (re.compile(r'(?i)(sid|session)\s*[=:]\s*[a-z0-9\-\.]{10,}'), r"\1=[REDACTED]"),
]

# Field names that should never appear with their real value in logs/artifacts,
# even if the value itself doesn't match a pattern above (e.g. a short test password).
SENSITIVE_FIELD_NAMES = {"password", "passwd", "pwd", "ssn", "card_number", "cvv", "token", "api_key"}


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_form_value(field_name: str, value: str) -> str:
    if field_name.lower() in SENSITIVE_FIELD_NAMES:
        return "[REDACTED]"
    return redact(value)
