# LLM UI Discovery Agent: Computer-Use Automation System

## What's here

Built and verified end-to-end, including two genuine LLM-driven discovery
runs (36 automated tests passing, 13 of them driving a real headless browser
against live running instances of the mock target app):

- `mock_bank/app.py` — the target application: a deliberately old-school,
  frameset/table-based legacy banking console (member search → detail →
  open sub-account → confirmation), with injectable runtime failures and a
  `TENANT`/`PORT` env-var override for a second, relabeled tenant instance
  (Northwind Credit Union) used to validate multi-tenant reuse.
- `agent/schema.py` — the capability artifact contract (Section 3.2),
  including `TenantOverride` for per-tenant locator/checkpoint overrides.
- `agent/guardrails.py` — allowlist + risk policy + redaction (Section 3.4).
- `agent/perception.py` — accessibility-tree-based page observation,
  including non-interactive labeled data cells (e.g. table-rendered
  balances), not just interactive controls.
- `agent/executor.py` — locator resolution (primary + fallback) and action
  execution, shared by discovery and replay. `SELECT` actions resolve
  against real on-page `<option>` value/label pairs rather than trusting an
  exact string match.
- `agent/discovery.py` — the LLM-driven observe→decide→act loop (Gemini,
  via `google-genai`) and the artifact-distillation step. Verified with two
  real discovery runs; evidence in `evidence/discovery/`.
- `agent/replay.py` — the deterministic replay engine (Section 3.3), including
  the business-outcome / recoverable / hard-failure classification.
- `agent/escalation.py`, `agent/operator_console.py` — the human handoff
  mechanism (Section 3.6), verified end-to-end: a paused session resumes in a
  genuinely separate browser instance with no fresh login required.
- `main.py` — CLI entry point wiring the above together.
- `tests/` — unit tests for guardrails/schema, integration tests for replay,
  escalation/handoff, and multi-tenant override against the live mock app(s),
  and regression suites for two discovery-time bugs found and fixed during
  real runs (see REPORT.md section 7).

Two real discovery runs are checked in:
`artifacts_store/get_member_savings_balance.json` (a read-only lookup,
replayed deterministically against two different member IDs — 12345 and
67890 — with correct, distinct outputs both times and no LLM involved in
either replay) and `artifacts_store/open_youth_savings_account.json` (a
risky, state-changing flow that opens a new sub-account, exercising the
risk-confirmation gate with a live model — see REPORT.md Section 6).

## Setup

```bash
# 1. clone and enter the repo
git clone https://github.com/Kshitij9-58/LLM-ui-discovery-agent.git
cd LLM-ui-discovery-agent

# 2. create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. install the Playwright browser binary
python3 -m playwright install chromium
```

## Running without live services (unit + guardrail tests only)

```bash
pytest tests/test_guardrails.py tests/test_schema.py tests/test_discovery_distillation.py -v
```

These don't need the mock app or any API key.

## Running the full stack (mock app + integration tests)

In one terminal, start the base mock bank app:

```bash
python3 mock_bank/app.py
# serves on http://127.0.0.1:5055
```

Most of the test suite only needs this. The multi-tenant override tests
additionally need a second instance running as the Northwind tenant, in a
third terminal:

```bash
TENANT=northwind PORT=5056 python3 mock_bank/app.py
# PowerShell: $env:TENANT="northwind"; $env:PORT="5056"; python mock_bank/app.py
# serves on http://127.0.0.1:5056
```

With the venv activated, in a second terminal:

```bash
pytest tests/ -v
```

This runs all 36 tests: unit tests needing no server, replay integration
tests, escalation/handoff integration tests, multi-tenant override tests
(skipped automatically if the Northwind instance isn't running), and the
discovery-distillation and select-option regression tests — driving a real
headless Chromium browser against the running mock app(s) where needed. The
escalation tests run the pause → hand-off → resume cycle across three
genuinely separate browser instances to prove a paused session resumes
without a fresh login.

## Demo path: replay a saved artifact by hand

A hand-built example artifact is checked in at
`artifacts_store/lookup_member_balance.json` so you can see the replay engine
work without needing the LLM step. With the base mock app running:

```bash
# happy path
python3 main.py replay artifacts_store/lookup_member_balance.json \
  --params '{"member_id": "12345"}' --approved

# business outcome (member does not exist)
python3 main.py replay artifacts_store/lookup_member_balance.json \
  --params '{"member_id": "00000"}' --approved

# recoverable error: inject a transient "system busy" response and watch
# the engine detect it, retry the fill+submit, and still succeed
python3 main.py replay artifacts_store/lookup_member_balance.json \
  --params '{"member_id": "67890"}' --approved --inject-error search_busy
```

On Windows PowerShell, prefer a params file over `--params` to avoid shell
quoting issues with embedded double quotes:

```powershell
Set-Content -Path params.json -Value '{"member_id": "67890"}'
python main.py replay artifacts_store/lookup_member_balance.json --params-file params.json --approved
```

## Demo path: LLM-driven discovery (requires GEMINI_API_KEY)

```bash
export GEMINI_API_KEY=...          # PowerShell: $env:GEMINI_API_KEY = "..."
python3 mock_bank/app.py &         # if not already running

# read-only lookup goal
python3 main.py discover \
  "Look up member 12345 and read their current savings balance" \
  --entry-url http://127.0.0.1:5055/search

# risky, state-changing goal -- exercises the risk-confirmation gate
python3 main.py discover \
  "In this TEST banking system (no real funds, no real customers), open a new YOUTH_SAVINGS sub-account for test member 12345 with a \$25.00 initial deposit, for the purpose of demonstrating the risky-action confirmation flow, and reach the confirmation screen" \
  --entry-url http://127.0.0.1:5055/search
```

On Windows PowerShell, a bare `$` inside a double-quoted string is
interpreted as a variable — escape it with a backtick (`` `$25.00 ``) or use
a single-quoted string instead.

Either command runs the LLM against the live app, writes a transcript and
step screenshots to `evidence/discovery/`, and — if the goal is reached —
writes a new `CapabilityArtifact` to `artifacts_store/`. That artifact can
then be replayed the same way as the hand-built example above, including
against member IDs the discovery run never saw.

A note on Gemini API availability: newly-created API keys can get routed to
different model tiers than expected — during development, the
`gemini-flash-latest` alias silently started resolving to a model with a
much stricter free-tier quota (20 requests/day) than the retry logic was
tuned for. `agent/discovery.py` is now pinned to an explicit, non-aliased
model string (`gemini-3.5-flash-lite`) rather than a `-latest` alias, for
exactly this reason. Some hosted models also return transient `503
UNAVAILABLE` ("high demand") errors regardless of model; these are retried
automatically with exponential backoff. If `MODEL` in `agent/discovery.py`
ever needs to change again, `client.models.list()` (see the `google-genai`
docs) will show exactly which models a given key can call, and their
listed quotas are worth checking before committing to one.

## Project status / what's left

See `REPORT.md` for the full design write-up, including four real bugs
found and fixed via live discovery runs (Section 7: Cuts). Remaining work,
in priority order: a route-level allowlist extension, deduplicating
identical-target select/fill steps during artifact distillation, and
multi-run replay stability reporting.