# interface.ai Take-Home: Computer-Use Automation System

## What's actually here right now

Built and verified working (22 automated tests passing against a live running
instance of the mock target app):

- `mock_bank/app.py` — the target application: a deliberately old-school,
  frameset/table-based legacy banking console (member search → detail →
  open sub-account → confirmation), with injectable runtime failures.
- `agent/schema.py` — the capability artifact contract (Section 3.2).
- `agent/guardrails.py` — allowlist + risk policy + redaction (Section 3.4).
- `agent/perception.py` — accessibility-tree-based page observation.
- `agent/executor.py` — locator resolution (primary + fallback) and action
  execution, shared by discovery and replay.
- `agent/replay.py` — the deterministic replay engine (Section 3.3), including
  the business-outcome / recoverable / hard-failure classification.
- `agent/escalation.py`, `agent/operator_console.py` — the human handoff
  mechanism (Section 3.6), built on Playwright's `storage_state` so a paused
  session can genuinely be picked up by an operator process.
- `main.py` — CLI entry point wiring the above together.
- `tests/` — unit tests for guardrails/schema, plus integration tests for
  replay that run against the live mock app.

**Not yet run**: `agent/discovery.py`, the LLM-driven observe→decide→act loop
(Section 3.1) and the artifact-distillation step (Section 3.2). It's written
and imports without errors, but it has never been executed against a real
model, so it should be treated as unverified until a real discovery run is
performed and checked into `/evidence/discovery/`. That's the next step and
requires an `ANTHROPIC_API_KEY`.

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
pytest tests/test_guardrails.py tests/test_schema.py -v
```

These don't need the mock app or any API key.

## Running the full stack (mock app + replay integration tests)

In one terminal, start the mock bank app:

```bash
python3 mock_bank/app.py
# serves on http://127.0.0.1:5055
```

In a second terminal, with the venv activated:

```bash
pytest tests/ -v
```

This runs all 22 tests, including 5 integration tests that drive a real
headless Chromium browser against the running mock app: happy-path replay,
a business-outcome case (member not found), a recoverable-error case
(injected transient failure that the engine detects and retries), a missing-
parameter hard failure, and a guardrail block on an unapproved risky action.

## Demo path: replay a saved artifact by hand

A hand-built example artifact is checked in at
`artifacts_store/lookup_member_balance.json` so you can see the replay engine
work without needing the LLM step first. With the mock app running:

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

## Demo path: LLM-driven discovery (requires ANTHROPIC_API_KEY)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 mock_bank/app.py &   # if not already running

python3 main.py discover \
  "Look up member 12345 and read their current savings balance" \
  --entry-url http://127.0.0.1:5055/search
```

This runs the LLM against the live app, writes a transcript to
`evidence/discovery/`, and — if the goal is reached — writes a new
`CapabilityArtifact` to `artifacts_store/`. That artifact can then be replayed
the same way as the hand-built example above.

## Project status / what's left

See `REPORT.md` (not yet written) for the full design write-up. Immediate
next steps: run a real discovery session, capture evidence, replay the
resulting artifact including one error case, and write up the seven-section
report the brief requires.
