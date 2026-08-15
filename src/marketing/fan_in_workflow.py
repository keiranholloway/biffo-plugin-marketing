"""The fan-in workflow definition this build declares, and how to compare it.

**Separated from ``scripts/seed_fan_in_workflow.py`` so that more than one
thing can act on it (issue #160).** The declared configuration used to live
only inside the seeding script, which meant the only way to apply it was to
be an operator with a shell, a checkout and a real Cognito admin token. Three
detectors then grew around that script — a CI fingerprint pin, its own
``--check``, and ``pipeline.synthesis_config_drift`` — and every one of them
could only ever *report* the staleness it found, because the fix lived in a
place none of them could reach.

Importing it here changes nothing about what is seeded: the script still owns
the CLI, the HTTP calls and the operator procedure, and imports these names
rather than defining them. What it adds is a second caller — ``admin_app``'s
``GET /fan-in-workflow``, which serves this same declaration to the admin UI
so an operator can see the drift and re-seed from the browser they already
have an admin session in.

**Why the browser, and not this Lambda.** Creating or replacing a workflow
definition is admin-Cognito-gated on Core's ``/api/v1/orchestration/workflows``
router; there is no service-principal counterpart, so a plugin's SigV4-signed
Lambda cannot do it (measured against ``tabsii-platform`` @ 2026-08-15:
``WorkflowDefinition`` writes appear only in ``routers/orchestration.py``,
never in a ``require_service_principal`` router). The plugin's own web-admin,
though, already runs inside an admin's session and already holds a Cognito ID
token — so it is the one component in this repo that can both read what is
deployed and replace it. That is the seam this module exists to serve.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .definitions import (
    DEFAULT_SYNTHESIS_MODEL,
    RESEARCH_AGENT_NAMES,
    RESEARCH_SYNTHESIS_AGENT_NAME,
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    research_synthesis_definition,
    research_synthesis_tool_schema,
)

WORKFLOW_NAME = "Marketing — synthesise research once both angles complete"


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
SEEDED_CONFIG_FINGERPRINT = "sha256:31883429cd45ebe4"


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
