"""Brevity as a constraint rather than a preference (issue #128).

The copy this pipeline produced was accurate, grounded, on-message — and three
sentences where one would land harder, closing with a CTA that restated the
headline instead of asking for anything. Nothing in the codebase disagreed
with it: `COPY_INSTRUCTIONS` said nothing about length, `ChannelCopy`'s fields
had no size, and the only character limits anywhere lived on the PAID path
(`paid_pack_routes._PLATFORM_LIMITS`), which is a different question — what
fits an Ads Manager form, not what was written to be read.

These tests are the fail-first proof. Before the fix they fail on the absence
of `COPY_LENGTH_BUDGET` itself; after it they pin the three properties that
make the constraint real rather than requested:

1. The number is **one** number — quoted into the prompt, into the schema the
   model fills, and into the checker. A prompt asking for brevity and a
   checker deciding what brevity means are two places for one rule to drift.
2. Over budget is **recorded on the artefact**, not rejected: the #128 example
   is over on all three fields and is still worth an operator's judgement.
3. Grossly over IS rejected, because past that point there is nothing left to
   judge.
"""

from __future__ import annotations

from typing import Any

import pytest

from marketing.definitions import (
    COPY_INSTRUCTIONS,
    COPY_LENGTH_BUDGET,
    COPY_LENGTH_CEILING_MULTIPLE,
    copy_tool_schema,
)
from marketing.pipeline import CopyTooLongError, extract_copy

_SOURCE = {"url": "https://example.com/thread", "note": "A forum thread."}

#: The copy issue #128 actually reports, from campaign `fa6590f1`, verbatim.
#: Its lengths (63 / 254 / 57) are the calibration argument for both halves of
#: this rule at once: over budget on every field, nowhere near the ceiling on
#: any of them.
_REPORTED_HEADLINE = "Franchise management pricing you can see before you book a demo"
_REPORTED_BODY = (
    "Comparing UK franchise-management platforms? Most make you request a demo just to "
    "learn what it costs. We publish clear per-location pricing you can plan a budget "
    "around — no demo gate. See exactly what running 5–50 units on one platform costs, "
    "up front."
)
_REPORTED_CTA = "See our per-location pricing up front — no demo required."


def _copy_call(**overrides: Any) -> list[dict[str, Any]]:
    channel = {
        "channel_key": "organic_search",
        "headline": "Pricing, published.",
        "body": "Per-location pricing on the website. No demo gate.",
        "cta": "See the pricing",
        "sources": [_SOURCE],
    }
    channel.update(overrides)
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "submit_copy", "arguments": {"channels": [channel]}}}
            ],
        }
    ]


def _extract(**overrides: Any) -> Any:
    return extract_copy(
        _copy_call(**overrides), channel_plan_channels={"organic_search": "organic"}
    )


# ── One number, three readers ────────────────────────────────────────────────


def test_the_budget_is_quoted_into_the_prompt() -> None:
    """The instructions must carry the SAME numbers the checker enforces.

    A hand-written "keep headlines under 70 characters" in the prompt beside a
    checker that measures 60 is a rule the model can satisfy and still be
    marked down for, which is how a constraint decays back into a preference.
    """
    for budget in COPY_LENGTH_BUDGET.values():
        assert str(budget) in COPY_INSTRUCTIONS
    assert str(COPY_LENGTH_CEILING_MULTIPLE) in COPY_INSTRUCTIONS


def test_the_budget_is_quoted_into_the_field_descriptions_the_model_fills() -> None:
    """The number must also reach the tool schema, not only the system prompt.

    The schema description is what the model is reading at the moment it fills
    that specific field; a length rule three paragraphs up a prompt is a thing
    to remember instead."""
    properties = copy_tool_schema()["function"]["parameters"]["$defs"]["ChannelCopy"]["properties"]
    for field, budget in COPY_LENGTH_BUDGET.items():
        assert str(budget) in properties[field]["description"], field


def test_the_length_fields_carry_no_max_length() -> None:
    """Deliberately NOT enforced by pydantic.

    A `max_length` fails `model_validate` inside `_extract_cited_artefact`,
    which discards every other channel's copy over one long field and reports
    it as a raw `MalformedOutputError`. It also rewards the wrong behaviour:
    the cheapest way to satisfy it is to write the long sentence and stop
    typing, which is a wordy headline with the end cut off — not copy written
    to be short."""
    properties = copy_tool_schema()["function"]["parameters"]["$defs"]["ChannelCopy"]["properties"]
    for field in COPY_LENGTH_BUDGET:
        assert "maxLength" not in properties[field], field


