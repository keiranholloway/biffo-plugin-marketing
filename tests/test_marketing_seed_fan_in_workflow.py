"""The workflow definition this plugin cannot research without.

Without it a research run never leaves `pending`: the two research agents run,
bill, and nothing reconciles them into a `research` artefact. That failure is
silent, so the contents of this definition are asserted rather than trusted —
same rationale as idea-scout's `test_idea_scout_seed_fan_in_workflow.py`.
"""

from __future__ import annotations

import io

import pytest
from _scripts import load_script

from marketing.definitions import (
    AGENT_TIMEOUT_SECONDS,
    DEFAULT_SYNTHESIS_MODEL,
    RESEARCH_AGENT_NAMES,
    RESEARCH_SYNTHESIS_AGENT_NAME,
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    RESEARCH_SYNTHESIS_TOOL_NAME,
)

# The declaration and its comparison helpers moved into the package for #160,
# so the admin UI can serve and act on the same document the script seeds. The
# script re-exports them, so everything below still reads the script's own
# contract; only the sentinel — which the script no longer references itself —
# is imported from its new home.
from marketing.fan_in_workflow import REDACTED_SENTINEL

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


# ── The snapshot goes stale, and nothing used to say so (issue #160) ──────────
#
# Everything above asserts what this checkout WOULD seed. None of it can see
# what is deployed — which is the whole of #160: for two days the deployed
# `action_config` ran `anthropic/claude-opus-4.8` on a 120s clock while every
# test in this file was green about `claude-opus-5` and 240s, because the
# script copies those values into the workflow at seed time and the deployed
# copy is a different document from this one.
#
# That is biffo-template#1362's class — a guard reading a different document
# from the one that acts — so the tests below are careful about which document
# each of them can actually speak for:
#
# * `config_drift` is exercised here against a HAND-WRITTEN copy of the real
#   deployed config, so it proves the comparison catches #160's exact drift.
#   Against a live Core it is `--check` that supplies the deployed side.
# * The fingerprint pin speaks only for this repo: it goes red when someone
#   changes what would be seeded, which is when a person can still act.
# * Neither can prove tabsii dev was re-seeded. Only `--check` or
#   `marketing.pipeline.synthesis_config_drift` (which reads the definition
#   snapshot of the run that actually ran) can, and both need a live system.


#: The `action_config` as it was actually deployed on tabsii dev on 2026-08-14,
#: transcribed from the run table in #160: today's prompt and turn budget, a
#: model two releases behind, and a wall clock the runtime substituted because
#: the seeded copy predates `timeout_seconds` existing at all.
_DEPLOYED_ON_DEV_2026_08_14 = {
    "expect_agents": ",".join(RESEARCH_AGENT_NAMES),
    "agent_name": RESEARCH_SYNTHESIS_AGENT_NAME,
    "instructions": RESEARCH_SYNTHESIS_INSTRUCTIONS,
    "model": "anthropic/claude-opus-4.8",
    "tools": [],
    "max_turns": definition()["action_config"]["max_turns"],
    "output_tools": definition()["action_config"]["output_tools"],
}


def test_config_drift_catches_the_exact_pair_that_was_frozen_on_dev() -> None:
    """The regression test for #160 itself.

    Both halves, not just the model: #62 predicted the model would stick and
    said so in writing, and the timeout stuck anyway because the prediction
    was about one key rather than about the snapshot. A check that only
    compared models would still be green on this config.
    """
    drift = _seed.config_drift(_DEPLOYED_ON_DEV_2026_08_14)

    assert set(drift) == {"model", "timeout_seconds"}
    assert drift["model"] == ("anthropic/claude-opus-4.8", DEFAULT_SYNTHESIS_MODEL)
    # Absent in the deployed copy — which is exactly how it produced 120s:
    # `RunLimits.from_snapshot` substitutes the runtime default for a key
    # nobody wrote, so "missing" and "wrong" are the same defect here.
    assert drift["timeout_seconds"] == (None, AGENT_TIMEOUT_SECONDS)


def test_config_drift_is_empty_when_the_deployed_copy_is_this_one() -> None:
    """The negative control. A check that cannot come back clean is not a
    check, it is an alarm."""
    assert _seed.config_drift(definition()["action_config"]) == {}


def test_config_drift_reports_a_key_the_deployed_copy_has_and_this_one_does_not() -> None:
    """Drift is symmetric: a leftover key from an older seed is stale
    configuration the engine still reads, not an absence."""
    deployed = {**definition()["action_config"], "retired_key": "left over"}

    assert _seed.config_drift(deployed)["retired_key"] == ("left over", None)


