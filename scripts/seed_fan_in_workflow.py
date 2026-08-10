#!/usr/bin/env python3
"""Seed the workflow definition that makes a research run finish unattended.

This plugin fans out two research agents under one causation chain. Nothing in
the plugin then watches them — the orchestration engine does, via an
``agent_fan_in`` action (biffo-template#657) that fires the research-synthesis
agent once both research runs in the chain are terminal.

Modelled on ``biffo-plugin-idea-scout``'s ``scripts/seed_fan_in_workflow.py``,
with one deliberate difference: idea-scout omits ``instructions``/``model``
from ``action_config`` so Core resolves them from the plugin's admin-editable
config at agent-run creation time. This plugin has not built that
admin-editable-config layer (out of scope for M3 — the research/positioning
prompts are fixed constants in ``definitions.py``, not yet admin-editable), so
``action_config`` carries ``instructions``, ``model`` **and** ``output_tools``
directly. Nothing in Core resolves ``output_tools`` from a registry at all
(only ``instructions``/``model`` have that fallback,
``services/api/src/api/routers/internal_agents.py``), so a fan-in-fired run
with no ``output_tools`` in its config would have no structured-output tool to
call regardless of the registry question — carrying it explicitly here is
correct however the prompt/model question is eventually resolved.

**Without this definition, a research run never leaves ``pending``.** The two
research agents still run and still bill; nothing reconciles them into a
``research`` artefact. That is the whole failure mode this script exists to
prevent, and it is silent — which is why it is called out here rather than
left to a deploy runbook.

Run this ONCE per environment, by an operator with real Cognito admin
credentials, after the plugin is installed. It is idempotent: an existing
definition with the same name is left alone unless ``--replace`` is passed.

Usage:
    CORE_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com \
    ADMIN_BEARER_TOKEN=<a real Cognito admin id/access token> \
    python scripts/seed_fan_in_workflow.py [--dry-run] [--replace]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# Importable without the package installed, so an operator can run this from a
# checkout without a `uv sync` first.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from marketing.definitions import (  # noqa: E402
    DEFAULT_SYNTHESIS_MODEL,
    RESEARCH_AGENT_NAMES,
    RESEARCH_SYNTHESIS_AGENT_NAME,
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    research_synthesis_definition,
    research_synthesis_tool_schema,
)

WORKFLOW_NAME = "Marketing — synthesise research once both angles complete"

_DEFINITIONS_PATH = "/api/v1/admin/orchestration/workflows"


def definition(*, synthesis_model: str = DEFAULT_SYNTHESIS_MODEL) -> dict:
    """The workflow this plugin needs in order to finish a research run on its
    own.

    Triggered by every ``agent.run.completed``: the fan-in action itself
    decides whether the event belongs to a chain it cares about, and no-ops
    otherwise. Filtering by agent name in the trigger would still fire twice
    per research run (once per angle); the action's own
    all-siblings-terminal check is what collapses those two into one.
    """
    run_definition = research_synthesis_definition(
        model=synthesis_model, instructions=RESEARCH_SYNTHESIS_INSTRUCTIONS
    )
    return {
        "name": WORKFLOW_NAME,
        "trigger_source": "biffo.core",
        "trigger_detail_type": "agent.run.completed",
        "action_type": "agent_fan_in",
        "action_config": {
            # The set to wait for. These names must match what the plugin
            # actually requests — see marketing.pipeline.start_research.
            "expect_agents": ",".join(RESEARCH_AGENT_NAMES),
            "agent_name": RESEARCH_SYNTHESIS_AGENT_NAME,
            **run_definition,
            "output_tools": [research_synthesis_tool_schema()],
        },
        "enabled": True,
    }


def _request(method: str, url: str, token: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read() or b"null")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing definition of the same name",
    )
    args = parser.parse_args()

    payload = definition()

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    api = os.environ.get("CORE_API_URL", "").rstrip("/")
    token = os.environ.get("ADMIN_BEARER_TOKEN", "")
    if not api or not token:
        print("CORE_API_URL and ADMIN_BEARER_TOKEN are both required.", file=sys.stderr)
        return 2

    url = f"{api}{_DEFINITIONS_PATH}"
    try:
        existing = _request("GET", url, token) or []
    except urllib.error.HTTPError as exc:
        print(f"Could not list workflows: {exc.code} {exc.reason}", file=sys.stderr)
        return 1

    match = next((w for w in existing if w.get("name") == WORKFLOW_NAME), None)
    if match and not args.replace:
        print(f"Already seeded (id {match.get('id')}). Pass --replace to overwrite.")
        return 0

    try:
        if match:
            _request("PUT", f"{url}/{match['id']}", token, payload)
            print(f"Replaced workflow {match['id']}.")
        else:
            created = _request("POST", url, token, payload)
            print(f"Created workflow {(created or {}).get('id')}.")
    except urllib.error.HTTPError as exc:
        print(f"Failed: {exc.code} {exc.reason} — {exc.read()[:400]!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
