"""
Operator Console (minimal, mocked UI per brief Section 3.6 scope note)
==========================================================================
A real operator console would be a rich real-time co-browsing UI -- explicitly
out of scope. What this provides instead, which the brief asks to be real:

  - A page showing the intervention's context (goal, step, reason, screenshot).
  - A "Take Control" action that loads the PAUSED session's storage_state into a
    fresh Playwright page the operator drives directly (same cookies = same
    server-side session as the automation was using -- this is the actual
    hand-off, not a re-login).
  - A way for the operator to perform the needed manual step(s) and mark the
    intervention resolved, snapshotting the session state again so automation
    can resume with whatever the operator changed.

This is intentionally a CLI-driven "console" rather than a polished web UI --
see REPORT.md section 5 for why. It is real: it does load the actual paused
Playwright session, does drive a real page, and does capture what the operator
did, headlessly-navigable but demonstrable end-to-end.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from agent.escalation import InterventionStore, ControlState


def show_intervention(store: InterventionStore, intervention_id: str) -> None:
    req = store.load(intervention_id)
    if not req:
        print(f"No intervention found with id {intervention_id}")
        return
    print("=" * 70)
    print(f"INTERVENTION {req.intervention_id}  [{req.control_state.value}]")
    print(f"Goal/capability : {req.capability_or_goal}")
    print(f"Stopped at step : {req.step_index}")
    print(f"Current URL     : {req.current_url}")
    print(f"Reason          : {req.reason}")
    print(f"Screenshot      : {req.screenshot_path}")
    print(f"Session state   : {req.session_state_path}")
    print("Operator log:")
    for line in req.operator_actions_log:
        print(f"  {line}")
    print("=" * 70)


def operator_take_control_and_resolve(
    store: InterventionStore,
    intervention_id: str,
    manual_actions: list[dict],
    operator_name: str = "operator1",
) -> None:
    """
    Loads the paused session, applies a list of manual actions the operator
    performs (represented here as simple {type, ...} dicts to keep this
    demonstrable non-interactively), then hands control back.

    manual_actions items look like:
      {"type": "goto", "url": "..."}
      {"type": "fill", "selector": "#fld_username", "value": "..."}
      {"type": "click", "selector": "input[type=submit]"}
    This stands in for whatever a real operator does by hand in a real console.
    """
    req = store.take_control(intervention_id, operator_name=operator_name)
    print(f"Operator '{operator_name}' has taken control of intervention {intervention_id}.")
    print(f"Loading the SAME live session from {req.session_state_path} (not a fresh login) ...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=req.session_state_path)
        page = context.new_page()
        page.goto(req.current_url)

        for act in manual_actions:
            if act["type"] == "goto":
                page.goto(act["url"])
                store.record_operator_action(intervention_id, f"navigated to {act['url']}")
            elif act["type"] == "fill":
                page.fill(act["selector"], act["value"])
                store.record_operator_action(intervention_id, f"filled {act['selector']}")
            elif act["type"] == "click":
                page.click(act["selector"])
                store.record_operator_action(intervention_id, f"clicked {act['selector']}")
            elif act["type"] == "wait":
                page.wait_for_timeout(act.get("ms", 500))

        print(f"Operator finished manual steps. Landed at: {page.url}")
        store.resolve_and_hand_back(intervention_id, context)
        browser.close()

    print(f"Control handed back to automation for intervention {intervention_id}.")


if __name__ == "__main__":
    import argparse
    from agent.escalation import InterventionStore

    parser = argparse.ArgumentParser(description="Minimal operator console CLI")
    parser.add_argument("--evidence-dir", default="evidence/interventions")
    parser.add_argument("intervention_id")
    args = parser.parse_args()

    store = InterventionStore(args.evidence_dir)
    show_intervention(store, args.intervention_id)