def test_config_drift_cannot_speak_for_a_deployment_with_nothing_seeded() -> None:
    """`None` is "there is no deployed config", which is not a per-key diff —
    and must never be reported as agreement. `main()` handles it separately
    and exits 1; this pins that `config_drift` itself claims nothing."""
    assert _seed.config_drift(None) == {}


def test_config_drift_skips_a_value_core_masked_as_a_secret() -> None:
    """Core redacts secret config fields on read (`redact_secrets`), so their
    stored value cannot be compared. Reporting the sentinel as drift would be
    permanent red no re-seed could clear — an alarm nobody can silence gets
    ignored, which is how the real one gets missed."""
    deployed = {**definition()["action_config"], "model": REDACTED_SENTINEL}

    assert _seed.config_drift(deployed) == {}


def test_the_seeded_config_fingerprint_is_pinned_to_this_checkout() -> None:
    """The tripwire that makes `--replace` obviously necessary.

    Change the synthesis model, prompt, tool schema, turn budget or wall clock
    and this fails **in the PR that changes it**, not two days later in a
    portal table. Updating the constant is the acknowledgement that every
    environment now needs a re-seed.
    """
    actual = _seed.config_fingerprint(definition()["action_config"])

    assert _seed.SEEDED_CONFIG_FINGERPRINT == actual, (
        "The seeded workflow config changed. No deploy refreshes the copy that "
        "is already out there — re-seed every environment with `uv run python "
        "scripts/seed_fan_in_workflow.py --replace`, verify with `--check`, "
        f"then pin SEEDED_CONFIG_FINGERPRINT to {actual!r}."
    )


@pytest.mark.parametrize("key", sorted(definition()["action_config"]))
def test_the_fingerprint_covers_every_key_of_the_snapshot(key: str) -> None:
    """The generalisation #62 did not make: **the whole snapshot is frozen**,
    so every key of it has to move the pin — the prompt and the wall clock as
    much as the model. Enumerated from the config itself, so a key added later
    is covered the day it is added rather than the day someone remembers."""
    mutated = {**definition()["action_config"], key: "changed"}

    assert _seed.config_fingerprint(mutated) != _seed.SEEDED_CONFIG_FINGERPRINT


# ── `--check`: the only check that reads the DEPLOYED document ────────────────
#
# Everything else in this file (and in the whole repo) compares this checkout
# with itself. `--check` is the one that asks Core what is actually out there,
# so its exit codes are the contract: 0 in step · 1 stale or not seeded ·
# 2 cannot tell. Exit 2 is never a pass — an unreadable Core is not a clean
# bill of health, which is the fail-open shape this estate keeps rediscovering.


