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
    UncitedSourceError,
    citation_source_urls,
    extract_channel_plan,
    extract_copy,
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
        extract_channel_plan(messages, taxonomy={})


def test_copy_with_no_channels_raises_no_citations() -> None:
    """Same guard, the copy half (M5, issue #4): an empty channel list cites
    nothing and must not be proposed to an operator as publish-ready."""
    messages = _tool_call("submit_copy", {"channels": []})

    with pytest.raises(NoCitationsError):
        extract_copy(messages, channel_plan_channels={})


# ── The guard's message is grounded in `annotations`, not just `sources`
# (issue #82) ─────────────────────────────────────────────────────────────────
#
# OpenRouter returns `:online` grounding citations out of band, on
# `message.annotations` (biffo-template#1528/#1530) — a fact about the RUN,
# independent of whatever the model chose to retype into its structured tool
# call. Before this, a run that genuinely retrieved evidence and a run that
# never retrieved anything were indistinguishable the moment the model failed
# to transcribe: both produced empty `sources` and the identical "fetched
# zero URLs" message, which is false for the first. `annotations` is what
# makes the two tell apart. `extract_research_synthesis` stands in for all
# four `_extract_cited_artefact` callers below since they share one
# implementation — proven directly on it, then spot-checked on the other
# three so a change to any one extractor's wiring cannot silently drop the
# `annotations` argument.

_A_URL_CITATION = {"type": "url_citation", "url": "https://example.com/found", "title": "Found"}


def test_empty_sources_with_null_annotations_reads_as_not_known_not_as_zero() -> None:
    """`annotations=None` — this run predates the annotations column, or was
    never a grounded (`:online`) run. The guard still fails (empty `sources`
    is still empty `sources`; this is not a new pass condition), but the
    message must not claim retrieval fetched zero URLs, because that is not
    established. Deliberately the SAME failure as before this issue's fix —
    a pre-upgrade artefact is not retroactively treated as a proven-zero
    retrieval, only described honestly as an unknown one."""
    messages = _tool_call(
        "submit_research_synthesis", {"summary": "Nothing usable was found.", "findings": []}
    )

    with pytest.raises(NoCitationsError) as excinfo:
        extract_research_synthesis(messages, annotations=None)

    detail = str(excinfo.value)
    assert "not known" in detail
    assert "fetched zero URLs" in detail  # the base sentence is kept, not replaced


def test_empty_sources_with_empty_annotations_is_a_genuine_zero_url_retrieval() -> None:
    """`annotations=[]` — the run WAS asked to ground and retrieval genuinely
    found nothing. This is the one case where the original "fetched zero
    URLs, try again" message is fully accurate, so it is used unchanged, and
    retrying is explicitly not promised to help."""
    messages = _tool_call(
        "submit_research_synthesis", {"summary": "Nothing usable was found.", "findings": []}
    )

    with pytest.raises(NoCitationsError) as excinfo:
        extract_research_synthesis(messages, annotations=[])

    detail = str(excinfo.value)
    assert detail == (
        "The research run fetched zero URLs. Nothing was found to cite, so no "
        "artefact was produced — try running research again."
    )


def test_empty_sources_with_non_empty_annotations_is_a_transcription_not_a_dead_search() -> None:
    """`annotations` non-empty, `sources` empty — the defect issue #82 reports:
    retrieval worked, the model did not transcribe it. This must NOT read as
    "fetched zero URLs" (false — it fetched some) and must be phrased as
    transient, since retrying is likely to produce a citable result without
    the brief changing at all."""
    messages = _tool_call(
        "submit_research_synthesis", {"summary": "Nothing usable was found.", "findings": []}
    )

    with pytest.raises(NoCitationsError) as excinfo:
        extract_research_synthesis(messages, annotations=[_A_URL_CITATION])

    detail = str(excinfo.value)
    assert "fetched zero URLs" not in detail
    assert "1 source" in detail
    assert "transcription failure" in detail
    assert "try running research again" in detail


