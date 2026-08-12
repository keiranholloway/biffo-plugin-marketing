"""What the two research angles actually search for, and how broad the pool
they came back with was (issue #101).

The defect these cover is not a wrong answer — every run was green. Both
research agents were sent the byte-identical `input_payload`, and an
`:online` run's retrieval is derived from its input, not from its
instructions: the provider searches *before* the model is invoked, so every
difference between the audience and competitive prompts was invisible to the
search. Measured on campaign `ed7c5bc2`: 5 URLs each, **4 of them the same**,
6 distinct across both agents, and **zero** of the six a page rather than a
site root.

Two things are asserted here, matching the two halves of the fix:

1. The angles diverge in what gets *searched* — a property of
   `research_search_query` and of what `start_research` puts on the wire,
   both of which are checkable without a live model.
2. The breadth of what came back is measured on every run
   (`evidence_profile`), so nobody has to read provider annotations by hand
   to notice a thin evidence base again.

What these tests deliberately do NOT claim: that retrieval now returns deeper
or more numerous pages. That is the provider's answer to a better query and
only a live run can show it — see the PR body.
"""

from __future__ import annotations

from typing import Any

import pytest

from marketing import pipeline
from marketing.definitions import (
    RESEARCH_AGENT_NAMES,
    RESEARCH_AUDIENCE_AGENT_NAME,
    RESEARCH_COMPETITIVE_AGENT_NAME,
    RESEARCH_INSTRUCTIONS,
    RESEARCH_SEARCH_FRAMING,
    SEARCH_QUERY_BRIEF_CHARS,
    research_search_query,
)
from marketing.pipeline import AgentRunView, evidence_profile


