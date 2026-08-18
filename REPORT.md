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
locator, checks the allowlist and risk gate, and performs a Playwright action. Both
discovery and replay call it. If locator/guardrail logic lived separately in each path,
they could drift apart and "what got recorded" would stop matching "what replay does."

**Synchronous, single-process, file-backed.** No queue, no DB. An artifact is a JSON file;
an intervention is a JSON file. The brief doesn't reward scaling infrastructure ahead of a
working core, and this is a conscious simplification for a single-tenant demo (see Section 4
for how it would change at scale).

**Accessibility-tree-first perception, not screenshot+coordinates.** `perception.py`
builds observations from role + accessible-name + frame, computed the way a browser
computes the accessibility tree. Coordinates are the least stable thing to persist into a
replayable artifact (break on resize/zoom/DPI); the accessibility tree is, per the brief's
own glossary, "often more stable than raw markup, and available on desktop apps too" --
which is also the strongest bridge to the desktop-surface story in Section 4. Trade-off:
this reads the DOM and projects it into role/name rather than calling a true OS
accessibility API (Playwright's Python sync API doesn't expose one for web content); the
projection uses the same signals real accessibility trees are built from, but the swap to
a true API is isolated to `perception.py`/`executor.py`, not spread through the system.
`perception.py` also surfaces non-interactive, id-bearing elements (e.g. table cells
showing balances) as labeled, extractable observations, not just clickable/fillable
controls -- legacy table-layout UIs render meaningful data outside any interactive
element, and a perception layer scoped only to buttons/inputs/links would be blind to it.

## 2. Artifact schema

**Locators carry a primary strategy plus ordered fallbacks with a `robustness_note`.**
Legacy apps have no test IDs, so one selector isn't viable. Primary might be ARIA
role+name; fallbacks degrade through element id, visible text, structural CSS -- chosen so
drift on one axis doesn't take down every fallback at once. `resolve_target()` tries them
in order and only proceeds on exactly one match (ambiguous multi-match is treated like
zero-match).

**Checkpoints are assertions, not hopes.** Every state-changing step can declare a
`Checkpoint` (URL match, text present/absent, element visible) that must hold after the
action -- this is what separates "the click worked" from "the click silently did nothing."
A `success_checkpoint` independent of any single step verifies overall completion.

**Inputs/outputs are a typed, named contract.** A caller sees `{member_id: str} ->
{savings_balance: str}`, not "whatever step 3 typed" -- generated directly from
`InputParam`/`OutputField`. Distillation identifies which recorded values are plausibly
caller-supplied parameters (a member ID, an amount) versus fixed constants, and names them
by a stable integer id assigned during discovery rather than by matching free-text
descriptions -- see Section 7 for why that distinction matters in practice.

**Outcomes are a closed, named taxonomy.** `DeclaredOutcome` entries are tagged
`business_outcome` or `recoverable` with their own detection checkpoint -- the schema-level
expression of "no such member is not a crash."

**`approval_state` (`draft`/`approved`) lives on the artifact**, which is what the risk
gate checks before letting a risky step run unattended (Section 6).

## 3. Determinism & error handling

Replay never calls an LLM. It walks steps in order, renders `value_template` against the
caller's `params`, executes through the shared executor, and checks each step's
`Checkpoint`.

**The three-way split is structural.** When a step fails, the engine first checks the
*current page state* against every `DeclaredOutcome.detection` on the artifact. A matching
`business_outcome` returns immediately as a typed result, not an exception. A matching
`recoverable` outcome triggers one bounded, policy-defined recovery attempt (capped at
`MAX_RECOVERY_ATTEMPTS = 1`) before surfacing failure. Nothing matching is a `hard_failure`,
returned with exactly which step, what was expected, what was observed.

**Recovery rewinds to the right scope, not just the failed step.** A naive retry of only
the step where failure was *detected* (e.g. an `EXTRACT` reading a balance) is wrong when
the real cause was a failed form submission two steps earlier -- the page reload already
cleared the form, so retrying just the read can't help. `_rewind_target_for_recovery()`
walks back to the nearest state-changing action and further back over any `FILL`/`SELECT`
steps feeding it, so recovery redoes the whole input-and-submit block. Verified by
`test_replay_recovers_from_injected_transient_failure`, which injects a one-shot "system
busy" response and confirms replay recovers with the correct extracted value.

**Concrete recovery policies implemented (bounded, not open-ended):** `SESSION_TIMEOUT` ->
re-authenticate once, retry the rewound block. `SYSTEM_BUSY` -> wait 1.5s, retry once. Both
are hardcoded and auditable, not an LLM call -- adding an LLM escape hatch to replay would
undermine the "no model in the decision loop" property this section is built around.

