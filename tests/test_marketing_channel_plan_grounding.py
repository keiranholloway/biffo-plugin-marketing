"""The channel plan must be grounded in its OWN retrieval, not positioning's
leftovers (issue #65).

Research asks "who is this audience and what do competitors say". Channel
planning asks "where does this audience convert". Those are different
questions needing different evidence, and until this module existed the second
was answered entirely with the first's: the channel-plan agent ran on a
non-searching model, was told in as many words that it had no web access, and
was required to copy its `Source` objects verbatim out of the approved
positioning artefact. Every channel recommendation — the decision the whole
campaign's budget follows — was therefore justified by evidence gathered to
answer a different question, and nothing in the system could tell that apart
from a channel plan somebody had actually researched.

Four things are pinned here, because each of them fails silently:

1. **The route.** `:online` is how grounded retrieval works in this estate
   (Biffo's native `web_search` is silently never offered on dev), and not
   every `:online` route grounds — `sonnet-5:online` returns zero annotations
   (#136/#141). A model id is a plain string nothing validates.
2. **The prompt.** A searching agent still told "you do not have web access"
   will not search.
3. **Provenance, re-derived for a stage that retrieves (issue #113/#22).**
   The old rule — every cited URL must appear in the approved parent — is
   exactly what #65 exists to break, so it cannot simply carry over. The rule
   that replaces it is stated in `pipeline.extract_channel_plan` and proved
   below: the legitimate set is the parent's citations UNION what the
   runtime's own `annotations` record says this run retrieved.
4. **That the retrieval actually reached the recommendations.** A run can
   search, and then cite nothing it found. The count-based guard passes on
   positioning's carried-over URLs exactly as well as on a conversion
   benchmark, which is the gap issue #65 names in its own words: "re-using
   positioning's citations should stop being sufficient — otherwise the guard
   passes on the old evidence and nothing changes".
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from marketing import pipeline
from marketing.definitions import (
    CHANNEL_PLAN_AGENT_NAME,
    CHANNEL_PLAN_INSTRUCTIONS,
    CHANNEL_PLAN_MAX_TURNS,
    DEFAULT_CHANNEL_PLAN_MODEL,
    POSITIONING_MAX_TURNS,
    channel_plan_definition,
)

# ── Fixtures: one parent URL, one retrieved URL, one invented one ────────────
#
# Deliberately three different hosts. The point of the widened provenance rule
# is that "retrieved" and "carried over from the parent" are BOTH legitimate
# and distinguishable, and that anything in neither set is still refused —
# which a shared host would blur (`_url_key` keeps the path, so "same domain"
# has never been provenance here).

_PARENT_URL = "https://positioning.example.com/segment-evidence"
_RETRIEVED_URL = "https://benchmarks.example.com/paid-search-cpa-2026"
_FABRICATED_URL = "https://invented.example.com/statistics"

_ANNOTATIONS = [{"type": "url_citation", "url": _RETRIEVED_URL, "title": "CPA benchmarks"}]

_TAXONOMY: dict[str, Literal["organic", "paid"]] = {
    "google_search_paid": "paid",
    "linkedin_organic": "organic",
}


def _tool_call(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "submit_channel_plan", "arguments": arguments}}],
        }
    ]


def _channel(channel_key: str, *urls: str, rank: int = 1) -> dict[str, Any]:
    return {
        "channel_key": channel_key,
        "rank": rank,
        "rationale": "Where this segment demonstrably converts.",
        "sources": [{"url": url, "note": "n"} for url in urls],
    }


def _plan_call(*channels: dict[str, Any]) -> list[dict[str, Any]]:
    return _tool_call({"channels": list(channels)})


class _FakeGateway:
    """Records the requested run and lets a test complete it — the same shape
    `test_marketing_pipeline_orchestration.py` uses, kept local because this
    module only ever drives the one stage."""

    def __init__(self) -> None:
        self.requested: list[dict[str, Any]] = []
        self._runs: dict[str, pipeline.AgentRunView] = {}

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        del output_tool
        run_id = f"run-{len(self.requested) + 1}"
        self.requested.append(
            {
                "agent_name": agent_name,
                "definition": definition,
                "input_payload": input_payload,
                "causation_id": causation_id,
            }
        )
        self._runs[run_id] = pipeline.AgentRunView(id=run_id, status="pending")
        return run_id

    async def find_chain_run(self, *, chain_id: str, agent_name: str):  # pragma: no cover
        del chain_id, agent_name
        return None

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
        return self._runs.get(run_id)

    def complete(
        self,
        run_id: str,
        *,
        status: str = "completed",
        messages: list[dict[str, Any]] | None = None,
        annotations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._runs[run_id] = pipeline.AgentRunView(
            id=run_id, status=status, messages=messages or [], annotations=annotations
        )


_POSITIONING_BODY = {
    "segments": [
        {
            "name": "Multi-location operators",
            "description": "Run 3+ sites and cannot see them in one place.",
            "sources": [{"url": _PARENT_URL, "note": "n"}],
        }
    ],
    "pillars": [],
    "ctas": [],
}

_TAXONOMY_ROWS = [
    {
        "channel_key": "google_search_paid",
        "label": "Google Search ads",
        "motion": "paid",
        "category": "search",
    }
]


# ── 1. The route ─────────────────────────────────────────────────────────────


def test_the_channel_plan_stage_runs_on_a_grounded_route() -> None:
    """Without `:online` the stage cannot retrieve at all, and this is a plain
    string nothing else validates."""
    assert DEFAULT_CHANNEL_PLAN_MODEL.endswith(":online"), (
        "channel planning must retrieve its own conversion evidence (#65), and "
        f"`:online` is how retrieval works here; got {DEFAULT_CHANNEL_PLAN_MODEL!r}"
    )


def test_the_channel_plan_route_is_one_whose_grounding_has_been_observed_live() -> None:
    """`:online` is necessary and not sufficient. `sonnet-5:online` carries the
    suffix and returns **zero** annotations (#136) — the failure that cost a
    day because it presents as good output on familiar topics and honest
    emptiness on unfamiliar ones. So the route is pinned to the one this
    estate has actually watched ground, and moving it means running the cheap
    probe in `DEFAULT_RESEARCH_MODEL`'s docstring first."""
    assert DEFAULT_CHANNEL_PLAN_MODEL == "anthropic/claude-sonnet-4:online", (
        "channel planning must run on a route whose `:online` grounding has "
        f"been observed live (#136/#141); got {DEFAULT_CHANNEL_PLAN_MODEL!r}. If "
        "you are moving it, run the stage against a fact that post-dates "
        "training and confirm `annotations` comes back non-empty first."
    )


# ── 2. The prompt and the budget ─────────────────────────────────────────────


def test_the_prompt_no_longer_tells_the_agent_it_has_no_web_access() -> None:
    """The instruction that made #65 true, quoted in the issue itself."""
    assert "do not have web access" not in CHANNEL_PLAN_INSTRUCTIONS


def test_the_prompt_asks_for_conversion_evidence_specifically() -> None:
    """Not "search the web" — the whole point is *which* question the search
    answers. Generic channel wisdom retrieved live is still generic channel
    wisdom."""
    lowered = CHANNEL_PLAN_INSTRUCTIONS.lower()
    assert "convert" in lowered
    assert "search results" in lowered


def test_the_turn_budget_leaves_room_to_search_and_then_answer() -> None:
    """`POSITIONING_MAX_TURNS` is the budget of a stage that only reasons over
    what it is given. A searching stage that keeps it can search once, at
    most, and then must answer — which is not a stage that researches."""
    assert CHANNEL_PLAN_MAX_TURNS > POSITIONING_MAX_TURNS


def test_the_definition_still_declares_no_registry_tool() -> None:
    """Retrieval travels with the model id, never as a declared `web_search`
    tool: on dev that tool is silently unavailable, so an agent that declares
    it fabricates rather than errors."""
    assert channel_plan_definition(model="m", instructions="i")["tools"] == []


# ── 3. The searched query is derived from the CHANNEL question ───────────────


@pytest.mark.asyncio
async def test_the_run_leads_with_a_conversion_search_query() -> None:
    """An `:online` run's retrieval is derived from its input payload — the
    provider searches before the model is invoked — so the payload, not the
    instructions, is the only place this stage can affect what comes back
    (issue #101, measured: identical payloads produced identical pages). A
    channel-plan payload that leads with the positioning body would retrieve
    the positioning question all over again."""
    gateway = _FakeGateway()

    await pipeline.start_channel_plan(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )

    payload = gateway.requested[0]["input_payload"]
    assert next(iter(payload)) == "search_query", (
        "the searched query must be the FIRST key of the payload, for the same "
        "reason research's is (#101)"
    )
    query = payload["search_query"].lower()
    assert "convert" in query  # the channel question, not the audience question
    assert "multi-location operators" in query  # this campaign's audience, not any audience
    assert "google search ads" in query  # the channels actually on the table
    assert gateway.requested[0]["agent_name"] == CHANNEL_PLAN_AGENT_NAME


# ── 4. Provenance, for a stage that retrieves (issue #113/#22 re-derived) ────


def test_a_url_this_run_retrieved_is_not_treated_as_fabricated() -> None:
    """The check that had to change. `channel_plan` is a guarded stage: every
    cited URL used to be checked against the approved positioning's set, and
    citing evidence the parent did NOT contain is now the whole point of the
    stage. The rule is not dropped — it is widened to the set that can
    actually have been read: the parent's citations UNION the runtime's own
    record of what this run retrieved."""
    messages = _plan_call(_channel("google_search_paid", _RETRIEVED_URL))

    plan = pipeline.extract_channel_plan(
        messages,
        taxonomy=_TAXONOMY,
        allowed_source_urls=[_PARENT_URL],
        annotations=_ANNOTATIONS,
    )

    assert [s.url for s in plan.channels[0].sources] == [_RETRIEVED_URL]


def test_a_url_in_neither_the_parent_nor_the_retrieval_is_still_refused() -> None:
    """The fabrication guard has to survive the widening, or #65 silently
    switches off the most dangerous check in the pipeline. A URL the runtime
    never recorded retrieving, and the parent never contained, cannot have
    been read by anything."""
    messages = _plan_call(_channel("google_search_paid", _RETRIEVED_URL, _FABRICATED_URL))

    with pytest.raises(pipeline.UncitedSourceError) as excinfo:
        pipeline.extract_channel_plan(
            messages,
            taxonomy=_TAXONOMY,
            allowed_source_urls=[_PARENT_URL],
            annotations=_ANNOTATIONS,
        )

    assert _FABRICATED_URL in str(excinfo.value)
    assert _RETRIEVED_URL not in str(excinfo.value)  # only the offending one is named


def test_the_parents_own_sources_are_still_legitimate_alongside_retrieval() -> None:
    """Widened, not replaced. A recommendation may still lean on the
    positioning that justified the campaign — it simply may not lean on that
    ALONE (the check below)."""
    messages = _plan_call(_channel("google_search_paid", _PARENT_URL, _RETRIEVED_URL))

    plan = pipeline.extract_channel_plan(
        messages,
        taxonomy=_TAXONOMY,
        allowed_source_urls=[_PARENT_URL],
        annotations=_ANNOTATIONS,
    )

    assert {s.url for s in plan.channels[0].sources} == {_PARENT_URL, _RETRIEVED_URL}


def test_unknown_retrieval_cannot_adjudicate_provenance_either_way() -> None:
    """`annotations is None` is "not known", never "retrieved nothing" — the
    tri-state this module keeps everywhere. With no record of what the run
    retrieved there is no closed set to check against, so the check is skipped
    rather than guessed at: enforcing the parent-only set would fail a
    genuinely grounded run whose annotations the runtime did not record, which
    is how a guard ends up switched off for real."""
    messages = _plan_call(_channel("google_search_paid", _FABRICATED_URL))

    plan = pipeline.extract_channel_plan(
        messages,
        taxonomy=_TAXONOMY,
        allowed_source_urls=[_PARENT_URL],
        annotations=None,
    )

    assert plan.channels[0].sources[0].url == _FABRICATED_URL


# ── 5. Retrieval must actually reach the recommendations ────────────────────


def test_a_plan_resting_entirely_on_positionings_sources_is_refused() -> None:
    """Issue #65's central demand. This run searched — `annotations` says so —
    and then justified its channel choice with the audience research it was
    handed. Every count-based check passes; the plan is exactly as ungrounded
    as it was before the stage could search at all."""
    messages = _plan_call(_channel("google_search_paid", _PARENT_URL))

    with pytest.raises(pipeline.UngroundedRecommendationError) as excinfo:
        pipeline.extract_channel_plan(
            messages,
            taxonomy=_TAXONOMY,
            allowed_source_urls=[_PARENT_URL],
            annotations=_ANNOTATIONS,
        )

    assert "google_search_paid" in str(excinfo.value)


def test_every_recommendation_needs_its_own_retrieved_evidence() -> None:
    """Per recommendation, not per plan. A plan-level check passes as soon as
    one channel is researched, and the other four then ride along on it — and
    a channel recommendation is generated-per-channel work downstream, so a
    wrong one is not one wrong artefact, it is every artefact after it."""
    messages = _plan_call(
        _channel("google_search_paid", _RETRIEVED_URL),
        _channel("linkedin_organic", _PARENT_URL),
    )

    with pytest.raises(pipeline.UngroundedRecommendationError) as excinfo:
        pipeline.extract_channel_plan(
            messages,
            taxonomy=_TAXONOMY,
            allowed_source_urls=[_PARENT_URL],
            annotations=_ANNOTATIONS,
        )

    assert "linkedin_organic" in str(excinfo.value)
    assert "google_search_paid" not in str(excinfo.value)


def test_a_zero_url_channel_plan_run_fails() -> None:
    """`annotations == []` is the one state that genuinely means retrieval
    returned nothing. The plan can then only be resting on carried-over
    sources, whatever it looks like — the same reasoning research already
    applies, and stated as a requirement in #65's own "done when"."""
    messages = _plan_call(_channel("google_search_paid", _PARENT_URL))

    with pytest.raises(pipeline.UngroundedRecommendationError) as excinfo:
        pipeline.extract_channel_plan(
            messages,
            taxonomy=_TAXONOMY,
            allowed_source_urls=[_PARENT_URL],
            annotations=[],
        )

    # ...and the message says WHICH of the two ungrounded shapes this is:
    # "the search found nothing" and "the search results never reached the
    # plan" need different responses from the operator reading it.
    assert "own search returned nothing" in str(excinfo.value)


def test_the_grounding_check_is_skipped_when_retrieval_is_not_known() -> None:
    """Same tri-state as provenance above, and the reason every existing
    channel-plan test keeps passing: a run this repo has no retrieval record
    for is not retroactively failed for a rule it was never run under."""
    messages = _plan_call(_channel("google_search_paid", _PARENT_URL))

    plan = pipeline.extract_channel_plan(
        messages, taxonomy=_TAXONOMY, allowed_source_urls=[_PARENT_URL], annotations=None
    )

    assert plan.channels[0].channel_key == "google_search_paid"


def test_an_ungrounded_recommendation_is_a_pipeline_error() -> None:
    """`admin_app._pipeline_error_to_http` maps the BASE class, so a new error
    type that missed it would fall through as a bare 500 rather than the 502
    every other agent-output failure produces."""
    assert issubclass(pipeline.UngroundedRecommendationError, pipeline.PipelineError)


# ── 6. The retrieval-breadth instrument, pointed at this stage ───────────────


def _breadth_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    prefix = "channel plan retrieval breadth"
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.mark.asyncio
async def test_channel_plan_retrieval_breadth_is_measured_like_researchs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#101 was settled only because somebody measured research's retrieval by
    hand and then instrumented it. "Is the channel plan grounded?" is now
    exactly the same question about exactly the same kind of evidence, and
    without the same instrument it is answerable only by the same day of
    archaeology."""
    gateway = _FakeGateway()
    causation_id, run_id = await pipeline.start_channel_plan(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )
    gateway.complete(
        run_id,
        messages=_plan_call(_channel("google_search_paid", _RETRIEVED_URL)),
        annotations=[*_ANNOTATIONS, {"type": "url_citation", "url": "https://roots.example.com/"}],
    )

    with caplog.at_level("INFO"):
        plan = await pipeline.advance_channel_plan(
            gateway,
            run_id=run_id,
            causation_id=causation_id,
            taxonomy=_TAXONOMY,
            allowed_source_urls=[_PARENT_URL],
        )

    assert plan is not None
    (record,) = _breadth_records(caplog)
    assert "2 distinct URLs across 1 grounded run(s)" in record.getMessage()
    assert record.causation_id == causation_id
    assert record.channel_plan_distinct_urls == 2
    assert record.channel_plan_deep_pages == 1
    assert record.channel_plan_site_roots == 1


@pytest.mark.asyncio
async def test_channel_plan_breadth_is_logged_when_the_run_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The path an operator most needs it on, exactly as for research: "the
    retrieval was thin" and "the run died" produce the same dead artefact."""
    gateway = _FakeGateway()
    causation_id, run_id = await pipeline.start_channel_plan(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )
    gateway.complete(run_id, status="failed", annotations=_ANNOTATIONS)

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_channel_plan(
            gateway, run_id=run_id, causation_id=causation_id, taxonomy=_TAXONOMY
        )

    (record,) = _breadth_records(caplog)
    assert record.channel_plan_distinct_urls == 1


@pytest.mark.asyncio
async def test_channel_plan_breadth_says_unmeasured_rather_than_a_false_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`0 distinct URLs` and "we have no record" must not read the same — the
    second sends nobody hunting a retrieval outage that never happened."""
    gateway = _FakeGateway()
    causation_id, run_id = await pipeline.start_channel_plan(
        gateway,
        positioning_body=_POSITIONING_BODY,
        taxonomy=_TAXONOMY_ROWS,
        campaign_motion="paid",
    )
    gateway.complete(run_id, status="failed", annotations=None)

    with caplog.at_level("INFO"), pytest.raises(pipeline.RunNotSucceededError):
        await pipeline.advance_channel_plan(
            gateway, run_id=run_id, causation_id=causation_id, taxonomy=_TAXONOMY
        )

    (record,) = _breadth_records(caplog)
    assert "not measured" in record.getMessage()
    assert record.channel_plan_distinct_urls is None
