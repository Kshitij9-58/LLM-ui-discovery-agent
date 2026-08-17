# LLM UI Discovery Agent: Computer-Use Automation System

## What's here

Built and verified end-to-end, including a genuine LLM-driven discovery run
(28 automated tests passing, 8 of them driving a real headless browser
against a live running instance of the mock target app):

- `mock_bank/app.py` — the target application: a deliberately old-school,
  frameset/table-based legacy banking console (member search → detail →
  open sub-account → confirmation), with injectable runtime failures.
- `agent/schema.py` — the capability artifact contract (Section 3.2).
- `agent/guardrails.py` — allowlist + risk policy + redaction (Section 3.4).
- `agent/perception.py` — accessibility-tree-based page observation.
- `agent/executor.py` — locator resolution (primary + fallback) and action
  execution, shared by discovery and replay.
- `agent/discovery.py` — the LLM-driven observe→decide→act loop (Gemini,
  via `google-genai`) and the artifact-distillation step. Verified with a
  real discovery run; evidence in `evidence/discovery/`.
- `agent/replay.py` — the deterministic replay engine (Section 3.3), including
  the business-outcome / recoverable / hard-failure classification.
- `agent/escalation.py`, `agent/operator_console.py` — the human handoff
  mechanism (Section 3.6), verified end-to-end: a paused session resumes in a
  genuinely separate browser instance with no fresh login required.
- `main.py` — CLI entry point wiring the above together.
- `tests/` — unit tests for guardrails/schema, integration tests for replay
  and escalation/handoff against the live mock app, and a regression suite
  for a discovery-time parameterization bug found and fixed during the real
  run (see REPORT.md section 7).

A real discovery run against member 12345 produced
`artifacts_store/get_member_savings_balance.json`, which has since been
replayed deterministically against two different member IDs (12345 and
67890) with correct, distinct outputs both times and no LLM involved in
either replay.

## Setup

```bash
# 1. clone and enter the repo
git clone <repo-url>
cd interfaceai-takehome

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

In one terminal, start the mock bank app:

```bash
python3 mock_bank/app.py
# serves on http://127.0.0.1:5055
```

In a second terminal, with the venv activated:

```bash
pytest tests/ -v
```

This runs all 28 tests: unit tests needing no server, replay integration
tests, escalation/handoff integration tests, and the discovery-distillation
regression tests, driving a real headless Chromium browser against the
running mock app where needed. The escalation tests run the pause →
hand-off → resume cycle across three genuinely separate browser instances to
prove a paused session resumes without a fresh login.

## Demo path: replay a saved artifact by hand

A hand-built example artifact is checked in at
`artifacts_store/lookup_member_balance.json` so you can see the replay engine
work without needing the LLM step. With the mock app running:

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

python3 main.py discover \
  "Look up member 12345 and read their current savings balance" \
  --entry-url http://127.0.0.1:5055/search
```

This runs the LLM against the live app, writes a transcript to
`evidence/discovery/`, and — if the goal is reached — writes a new
`CapabilityArtifact` to `artifacts_store/`. That artifact can then be
replayed the same way as the hand-built example above, including against
member IDs the discovery run never saw.

A note on Gemini API availability: newly-created API keys can get routed to
different model tiers than expected, and some hosted models return transient
`503 UNAVAILABLE` ("high demand") errors. `agent/discovery.py` retries these
automatically with exponential backoff. If `MODEL` in `agent/discovery.py`
ever needs to change, `client.models.list()` (see the `google-genai` docs)
will show exactly which models a given key can call.

## Project status / what's left

See `REPORT.md` for the full design write-up, including two real bugs found
and fixed via the live discovery run (Section 7: Cuts). Remaining work, in
priority order: a route-level allowlist extension, a concrete tenant-override
mechanism, and multi-run replay stability reporting.