**Locator resolution is robust to option-value/label mismatches, not just element
drift.** `SELECT` actions resolve against the real `<option>` value/label pairs present on
the page (exact value, then exact label case-insensitively, then a normalized match)
rather than trusting a caller-supplied string to match Playwright's raw
`select_option(value=...)` semantics exactly. This matters because the discovered
`value_template` for a select step is itself LLM-proposed at discovery time and may not
match the literal HTML `value=` attribute verbatim -- resolving against real DOM state
means replay succeeds even when the saved template is a close-but-imperfect string, the
same way locator fallbacks tolerate imperfect selectors.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is already drawn at perception/executor: everything
above only depends on `Observation`/`TargetElement`/`Locator`, never Playwright directly.
The mock target is already a frameset app, so that case is covered. For a desktop app,
`observe()`/`resolve_target()` would be swapped for a real accessibility-API client (UIA/
AT-SPI) returning the same shapes; `ARIA_ROLE_NAME`/`TEST_ID` generalize directly to
desktop roles/automation-ids. The artifact schema itself wouldn't change. *(This part of
the desktop story remains design-only -- not built, per the brief's scope note.)*

**Multi-tenant reuse -- built and verified against a second live tenant instance.**
`ArtifactMetadata.tenant_scope` (`"base"` or a tenant id) drives the intended model: a
`base` artifact from one tenant's discovery run is tried first for every tenant on the
same vendor product. A tenant-scoped override layer (`TenantOverride`/
`apply_tenant_override` in `schema.py`) replaces individual `Step.action.target` locators
and checkpoint expectations -- not whole steps -- for a given tenant, so the flow shape,
checkpoints, and outcome taxonomy stay shared and only the handful of locators that
actually differ get overridden. `mock_bank/app.py` supports a `TENANT`/`PORT` env-var
override (Northwind Credit Union, port 5056) that relabels form fields and result labels
while keeping the same underlying flow, giving a genuinely different second instance to
validate against rather than a hypothetical one. `tests/test_multitenant_override.py`
covers three scenarios against both live instances: the unmodified base artifact correctly
fails against Northwind (proving the override is actually necessary, not a no-op), the
same artifact with a Northwind `TenantOverride` applied succeeds and reads the correct
value from Northwind's relabeled field, and the unmodified base artifact still works
against the base tenant (proving the override mechanism doesn't regress the default path).

**Drift detection**, from data replay already produces: `ExecutionLogEntry.locator_used`
shows which strategy in the fallback chain actually resolved each step -- a shift from
primary to fallback for one tenant is a leading indicator before it fully breaks. A rising
`hard_failure` rate isolated to one `tenant_scope` is the trailing indicator a targeted
override is needed.

## 5. Escalation & handoff

`agent/escalation.py` implements `AUTOMATION -> PENDING_HUMAN -> HUMAN -> RESUMING ->
AUTOMATION`. `InterventionRequest` carries the goal/capability, step index, current URL, a
screenshot, and the reason -- written to `/evidence/`.

**The actual hand-off mechanism -- real, not mocked, and verified end-to-end.** On
escalation, `context.storage_state()` dumps the live browser context's cookies/localStorage
to disk. That file *is* the live session for a cookie-authenticated app: whoever loads it
next talks to the same authenticated server-side session, not a fresh login. This is proven
by `tests/test_escalation_integration.py::test_operator_resumes_paused_session_without_fresh_login`,
which runs the handoff across three genuinely separate Playwright browser instances (not
just separate pages in one browser) to simulate a real process boundary: automation
escalates and fully closes its browser; a fresh browser loads *only* the saved
`storage_state` (no credentials, no `/login` call) and asserts it lands on the protected
member page rather than a login form; the operator performs a manual action and hands
back; a third fresh browser loads the updated state and confirms it's still authenticated.
The operator console loads that file, drives the same session, performs manual steps, then
`resolve_and_hand_back()` re-dumps `storage_state` (capturing anything the operator
changed) and flips control to `RESUMING`. Automation polls `wait_for_resolution()` and
resumes from that state.

**What's mocked:** no live pixel-streaming/co-browsing -- explicitly out of scope.
Operator "actions" in `operator_console.py` are a fixed list of `{type, ...}` dicts rather
than a live-clicked UI, so the demo is scriptable. What's *not* mocked is the hard part:
the control-transfer model, provable by a page loaded from the resumed state staying
authenticated without a fresh login. Generalizing beyond cookies: the pattern holds
("hand off whatever session artifact the surface uses") but `storage_state` itself is
Playwright/cookie-specific.

## 6. Safety

Both controls sit in `ActionExecutor.execute()`, so neither discovery nor replay can
bypass them via a different path.

**Allowlist.** `AllowlistPolicy` checks the destination host on every navigate and the
action type against a configured set, before execution -- not logged after.

**Risk gating.** Every action is `safe`/`risky` (`classify_risk()` -- a small explicit
keyword list, not a "smart" heuristic, because a static auditable rule is the right shape
for a safety control). During discovery, a risky action only executes if the LLM's stated
reasoning explicitly signals intent to perform that specific risky action -- the system
prompt requires this statement, and `risk_gate.gate(risk, confirmed=...)` mechanically
checks for it before the action is allowed to proceed. During replay, a risky step only
executes if `artifact.metadata.approval_state == "approved"`.

Verified two ways. On the replay side, `test_replay_blocks_risky_action_on_unapproved_artifact`:
a draft artifact with an "open sub-account" step is blocked; the same artifact with
`--approved` proceeds. On the discovery side, a live LLM run against the real mock app
opened a YOUTH_SAVINGS sub-account for member 12345 with a $25.00 initial deposit end to
end: the model's stated reasoning for the account-opening click explicitly gave the
required risky-action justification before the click was permitted to execute, and the
run reached the confirmation screen showing the new sub-account
(`evidence/discovery/transcript.json`, `artifacts_store/open_youth_savings_account.json`).
This gives discovery-time coverage of the gate alongside the hand-built artifact that
already covered it on the replay side.

**Redaction.** `redact()`/`redact_form_value()` sit on the log-write path in
`ActionExecutor._record()`, not something each call site has to remember. Pattern-based
for password/token/SSN/card-shaped values; field-name-based for known-sensitive fields
regardless of content. The `is_sensitive` flag on discovery's `fill` tool additionally
stops a credential's literal value from ever being recorded into the artifact at all,
rather than relying on redaction to catch it downstream.

**Limits, stated honestly:** risk classification is keyword-based and would miss risky
intent phrased unexpectedly -- a production version would want this configurable per-app.
The allowlist checks hostnames, not routes. Redaction is a backstop, not a guarantee.

## 7. Cuts

**Built and verified end-to-end, on two separate real capabilities, not just described.**
A read-only lookup (`artifacts_store/get_member_savings_balance.json`) and a risky
state-changing flow (`artifacts_store/open_youth_savings_account.json`) were both
discovered live by an LLM (Gemini, via `google-genai`) driving the real mock app, then
replayed deterministically with no model involved. The lookup artifact was replayed
against two different member IDs with correct, distinct outputs both times ($4,821.63 for
member 12345, $250.00 for member 67890); the risky artifact correctly gated its
account-opening step behind the risk-confirmation rule described in Section 6 both at
discovery time (a live LLM run) and at replay time (an automated test). 36 automated tests
back this up, including 13 integration tests driving a real headless browser across
replay, escalation/handoff, and multi-tenant scenarios, plus unit coverage of the schema,
guardrails, and distillation logic.

**Four real bugs surfaced by actually running the system, not just reading the code,
each fixed and covered by a regression test:** perception initially missed non-interactive
data cells (fixed by extending `perception.py`, see Section 1); parameter templating
initially matched by free-text description and could silently fail to parameterize a
value (fixed by matching on a stable id, see Section 2 and
`tests/test_discovery_distillation.py`); a floating LLM model alias
(`gemini-flash-latest`) silently resolved to a much stricter-quota model mid-project
(fixed by pinning an explicit model string); and `SELECT` actions initially matched
option values too literally, causing the LLM to need multiple guesses at casing/format
before one resolved (fixed by `_select_option_smart()`'s value/label/normalized matching,
see Section 3 and `tests/test_select_option_smart.py`). All four were found by running
real discovery sessions against the live app rather than by code review, which is the
main argument for why the two live discovery runs in this report matter as evidence, not
just as a demo.

**Known limitation:** distillation parameterizes each recorded `FILL`/`SELECT` step
independently, so two select actions targeting the same logical choice (an account type,
selected once and then re-confirmed before submission) can be named as two separate input
parameters with identical example values rather than recognized as the same input used
twice. The right fix is deduplicating by target locator during distillation before asking
the LLM to name parameters, not a post-hoc merge -- not done here for lack of time.

**Deliberately not built:** a real-time operator console (explicitly out of scope);
desktop-surface support (also out of scope); an agent-facing capability catalog (optional
stretch goal, skipped in favor of making the core requirements each genuinely real).

**Next, in order:** (1) route-level extension to the allowlist, (2) deduplicate
identical-target select/fill steps during distillation before parameterization, so
re-confirmed choices don't surface as redundant caller-facing inputs (see known
limitation above), (3) multi-run stability (replay N times, report a flakiness signal) --
cheapest stretch goal given the replay engine already returns a structured result per run.