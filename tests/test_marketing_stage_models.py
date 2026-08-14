"""The model each pipeline stage runs on.

Issue #62 moved every stage from the Claude 4 generation to the Claude 5
family. None of that was covered by a test, which is exactly why the move was
invisible: a model id is a plain string that no schema validates, so a typo, a
half-finished upgrade, or a dropped ``:online`` suffix all read as ordinary
configuration until a run comes back wrong in production.

The two invariants worth pinning are the ones whose breakage is silent:

* **Family.** A stage left behind on an older generation still runs and still
  bills — nothing fails — so the only way to notice is to assert it.
* **``:online`` on exactly the stages that retrieve.** The suffix is what
  gives a stage grounded retrieval, and it is the sole reason the
  zero-citation guard has anything to cite (Biffo's native ``web_search`` is
  silently never offered on dev, so an agent that declares it fabricates
  rather than errors). Losing it on a retrieving stage produces confident,
  uncited output; gaining it on a stage that only reasons would bill for
  retrieval that stage does not use and would make issue #90's "this run was
  never grounded" reasoning wrong about it.

  Since issue #65 that set is ``research`` **and** ``channel_plan``. Channel
  planning asks "where does this audience convert", which research's evidence
  — gathered to answer "who is this audience and what do competitors say" —
  cannot answer; it now retrieves its own. See
  ``tests/test_marketing_channel_plan_grounding.py`` for the guards that make
  that retrieval mean something rather than merely happen.
"""

from __future__ import annotations

import pytest

from marketing.definitions import (
    DEFAULT_CHANNEL_PLAN_MODEL,
    DEFAULT_COPY_MODEL,
    DEFAULT_POSITIONING_MODEL,
    DEFAULT_RESEARCH_MODEL,
    DEFAULT_SYNTHESIS_MODEL,
)

#: Every stage constant, named as the pipeline names it.
_STAGE_MODELS = {
    "research": DEFAULT_RESEARCH_MODEL,
    "synthesis": DEFAULT_SYNTHESIS_MODEL,
    "positioning": DEFAULT_POSITIONING_MODEL,
    "channel_plan": DEFAULT_CHANNEL_PLAN_MODEL,
    "copy": DEFAULT_COPY_MODEL,
}


#: The stages that retrieve, and are therefore deliberately NOT on Claude 5 —
#: see issue #136 and `DEFAULT_RESEARCH_MODEL`'s docstring. Named here rather
#: than special-cased inline so the exception is one reviewable fact, not a
#: condition buried in an assertion.
#:
#: `channel_plan` joined `research` in issue #65: it retrieves its own
#: conversion evidence now, so it needs `:online`, and `:online` only
#: demonstrably grounds on `sonnet-4` in this estate. That is a tier
#: *downgrade* for an artefact-producing stage, taken deliberately — grounding
#: that has been observed beats a tier whose grounding has been observed to be
#: absent, and the fabrication risk the Opus tier was standing in for is now
#: carried by mechanical guards (`UngroundedRecommendationError`, and
#: provenance widened to the run's own `annotations`) rather than by trusting
#: a better model.
_GROUNDED_STAGES = frozenset({"research", "channel_plan"})

#: The one route in this estate whose `:online` grounding has actually been
#: watched to work. #136's table, in one constant.
_OBSERVED_GROUNDED_ROUTE = "anthropic/claude-sonnet-4:online"


@pytest.mark.parametrize(("stage", "model"), sorted(_STAGE_MODELS.items()))
def test_every_stage_runs_on_the_claude_5_family(stage: str, model: str) -> None:
    """A stage left on an older generation bills and succeeds — assert it.

    Research is exempt, and the exemption is the point rather than a
    concession. #62 moved it to `sonnet-5:online` on the reasoning that
    `:online` is a routing directive applied to any supported chat model. That
    is false: measured on tabsii dev, every `sonnet-5:online` run reports
    **zero** `annotations`, and a probe for a fact that post-dates training
    (the current UK Bank Rate) returned `findings: []` — while the same model
    cited nine plausible URLs on a topic it could answer from memory.
    Grounding, not model recency, is what research is for.
    """
    base = model.split(":", 1)[0]

    assert base.startswith("anthropic/claude-"), f"{stage} is not on an Anthropic slug: {model!r}"

    if stage in _GROUNDED_STAGES:
        # Asserted positively rather than skipped: a retrieving stage must stay
        # on a route whose grounding has actually been observed, so silently
        # drifting it forward again fails here rather than in production.
        assert model == _OBSERVED_GROUNDED_ROUTE, (
            f"{stage} must stay on a model whose `:online` grounding has been "
            f"verified live (#136); got {model!r}. If you are moving it, run "
            "the stage against a fact that post-dates training and confirm "
            "`annotations` comes back non-empty first."
        )
        return

    assert base.endswith("-5"), f"{stage} is not on the Claude 5 family: {model!r}"


def test_exactly_the_retrieving_stages_carry_the_online_suffix() -> None:
    """`:online` is grounded retrieval. Research and channel planning need it
    (#65); nothing else does, and issue #90's not-grounded reasoning about the
    synthesis run depends on that staying true."""
    online = {stage for stage, model in _STAGE_MODELS.items() if ":online" in model}

    assert online == set(_GROUNDED_STAGES)


def test_the_seeded_synthesis_model_is_the_constant() -> None:
    """The trap in issue #62: `scripts/seed_fan_in_workflow.py` **copies**
    `DEFAULT_SYNTHESIS_MODEL` into the workflow's `action_config` at seed time,
    so synthesis reads the frozen copy at run time, not this constant. Changing
    the constant alone therefore leaves every environment on the old model
    until the workflow is re-seeded with `--replace`.

    This asserts the two are the same value at the point of seeding, so the
    diff at least names the seeded field a reader has to go re-seed.
    """
    from _scripts import load_script

    config = load_script("seed_fan_in_workflow").definition()["action_config"]

    assert config["model"] == DEFAULT_SYNTHESIS_MODEL
