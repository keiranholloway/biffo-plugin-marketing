"""A run that ends without calling its output tool produces nothing (issue #159).

On tabsii dev, 2026-08-14, the channel-plan stage failed with
``the channel-plan run produced no submit_channel_plan tool call (502)``. The
run itself **completed**: 16.6s against a 240s budget, no clamp warning,
``anthropic/claude-sonnet-4:online``, $0.1344 billed. It ran, it cost money,
and it answered in prose.

## Why this module exists rather than one more assertion in the grounding suite

The same defect has now happened twice in this plugin, in two different
prompts, and the second time was written by the fix for the first.

``_EVIDENCE_RULE``'s own docstring records the first: research's rule "used to
say 'search the web for current material', which is an instruction the model
cannot follow, immediately followed by an escape hatch for when it finds
nothing — and it took the escape hatch". Measured on dev 2026-08-12,
retrieval returned 10 sources on every run and the model cited none of them,
twice consecutively.

Issue #155 then rebuilt ``CHANNEL_PLAN_INSTRUCTIONS`` around exactly that
shape again — "**You also have live web search, and you are expected to use
it**", "Search before you decide", "If you searched and found nothing about a
channel's performance for this audience, do not recommend that channel — say
nothing rather than reach for the positioning" — against a definition that
declares ``tools: []`` and a provider that searches **once, from the payload,
before the model is invoked** (#101). The agent is ordered to take a step it
has no tool for, and then handed a licence to produce nothing when that step
does not happen.

Research took the escape hatch and returned an empty artefact. Channel
planning went one worse and did not call the tool at all, because nothing in
its prompt ever said that "recommend nothing" is still delivered *by calling
the tool*.

So the rules pinned here are stage-agnostic on purpose. Both of them are
properties of the whole pipeline, not of one prompt:

1. **No agent is ordered to perform a search it has no tool for.** Every
   definition in this plugin declares ``tools: []``; retrieval, where it
   happens at all, is the ``:online`` suffix doing it before the model runs.
2. **Every agent is told that having nothing to say is still delivered by
   calling its output tool.** A stage's honest "I found nothing" and its
   catastrophic "no artefact at all" are the same 502 to an operator, and
   only the first is recoverable by reading it.

Whether the agent then *does* reliably call the tool is only observable on a
live run — these guards claim no more than that the prompt no longer asks for
the impossible and no longer leaves silence as a legal answer.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pytest

from marketing import pipeline
from marketing.definitions import (
    AUDIENCE_RESEARCH_INSTRUCTIONS,
    CHANNEL_EVIDENCE_INSTRUCTIONS,
    CHANNEL_EVIDENCE_TOOL_NAME,
    CHANNEL_PLAN_INSTRUCTIONS,
    CHANNEL_PLAN_TOOL_NAME,
    COMPETITIVE_RESEARCH_INSTRUCTIONS,
    COPY_INSTRUCTIONS,
    COPY_TOOL_NAME,
    DEFAULT_CHANNEL_EVIDENCE_MODEL,
    DEFAULT_CHANNEL_PLAN_MODEL,
    DEFAULT_COPY_MODEL,
    DEFAULT_POSITIONING_MODEL,
    DEFAULT_RESEARCH_MODEL,
    DEFAULT_SYNTHESIS_MODEL,
    FINDINGS_TOOL_NAME,
    POSITIONING_INSTRUCTIONS,
    POSITIONING_TOOL_NAME,
    RESEARCH_SYNTHESIS_INSTRUCTIONS,
    RESEARCH_SYNTHESIS_TOOL_NAME,
    channel_evidence_definition,
    channel_plan_definition,
    copy_definition,
    positioning_definition,
    research_definition,
    research_synthesis_definition,
)

#: Every agent this plugin runs: its instructions, the model it runs on, the
#: output tool it must call, and the definition it is dispatched with.
#:
#: Assembled once here rather than per test so a sixth stage is one row, not a
#: sixth place to remember. Every rule below sweeps ALL of it — the two
#: defects this module exists for were each written into one prompt while
#: the others were fine, which is exactly the shape a per-stage assertion
#: misses.
_STAGES: dict[str, tuple[str, str, str, dict[str, Any]]] = {
    "research_audience": (
        AUDIENCE_RESEARCH_INSTRUCTIONS,
        DEFAULT_RESEARCH_MODEL,
        FINDINGS_TOOL_NAME,
        research_definition(model=DEFAULT_RESEARCH_MODEL, instructions="i"),
    ),
    "research_competitive": (
        COMPETITIVE_RESEARCH_INSTRUCTIONS,
        DEFAULT_RESEARCH_MODEL,
        FINDINGS_TOOL_NAME,
        research_definition(model=DEFAULT_RESEARCH_MODEL, instructions="i"),
    ),
    "synthesis": (
        RESEARCH_SYNTHESIS_INSTRUCTIONS,
        DEFAULT_SYNTHESIS_MODEL,
        RESEARCH_SYNTHESIS_TOOL_NAME,
        research_synthesis_definition(model=DEFAULT_SYNTHESIS_MODEL, instructions="i"),
    ),
    "positioning": (
        POSITIONING_INSTRUCTIONS,
        DEFAULT_POSITIONING_MODEL,
        POSITIONING_TOOL_NAME,
        positioning_definition(model=DEFAULT_POSITIONING_MODEL, instructions="i"),
    ),
    "channel_evidence": (
        CHANNEL_EVIDENCE_INSTRUCTIONS,
        DEFAULT_CHANNEL_EVIDENCE_MODEL,
        CHANNEL_EVIDENCE_TOOL_NAME,
        channel_evidence_definition(model=DEFAULT_CHANNEL_EVIDENCE_MODEL, instructions="i"),
    ),
    "channel_plan": (
        CHANNEL_PLAN_INSTRUCTIONS,
        DEFAULT_CHANNEL_PLAN_MODEL,
        CHANNEL_PLAN_TOOL_NAME,
        channel_plan_definition(model=DEFAULT_CHANNEL_PLAN_MODEL, instructions="i"),
    ),
    "copy": (
        COPY_INSTRUCTIONS,
        DEFAULT_COPY_MODEL,
        COPY_TOOL_NAME,
        copy_definition(model=DEFAULT_COPY_MODEL, instructions="i"),
    ),
}


# ── 1. Nothing is ordered to search ─────────────────────────────────────────


def test_no_agent_in_this_pipeline_has_a_search_tool_to_be_ordered_to_use() -> None:
    """The premise every rule below rests on, asserted rather than assumed.

    Retrieval in this plugin is the `:online` suffix, which the provider
    applies to the *payload* before the model is invoked (#101) — never a
    registry tool, because a registered-but-unconfigured `web_search` is
    silently dropped rather than failed. So no agent here can search on
    purpose, at any point, on any turn.
    """
    for stage, (_instructions, _model, _tool, definition) in _STAGES.items():
        assert definition["tools"] == [], (
            f"{stage} declares a registry tool; every rule in this module assumes "
            "retrieval travels with the model id, never as a tool the agent calls"
        )


#: Phrasings that order the model to go and search — the shape
#: `_EVIDENCE_RULE`'s docstring says research took the escape hatch on, and
#: that #155 wrote back into the channel-plan prompt.
#:
#: Matched as regexes over the lowered prompt rather than as an exact list of
#: sentences, because the defect is the *mood* of the instruction (an
#: imperative, or a claim of a capability) rather than any one wording.
_ORDERS_A_SEARCH = (
    r"\bsearch before you\b",
    r"\byou (also )?have live web search\b",
    r"\bexpected to use it\b",
    r"\bif you searched\b",
    r"\byour own search results\b",
    r"\bgo and search\b",
    r"\bsearch the web\b",
)


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_no_stage_is_ordered_to_run_a_search_it_has_no_tool_for(stage: str) -> None:
    """The first half of #159, and a defect this plugin has now shipped twice.

    An agent told to search, on a run with no search tool, has been given an
    unsatisfiable precondition for its own answer. Research's version of this
    produced empty artefacts; channel planning's produced no tool call at all.
    Describing the retrieval in the past tense costs nothing and removes the
    precondition entirely.
    """
    instructions, _model, _tool, _definition = _STAGES[stage]
    lowered = instructions.lower()
    offenders = [pattern for pattern in _ORDERS_A_SEARCH if re.search(pattern, lowered)]
    assert offenders == [], (
        f"{stage}'s instructions order a search the run cannot perform "
        f"({offenders}). Retrieval here happens once, from the payload, before "
        "the model is invoked (#101) — say what was retrieved FOR it, never "
        "what it should go and retrieve."
    )


#: The sentence a grounded stage must carry, quoted rather than imported so
#: this module still imports (and still fails, legibly) against a tree where
#: the rule does not exist yet. `test_the_already_ran_sentence_is_the_one_the
#: _prompt_ships` below is what stops the quote and the constant drifting.
_ALREADY_RAN_SENTENCE = "That search has already run."


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_every_grounded_stage_is_told_its_search_has_already_run(stage: str) -> None:
    """The positive form of the rule above: a stage on an `:online` route DOES
    have retrieved pages in front of it, and must be told so in as many words.

    Not decoration. A model that believes it still has to search, and cannot,
    has two options — answer from weights (which is #65) or explain the
    problem in prose (which is #159). Telling it the results are already here
    removes both.
    """
    instructions, model, _tool, _definition = _STAGES[stage]
    if not model.endswith(":online"):
        pytest.skip(f"{stage} does not retrieve, so it has no retrieval to describe")
    assert _ALREADY_RAN_SENTENCE in instructions, (
        f"{stage} runs on a grounded route but is never told that its retrieval "
        "already happened — see `RETRIEVAL_ALREADY_RAN_RULE`"
    )


def test_the_already_ran_sentence_is_the_one_the_prompt_ships() -> None:
    """Drift guard for the quoted literal above: the sentence this module
    checks for must be the sentence the shared rule actually contains, or
    every assertion above quietly stops meaning anything."""
    from marketing.definitions import RETRIEVAL_ALREADY_RAN_RULE

    assert _ALREADY_RAN_SENTENCE in RETRIEVAL_ALREADY_RAN_RULE


# ── 2. Silence is never a legal answer ──────────────────────────────────────


#: Quoted for the same reason `_ALREADY_RAN_SENTENCE` is, and pinned against
#: the real rule by the drift test below.
_EMPTY_IS_STILL_AN_ANSWER = "Having nothing to report is still an answer"


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_every_stage_is_told_that_having_nothing_to_say_still_means_calling_the_tool(
    stage: str,
) -> None:
    """The second half of #159, and the half that is not specific to search.

    Four of these six prompts already grant an explicit licence to return
    nothing — "return empty lists rather than inventing content", "return
    fewer channels (or none)", "return an empty findings list". Not one of
    them said how an empty answer is *delivered*, and the delivery is the
    whole difference between an artefact an operator can read ("the research
    came back empty") and a dead stage they cannot ("produced no
    submit_channel_plan tool call").
    """
    instructions, _model, _tool, _definition = _STAGES[stage]
    assert _EMPTY_IS_STILL_AN_ANSWER in instructions, (
        f"{stage} tells the model it may have nothing to report, but never that "
        "an empty answer is still delivered by calling the output tool — see "
        "`return_via_tool`"
    )


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_every_stage_names_its_own_output_tool_in_that_rule(stage: str) -> None:
    """The rule is generated per tool, so a stage cannot inherit another
    stage's tool name — the failure that would make the instruction actively
    wrong rather than merely absent."""
    from marketing.definitions import return_via_tool

    instructions, _model, tool, _definition = _STAGES[stage]
    assert return_via_tool(tool) in instructions


def test_the_empty_answer_sentence_is_the_one_the_prompt_ships() -> None:
    """Drift guard, as above."""
    from marketing.definitions import return_via_tool

    assert _EMPTY_IS_STILL_AN_ANSWER in return_via_tool("submit_anything")


# ── 3. The live failure's own shape, pinned ─────────────────────────────────
#
# These two do NOT fail before the fix — the extraction path behaved correctly
# on 2026-08-14 and still does. They are here because "the run completed and
# returned prose" is the shape the whole module is about, and nothing else in
# this repo reproduces it end to end: `test_marketing_channel_plan_grounding`
# always hands `extract_channel_plan` a tool call.


_GROUNDING_RUN = "run-evidence"
_PLANNING_RUN = "run-plan"


class _CompletedWithProse:
    """A gateway whose channel-PLANNING run completes, successfully, having
    said something reasonable-sounding and called nothing.

    Its grounding run behaves perfectly — it retrieved, and it reported what it
    read — because the failure being reproduced is the planning run answering
    in prose, and a two-step stage that fell over one run earlier would not be
    the same defect.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    async def request_agent_run(self, **_kwargs: Any) -> str:
        return _PLANNING_RUN

    async def find_chain_run(self, **_kwargs: Any) -> pipeline.AgentRunView | None:
        return None

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView:
        annotations = [{"type": "url_citation", "url": "https://benchmarks.example.com/cpa"}]
        if run_id == _GROUNDING_RUN:
            return pipeline.AgentRunView(
                id=run_id,
                status="completed",
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_channel_evidence",
                                    "arguments": {
                                        "evidence": [
                                            {
                                                "url": "https://benchmarks.example.com/cpa",
                                                "note": "Reported CPA for this buyer.",
                                            }
                                        ]
                                    },
                                }
                            }
                        ],
                    }
                ],
                annotations=annotations,
            )
        return pipeline.AgentRunView(
            id=run_id,
            status="completed",
            messages=[{"role": "assistant", "content": self._text}],
            annotations=None,
        )