def test_the_three_annotation_cases_produce_three_different_messages() -> None:
    """The whole point: an operator (or a log line) must be able to tell the
    three cases apart without reading code. Guards against a future edit that
    reconverges them onto one message string."""
    messages = _tool_call(
        "submit_research_synthesis", {"summary": "Nothing usable was found.", "findings": []}
    )

    def _message(annotations: list[dict[str, Any]] | None) -> str:
        with pytest.raises(NoCitationsError) as excinfo:
            extract_research_synthesis(messages, annotations=annotations)
        return str(excinfo.value)

    null_message = _message(None)
    empty_message = _message([])
    found_message = _message([_A_URL_CITATION])

    assert len({null_message, empty_message, found_message}) == 3


def test_non_empty_annotations_does_not_salvage_into_sources_and_still_fails() -> None:
    """The decision this issue asks to be argued: salvaging `annotations` into
    a finding's `sources` is tempting and wrong, because an annotation is not
    attributed to any particular claim — attaching one to a finding would
    fabricate the exact claim-to-evidence mapping the citation discipline
    exists to guarantee. So a run with rich `annotations` but empty `sources`
    still raises `NoCitationsError`; nothing manufactures a `Source` from an
    annotation on its behalf."""
    messages = _tool_call(
        "submit_research_synthesis", {"summary": "Nothing usable was found.", "findings": []}
    )

    with pytest.raises(NoCitationsError):
        extract_research_synthesis(
            messages,
            annotations=[
                _A_URL_CITATION,
                {"type": "url_citation", "url": "https://example.com/other", "title": "Other"},
            ],
        )


def test_positioning_wires_annotations_into_its_message() -> None:
    """Spot check on `extract_positioning`: the other three `_extract_cited_
    artefact` callers share the same implementation as research-synthesis, but
    a future refactor could drop the argument from one of them silently — this
    proves it is actually wired, not just present in the signature."""
    messages = _tool_call("submit_positioning", {"segments": [], "pillars": [], "ctas": []})

    with pytest.raises(NoCitationsError) as excinfo:
        extract_positioning(messages, annotations=[_A_URL_CITATION])

    assert "transcription failure" in str(excinfo.value)


def test_channel_plan_wires_annotations_into_its_message() -> None:
    messages = _tool_call("submit_channel_plan", {"channels": []})

    with pytest.raises(NoCitationsError) as excinfo:
        extract_channel_plan(messages, taxonomy={}, annotations=[_A_URL_CITATION])

    assert "transcription failure" in str(excinfo.value)


