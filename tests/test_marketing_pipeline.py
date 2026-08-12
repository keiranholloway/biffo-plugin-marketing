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
