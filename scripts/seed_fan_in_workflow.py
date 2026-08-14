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
    python scripts/seed_fan_in_workflow.py [--dry-run] [--replace] [--check]

**What this script writes is a SNAPSHOT, and a snapshot goes stale (#160).**

Everything in ``action_config`` below is a *copy*, taken from this checkout at
the moment the script runs and read back by the engine months later. Two days
after #108 moved every stage to the Claude 5 family and #131 gave every agent
a 240s wall clock, the deployed synthesis stage was still running
``anthropic/claude-opus-4.8`` on 120s, because nobody re-ran this. #62's
implementer predicted exactly that, in writing, for the **model** — and the
timeout went unnoticed anyway, because the lesson was recorded as "the model
is stale" rather than "**the whole snapshot is stale**".

**Why the config cannot simply resolve at run time**, which is the fix anyone
would reach for first. Measured against Core (``tabsii-platform`` @ 2026-08-14),
not assumed:

- **``instructions``/``model`` can be omitted** — ``services/api/src/api/
  routers/internal_agents.py`` resolves each from the ``plugin_chat_agents``
  registry row for the agent when the snapshot has none. But this plugin
  registers **no** such row (nothing in ``biffo.plugin.json`` or anywhere else
  creates one), and the fallbacks are hostile: a missing ``instructions`` with
  no registry row is a **422 refusal** at run-creation time, and a missing
  ``model`` never raises at all — it is silently filled from
  ``settings.agent_default_model``, which on Core today is
  ``moonshotai/kimi-k3``. Omitting the model would not resolve this plugin's
  choice at run time; it would swap Opus 5 for a different vendor's model,
  quietly, in the reconciling stage.
- **``max_turns``/``timeout_seconds`` cannot be omitted at all.** Nothing
  anywhere resolves them. ``agent_runtime.loop.RunLimits.from_snapshot`` reads
  both straight from the snapshot and substitutes its own defaults
  (``DEFAULT_TIMEOUT_SECONDS`` = 120.0) for whatever is absent — that
  substitution *is* the 120s clock #160 measured. The registry row has a
  ``timeout_seconds`` column, and ``internal_agents.py`` never copies it into
  the snapshot, so even a registered agent would not move it.
- Registering an agent row would therefore **relocate the frozen copy** into
  another admin-editable record seeded by another script, not remove it, and
  would still leave the limits frozen here.

So the snapshot stays, and what changes instead is that its staleness is no
longer silent, in three places that each read a different document:

1. :func:`config_fingerprint` / :data:`SEEDED_CONFIG_FINGERPRINT` — a pin over
   the whole ``action_config`` this checkout would seed, asserted by
   ``tests/test_marketing_seed_fan_in_workflow.py``. Changing a model, a
   prompt, a turn budget or a wall clock turns CI **red at the point of the
   change**, naming the re-seed command, instead of staying green for two
   days. It reads this repo, so it proves nothing about any deployment —
   it proves that nobody changed the seeded config without acknowledging it.
2. ``--check`` — reads the **deployed** definition through Core's API and
   diffs it against what this checkout would seed. This is the only check that
   can actually answer "is dev stale?", and it needs an admin token, so it is
   an operator/ops command rather than a CI gate.
3. ``marketing.pipeline.synthesis_config_drift`` — reads the
   ``definition_snapshot`` of the synthesis run that **actually ran** and
   reports drift on every terminal research chain. No token, no operator: it
   fires by itself, in the environment where it is wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# Resolve `marketing.*` from the checkout rather than an installed package.
#
# This used to claim the script ran without a `uv sync` first. It does not, and
# never did: `marketing.definitions` imports `marketing/__init__.py`, which
# imports `plugin.py`, which imports `aws_lambda_powertools`. A bare `python3`
# run dies on that import before reaching any of this file's own logic. Run it
# with `uv run python scripts/seed_fan_in_workflow.py` from the checkout.
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

#: Core mounts the orchestration router at `/api/v1/orchestration`, NOT under
#: `/api/v1/admin`. This carried a stray `admin/` segment until 2026-08-11,
#: which meant the script 404'd on every run in every environment and had
#: therefore never once seeded a definition. Verified against the deployed
#: `openapi.json` rather than read from source — with a valid admin token,
#: `/api/v1/orchestration/workflows` answers 200 and the `admin/` variant
#: answers 404, indistinguishable from a route that does not exist.
_DEFINITIONS_PATH = "/api/v1/orchestration/workflows"


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


#: A value that can never be compared, because Core masks it on read
#: (``schemas/orchestration.py``'s ``redact_secrets``, where it is called
#: ``SECRET_SENTINEL``): a credential-bearing config field comes back as this
#: placeholder rather than its stored value. ``agent_fan_in`` declares no such
#: field today, so this is a guard against a future one being reported as
#: permanent, unfixable drift rather than as "cannot tell".
REDACTED_SENTINEL = "••••••••"


def config_fingerprint(config: dict[str, Any]) -> str:
    """A stable digest of the whole ``action_config`` this checkout would seed.

    **The whole config, not the model.** #62 recorded its lesson as "synthesis
    is stuck on an old model", so #131's timeout change sailed past it two days
    later on the same mechanism. Every key here is a copy that a deploy does
    not update — the prompt, the tool schema, the turn budget and the wall
    clock exactly as much as the model — so the thing that gets pinned is the
    snapshot, and a change to any part of it has to be acknowledged.

    Truncated to 16 hex characters: long enough that a change cannot collide
    with the pinned value in practice, short enough to read in a diff.
    """
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]
    )