def test_copy_wires_annotations_into_its_message() -> None:
    messages = _tool_call("submit_copy", {"channels": []})

    with pytest.raises(NoCitationsError) as excinfo:
        extract_copy(messages, channel_plan_channels={}, annotations=[_A_URL_CITATION])

    assert "transcription failure" in str(excinfo.value)


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
    in aggregate across the whole list, not per-motion. Motion is DERIVED
    from the taxonomy (#76 increment 2), so neither entry's own `motion`
    field is trusted — omitted entirely here to prove that."""
    messages = _tool_call(
        "submit_channel_plan",
        {
            "channels": [
                {
                    "channel_key": "instagram_organic",
                    "rank": 1,
                    "rationale": "Multi-location operators already discuss this there.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                },
                {
                    "channel_key": "google_search_paid",
                    "rank": 1,
                    "rationale": "Captures the specific search intent the research surfaced.",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                },
            ]
        },
    )

    plan = extract_channel_plan(
        messages, taxonomy={"instagram_organic": "organic", "google_search_paid": "paid"}
    )

    assert {c.motion for c in plan.channels} == {"organic", "paid"}


def test_copy_with_a_grounded_channel_succeeds() -> None:
    messages = _tool_call(
        "submit_copy",
        {
            "channels": [
                {
                    "channel_key": "instagram_organic",
                    "headline": "Run every site the same way, finally.",
                    "body": "One dashboard, every location, no more group chats.",
                    "cta": "See how it works",
                    "sources": [{"url": "https://example.com/thread", "note": "A forum thread."}],
                }
            ]
        },
    )

    copy = extract_copy(messages, channel_plan_channels={"instagram_organic": "organic"})

    assert len(copy.channels) == 1
    assert copy.channels[0].channel_key == "instagram_organic"
    assert copy.channels[0].motion == "organic"
    assert copy.channels[0].sources[0].url == "https://example.com/thread"


# ── Provenance: a cited URL must have come from this stage's own input
# (issue #22) ─────────────────────────────────────────────────────────────────
#
# The guard above counts citations; it never asked where they came from. For
# `positioning`, `channel_plan` and `copy` the legitimate source set is closed
# and already known — it is exactly the approved parent artefact's `citations`
# column — so an agent that invents one plausible-looking URL per claim
# (a well-known marketing-statistics domain, say) sails past `total == 0` and
# is proposed to an operator as evidenced when it is not.
#
# `research` is deliberately NOT given this check: the web is its source, so
# there is no closed prior set to check against.

#: What the approved parent artefact actually cited — the whole legitimate
#: source set for the stage below it.
_APPROVED = ["https://example.com/thread", "https://example.com/report"]

#: Plausible, well-formed, and never in `_APPROVED` — the fabrication shape
#: this issue exists for.
_FABRICATED = "https://marketing-statistics.example/2026-benchmarks"


def _positioning_args(*urls: str) -> dict[str, Any]:
    return {
        "segments": [
            {
                "name": "Multi-location operators",
                "description": "Run 2+ sites and feel the coordination pain first.",
                "sources": [{"url": url, "note": ""} for url in urls],
            }
        ],
        "pillars": [],
        "ctas": [],
    }


def _channel_plan_args(*urls: str) -> dict[str, Any]:
    return {
        "channels": [
            {
                "channel_key": "instagram_organic",
                "rank": 1,
                "rationale": "Multi-location operators already discuss this there.",
                "sources": [{"url": url, "note": ""} for url in urls],
            }
        ]
    }


def _copy_args(*urls: str) -> dict[str, Any]:
    return {
        "channels": [
            {
                "channel_key": "instagram_organic",
                "headline": "Run every site the same way, finally.",
                "body": "One dashboard, every location, no more group chats.",
                "cta": "See how it works",
                "sources": [{"url": url, "note": ""} for url in urls],
            }
        ]
    }


def test_positioning_citing_a_url_the_approved_research_never_contained_is_refused() -> None:
    """The defect: one fabricated URL per claim passes the count-only guard.
    It must not — a positioning segment can only cite what the approved
    research it was given actually contained."""
    messages = _tool_call("submit_positioning", _positioning_args(_FABRICATED))

    with pytest.raises(UncitedSourceError) as excinfo:
        extract_positioning(messages, allowed_source_urls=_APPROVED)

    detail = str(excinfo.value)
    assert _FABRICATED in detail, "the operator must be told WHICH url was not in the input"
    assert "approved research" in detail


def test_positioning_citing_only_approved_research_urls_succeeds() -> None:
    """The guard must not fire on the normal case: every source is one the
    approved research actually carried."""
    messages = _tool_call("submit_positioning", _positioning_args(*_APPROVED))

    positioning = extract_positioning(messages, allowed_source_urls=_APPROVED)

    assert {s.url for s in positioning.segments[0].sources} == set(_APPROVED)


def test_positioning_is_refused_even_when_it_also_cites_a_real_url() -> None:
    """The realistic shape, and the reason a per-source check is needed rather
    than "does it cite anything from the parent": an agent that copies one
    genuine URL and invents a second still produces a claim backed by nothing.
    A single fabricated source fails the whole artefact."""
    messages = _tool_call("submit_positioning", _positioning_args(_APPROVED[0], _FABRICATED))

    with pytest.raises(UncitedSourceError):
        extract_positioning(messages, allowed_source_urls=_APPROVED)


def test_channel_plan_citing_a_url_the_approved_positioning_never_contained_is_refused() -> None:
    """The same check one stage down, which is why this issue was filed during
    M4: channel advice reads as generic wisdom whether or not anyone
    researched it, so an invented citation is least visible here."""
    messages = _tool_call("submit_channel_plan", _channel_plan_args(_FABRICATED))

    with pytest.raises(UncitedSourceError) as excinfo:
        extract_channel_plan(
            messages,
            taxonomy={"instagram_organic": "organic"},
            allowed_source_urls=_APPROVED,
        )

    assert "approved positioning" in str(excinfo.value)


def test_channel_plan_citing_only_approved_positioning_urls_succeeds() -> None:
    messages = _tool_call("submit_channel_plan", _channel_plan_args(*_APPROVED))

    plan = extract_channel_plan(
        messages, taxonomy={"instagram_organic": "organic"}, allowed_source_urls=_APPROVED
    )

    assert plan.channels[0].motion == "organic"


def test_copy_citing_a_url_its_approved_inputs_never_contained_is_refused() -> None:
    """M5's stage has the same closed source set (the approved positioning and
    channel plan it was started against), so it gets the same check rather
    than inheriting the gap one stage further down."""
    messages = _tool_call("submit_copy", _copy_args(_FABRICATED))

    with pytest.raises(UncitedSourceError):
        extract_copy(
            messages,
            channel_plan_channels={"instagram_organic": "organic"},
            allowed_source_urls=_APPROVED,
        )


def test_copy_citing_only_approved_urls_succeeds() -> None:
    messages = _tool_call("submit_copy", _copy_args(_APPROVED[1]))

    result = extract_copy(
        messages,
        channel_plan_channels={"instagram_organic": "organic"},
        allowed_source_urls=_APPROVED,
    )

    assert result.channels[0].sources[0].url == _APPROVED[1]


def test_an_unknown_approved_set_skips_the_check_rather_than_failing_everything() -> None:
    """`None` is "the approved set is not known", never "nothing is allowed" —
    the same tri-state discipline `annotations` already follows. An artefact
    started before this check existed carries no stashed set, and must still
    be able to advance; failing it closed would strand every in-flight run at
    deploy time on evidence nobody has."""
    messages = _tool_call("submit_positioning", _positioning_args(_FABRICATED))

    positioning = extract_positioning(messages, allowed_source_urls=None)

    assert positioning.segments[0].sources[0].url == _FABRICATED


def test_an_empty_approved_set_also_skips_the_check() -> None:
    """An approved parent with zero citations cannot exist — it would have
    failed the zero-citation guard before it could be proposed — so an empty
    set means a legacy or hand-edited row, i.e. unknown, not "cite nothing"."""
    messages = _tool_call("submit_positioning", _positioning_args(_FABRICATED))

    assert extract_positioning(messages, allowed_source_urls=[]) is not None


def test_a_trailing_slash_or_fragment_is_not_treated_as_a_fabrication() -> None:
    """Provenance is about the document, not the byte string. A model that
    re-types the same URL with a trailing slash, a fragment, or a
    differently-cased host has cited the parent's source, and failing it would
    make the guard fire on honest runs — which is how a guard gets turned off.
    """
    messages = _tool_call(
        "submit_positioning", _positioning_args("https://EXAMPLE.com/thread/#section-2")
    )

    assert extract_positioning(messages, allowed_source_urls=_APPROVED) is not None


def test_a_different_path_on_an_allowed_host_is_still_a_fabrication() -> None:
    """The normalisation above must not soften into "same domain is good
    enough": citing a real publisher's home page for a statistic that lives
    nowhere in the parent is exactly the fabrication being caught."""
    messages = _tool_call("submit_positioning", _positioning_args("https://example.com/invented"))

    with pytest.raises(UncitedSourceError):
        extract_positioning(messages, allowed_source_urls=_APPROVED)


def test_research_synthesis_has_no_provenance_check_because_the_web_is_its_source() -> None:
    """Deliberate asymmetry, stated as a test so a future "make it uniform"
    refactor has to argue with it: research has no closed prior set — it is
    the stage that goes and finds the URLs — so there is nothing to check a
    citation against."""
    import inspect

    assert "allowed_source_urls" not in inspect.signature(extract_research_synthesis).parameters


# ── citation_source_urls — reading the parent's `citations` column ───────────


def test_citation_source_urls_reads_the_json_string_core_stores() -> None:
    """`marketing_artefact.citations` is written as a JSON string
    (`_advance_artefact`'s `json.dumps(flatten_citations(...))`), so the
    routes' natural read is a string."""
    citations = '[{"url": "https://example.com/a", "note": "n"}, {"url": "https://example.com/b"}]'

    assert citation_source_urls(citations) == ["https://example.com/a", "https://example.com/b"]


def test_citation_source_urls_reads_an_already_parsed_list_too() -> None:
    parsed = [{"url": "https://example.com/a", "note": "n"}]

    assert citation_source_urls(parsed) == ["https://example.com/a"]


def test_citation_source_urls_of_an_absent_or_unreadable_column_is_empty() -> None:
    """Not known, rather than an exception: a legacy row with no citations
    column must not 502 the stage that reads it — it degrades to the
    check-skipped case above."""
    assert citation_source_urls(None) == []
    assert citation_source_urls("not json at all") == []
    assert citation_source_urls([{"note": "no url key"}]) == []


def test_citation_source_urls_dedupes_and_keeps_first_seen_order() -> None:
    citations = [
        {"url": "https://example.com/b"},
        {"url": "https://example.com/a"},
        {"url": "https://example.com/b"},
    ]

    assert citation_source_urls(citations) == ["https://example.com/b", "https://example.com/a"]


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
        extract_channel_plan(
            [{"role": "assistant", "content": "Here is my channel plan..."}], taxonomy={}
        )


def test_copy_with_no_tool_call_raises_malformed() -> None:
    with pytest.raises(MalformedOutputError):
        extract_copy(
            [{"role": "assistant", "content": "Here is my copy..."}], channel_plan_channels={}
        )


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
        ChannelRecommendation(channel_key="instagram_organic", rank=1, rationale="x", sources=[])


def test_channel_copy_cannot_be_built_with_empty_sources() -> None:
    """Same structural half of the guard, for `ChannelCopy` (M5)."""
    from pydantic import ValidationError

    from marketing.definitions import ChannelCopy

    with pytest.raises(ValidationError):
        ChannelCopy(
            channel_key="instagram_organic",
            headline="h",
            body="b",
            cta="c",
            sources=[],
        )


def test_segment_cannot_be_built_with_empty_sources() -> None:
    """Same structural half of the guard, for `Segment` (M3 positioning) —
    the one the duplicated-links defect touched directly: fixing how sources
    are rendered/attached downstream must not loosen this `min_length=1`."""
    from pydantic import ValidationError

    from marketing.definitions import Segment

    with pytest.raises(ValidationError):
        Segment(name="x", description="y", sources=[])


def test_message_pillar_cannot_be_built_with_empty_sources() -> None:
    """Same structural half of the guard, for `MessagePillar` (M3 positioning)."""
    from pydantic import ValidationError

    from marketing.definitions import MessagePillar

    with pytest.raises(ValidationError):
        MessagePillar(pillar="x", rationale="y", sources=[])


def test_call_to_action_cannot_be_built_with_empty_sources() -> None:
    """Same structural half of the guard, for `CallToAction` (M3 positioning)."""
    from pydantic import ValidationError

    from marketing.definitions import CallToAction

    with pytest.raises(ValidationError):
        CallToAction(text="x", rationale="y", sources=[])


def test_source_note_may_be_empty_but_url_still_cannot() -> None:
    """The de-duplication fix (definitions.py's `POSITIONING_INSTRUCTIONS` /
    `CHANNEL_PLAN_INSTRUCTIONS` / `COPY_INSTRUCTIONS`) now tells downstream
    agents to leave `note` empty rather than paste a research note verbatim —
    so `note` must genuinely accept `""`. `url` must not: it is the actual
    citation, `Field(min_length=1)`, and nothing about this fix should touch
    that half of the guard."""
    from pydantic import ValidationError

    from marketing.definitions import Segment, Source

    # note="" is a legitimate downstream citation now — must not raise.
    segment = Segment(
        name="x", description="y", sources=[Source(url="https://example.com/a", note="")]
    )
    assert segment.sources[0].note == ""

    # url="" must still be rejected — the guard's actual teeth are unchanged.
    with pytest.raises(ValidationError):
        Source(url="", note="")


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
                        "channel_key": "instagram_organic",
                        "rank": 1,
                        "rationale": "r",
                        "sources": [{"url": "https://example.com/dup", "note": "a"}],
                    },
                    {
                        "channel_key": "google_search_paid",
                        "rank": 1,
                        "rationale": "r",
                        "sources": [{"url": "https://example.com/dup", "note": "b"}],
                    },
                ]
            },
        ),
        taxonomy={"instagram_organic": "organic", "google_search_paid": "paid"},
    )

    citations = flatten_citations(plan)

    assert citations == [{"url": "https://example.com/dup", "note": "a"}]


