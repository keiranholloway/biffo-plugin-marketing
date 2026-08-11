"""The workflow definition this plugin cannot research without.

Without it a research run never leaves `pending`: the two research agents run,
bill, and nothing reconciles them into a `research` artefact. That failure is
silent, so the contents of this definition are asserted rather than trusted —
same rationale as idea-scout's `test_idea_scout_seed_fan_in_workflow.py`.
"""

from __future__ import annotations

from _scripts import load_script

from marketing.definitions import (
    RESEARCH_AGENT_NAMES,
    RESEARCH_SYNTHESIS_AGENT_NAME,
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    RESEARCH_SYNTHESIS_TOOL_NAME,
)

_seed = load_script("seed_fan_in_workflow")
WORKFLOW_NAME = _seed.WORKFLOW_NAME
definition = _seed.definition


def test_it_waits_for_exactly_the_agents_the_plugin_requests() -> None:
    """The names in expect_agents and the names start_research actually fires
    must be the same set. A drift here is a run that hangs forever."""
    config = definition()["action_config"]

    assert config["expect_agents"].split(",") == list(RESEARCH_AGENT_NAMES)


def test_it_fires_the_synthesis_agent_the_plugin_looks_for() -> None:
    """The pipeline discovers the engine's run by agent name — these must
    match, or `advance_research`'s `find_chain_run` never finds it."""
    config = definition()["action_config"]

    assert config["agent_name"] == RESEARCH_SYNTHESIS_AGENT_NAME


def test_it_carries_the_synthesis_prompt_and_model() -> None:
    """Unlike idea-scout, this plugin has no admin-editable agent config to
    resolve from at run-creation time (out of scope for M3), so the prompt and
    model must be frozen into the workflow itself or Core has nothing to run
    the fan-in-fired agent with."""
    config = definition()["action_config"]

    assert config["instructions"] == RESEARCH_SYNTHESIS_INSTRUCTIONS
    assert config["model"]


def test_it_carries_its_own_output_tool() -> None:
    """Core resolves `instructions`/`model` from a registry when absent, but
    resolves `output_tools` from nowhere — a fan-in-fired run with none would
    have no structured-output tool to call, whatever the registry question."""
    config = definition()["action_config"]

    tool_names = {t["function"]["name"] for t in config["output_tools"]}
    assert tool_names == {RESEARCH_SYNTHESIS_TOOL_NAME}


def test_it_carries_max_turns() -> None:
    """Max turns bounds the synthesis agent's cost."""
    config = definition()["action_config"]

    assert "max_turns" in config


def test_it_triggers_on_agent_completions() -> None:
    payload = definition()

    assert payload["trigger_source"] == "biffo.core"
    assert payload["trigger_detail_type"] == "agent.run.completed"
    assert payload["action_type"] == "agent_fan_in"


def test_it_is_enabled_and_named_stably() -> None:
    """The name is the idempotency key the seeder matches on — changing it
    would seed a duplicate rather than replace."""
    payload = definition()

    assert payload["enabled"] is True
    assert payload["name"] == WORKFLOW_NAME


def test_it_posts_to_the_path_core_actually_mounts() -> None:
    """Every other test here validates the *payload* and none validated the
    *endpoint*, so the script shipped posting to a path that has never existed
    and the suite stayed green.

    Core mounts workflow-definition CRUD at `/api/v1/orchestration/workflows`.
    `/api/v1/admin/orchestration` is a different router that exposes only
    `/test` (the dry-run), so the `admin/` variant 404s exactly like a route
    that was never defined — which is why nothing surfaced it: the seeder
    reported `Could not list workflows: 404 Not Found` and that reads as a
    permissions or environment problem, not a wrong URL.

    Measured against deployed dev on 2026-08-11 with a valid admin token:
    `/api/v1/orchestration/workflows` -> 200, `/api/v1/admin/...` -> 404,
    identical to a known-bad control path.

    This pins the string; it cannot prove Core still mounts it there. A
    404 from this script means check the deployed `openapi.json` before
    assuming the token or environment is at fault.
    """
    assert _seed._DEFINITIONS_PATH == "/api/v1/orchestration/workflows"
    assert "/admin/" not in _seed._DEFINITIONS_PATH
