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
    AGENT_TIMEOUT_SECONDS,
    CHANNEL_PLAN_MAX_TURNS,
    COPY_MAX_TURNS,
    POSITIONING_MAX_TURNS,
    RESEARCH_MAX_TURNS,
    RUNTIME_DEFAULT_TIMEOUT_SECONDS,
    RUNTIME_TIMEOUT_CEILING_SECONDS,
    SYNTHESIS_MAX_TURNS,
    channel_plan_definition,
    copy_definition,
    discover_definition_factories,
    positioning_definition,
    research_definition,
    research_synthesis_definition,
)

#: Derived here, in this test module, rather than imported as a
#: pre-computed constant from `definitions.py` — deliberately: see
#: `discover_definition_factories`'s docstring for why a snapshot taken
#: DURING that module's own import can silently miss a factory depending on
#: where in the file it is added. By the time the `from marketing.definitions
#: import ...` above returns, that whole module has finished executing, so
#: calling the discovery function here sees its complete, final namespace
#: regardless of factory placement.
DEFINITION_FACTORIES = discover_definition_factories()

#: `RUNTIME_DEFAULT_TIMEOUT_SECONDS`/`RUNTIME_TIMEOUT_CEILING_SECONDS` (issue
#: #132 hole 2) used to be re-typed here as this file's OWN literals
#: (`_RUNTIME_DEFAULT_TIMEOUT = 120.0`, `_RUNTIME_TIMEOUT_CEILING = 240.0`),
#: independently of `definitions.py`'s copy of the same belief — two places
#: that were free to drift from EACH OTHER even though both live in this repo
#: and neither can see the real upstream value. Importing collapses that to
#: one place; it does not (and cannot, from this repo — see their docstrings)
#: make either number provably correct against `agent_runtime` itself.
_RUNTIME_DEFAULT_TIMEOUT = RUNTIME_DEFAULT_TIMEOUT_SECONDS
_RUNTIME_TIMEOUT_CEILING = RUNTIME_TIMEOUT_CEILING_SECONDS


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
    change (setting `AGENT_RUNTIME_MAX_SECONDS` for the first time, or
    changing the runtime's own default upstream), not a plugin one.

    **What this assertion cannot catch (issue #132 hole 2).** Both sides here
    are this repo's OWN copies of numbers that live in `agent_runtime`, which
    is not a dependency of this plugin and so cannot be imported. If the real
    ceiling is ever lowered, `_RUNTIME_TIMEOUT_CEILING` does not move with
    it — nothing tells this test the number it is comparing against is stale
    — so this proves internal self-consistency between two beliefs held in
    this repo, never agreement with `agent_runtime` itself. That gap needs an
    upstream contract test that can read both sides (biffo-template#1364) or
    a loud clamp in `from_snapshot`; neither is buildable from here.
    """
    assert AGENT_TIMEOUT_SECONDS <= _RUNTIME_TIMEOUT_CEILING, (
        f"{AGENT_TIMEOUT_SECONDS}s exceeds the runtime ceiling "
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
    reachable_turns = AGENT_TIMEOUT_SECONDS / observed_seconds_per_searching_turn
    assert reachable_turns > 3, (
        f"{AGENT_TIMEOUT_SECONDS}s buys about {reachable_turns:.1f} searching "
        f"turns, but RESEARCH_MAX_TURNS is {RESEARCH_MAX_TURNS} — the turn "
        "budget describes a run the wall clock forbids, which is #126"
    )


def test_no_agent_inherits_the_runtime_default_wall_clock() -> None:
    """The invariant #130 replaced, and the reason it had to be replaced.

    #126 raised the wall clock for the searching agents only, and this test
    previously asserted the opposite of what it now does: that the
    non-searching agents deliberately kept the 120s default, because they had
    all finished well inside it.

    That was measured on a run where the audience research angle had FAILED, so
    synthesis had half the findings to reconcile. With both angles restored it
    produced 8,546 output tokens and died on the same hard stop. The old
    assertion pinned a belief that a single degraded run had made look true.

    The axis is total work, not retrieval — every agent here emits a large
    structured artefact — so the correct invariant is that none of them relies
    on a default chosen for smaller calls.

    Sweeps `DEFINITION_FACTORIES` rather than a hand-listed tuple (issue #132
    hole 1): that tuple is derived from `definitions.py` itself via
    `discover_definition_factories()`, which raises if the count disagrees
    with what it expects — see that function's docstring for why the
    discovery is a function called here rather than a constant computed
    inside `definitions.py`. Either way, a sixth factory added tomorrow is
    swept here automatically rather than needing this test remembered and
    updated.
    """
    for factory in DEFINITION_FACTORIES:
        definition = _definition(factory)
        assert "timeout_seconds" in definition, (
            f"{factory.__name__} must declare its own wall clock — the "
            f"runtime's {_RUNTIME_DEFAULT_TIMEOUT:g}s default is set for "
            "smaller calls than this plugin makes (#130)"
        )
        assert definition["timeout_seconds"] > _RUNTIME_DEFAULT_TIMEOUT


def test_every_definition_still_declares_a_turn_budget() -> None:
    """Guards the other half: `from_snapshot` defaults `max_turns` to 1, so a
    definition that stopped declaring one would quietly become single-turn.

    The exact turns-per-stage mapping below cannot itself be derived — there
    is no naming convention tying e.g. `research_synthesis_definition` to
    `SYNTHESIS_MAX_TURNS` — but the same coverage-assert shape as
    `definitions.py:694` still applies to what CAN be checked mechanically:
    that this hand-written mapping has not quietly fallen out of step with
    `DEFINITION_FACTORIES` itself (issue #132 hole 1, same shape as the sweep
    above)."""
    expected = {
        research_definition: RESEARCH_MAX_TURNS,
        research_synthesis_definition: SYNTHESIS_MAX_TURNS,
        positioning_definition: POSITIONING_MAX_TURNS,
        channel_plan_definition: CHANNEL_PLAN_MAX_TURNS,
        copy_definition: COPY_MAX_TURNS,
    }
    assert set(expected) == set(DEFINITION_FACTORIES), (
        "expected must cover exactly DEFINITION_FACTORIES — a *_definition "
        f"factory was added, removed or renamed without updating this "
        f"mapping (expected covers {sorted(f.__name__ for f in expected)}, "
        f"DEFINITION_FACTORIES has {sorted(f.__name__ for f in DEFINITION_FACTORIES)})"
    )
    for factory, turns in expected.items():
        assert _definition(factory)["max_turns"] == turns