def test_flatten_citations_of_copy_dedupes_by_url() -> None:
    copy = extract_copy(
        _tool_call(
            "submit_copy",
            {
                "channels": [
                    {
                        "channel_key": "instagram_organic",
                        "headline": "h1",
                        "body": "b1",
                        "cta": "c1",
                        "sources": [{"url": "https://example.com/dup", "note": "a"}],
                    },
                    {
                        "channel_key": "google_search_paid",
                        "headline": "h2",
                        "body": "b2",
                        "cta": "c2",
                        "sources": [{"url": "https://example.com/dup", "note": "b"}],
                    },
                ]
            },
        ),
        channel_plan_channels={"instagram_organic": "organic", "google_search_paid": "paid"},
    )

    citations = flatten_citations(copy)

    assert citations == [{"url": "https://example.com/dup", "note": "a"}]


def test_flatten_citations_raises_on_an_unsupported_output_type() -> None:
    """A defensive check, not a reachable path today: every real caller only
    ever passes what `extract_research_synthesis`/`extract_positioning`/
    `extract_channel_plan` return. Exercised directly so a fourth artefact
    type added here without its own branch fails loudly instead of quietly
    reusing `ChannelPlan`'s `.channels` shape."""
    with pytest.raises(TypeError):
        flatten_citations(object())  # type: ignore[arg-type]


