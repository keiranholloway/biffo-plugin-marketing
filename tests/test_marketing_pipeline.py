"""The citation-enforcement guard (M3, extended to channel planning in M4) —
the milestone, not a detail.

Biffo's agent `web_search` is silently unavailable on dev (an empty Brave
key), and an agent given a tool it cannot use does not error — it fabricates.
Audience research is precisely where that is invisible: plausible, confident,
well-structured, and entirely invented. Channel planning (issue #3) is
arguably worse: "run paid social" reads as correct whether or not anyone
researched it, because it is indistinguishable from generic marketing advice.

These tests are the fail-first proof for the guard: `extract_research_synthesis`,
`extract_positioning` and `extract_channel_plan` must refuse to hand back an
artefact whose entire output cites nothing, rather than let it through as a
"thin but valid" result. Written before `marketing.pipeline` existed, so the
first commit in this milestone's history fails on `ModuleNotFoundError` — the
second commit is the implementation that makes it pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from marketing.pipeline import (
    MalformedOutputError,
    NoCitationsError,
    extract_channel_plan,
    extract_positioning,
    extract_research_findings,
    extract_research_synthesis,
    flatten_citations,
)


def _tool_call(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """One assistant message carrying a single tool call, in the shape the
    agent runtime's transcript stores it — copied from idea-scout's
    `_tool_call_arguments` contract (`function.name` / `function.arguments`)."""
    return [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": tool_name, "arguments": arguments}}],
        }
    ]


# ── The guard: zero citations fails outright ─────────────────────────────────


def test_research_synthesis_with_no_findings_raises_no_citations() -> None:
    """A research run that fetched nothing must fail, not propose an artefact
    with an empty (but structurally valid) findings list. This is the literal
    "zero URLs fetched" case: both research agents came back empty, so there is
    nothing to synthesise and nothing to show an operator."""
    messages = _tool_call(
        "submit_research_synthesis",
        {"summary": "Nothing usable was found.", "findings": []},
    )

    with pytest.raises(NoCitationsError):
        extract_research_synthesis(messages)


def test_positioning_with_no_segments_pillars_or_ctas_raises_no_citations() -> None:
    """Same guard, the positioning half: an artefact with nothing in any of the
    three lists cites nothing and must not be proposed."""
    messages = _tool_call("submit_positioning", {"segments": [], "pillars": [], "ctas": []})

    with pytest.raises(NoCitationsError):
        extract_positioning(messages)


def test_channel_plan_with_no_channels_raises_no_citations() -> None:
    """Same guard, the channel-plan half (M4, issue #3): an empty channel list
    cites nothing and must not be proposed to an operator as evidenced."""
    messages = _tool_call("submit_channel_plan", {"channels": []})

    with pytest.raises(NoCitationsError):
        extract_channel_plan(messages)


# ── The guard does not fire on genuine content ───────────────────────────────


def test_research_synthesis_with_a_grounded_finding_succeeds() -> None:
    messages = _tool_call(
        "submit_research_synthesis",
        {
            "summary": "One clear signal from both angles.",
            "findings": [
                {
                    "signal": "Operators managing 2+ locations ask for this specifically.",
                    "why_it_matters": "It is the segment most likely to convert first.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                }
            ],
        },
    )

    synthesis = extract_research_synthesis(messages)

    assert synthesis.summary == "One clear signal from both angles."
    assert len(synthesis.findings) == 1
    assert synthesis.findings[0].sources[0].url == "https://example.com/thread"


def test_positioning_with_a_grounded_segment_succeeds() -> None:
    messages = _tool_call(
        "submit_positioning",
        {
            "segments": [
                {
                    "name": "Multi-location operators",
                    "description": "Run 2+ sites and feel the coordination pain first.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                }
            ],
            "pillars": [],
            "ctas": [],
        },
    )

    positioning = extract_positioning(messages)

    assert len(positioning.segments) == 1
    assert positioning.segments[0].name == "Multi-location operators"


def test_channel_plan_with_organic_and_paid_channels_succeeds() -> None:
    """Both motions are representable in one call, and citations are checked
    in aggregate across the whole list, not per-motion."""
    messages = _tool_call(
        "submit_channel_plan",
        {
            "channels": [
                {
                    "channel": "Instagram Reels",
                    "motion": "organic",
                    "rank": 1,
                    "rationale": "Multi-location operators already discuss this there.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                },
                {
                    "channel": "Google Search ads",
                    "motion": "paid",
                    "rank": 1,
                    "rationale": "Captures the specific search intent the research surfaced.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                },
            ]
        },
    )

    plan = extract_channel_plan(messages)

    assert {c.motion for c in plan.channels} == {"organic", "paid"}


# ── Missing or malformed tool calls are a different failure ─────────────────


def test_research_synthesis_with_no_tool_call_raises_malformed_not_no_citations() -> None:
    """A run that never called the tool at all is a different failure from one
    that called it honestly with nothing to cite — the operator-facing message
    should not conflate "the model broke" with "the research came back empty"."""
    with pytest.raises(MalformedOutputError):
        extract_research_synthesis([{"role": "assistant", "content": "Here is my summary..."}])


def test_positioning_with_no_tool_call_raises_malformed() -> None:
    with pytest.raises(MalformedOutputError):
        extract_positioning([{"role": "assistant", "content": "Here is my positioning..."}])


def test_channel_plan_with_no_tool_call_raises_malformed() -> None:
    with pytest.raises(MalformedOutputError):
        extract_channel_plan([{"role": "assistant", "content": "Here is my channel plan..."}])


def test_research_finding_cannot_be_built_with_empty_sources() -> None:
    """The structural half of the guard, exercised directly: unlike idea-scout's
    `Finding.sources` (which defaults to `[]`), a `ResearchFinding` with no
    source cannot be constructed at all."""
    from pydantic import ValidationError

    from marketing.definitions import ResearchFinding

    with pytest.raises(ValidationError):
        ResearchFinding(signal="x", why_it_matters="y", sources=[])


def test_channel_recommendation_cannot_be_built_with_empty_sources() -> None:
    """Same structural half of the guard, for `ChannelRecommendation` (M4)."""
    from pydantic import ValidationError

    from marketing.definitions import ChannelRecommendation

    with pytest.raises(ValidationError):
        ChannelRecommendation(
            channel="Instagram Reels", motion="organic", rank=1, rationale="x", sources=[]
        )


# ── extract_research_findings — per-angle, degrades rather than fails ────────
#
# Unlike the synthesis/positioning extractors above, one research angle
# returning nothing usable is tolerated (idea-scout's own rationale: a single
# angle finding little is a thinner result, not a failed run). This is not
# currently called anywhere in `pipeline.py` — the research-synthesis agent
# reads the raw fan-in payload itself, not this plugin — kept for the same
# reason idea-scout keeps its own equivalent (`service.extract_findings`,
# also uncalled outside its own tests): a future admin view showing what one
# research angle actually found needs exactly this.


def test_extract_research_findings_returns_none_for_a_missing_tool_call() -> None:
    assert extract_research_findings([{"role": "assistant", "content": "..."}]) is None


def test_extract_research_findings_returns_none_for_an_invalid_tool_call() -> None:
    """A finding with no source fails Pydantic validation — degraded, not
    raised, unlike the aggregate synthesis/positioning guard."""
    messages = _tool_call(
        "submit_research_findings",
        {"angle": "audience", "findings": [{"signal": "x", "why_it_matters": "y", "sources": []}]},
    )
    assert extract_research_findings(messages) is None


def test_extract_research_findings_returns_the_set_when_valid() -> None:
    messages = _tool_call(
        "submit_research_findings",
        {
            "angle": "audience",
            "findings": [
                {
                    "signal": "x",
                    "why_it_matters": "y",
                    "sources": [{"url": "https://example.com/z", "note": "n"}],
                }
            ],
        },
    )

    result = extract_research_findings(messages)

    assert result is not None
    assert result.angle == "audience"
    assert result.findings[0].sources[0].url == "https://example.com/z"


# ── flatten_citations — dispatches on the result TYPE, not a `kind` string ───


def test_flatten_citations_of_a_channel_plan_dedupes_by_url() -> None:
    plan = extract_channel_plan(
        _tool_call(
            "submit_channel_plan",
            {
                "channels": [
                    {
                        "channel": "Instagram Reels",
                        "motion": "organic",
                        "rank": 1,
                        "rationale": "r",
                        "sources": [{"url": "https://example.com/dup", "note": "a"}],
                    },
                    {
                        "channel": "Google Search ads",
                        "motion": "paid",
                        "rank": 1,
                        "rationale": "r",
                        "sources": [{"url": "https://example.com/dup", "note": "b"}],
                    },
                ]
            },
        )
    )

    citations = flatten_citations(plan)

    assert citations == [{"url": "https://example.com/dup", "note": "a"}]


def test_flatten_citations_raises_on_an_unsupported_output_type() -> None:
    """A defensive check, not a reachable path today: every real caller only
    ever passes what `extract_research_synthesis`/`extract_positioning`/
    `extract_channel_plan` return. Exercised directly so a fourth artefact
    type added here without its own branch fails loudly instead of quietly
    reusing `ChannelPlan`'s `.channels` shape."""
    with pytest.raises(TypeError):
        flatten_citations(object())  # type: ignore[arg-type]
