# Design Report: Computer-Use Automation System

## 1. Architecture

```
main.py (CLI)
  ├── mock_bank/app.py        target surface: legacy-style Flask app
  ├── agent/perception.py     observe() -- live page -> text observation
  ├── agent/executor.py       execute() -- the one choke point for actions + guardrails
  ├── agent/discovery.py      LLM observe->decide->act loop; distills a CapabilityArtifact
  ├── agent/replay.py         deterministic execution of a saved artifact, no LLM
  ├── agent/guardrails.py     allowlist, risk policy, redaction -- shared by both paths
  ├── agent/escalation.py     intervention state machine + session hand-off
  └── agent/schema.py         artifact + result data contracts
```

**One executor, two callers.** `ActionExecutor` is the single place that resolves a
locator, checks the allowlist and risk gate, and performs a Playwright action. Discovery
and replay both call it, so locator/guardrail logic can't drift between "what got
recorded" and "what replay does."

**Synchronous, single-process, file-backed.** An artifact is a JSON file; an intervention
is a JSON file. No queue, no DB -- a conscious simplification for a single-tenant demo
(Section 4 covers how this changes at scale).

**Accessibility-tree-first perception.** `perception.py` builds observations from role +
accessible-name + frame rather than screenshot coordinates, which break on
resize/zoom/DPI and don't persist meaningfully into a replayable artifact. It also
surfaces non-interactive, id-bearing elements (e.g. table cells showing balances), not
just clickable/fillable controls -- legacy table-layout UIs render meaningful data outside
any interactive element. Trade-off: this projects the DOM into role/name rather than
calling a true OS accessibility API (Playwright's sync API doesn't expose one for web
content); the projection uses the same signals a real accessibility tree is built from,
and the swap to a true API is isolated to `perception.py`/`executor.py`.

## 2. Artifact schema

**Locators carry a primary strategy plus ordered fallbacks with a `robustness_note`.**
Legacy apps have no test IDs, so one selector isn't viable. Primary is typically ARIA
role+name; fallbacks degrade through element id, visible text, structural CSS, so drift on
one axis doesn't take down every fallback at once. `resolve_target()` tries them in order
and only proceeds on exactly one match (ambiguous multi-match is treated like zero-match).

**Checkpoints are assertions, not hopes.** Every state-changing step can declare a
`Checkpoint` (URL match, text present/absent, element visible) that must hold after the
action, separating "the click worked" from "the click silently did nothing." A
`success_checkpoint` independent of any single step verifies overall completion.

**Inputs/outputs are a typed, named contract.** A caller sees `{member_id: str} ->
{savings_balance: str}`, generated directly from `InputParam`/`OutputField` -- not
"whatever step 3 typed." Distillation identifies which recorded values are plausibly
caller-supplied (a member ID, an amount) versus fixed constants, matched by a stable
integer id assigned during discovery rather than by free-text description (Section 7).

**Outcomes are a closed, named taxonomy.** `DeclaredOutcome` entries are tagged
`business_outcome` or `recoverable` with their own detection checkpoint -- the schema-level
expression of "no such member is not a crash."

**`approval_state` (`draft`/`approved`)** lives on the artifact and is what the risk gate
checks before letting a risky step run unattended (Section 6).

## 3. Determinism & error handling

Replay never calls an LLM: it walks steps in order, renders `value_template` against the
caller's `params`, executes through the shared executor, and checks each `Checkpoint`.

**The three-way split is structural.** On step failure, the engine checks current page
state against every `DeclaredOutcome.detection`. A matching `business_outcome` returns
immediately as a typed result, not an exception. A matching `recoverable` outcome
triggers one bounded recovery attempt (`MAX_RECOVERY_ATTEMPTS = 1`) before surfacing
failure. Nothing matching is a `hard_failure`, returned with exactly which step, what was
expected, what was observed.

**Recovery rewinds to the right scope.** Retrying only the step where failure was
*detected* is wrong when the real cause was a failed form submission earlier -- the page
reload already cleared the form. `_rewind_target_for_recovery()` walks back to the
nearest state-changing action and any `FILL`/`SELECT` steps feeding it, so recovery redoes
the whole input-and-submit block. Verified by
`test_replay_recovers_from_injected_transient_failure` (injects a one-shot "system busy"
response, confirms recovery with correct output) and reproduced live in
`evidence/replay/replay_recovered_transient_failure.log`. Concrete policies: `SESSION_TIMEOUT`
re-authenticates once and retries; `SYSTEM_BUSY` waits 1.5s and retries once. Both are
hardcoded and auditable, not an LLM call.

**`SELECT` resolves against real on-page options, not a literal string match.** Actions
match the actual `<option>` value/label pairs present on the page (exact value, then
exact label case-insensitively, then a normalized match) rather than trusting a saved
`value_template` to match Playwright's raw `select_option(value=...)` semantics exactly.
This matters because the template is itself LLM-proposed at discovery time and may not
match the literal HTML attribute verbatim -- resolving against live DOM state means
replay tolerates an imperfect saved string the same way locator fallbacks tolerate an
imperfect selector.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** Everything above `perception`/`executor` depends only on
`Observation`/`TargetElement`/`Locator`, never Playwright directly. For a desktop app,
`observe()`/`resolve_target()` would swap to a real accessibility-API client (UIA/AT-SPI)
returning the same shapes; the artifact schema wouldn't change. *(Desktop support is
design-only, per the brief's scope note.)*