# ── #76 increment 2: channel ids, replacing free-text channel strings ───────
#
# #75's actual failure: a 121-character agent-generated channel name
# overflowed `marketing_link.channel` (`String(64)`), and nothing in the
# pipeline bounded it — every existing fixture used a short, hand-written
# name like "Instagram Reels", so the suite had never once exercised a
# realistic one. These tests use that EXACT 121-character string.

_OVERLONG_CHANNEL_NAME = (
    "Google Search ads (non-brand: terms like 'franchise management "
    "software UK', 'franchise operations pricing per location')"
)
assert len(_OVERLONG_CHANNEL_NAME) == 121  # guard the guard: this must still be the #75 case


def test_a_121_character_agent_generated_name_is_fine_as_a_proposal() -> None:
    """The #75 name, reused as a `suggested_label` proposal rather than a
    `channel_key`. Proposals are free text on purpose (#67's "the agent may
    add"), so an overlong one must not be structurally impossible to
    construct — what makes #75 impossible is that a proposal can never reach
    `marketing_link.channel` un-vetted (see
    `test_a_proposal_cannot_be_written_as_copy_because_it_has_no_channel_key`
    below), not that the label itself is bounded."""
    from marketing.definitions import ChannelRecommendation, Source

    recommendation = ChannelRecommendation(
        suggested_label=_OVERLONG_CHANNEL_NAME,
        motion="paid",
        rank=1,
        rationale="Strong evidence, but not in the taxonomy given.",
        sources=[Source(url="https://example.com/z", note="n")],
    )

    assert recommendation.channel_key is None
    assert recommendation.suggested_label == _OVERLONG_CHANNEL_NAME