def _run_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], listed: list[dict] | Exception
) -> int:
    """`main()` with the network replaced and both credentials present."""
    monkeypatch.setenv("CORE_API_URL", "https://core.example")
    monkeypatch.setenv("ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(_seed.sys, "argv", ["seed_fan_in_workflow.py", *argv])

    def _fake_request(method: str, url: str, token: str, body: dict | None = None) -> object:
        del url, token, body
        if isinstance(listed, Exception):
            raise listed
        return listed if method == "GET" else {"id": "wf-1"}

    monkeypatch.setattr(_seed, "_request", _fake_request)
    return _seed.main()


def _deployed(config: dict) -> list[dict]:
    return [{"id": "wf-1", "name": WORKFLOW_NAME, "action_config": config}]


def test_check_exits_zero_when_the_deployed_config_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = _deployed(definition()["action_config"])

    assert _run_main(monkeypatch, ["--check"], listed) == 0


def test_check_exits_one_on_the_config_that_was_really_deployed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = _deployed(_DEPLOYED_ON_DEV_2026_08_14)

    assert _run_main(monkeypatch, ["--check"], listed) == 1
    out = capsys.readouterr().out
    assert "STALE" in out
    assert "model" in out and "timeout_seconds" in out
    assert "--replace" in out  # the fix, in the output that reports the fault


def test_check_exits_one_when_nothing_is_seeded_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original failure this script exists to prevent — a research run that
    never leaves `pending` — is also a drift result, not a silent 0."""
    assert _run_main(monkeypatch, ["--check"], []) == 1


def test_check_exits_two_when_core_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cannot tell, never a pass."""
    boom = _seed.urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    assert _run_main(monkeypatch, ["--check"], boom) == 2


def test_check_exits_two_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORE_API_URL", raising=False)
    monkeypatch.delenv("ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(_seed.sys, "argv", ["seed_fan_in_workflow.py", "--check"])

    assert _seed.main() == 2


def test_a_seeding_run_no_longer_calls_a_stale_deployment_done(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `--replace`, this used to print "Already seeded" and exit 0 —
    a reassuring line that was true, and useless, throughout #160. The
    idempotent run an operator is told to make must now say what is stale."""
    listed = _deployed(_DEPLOYED_ON_DEV_2026_08_14)

    assert _run_main(monkeypatch, [], listed) == 1
    assert "NOT in step" in capsys.readouterr().out


def test_a_seeding_run_stays_quiet_when_the_deployment_is_in_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = _deployed(definition()["action_config"])

    assert _run_main(monkeypatch, [], listed) == 0


def test_a_failed_create_reports_the_core_error_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SECOND `except urllib.error.HTTPError` — the write itself (here, a
    POST since nothing is seeded yet) failing, distinct from
    `test_check_exits_two_when_core_cannot_be_read` above (the initial GET
    that lists existing workflows failing). A write failure is a real,
    non-`--check` failure — exit 1, not 2 — and must name what Core said,
    not raise the raw `HTTPError` out of `main()`."""
    monkeypatch.setenv("CORE_API_URL", "https://core.example")
    monkeypatch.setenv("ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(_seed.sys, "argv", ["seed_fan_in_workflow.py"])

    def _fake_request(method: str, url: str, token: str, body: dict | None = None) -> object:
        del url, token, body
        if method == "GET":
            return []
        raise _seed.urllib.error.HTTPError(
            "u",
            502,
            "Bad Gateway",
            {},
            io.BytesIO(b"upstream refused"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(_seed, "_request", _fake_request)

    assert _seed.main() == 1
    err = capsys.readouterr().err
    assert "Failed" in err
    assert "502" in err


def test_replace_puts_over_the_existing_definition_rather_than_creating_a_second(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-seed must REPLACE. Core matches nothing on name — `PUT
    /workflows/{id}` replaces the whole `action_config`, while a POST would
    create a second definition with the same name and the fan-in would then
    fire twice per chain. The match is by name over the full, unpaginated
    list, so this is what makes `--replace` safe to run on dev."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setenv("CORE_API_URL", "https://core.example")
    monkeypatch.setenv("ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(_seed.sys, "argv", ["seed_fan_in_workflow.py", "--replace"])

    def _fake_request(method: str, url: str, token: str, body: dict | None = None) -> object:
        del token, body
        calls.append((method, url))
        return _deployed(_DEPLOYED_ON_DEV_2026_08_14) if method == "GET" else {"id": "wf-1"}

    monkeypatch.setattr(_seed, "_request", _fake_request)

    assert _seed.main() == 0
    assert calls[1] == ("PUT", f"https://core.example{_seed._DEFINITIONS_PATH}/wf-1")
    assert "Replaced workflow wf-1." in capsys.readouterr().out


def test_config_drift_agrees_with_the_run_side_detector_about_a_stripped_prompt() -> None:
    """Issue #175's second half: the two detectors must not disagree about the
    same config.

    `--check` reads the stored *workflow definition* (unstripped) while
    `pipeline.synthesis_config_drift` reads a *run's* snapshot (stripped by
    Core). On 2026-08-16 they answered differently about the same workflow at
    the same moment — `--check` said "in step", the runtime said "STALE" —
    which is most of why the cause took a while to find.

    So this side normalises too, even though it is not the side that was
    firing: a stored config that differs only by the newline Core would strip
    is not drift by either reading.
    """
    stored = definition()["action_config"]
    assert stored["instructions"].endswith("\n"), (
        "this test is about the trailing newline Core strips; if the declared "
        "prompt no longer has one, keep the case but plant it explicitly"
    )
    as_a_run_would_hold_it = {**stored, "instructions": stored["instructions"].strip()}

    assert _seed.config_drift(as_a_run_would_hold_it) == {}


def test_config_drift_still_reports_a_prompt_whose_words_changed() -> None:
    """The fix must not blunt the detector — whitespace is normalised, content
    is not. This is the drift #160 exists to catch."""
    deployed = {**definition()["action_config"], "instructions": "Summarise it. Keep it short.\n"}

    assert "instructions" in _seed.config_drift(deployed)
