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
`InputParam`/`OutputField`.

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

**A real bug I hit and fixed, because it's illustrative of the actual hard part:** my
first version retried only the step where failure was *detected* (e.g. an `EXTRACT`
reading a balance). Wrong -- if the real cause was a failed form submission two steps
earlier, retrying just the read doesn't help, since the page reload already cleared the
form. `_rewind_target_for_recovery()` now walks back to the nearest state-changing action
and further back over any `FILL`/`SELECT` steps feeding it, so recovery redoes the whole
input-and-submit block. Verified by
`test_replay_recovers_from_injected_transient_failure`, which injects a one-shot "system
busy" response and confirms replay recovers with the correct extracted value.

**Concrete recovery policies implemented (bounded, not open-ended):** `SESSION_TIMEOUT` ->
re-authenticate once, retry the rewound block. `SYSTEM_BUSY` -> wait 1.5s, retry once. Both
are hardcoded and auditable, not an LLM call -- adding an LLM escape hatch to replay would
undermine the "no model in the decision loop" property Section 3.3 is built around.

## 4. Heterogeneity & multi-tenant

*(Design only -- not built, per the brief's scope note.)*

**Surface abstraction.** The seam is already drawn at perception/executor: everything
above only depends on `Observation`/`TargetElement`/`Locator`, never Playwright directly.
The mock target is already a frameset app, so that case is covered. For a desktop app,
`observe()`/`resolve_target()` would be swapped for a real accessibility-API client (UIA/
AT-SPI) returning the same shapes; `ARIA_ROLE_NAME`/`TEST_ID` generalize directly to
desktop roles/automation-ids. The artifact schema itself wouldn't change.

**Multi-tenant reuse.** `ArtifactMetadata.tenant_scope` (`"base"` or a tenant id) is
already in the schema. The intended model: a `base` artifact from one tenant's discovery
run is tried first for every tenant on the same vendor product. The design for
specialization (not built): a tenant-scoped override layer that replaces individual
`Step.action.target` locators -- not whole steps -- for a given tenant, so the flow shape,
checkpoints, and outcome taxonomy stay shared and only the handful of locators that
actually differ get overridden.

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
reasoning explicitly signals intent. During replay, only if
`artifact.metadata.approval_state == "approved"`. Verified by
`test_replay_blocks_risky_action_on_unapproved_artifact`: a draft artifact with an "open
sub-account" step is blocked; the same artifact with `--approved` proceeds.

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

**Built and verified end-to-end**, including a genuine LLM-driven discovery run (Gemini
2.x, via `google-genai`) against the live mock app, not just a description of one: 28
automated tests, including 8 integration tests driving a real headless browser and one
regression suite locking in a discovery-time bug fix (below). The discovered artifact
(`artifacts_store/get_member_savings_balance.json`) was replayed deterministically against
two different member IDs with correct, distinct outputs both times ($4,821.63 for member
12345, $250.00 for member 67890) and no LLM involved in either replay.

**Two real bugs the discovery run surfaced, both fixed and covered by tests:**
1. *Perception only exposed interactive elements.* The first live run correctly filled
   and submitted the search form, but couldn't extract the balance -- it's rendered in a
   plain `<td>`, not a button/input/link, so it was invisible to the perception layer. The
   model recovered by reading the value out of raw page text and reporting it in prose,
   but that value never became a structured output. Fixed by extending `perception.py` to
   also surface id-bearing table cells as labeled, extractable elements.
2. *Parameter templating matched on free-text, and silently failed.* The distillation
   step asked the LLM to echo back which step it typed a value into, matched by comparing
   description strings verbatim. In the real run the LLM's response didn't reproduce that
   text exactly, the match silently failed, and the resulting artifact had member 12345's
   ID hardcoded into what was supposed to be a reusable capability -- replaying it against
   a different member still returned the original member's balance. Fixed by matching on a
   stable integer id instead of free text; `tests/test_discovery_distillation.py` locks
   this in with mocked LLM responses, including one that deliberately paraphrases to prove
   the match no longer depends on exact wording.

**Deliberately not built:** a tenant-override resolution mechanism in `replay.py` (no
second real tenant's UI to validate it against -- the design is stated in Section 4, but
building a mechanism I can't exercise seemed worse than describing it clearly); a
real-time operator console (explicitly out of scope); desktop-surface support (also out of
scope); an agent-facing capability catalog (optional stretch goal, skipped in favor of
making the five core requirements each genuinely real).

**Next, in order:** (1) route-level extension to the allowlist, (2) a concrete
tenant-override mechanism validated against a second, deliberately-varied instance of the
mock app, (3) multi-run stability (replay N times, report a flakiness signal) -- cheapest
stretch goal given the replay engine already returns a structured result per run, (4) a
second discovery goal exercising the risky-action path (opening a sub-account) end-to-end
with the LLM, to get discovery-time coverage of the risk-confirmation gate alongside the
hand-built artifact that already covers it on the replay side.