def test_a_proposal_cannot_be_written_as_copy_because_it_has_no_channel_key() -> None:
    """The structural half of "#75 cannot recur": `ChannelCopy.channel_key`
    is required, and a proposal (`suggested_label`, no `channel_key`) has
    nothing to satisfy it with. Copy — and, downstream, `marketing_link` via
    `pack_routes._ensure_links` — can only ever be written for a real,
    taxonomy-seeded (short-by-construction) key."""
    import inspect

    from marketing.definitions import ChannelCopy

    assert "channel_key" in inspect.signature(ChannelCopy).parameters
    assert "channel" not in ChannelCopy.model_fields  # the old free-text field is gone


def test_channel_recommendation_requires_exactly_one_of_channel_key_or_suggested_label() -> None:
    from pydantic import ValidationError

    from marketing.definitions import ChannelRecommendation, Source

    sources = [Source(url="https://example.com/z", note="n")]

    with pytest.raises(ValidationError):  # neither set
        ChannelRecommendation(rank=1, rationale="r", sources=sources)

    with pytest.raises(ValidationError):  # both set
        ChannelRecommendation(
            channel_key="instagram_organic",
            suggested_label="Reddit r/franchise",
            motion="paid",
            rank=1,
            rationale="r",
            sources=sources,
        )