def test_the_overage_record_is_hidden_from_the_agent() -> None:
    """`over_budget` is derived, and unlike `motion` it is not even shown to
    the agent: it records the agent having missed a rule, so offering it a
    field to assert compliance in is worse than useless, and the copy agent
    has no prompt-size headroom to spend on it (#131)."""
    properties = copy_tool_schema()["function"]["parameters"]["$defs"]["ChannelCopy"]["properties"]
    assert "over_budget" not in properties


# ── Over budget: recorded, and the run survives ──────────────────────────────


def test_copy_within_budget_records_no_overage() -> None:
    copy = _extract()

    assert copy.channels[0].over_budget == []


def test_the_reported_copy_is_flagged_on_every_field_and_still_returned() -> None:
    """#128's own example. Every field is over; the run still produces an
    artefact, because an operator called this copy "accurate, well-grounded and
    on-message" — rejecting it outright would throw that away to fix wordiness
    a human can judge in seconds."""
    copy = _extract(headline=_REPORTED_HEADLINE, body=_REPORTED_BODY, cta=_REPORTED_CTA)

    overages = copy.channels[0].over_budget
    assert [o.field for o in overages] == ["headline", "body", "cta"]
    assert [o.length for o in overages] == [63, 254, 57]
    assert [o.budget for o in overages] == [
        COPY_LENGTH_BUDGET["headline"],
        COPY_LENGTH_BUDGET["body"],
        COPY_LENGTH_BUDGET["cta"],
    ]
    # The artefact body is what the approval gate renders, so the finding has
    # to survive the dump — this is the whole mechanism by which a non-fatal
    # check stays visible rather than advisory.
    dumped = copy.model_dump()["channels"][0]["over_budget"]
    assert dumped[0] == {"field": "headline", "length": 63, "budget": 60}


def test_a_field_exactly_on_its_budget_is_not_flagged() -> None:
    """The budget is a ceiling, not an exclusive bound — off-by-one here would
    flag copy that obeyed the instruction it was given."""
    copy = _extract(headline="x" * COPY_LENGTH_BUDGET["headline"])

    assert copy.channels[0].over_budget == []


# ── Grossly over: rejected ───────────────────────────────────────────────────


def test_copy_far_past_the_ceiling_is_rejected() -> None:
    """Past `COPY_LENGTH_CEILING_MULTIPLE` the constraint was not applied at
    all, so there is nothing for an operator to weigh up."""
    runaway = "x" * (COPY_LENGTH_BUDGET["headline"] * COPY_LENGTH_CEILING_MULTIPLE + 1)

    with pytest.raises(CopyTooLongError) as exc:
        _extract(headline=runaway)

    message = str(exc.value)
    # A guard that says only "no" gets worked around: the operator must be able
    # to see which channel, which field, how long, and what to do next.
    assert "organic_search" in message
    assert "headline" in message
    assert str(len(runaway)) in message
    assert "copy generation again" in message


def test_the_fatal_case_reaches_the_operator_as_a_502_rather_than_a_500() -> None:
    """`admin_app._pipeline_error_to_http` maps the BASE class deliberately,
    "so a new pipeline error type cannot silently fall through as a 500". This
    IS a new pipeline error type — this is the test that says so."""
    from fastapi import status

    from marketing.admin_app import _pipeline_error_to_http
    from marketing.pipeline import PipelineError

    assert issubclass(CopyTooLongError, PipelineError)
    http = _pipeline_error_to_http(CopyTooLongError("the headline is 400 characters"))
    assert http.status_code == status.HTTP_502_BAD_GATEWAY
    assert http.detail == "the headline is 400 characters"


def test_copy_exactly_at_the_ceiling_is_flagged_rather_than_rejected() -> None:
    """The fatal case is the one that is unambiguously not copy — the boundary
    itself still belongs to the operator."""
    at_ceiling = "x" * (COPY_LENGTH_BUDGET["headline"] * COPY_LENGTH_CEILING_MULTIPLE)

    copy = _extract(headline=at_ceiling)

    assert [o.field for o in copy.channels[0].over_budget] == ["headline"]


# ── What must not have changed ───────────────────────────────────────────────


def test_length_is_checked_after_the_citation_guards() -> None:
    """#113's provenance guard must still be what a fabricating run is reported
    for. A run that invented a source AND wrote a 500-character headline is a
    run that invented a source; reporting it as wordy would be a real
    regression dressed up as a length check."""
    from marketing.pipeline import UncitedSourceError

    runaway = "x" * (COPY_LENGTH_BUDGET["headline"] * COPY_LENGTH_CEILING_MULTIPLE + 1)

    with pytest.raises(UncitedSourceError):
        extract_copy(
            _copy_call(headline=runaway, sources=[{"url": "https://invented.example", "note": ""}]),
            channel_plan_channels={"organic_search": "organic"},
            allowed_source_urls=["https://example.com/thread"],
        )
