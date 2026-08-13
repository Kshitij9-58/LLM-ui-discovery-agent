"""
CLI entry point.

Usage:
  python3 main.py discover "GOAL TEXT" --entry-url URL [--artifact-out PATH]
  python3 main.py replay ARTIFACT_PATH --params '{"member_id": "12345"}'
  python3 main.py replay ARTIFACT_PATH --params '{"member_id": "12345"}' --inject-error search_busy
  python3 main.py show-intervention INTERVENTION_ID
  python3 main.py resolve-intervention INTERVENTION_ID

See README.md for the full walkthrough including how authentication is handled
before discovery/replay (both assume an authenticated session -- see --login flag).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from playwright.sync_api import sync_playwright

from agent.schema import CapabilityArtifact
from agent.discovery import DiscoveryAgent
from agent.replay import ReplayEngine, ReplayContext
from agent.guardrails import AllowlistPolicy
from agent.escalation import InterventionStore

DEFAULT_ALLOWED_HOSTS = ["127.0.0.1"]
DEFAULT_LOGIN_URL = "http://127.0.0.1:5055/login"
DEFAULT_USERNAME = "operator1"
DEFAULT_PASSWORD = "test-password"


def _authenticated_context(playwright, storage_state_path: str | None = None):
    browser = playwright.chromium.launch(headless=True)
    if storage_state_path and os.path.exists(storage_state_path):
        context = browser.new_context(storage_state=storage_state_path)
    else:
        context = browser.new_context()
    return browser, context


def _login(page, login_url=DEFAULT_LOGIN_URL, username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD):
    page.goto(login_url, timeout=10000)
    page.fill("#fld_username", username)
    page.fill("#fld_password", password)
    page.click("input[type=submit]")


def cmd_discover(args):
    os.makedirs("evidence/discovery", exist_ok=True)
    os.makedirs("artifacts_store", exist_ok=True)

    with sync_playwright() as p:
        browser, context = _authenticated_context(p)
        page = context.new_page()
        _login(page)

        allowlist = AllowlistPolicy(allowed_hosts=DEFAULT_ALLOWED_HOSTS)
        agent = DiscoveryAgent(page, allowlist)

        print(f"Running discovery for goal: {args.goal!r}")
        result = agent.run(args.goal, args.entry_url, screenshot_dir="evidence/discovery")

        # write full transcript regardless of outcome, for evidence
        transcript_path = "evidence/discovery/transcript.json"
        with open(transcript_path, "w") as f:
            json.dump(
                [
                    {
                        "step": t.step, "reasoning": t.llm_reasoning, "tool": t.tool_name,
                        "tool_input": t.tool_input, "execution_detail": t.execution_detail,
                        "success": t.success,
                    }
                    for t in result.transcript
                ],
                f, indent=2,
            )
        print(f"Transcript written to {transcript_path}")
        print(f"Status: {result.status}")

        if result.status == "success" and result.artifact:
            out_path = args.artifact_out or f"artifacts_store/{result.artifact.metadata.name}.json"
            with open(out_path, "w") as f:
                f.write(result.artifact.model_dump_json_pretty())
            print(f"Artifact written to {out_path}")
            print(f"Outputs captured during discovery: {result.outputs}")
        elif result.status == "business_outcome":
            print(f"Discovery ended in a business outcome: {result.outcome_name}")
        elif result.status == "escalated":
            print("Discovery escalated to a human -- see transcript for the reason.")
        else:
            print("Discovery hit max steps without completing (hard_failure).")

        browser.close()


def cmd_replay(args):
    with open(args.artifact_path) as f:
        artifact = CapabilityArtifact.model_validate_json(f.read())

    params = json.loads(args.params) if args.params else {}

    if args.inject_error == "search_busy":
        requests.post("http://127.0.0.1:5055/admin/inject/search_busy")
        print("Injected: next member search will return SYSTEM BUSY.")

    os.makedirs("evidence/replay", exist_ok=True)

    with sync_playwright() as p:
        browser, context = _authenticated_context(p)
        page = context.new_page()
        _login(page)

        allowlist = AllowlistPolicy(allowed_hosts=DEFAULT_ALLOWED_HOSTS)
        recovery_ctx = ReplayContext(
            login_url=DEFAULT_LOGIN_URL, login_username=DEFAULT_USERNAME, login_password=DEFAULT_PASSWORD,
        )
        engine = ReplayEngine(
            page, allowlist, artifact_approved=args.approved,
            evidence_dir="evidence/replay", recovery_context=recovery_ctx,
        )

        print(f"Replaying artifact '{artifact.metadata.name}' v{artifact.metadata.version} with params={params}")
        result = engine.run(artifact, params)

        result_path = f"evidence/replay/replay_result_{int(time.time())}.json"
        with open(result_path, "w") as f:
            f.write(result.model_dump_json(indent=2))

        print(f"Status: {result.status}")
        if result.status == "success":
            print(f"Outputs: {result.outputs}")
        elif result.status == "business_outcome":
            print(f"Business outcome: {result.outcome_name}")
        elif result.status == "hard_failure":
            print(f"Hard failure at step {result.failed_step_index}: {result.error_detail}")
        print(f"Result written to {result_path}")

        browser.close()


def cmd_show_intervention(args):
    from agent.operator_console import show_intervention
    store = InterventionStore("evidence/interventions")
    show_intervention(store, args.intervention_id)


def cmd_resolve_intervention(args):
    from agent.operator_console import operator_take_control_and_resolve
    store = InterventionStore("evidence/interventions")
    actions = json.loads(args.actions) if args.actions else []
    operator_take_control_and_resolve(store, args.intervention_id, actions)


def main():
    parser = argparse.ArgumentParser(description="interface.ai take-home: computer-use automation system")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run an LLM-driven discovery run for a goal")
    p_discover.add_argument("goal")
    p_discover.add_argument("--entry-url", default="http://127.0.0.1:5055/search")
    p_discover.add_argument("--artifact-out", default=None)
    p_discover.set_defaults(func=cmd_discover)

    p_replay = sub.add_parser("replay", help="Deterministically replay a saved artifact")
    p_replay.add_argument("artifact_path")
    p_replay.add_argument("--params", default="{}")
    p_replay.add_argument("--approved", action="store_true", help="Treat artifact as approved (allows risky steps to run unattended)")
    p_replay.add_argument("--inject-error", choices=["search_busy"], default=None)
    p_replay.set_defaults(func=cmd_replay)

    p_show = sub.add_parser("show-intervention")
    p_show.add_argument("intervention_id")
    p_show.set_defaults(func=cmd_show_intervention)

    p_resolve = sub.add_parser("resolve-intervention")
    p_resolve.add_argument("intervention_id")
    p_resolve.add_argument("--actions", default="[]", help="JSON list of operator actions, e.g. '[{\"type\":\"click\",\"selector\":\"a\"}]'")
    p_resolve.set_defaults(func=cmd_resolve_intervention)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