def test_a_proposal_requires_its_own_motion_but_a_real_channel_does_not() -> None:
    """Motion is derived from the taxonomy for a real `channel_key`
    (`extract_channel_plan` overrides it regardless), so the agent need not
    assert it there — but for a `suggested_label` proposal there is no
    taxonomy row to derive it from, so it is the one case motion is
    required."""
    from pydantic import ValidationError

    from marketing.definitions import ChannelRecommendation, Source

    sources = [Source(url="https://example.com/z", note="n")]

    with pytest.raises(ValidationError):
        ChannelRecommendation(
            suggested_label="Reddit r/franchise", rank=1, rationale="r", sources=sources
        )  # no motion — must fail

    # A real channel_key needs no motion at all.
    recommendation = ChannelRecommendation(
        channel_key="instagram_organic", rank=1, rationale="r", sources=sources
    )
    assert recommendation.motion is None


def test_channel_plan_rejects_a_channel_key_outside_the_taxonomy_it_was_given() -> None:
    """`channel_key` is 'constrained to seeded values' (#76) — enforced, not
    merely documented: an agent inventing or mangling a key must fail loudly
    rather than silently writing an unrecognised key downstream."""
    from marketing.pipeline import UnknownChannelError

    messages = _tool_call(
        "submit_channel_plan",
        {
            "channels": [
                {
                    "channel_key": "a_key_that_was_never_offered",
                    "rank": 1,
                    "rationale": "r",
                    "sources": [{"url": "https://example.com/z", "note": "n"}],
                }
            ]
        },
    )

    with pytest.raises(UnknownChannelError):
        extract_channel_plan(messages, taxonomy={"instagram_organic": "organic"})


def test_copy_cannot_reference_a_channel_the_plan_did_not_include() -> None:
    """The exact behaviour #67/#75 exist to make structural: 'the copy stage
    cannot reference a channel the plan did not include.' Unlike the old
    prose contract ("Must match a channel from the approved channel plan
    exactly"), this is now enforced in code."""
    from marketing.pipeline import UnknownChannelError

    messages = _tool_call(
        "submit_copy",
        {
            "channels": [
                {
                    "channel_key": "google_search_paid",  # NOT in the plan given below
                    "headline": "h",
                    "body": "b",
                    "cta": "c",
                    "sources": [{"url": "https://example.com/z", "note": "n"}],
                }
            ]
        },
    )

    with pytest.raises(UnknownChannelError):
        extract_copy(messages, channel_plan_channels={"instagram_organic": "organic"})