**Multi-tenant reuse -- built and verified against a second live tenant instance.**
`ArtifactMetadata.tenant_scope` drives the model: a `base` artifact is tried first for
every tenant on the same vendor product. A `TenantOverride` layer (`schema.py`) replaces
individual `Step.action.target` locators and checkpoint expectations -- not whole steps --
so flow shape and outcome taxonomy stay shared and only the differing locators get
overridden. `mock_bank/app.py`'s `TENANT`/`PORT` override (Northwind Credit Union, port
5056) relabels form fields and result labels while keeping the same flow, giving a real
second instance to validate against. `tests/test_multitenant_override.py` confirms three
things against both live instances: the unmodified base artifact fails against Northwind
(the override is necessary, not a no-op), the same artifact with a Northwind override
succeeds, and the base artifact still works unmodified against the base tenant.

**Drift detection.** `ExecutionLogEntry.locator_used` shows which fallback strategy
actually resolved each step -- a shift from primary to fallback for one tenant is a
leading indicator before it fully breaks; a rising `hard_failure` rate isolated to one
`tenant_scope` is the trailing indicator an override is needed.

## 5. Escalation & handoff

`agent/escalation.py` implements `AUTOMATION -> PENDING_HUMAN -> HUMAN -> RESUMING ->
AUTOMATION`. `InterventionRequest` carries the goal, step index, URL, screenshot, and
reason, written to `/evidence/`.

**The hand-off mechanism is real, not mocked, and verified across a genuine process
boundary.** On escalation, `context.storage_state()` dumps the live browser context's
cookies/localStorage to disk -- for a cookie-authenticated app, that file *is* the live
session. `test_operator_resumes_paused_session_without_fresh_login` runs the handoff
across three separate Playwright browser instances (not just separate pages): automation
escalates and fully closes its browser; a fresh browser loads only the saved
`storage_state` and lands on the protected page with no login call; the operator acts and
hands back; a third fresh browser confirms the updated state is still authenticated. The
operator console drives the same session, then `resolve_and_hand_back()` re-dumps state
and flips control to `RESUMING`; automation polls `wait_for_resolution()` and resumes.

**What's mocked:** no live pixel-streaming/co-browsing (out of scope); operator "actions"
are a fixed list of dicts rather than a live-clicked UI. What's *not* mocked is the hard
part -- the control-transfer model, proven by the resumed session staying authenticated.
Generalizing beyond cookies: the pattern holds, but `storage_state` itself is
Playwright/cookie-specific.

## 6. Safety

Both controls sit in `ActionExecutor.execute()`, so neither discovery nor replay can
bypass them via a different path.

**Allowlist.** `AllowlistPolicy` checks destination host on every navigate and action type
against a configured set, before execution.

**Risk gating.** Every action is `safe`/`risky` (`classify_risk()`, a static keyword list
-- an auditable rule, not a "smart" heuristic). During discovery, a risky action executes
only if the LLM's stated reasoning explicitly signals intent to perform it, mechanically
checked by `risk_gate.gate(risk, confirmed=...)`. During replay, only if
`approval_state == "approved"`. Verified both ways:
`test_replay_blocks_risky_action_on_unapproved_artifact` blocks a draft artifact and
allows the same one approved; a live discovery run opened a YOUTH_SAVINGS sub-account end
to end, with the model's reasoning explicitly justifying the risky click before it was
permitted to execute (`evidence/discovery/transcript.json`,
`artifacts_store/open_youth_savings_account.json`).

**Redaction.** `redact()`/`redact_form_value()` sit on the log-write path in
`_record()`, not something each call site has to remember -- pattern-based for
password/token/SSN/card-shaped values, field-name-based regardless of content. The
`is_sensitive` flag on discovery's `fill` tool stops a credential's literal value from
ever entering the artifact.

**Limits, stated honestly:** risk classification is keyword-based and would miss risky
intent phrased unexpectedly. The allowlist checks hostnames, not routes. Redaction is a
backstop, not a guarantee.

## 7. Cuts

**Built and verified end-to-end on two real capabilities, not just described.** A
read-only lookup and a risky state-changing flow were both discovered live by an LLM
(Gemini, via `google-genai`) driving the real mock app, then replayed deterministically
with no model involved. The lookup artifact replayed correctly against two member IDs
($4,821.63 and $250.00) plus a business-outcome case (`MEMBER_NOT_FOUND`, reported as a
typed result) and a recovered transient failure -- all saved in `/evidence/replay/`. The
risky artifact's account-opening step was correctly gated at both discovery and replay
time. 36 automated tests back this up, 13 of them integration tests against a real
headless browser across replay, escalation, and multi-tenant scenarios.

**Four real bugs surfaced by running the system, not by reading the code, each fixed and
regression-tested:** perception initially missed non-interactive data cells (Section 1);
parameter templating matched by free-text description and could silently mis-parameterize
a value, fixed by matching on a stable id (Section 2,
`test_discovery_distillation.py`); the `gemini-flash-latest` alias silently resolved to a
model with a much stricter free-tier quota mid-project, fixed by pinning an explicit
model string; and `SELECT` actions matched option values too literally, needing multiple
LLM guesses at casing before one resolved, fixed by `_select_option_smart()`'s
value/label/normalized matching (Section 3, `test_select_option_smart.py`). All four
were found by running real discovery sessions, which is why the two live runs in this
report matter as evidence rather than as a demo.

**Known limitation:** distillation parameterizes each recorded `FILL`/`SELECT` step
independently, so two selects targeting the same logical choice (an account type,
selected then re-confirmed before submission) can surface as two separate input
parameters with identical values instead of one. The right fix is deduplicating by
target locator before naming parameters -- not done here for lack of time.

**Deliberately not built:** a real-time operator console and desktop-surface support
(both explicitly out of scope); an agent-facing capability catalog (optional stretch
goal, skipped to keep the core requirements each genuinely real).

**Next, in order:** (1) route-level allowlist extension, (2) deduplicate
identical-target select/fill steps during distillation (see known limitation), (3)
multi-run stability (replay N times, report a flakiness signal).