_TAXONOMY: dict[str, Literal["organic", "paid"]] = {"google_search_paid": "paid"}


@pytest.mark.asyncio
async def test_a_completed_run_that_answered_in_prose_is_refused_not_accepted() -> None:
    """The 502 an operator saw, reproduced against the plugin's own fakes.

    The run is `completed` and `succeeded` — this is NOT the failed-run path
    (`RunNotSucceededError`), which is why the agent-runs page showed a green
    row next to a red stage. The artefact is refused on the only thing that
    was actually missing: the tool call.
    """
    gateway = _CompletedWithProse(
        "I'd need conversion benchmarks for this audience before I could "
        "recommend channels responsibly. Here is what I would look for..."
    )

    with pytest.raises(pipeline.MalformedOutputError) as raised:
        await pipeline.advance_channel_plan(
            gateway,  # type: ignore[arg-type]
            evidence_run_id=_GROUNDING_RUN,
            plan_run_id=_PLANNING_RUN,
            taxonomy=_TAXONOMY,
        )

    assert str(raised.value) == (
        f"the channel-plan run produced no {CHANNEL_PLAN_TOOL_NAME} tool call"
    )


@pytest.mark.asyncio
async def test_the_prose_failure_is_a_pipeline_error_so_it_reaches_the_operator() -> None:
    """`MalformedOutputError` is a `PipelineError`, so `admin_app.
    _pipeline_error_to_http` maps it to a 502 carrying the sentence rather
    than to a bare 500 — which is what makes the stuck stage's reason
    readable at all, and what the retry control in `PipelineStage` keys off.
    """
    assert issubclass(pipeline.MalformedOutputError, pipeline.PipelineError)