def test_channel_plan_channel_map_excludes_proposals_and_derives_from_real_entries() -> None:
    from marketing.pipeline import channel_plan_channel_map

    plan_body = {
        "channels": [
            {"channel_key": "instagram_organic", "motion": "organic"},
            {"suggested_label": "Reddit r/franchise", "motion": "paid"},  # a proposal, no key
        ]
    }

    assert channel_plan_channel_map(plan_body) == {"instagram_organic": "organic"}


def test_channel_plan_channel_map_raises_on_a_pre_migration_plan() -> None:
    """The existing-dev-data guard (#76 increment 2): a plan with entries but
    NONE carrying a `channel_key` is the pre-migration free-text shape, not a
    plan that legitimately recommends nothing."""
    from marketing.pipeline import StaleChannelPlanError, channel_plan_channel_map

    plan_body = {"channels": [{"channel": _OVERLONG_CHANNEL_NAME, "motion": "paid"}]}

    with pytest.raises(StaleChannelPlanError):
        channel_plan_channel_map(plan_body)


def test_channel_plan_channel_map_does_not_raise_on_a_genuinely_empty_plan() -> None:
    """A plan that recommends nothing at all (`channels: []`) is a normal,
    if unusual, outcome — not the pre-migration case."""
    from marketing.pipeline import channel_plan_channel_map

    assert channel_plan_channel_map({"channels": []}) == {}
    assert channel_plan_channel_map({}) == {}


def test_require_channel_keyed_copy_raises_on_a_pre_migration_copy_artefact() -> None:
    """Same guard, the pack-assembly call site: `pack_routes`/
    `paid_pack_routes` hit this for a copy artefact approved before the
    migration existed, since it never passed through `start_copy_route`'s
    own guard at all."""
    from marketing.pipeline import StaleChannelPlanError, require_channel_keyed_copy

    channels = [{"channel": _OVERLONG_CHANNEL_NAME, "motion": "paid", "headline": "h"}]

    with pytest.raises(StaleChannelPlanError):
        require_channel_keyed_copy(channels)

    require_channel_keyed_copy([])  # must not raise on a genuinely empty list


# ── issue #152: staleness is about the plan as generated, not as narrowed ────


def test_channel_key_motions_never_calls_a_narrowed_plan_stale() -> None:
    """The #152 defect, at the unit level.

    A plan narrowed (#145) to only a `suggested_label`-only proposal has the
    same shape a pre-taxonomy plan has — entries, none carrying a
    `channel_key` — so `channel_plan_channel_map` called it stale and told the
    operator to re-run a plan that was current and valid, with no route to
    copy for the entry they had actually approved.

    `channel_key_motions` answers only "what are the keyed channels", with no
    opinion about staleness, so it returns empty rather than raising.
    `test_channel_plan_channel_map_raises_on_a_pre_migration_plan` above still
    covers the genuine legacy case, which must keep raising.
    """
    from marketing.pipeline import channel_key_motions

    narrowed = {
        "channels": [
            {
                "id": "e1",
                "channel_key": None,
                "suggested_label": "Review platforms",
                "motion": "organic",
            }
        ]
    }

    assert channel_key_motions(narrowed) == {}


def test_channel_key_motions_and_the_map_agree_on_a_healthy_plan() -> None:
    """Guards the split itself: two functions now derive the same mapping, and
    nothing else would notice if they drifted apart."""
    from marketing.pipeline import channel_key_motions, channel_plan_channel_map

    plan = {
        "channels": [
            {"id": "a", "channel_key": "search_organic", "motion": "organic"},
            {"id": "b", "channel_key": "linkedin_paid", "motion": "paid"},
        ]
    }

    assert channel_key_motions(plan) == channel_plan_channel_map(plan)