class _RecordingGateway:
    """Records the payload of every requested run — the only thing these
    tests care about, since the payload IS what gets searched."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        del definition, output_tool, causation_id
        self._next_id += 1
        self.payloads[agent_name] = input_payload
        return f"run-{self._next_id}"

    async def find_chain_run(self, *, chain_id: str, agent_name: str) -> AgentRunView | None:
        del chain_id, agent_name
        return None

    async def get_agent_run(self, *, run_id: str) -> AgentRunView | None:
        del run_id
        return None


# ── The angle has to live in what gets searched ──────────────────────────────


def test_every_research_agent_has_its_own_search_framing() -> None:
    """The drift guard, matching `RESEARCH_INSTRUCTIONS`'s own coverage: an
    agent in the fan-out with no framing would silently search the brief
    alone, which is the state this issue reported."""
    assert set(RESEARCH_SEARCH_FRAMING) == set(RESEARCH_AGENT_NAMES)
    assert set(RESEARCH_INSTRUCTIONS) == set(RESEARCH_AGENT_NAMES)


def test_the_two_angles_search_for_different_things() -> None:
    brief = {"campaign_id": "c1", "brief": "Franchise management software for UK operators"}

    audience = research_search_query(agent_name=RESEARCH_AUDIENCE_AGENT_NAME, brief=brief)
    competitive = research_search_query(agent_name=RESEARCH_COMPETITIVE_AGENT_NAME, brief=brief)

    assert audience != competitive
    # Not merely different strings: different *corners of the web*. One asks
    # for where people talk, the other for what vendors publish.
    assert "forum" in audience and "review" in audience
    assert "pricing" in competitive and "comparison" in competitive
    assert "pricing" not in audience
    assert "forum" not in competitive


def test_both_angles_ask_for_pages_rather_than_home_pages() -> None:
    """Zero of the six URLs the reported run retrieved carried a path. Naming
    the page type is the only lever this repo has on that."""
    for agent_name in RESEARCH_AGENT_NAMES:
        query = research_search_query(agent_name=agent_name, brief={"brief": "anything"})
        assert "home page" in query
        assert "page" in query


def test_the_search_query_leads_with_the_campaign_topic() -> None:
    query = research_search_query(
        agent_name=RESEARCH_AUDIENCE_AGENT_NAME,
        brief={"campaign_id": "c1", "brief": "Franchise management software"},
    )

    assert query.startswith("Franchise management software —")


def test_a_long_brief_is_truncated_on_a_word_boundary() -> None:
    """The full brief still reaches the model; only the searched line is
    bounded, so the angle framing is not appended to an essay."""
    words = " ".join(["operators"] * 200)

    query = research_search_query(agent_name=RESEARCH_AUDIENCE_AGENT_NAME, brief={"brief": words})

    topic = query.split(" — ")[0]
    assert len(topic) <= SEARCH_QUERY_BRIEF_CHARS
    assert not topic.endswith("operat")  # no mid-word cut
    assert topic.endswith("operators")


def test_newlines_in_a_brief_do_not_reach_the_query() -> None:
    query = research_search_query(
        agent_name=RESEARCH_AUDIENCE_AGENT_NAME, brief={"brief": "one\n\n  two\tthree"}
    )

    assert query.startswith("one two three —")


@pytest.mark.parametrize("brief", [None, {}, {"campaign_id": "c1"}, {"brief": ""}, {"brief": 7}])
def test_a_brief_with_no_usable_text_still_yields_the_angle_framing(brief: Any) -> None:
    """A campaign's brief is free text a human wrote. None of these shapes is
    worth failing a research run over — the angle simply stands alone."""
    query = research_search_query(agent_name=RESEARCH_COMPETITIVE_AGENT_NAME, brief=brief)

    assert query == RESEARCH_SEARCH_FRAMING[RESEARCH_COMPETITIVE_AGENT_NAME]


def test_an_unknown_research_agent_is_a_hard_error() -> None:
    """Falling back to "search the brief alone" for an unrecognised agent
    would reintroduce the identical-payload defect silently."""
    with pytest.raises(KeyError):
        research_search_query(agent_name="marketing-research-nonexistent", brief={})


@pytest.mark.asyncio
async def test_start_research_sends_each_agent_a_different_search_query() -> None:
    """The regression test for the reported defect itself: the two runs used
    to go out with identical payloads, so the provider ran the same search
    twice."""
    gateway = _RecordingGateway()

    await pipeline.start_research(
        gateway,  # type: ignore[arg-type]
        brief={"campaign_id": "c1", "brief": "Franchise management software"},
    )

    queries = [payload["search_query"] for payload in gateway.payloads.values()]
    assert len(queries) == len(RESEARCH_AGENT_NAMES)
    assert len(set(queries)) == len(RESEARCH_AGENT_NAMES)


@pytest.mark.asyncio
async def test_start_research_still_sends_the_whole_brief_to_every_agent() -> None:
    """The query line is additive. The model still reads the full brief — the
    truncation is only to the text retrieval derives a query from."""
    brief = {"campaign_id": "c1", "brief": "A very long brief " * 50}
    gateway = _RecordingGateway()

    await pipeline.start_research(gateway, brief=brief)  # type: ignore[arg-type]

    for agent_name in RESEARCH_AGENT_NAMES:
        payload = gateway.payloads[agent_name]
        assert payload["brief"] == brief
        # First key: JSON key order is preserved, and the angle should lead
        # the text a search query is derived from.
        assert next(iter(payload)) == "search_query"


# ── Measuring what came back ─────────────────────────────────────────────────


def _run(*urls: str) -> AgentRunView:
    return AgentRunView(
        id="r",
        status="completed",
        annotations=[{"type": "url_citation", "url": u, "title": "t"} for u in urls],
    )


def test_evidence_profile_reports_the_reported_campaigns_shape() -> None:
    """The numbers from issue #101, fed back in: 5 URLs each, 4 shared, 6
    distinct, every one a site root. This is the state the measurement has to
    be able to describe, or it cannot show an improvement on it either."""
    audience = _run(
        "https://gorilladash.com/",
        "https://subsuite.co.uk/",
        "https://www.franchise360.co.uk/",
        "https://www.franchisebase.uk/",
        "https://www.franchisesystems.ai/",
    )
    competitive = _run(
        "https://gorilladash.com/",
        "https://subsuite.co.uk/",
        "https://www.franchise360.co.uk/",
        "https://www.franchisebase.uk/",
        "https://www.boundaryiq.co.uk/",
    )

    profile = evidence_profile([audience, competitive])

    assert profile.runs_measured == 2
    assert profile.distinct_urls == 6
    assert profile.shared_urls == 4
    assert profile.deep_pages == 0
    assert profile.site_roots == 6


def test_evidence_profile_counts_pages_with_a_path_or_query_as_deep() -> None:
    profile = evidence_profile(
        [
            _run("https://example.com/", "https://example.com/pricing"),
            _run("https://example.com/search?q=reviews"),
        ]
    )

    assert profile.distinct_urls == 3
    assert profile.deep_pages == 2
    assert profile.site_roots == 1
    assert profile.shared_urls == 0


def test_evidence_profile_treats_the_same_page_written_two_ways_as_one() -> None:
    """Same normalisation the provenance guard uses (`_url_key`) — a trailing
    slash or a differently-cased host is the same document, and counting it
    twice would report a broader pool than there is."""
    profile = evidence_profile(
        [_run("https://Example.com/Pricing"), _run("https://example.com/Pricing/")]
    )

    assert profile.distinct_urls == 1
    assert profile.shared_urls == 1


def test_evidence_profile_reads_the_nested_url_citation_shape_too() -> None:
    view = AgentRunView(
        id="r",
        status="completed",
        annotations=[{"type": "url_citation", "url_citation": {"url": "https://example.com/a"}}],
    )

    assert evidence_profile([view]).distinct_urls == 1


def test_evidence_profile_ignores_runs_that_never_grounded() -> None:
    """`annotations=None` is "not known", never "retrieved nothing" — the
    same tri-state the citation guard keeps. A chain of unknowns must report
    zero runs measured, not two runs that found nothing."""
    profile = evidence_profile([None, AgentRunView(id="r", status="completed", annotations=None)])

    assert profile.runs_measured == 0
    assert profile.distinct_urls == 0


def test_evidence_profile_of_a_genuinely_empty_retrieval() -> None:
    profile = evidence_profile([AgentRunView(id="r", status="completed", annotations=[])])

    assert profile.runs_measured == 1
    assert profile.distinct_urls == 0


def test_evidence_profile_skips_annotation_entries_with_no_url() -> None:
    # `list[Any]`, not `list[dict]`: the point of the test is that a row Core
    # handed back in an unexpected shape is skipped rather than crashing the
    # measurement, and one of these entries is deliberately not a dict at all.
    annotations: list[Any] = [{"type": "url_citation"}, "not-a-dict", {"url": ""}]
    view = AgentRunView(id="r", status="completed", annotations=annotations)

    assert evidence_profile([view]).distinct_urls == 0
