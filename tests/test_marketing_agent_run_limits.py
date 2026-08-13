"""The run limits every agent definition declares, and why they must agree.

Issue #126. `agent_runtime.loop.RunLimits.from_snapshot` reads `max_turns` and
`timeout_seconds` from a definition **independently**, applying its own default
for whichever is absent. So a definition can declare a turn budget the wall
clock forbids, and nothing anywhere notices: the run simply dies partway
through with a hard-stop error, and the pipeline continues on whatever it got.

That is exactly what happened. `research_definition` set `max_turns = 8` and no
`timeout_seconds`, inheriting the runtime's 120s default. Once #120 gave each
research angle its own search, every turn carried a real retrieval — and the
agent died on turn 3 of an 8-turn budget:

    Turn 3 exceeded the run's remaining wall clock (120s total, ADR-0014 §8)
    225,298 input tokens, $0.63   (the same agent cost $0.0298 before #120)

Nothing in this repo asserted anything about these limits before this file, so
the disagreement was invisible until it cost a research angle in production.
"""

from __future__ import annotations

from marketing.definitions import (
    CHANNEL_PLAN_MAX_TURNS,
    COPY_MAX_TURNS,
    POSITIONING_MAX_TURNS,
    RESEARCH_MAX_TURNS,
    RESEARCH_TIMEOUT_SECONDS,
    SYNTHESIS_MAX_TURNS,
    channel_plan_definition,
    copy_definition,
    positioning_definition,
    research_definition,
    research_synthesis_definition,
)

#: `agent_runtime.loop.DEFAULT_TIMEOUT_SECONDS`. Duplicated deliberately rather
#: than imported: the runtime is a separate deployable this plugin does not
#: depend on, and the number that matters here is the one the plugin will
#: actually inherit if it stays silent.
_RUNTIME_DEFAULT_TIMEOUT = 120.0

#: `agent_runtime.loop.DEFAULT_TIMEOUT_CEILING`, and the value
#: `AGENT_RUNTIME_MAX_SECONDS` is set to on tabsii dev. `from_snapshot` clamps
#: to this, so a definition asking for more is silently reduced — which would
#: read in the source as a bigger budget than the run actually gets.
_RUNTIME_TIMEOUT_CEILING = 240.0


def _definition(factory):
    return factory(model="some/model", instructions="do the thing")


def test_research_declares_its_own_wall_clock_rather_than_inheriting() -> None:
    """The fix for #126. A searching agent must state its wall clock, because
    the inherited default is set for agents that do not search."""
    definition = _definition(research_definition)
    assert "timeout_seconds" in definition, (
        "research searches, so it must declare a wall clock rather than "
        "inheriting the runtime's 120s default — see issue #126"
    )
    assert definition["timeout_seconds"] > _RUNTIME_DEFAULT_TIMEOUT


def test_researchs_wall_clock_is_not_silently_clamped_by_the_runtime() -> None:
    """Asking for more than the ceiling is worse than asking for the ceiling:
    `from_snapshot` reduces it without complaint, so the source would claim a
    budget the run never has. Raising this past the ceiling is an instance
    change (`AGENT_RUNTIME_MAX_SECONDS` and the Lambda timeout), not a plugin
    one."""
    assert RESEARCH_TIMEOUT_SECONDS <= _RUNTIME_TIMEOUT_CEILING, (
        f"{RESEARCH_TIMEOUT_SECONDS}s exceeds the runtime ceiling "
        f"({_RUNTIME_TIMEOUT_CEILING}s) and would be clamped silently"
    )


def test_researchs_turn_budget_is_reachable_within_its_wall_clock() -> None:
    """The invariant that was violated, stated as an assertion.

    The exact seconds a turn costs cannot be known here — it depends on the
    model and how much the search returns. What CAN be asserted is the shape of
    the disagreement that actually occurred: a turn budget so large relative to
    the wall clock that the run must die on time rather than on turns.

    The observed failure was ~40s per searching turn (3 turns into 120s). At
    that rate an 8-turn budget needs ~320s, which is past the runtime ceiling —
    so this deliberately does not assert that all 8 turns fit. It asserts the
    weaker, honest thing: the budget must allow more than the two-and-a-bit
    turns that 120s bought, or `RESEARCH_MAX_TURNS` is describing a run that
    cannot happen.
    """
    observed_seconds_per_searching_turn = 40.0
    reachable_turns = RESEARCH_TIMEOUT_SECONDS / observed_seconds_per_searching_turn
    assert reachable_turns > 3, (
        f"{RESEARCH_TIMEOUT_SECONDS}s buys about {reachable_turns:.1f} searching "
        f"turns, but RESEARCH_MAX_TURNS is {RESEARCH_MAX_TURNS} — the turn "
        "budget describes a run the wall clock forbids, which is #126"
    )


def test_the_non_searching_agents_deliberately_keep_the_default() -> None:
    """Not an oversight, and worth pinning so nobody 'fixes' it later.

    Synthesis, positioning, channel plan and copy do not search — they reason
    over what they are handed — and all four completed comfortably inside 120s
    on the same run that killed research. Raising every agent's wall clock on
    principle would hide the next agent that genuinely needs more.
    """
    for factory in (
        research_synthesis_definition,
        positioning_definition,
        channel_plan_definition,
        copy_definition,
    ):
        assert "timeout_seconds" not in _definition(factory), (
            f"{factory.__name__} does not search; it should inherit the "
            "runtime default rather than declare a wall clock"
        )


def test_every_definition_still_declares_a_turn_budget() -> None:
    """Guards the other half: `from_snapshot` defaults `max_turns` to 1, so a
    definition that stopped declaring one would quietly become single-turn."""
    expected = {
        research_definition: RESEARCH_MAX_TURNS,
        research_synthesis_definition: SYNTHESIS_MAX_TURNS,
        positioning_definition: POSITIONING_MAX_TURNS,
        channel_plan_definition: CHANNEL_PLAN_MAX_TURNS,
        copy_definition: COPY_MAX_TURNS,
    }
    for factory, turns in expected.items():
        assert _definition(factory)["max_turns"] == turns
