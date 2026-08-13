"""The model each pipeline stage runs on.

Issue #62 moved every stage from the Claude 4 generation to the Claude 5
family. None of that was covered by a test, which is exactly why the move was
invisible: a model id is a plain string that no schema validates, so a typo, a
half-finished upgrade, or a dropped ``:online`` suffix all read as ordinary
configuration until a run comes back wrong in production.

The two invariants worth pinning are the ones whose breakage is silent:

* **Family.** A stage left behind on an older generation still runs and still
  bills — nothing fails — so the only way to notice is to assert it.
* **``:online`` on research, and only research.** The suffix is what gives
  research grounded retrieval, and it is the sole reason the zero-citation
  guard has anything to cite (Biffo's native ``web_search`` is silently never
  offered on dev, so an agent that declares it fabricates rather than errors).
  Losing it on research would produce confident, uncited findings; gaining it
  on a downstream stage would bill for retrieval that stage does not use and
  would make issue #90's "this run was never grounded" reasoning wrong.
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


#: Research is deliberately NOT on Claude 5 — see issue #136 and
#: `DEFAULT_RESEARCH_MODEL`'s docstring. Named here rather than special-cased
#: inline so the exception is one reviewable fact, not a condition buried in an
#: assertion.
_GROUNDED_STAGE = "research"


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

    if stage == _GROUNDED_STAGE:
        # Asserted positively rather than skipped: research must stay on a
        # route whose grounding has actually been observed, so silently
        # drifting it forward again fails here rather than in production.
        assert model == "anthropic/claude-sonnet-4:online", (
            "research must stay on a model whose `:online` grounding has been "
            f"verified live (#136); got {model!r}. If you are moving it, run "
            "research against a fact that post-dates training and confirm "
            "`annotations` comes back non-empty first."
        )
        return

    assert base.endswith("-5"), f"{stage} is not on the Claude 5 family: {model!r}"


def test_only_research_carries_the_online_suffix() -> None:
    """`:online` is grounded retrieval. Research needs it; nothing downstream
    does, and issue #90's not-grounded reasoning depends on that staying true."""
    online = {stage for stage, model in _STAGE_MODELS.items() if ":online" in model}

    assert online == {"research"}


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