#: The fingerprint of the ``action_config`` this checkout seeds.
#:
#: **This is a tripwire, not a record of what is deployed.** It goes red in CI
#: the moment anyone changes the synthesis model, prompt, tool schema, turn
#: budget or wall clock — which is the moment someone can still act on it,
#: rather than two days later while reading a table in the portal. Updating it
#: is the acknowledgement that the deployed workflow now needs
#: ``--replace`` run against every environment.
#:
#: What it deliberately does NOT claim: that anybody actually re-seeded
#: anything. Nothing inside this repo can know that — only ``--check`` against
#: a live Core, or ``marketing.pipeline.synthesis_config_drift`` reading a real
#: run's ``definition_snapshot``, can.
SEEDED_CONFIG_FINGERPRINT = "sha256:cb3c731624d6c34e"


def config_drift(
    deployed: dict[str, Any] | None, desired: dict[str, Any] | None = None
) -> dict[str, tuple[Any, Any]]:
    """``{key: (deployed, desired)}`` for every key that differs, empty if none.

    Compares the **whole** ``action_config``, including keys the deployed copy
    has and this checkout does not (reported with a desired value of ``None``)
    — a leftover key from an older seed is drift too.

    A key Core has masked as a secret is skipped rather than reported: its real
    value cannot be read back, so calling it drift would mean permanent,
    unfixable red. ``deployed`` of ``None`` — nothing seeded at all — is not
    expressible as a per-key diff and is the caller's job to report.
    """
    desired = definition()["action_config"] if desired is None else desired
    if deployed is None:
        return {}
    drift: dict[str, tuple[Any, Any]] = {}
    for key in sorted(set(desired) | set(deployed)):
        deployed_value = deployed.get(key)
        if deployed_value == REDACTED_SENTINEL:
            continue
        desired_value = desired.get(key)
        if deployed_value != desired_value:
            drift[key] = (deployed_value, desired_value)
    return drift


def _short(value: Any) -> str:
    """One drift value, short enough for a terminal line."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return f"{text[:70]}… ({len(text)} chars)" if len(text) > 70 else text


def _print_drift(drift: dict[str, tuple[Any, Any]]) -> None:
    print(f"The deployed workflow is STALE in {len(drift)} key(s):")
    for key, (deployed_value, desired_value) in drift.items():
        print(f"  {key}:")
        print(f"    deployed: {_short(deployed_value)}")
        print(f"    this checkout: {_short(desired_value)}")
    print(
        "\nRe-seed it:\n"
        "    CORE_API_URL=... ADMIN_BEARER_TOKEN=... \\\n"
        "      uv run python scripts/seed_fan_in_workflow.py --replace"
    )


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
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "compare the DEPLOYED definition against this checkout and change "
            "nothing. 0 in step · 1 stale or not seeded · 2 cannot tell"
        ),
    )
    args = parser.parse_args()

    payload = definition()

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        print(f"\nfingerprint: {config_fingerprint(payload['action_config'])}", file=sys.stderr)
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
        # "Cannot tell" on --check: an unreadable Core is not a clean bill of
        # health, and reporting it as one is how a drift check becomes a
        # fail-open. Unchanged (1) on a seeding run, where it is a real failure.
        print(f"Could not list workflows: {exc.code} {exc.reason}", file=sys.stderr)
        return 2 if args.check else 1

    match = next((w for w in existing if w.get("name") == WORKFLOW_NAME), None)

    if args.check:
        if match is None:
            print(
                f"NOT SEEDED: no workflow named {WORKFLOW_NAME!r} exists. "
                "A research run will never leave `pending` here.",
                file=sys.stderr,
            )
            return 1
        drift = config_drift(match.get("action_config"))
        if drift:
            _print_drift(drift)
            return 1
        print(f"In step (id {match.get('id')}) — deployed action_config matches this checkout.")
        return 0

    if match and not args.replace:
        # Never just "already seeded" again. That message was true throughout
        # #160 and said nothing about the deployed config being two releases
        # behind this checkout; an operator running the documented idempotent
        # command got a reassuring line back while synthesis ran the wrong
        # model on half the wall clock.
        drift = config_drift(match.get("action_config"))
        if drift:
            print(f"Already seeded (id {match.get('id')}), but NOT in step with this checkout.\n")
            _print_drift(drift)
            return 1
        print(f"Already seeded and in step (id {match.get('id')}). Nothing to do.")
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
