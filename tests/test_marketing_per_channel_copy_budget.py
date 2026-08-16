"""Per-channel copy ceilings, so paid copy inside budget is not truncated (#173).

The defect this file holds shut: `COPY_LENGTH_BUDGET` was one ceiling for every
channel, and several ad platforms are tighter than it on some fields. Copy could
satisfy the budget in full and still be trimmed by the paid pack on export —
measured on dev 2026-08-16, campaign `b6724387`, 3 of 6 paid fields.

The numbers in `_MEASURED` below are that live run's, kept verbatim rather than
rounded into a fixture: they are the evidence the issue was opened on, and a
test that drifts off them stops proving the thing it was written for.
"""

from __future__ import annotations

import re

import pytest

from marketing.definitions import (
    AD_PLATFORM_LIMITS,
    COPY_LENGTH_BUDGET,
    CopyField,
    ad_platform_limits,
    channel_copy_budgets,
    copy_length_budget,
)

#: `(channel_key, ad_platform, field, generated_length)` from the live run in
#: #173. Every one of these was inside `COPY_LENGTH_BUDGET` and truncated anyway.
_MEASURED: list[tuple[str, str, CopyField, int]] = [
    ("google_search_paid", "google", "headline", 46),
    ("google_search_paid", "google", "body", 132),
    ("linkedin_paid", "linkedin", "cta", 37),
]


@pytest.mark.parametrize(("channel_key", "platform", "field", "generated"), _MEASURED)
def test_the_field_that_was_truncated_is_now_over_its_ceiling(
    channel_key: str, platform: str, field: CopyField, generated: int
) -> None:
    """The whole point, stated as the three fields that actually failed.

    Each was inside the old budget — which is why nothing flagged it — and each
    exceeds the platform limit the pack then trimmed it to. Under the new
    ceiling the same copy is over budget, so it is recorded on the artefact and
    shown to the operator instead of silently losing its end.
    """
    budget = copy_length_budget(motion="paid", ad_platform=platform)

    assert generated <= COPY_LENGTH_BUDGET[field], "premise: it was inside the old budget"
    assert generated > budget[field], "it must now be over the ceiling it is measured against"


def test_a_paid_ceiling_is_never_wider_than_the_platform_will_hold() -> None:
    """The invariant that makes truncation impossible for in-budget copy.

    Asserted across every known platform and field rather than for the three
    that happened to fail, because a future platform added to the table with a
    generous limit must not be able to raise a ceiling above what the pack
    trims to.
    """
    for platform, limits in AD_PLATFORM_LIMITS.items():
        budget = copy_length_budget(motion="paid", ad_platform=platform)
        for field, limit in limits.items():
            assert budget[field] <= limit, f"{platform}.{field} would still be truncated"


def test_a_paid_ceiling_is_never_wider_than_the_brevity_budget_either() -> None:
    """#128's constraint is not relaxed by #173. `min` can only lower it, and
    TikTok's 100-character headline is the case that proves the direction
    matters: it is above the 60-character budget, and must not raise it."""
    for platform in AD_PLATFORM_LIMITS:
        budget = copy_length_budget(motion="paid", ad_platform=platform)
        for field, brevity in COPY_LENGTH_BUDGET.items():
            assert budget[field] <= brevity

    assert AD_PLATFORM_LIMITS["tiktok"]["headline"] > COPY_LENGTH_BUDGET["headline"]
    assert (
        copy_length_budget(motion="paid", ad_platform="tiktok")["headline"]
        == (COPY_LENGTH_BUDGET["headline"])
    )


def test_organic_keeps_exactly_the_budget_it_had() -> None:
    """ "The organic default stays as-is where no platform limit applies" —
    #173's own fourth acceptance line, and the reason this change is safe for
    every channel the issue did not measure.

    An organic channel has no ad form to fit, so there is no second constraint
    to take a minimum with. Applying `generic`'s 30/90/20 to a blog post would
    impose an ad's limits on something that is not an ad.
    """
    assert copy_length_budget(motion="organic", ad_platform=None) == dict(COPY_LENGTH_BUDGET)


def test_an_organic_channel_carrying_an_ad_platform_is_still_organic() -> None:
    """Motion decides, not the presence of a platform string. A LinkedIn
    organic post is not a LinkedIn ad, and the taxonomy could carry a platform
    on a channel of either motion."""
    assert copy_length_budget(motion="organic", ad_platform="google") == dict(COPY_LENGTH_BUDGET)


def test_an_unknown_paid_platform_gets_the_narrowest_ceiling_not_the_widest() -> None:
    """A gap must never overstate the room an operator has.

    `generic` is the narrowest row in the table, and it is what the paid pack
    trims an unrecognised platform to — so the ceiling has to agree with it, or
    the promise this issue makes ("in-budget copy is not truncated") holds only
    for platforms someone remembered to add.
    """
    unknown = copy_length_budget(motion="paid", ad_platform="pinterest")
    generic = copy_length_budget(motion="paid", ad_platform="generic")

    assert unknown == generic
    assert ad_platform_limits("pinterest") == AD_PLATFORM_LIMITS["generic"]
    assert ad_platform_limits(None) == AD_PLATFORM_LIMITS["generic"]


def test_a_paid_channel_missing_from_the_taxonomy_still_gets_a_fitting_ceiling() -> None:
    """The plan was approved against a taxonomy snapshot; a channel retired
    since must still get copy that fits, not an exception. It resolves to
    `generic` — conservative — rather than to the unconstrained budget."""
    budgets = channel_copy_budgets({"retired_channel": "paid"}, {})

    assert budgets["retired_channel"] == copy_length_budget(motion="paid", ad_platform=None)


def test_budgets_are_built_for_exactly_the_channels_the_run_writes_for() -> None:
    """Keyed by the approved plan's channels, not by the taxonomy: the map is
    what the copy agent is handed and what its output is measured against, so a
    channel it was never asked to write for has no business in it."""
    budgets = channel_copy_budgets(
        {"google_search_paid": "paid", "blog": "organic"},
        {"google_search_paid": "google", "blog": None, "linkedin_paid": "linkedin"},
    )

    assert set(budgets) == {"google_search_paid", "blog"}
    assert budgets["google_search_paid"]["headline"] == AD_PLATFORM_LIMITS["google"]["headline"]
    assert budgets["blog"] == dict(COPY_LENGTH_BUDGET)


# ── The wiring: told to the agent, and measured against on the way back ──────


def _copy(channel_key: str, *, headline: str = "h", body: str = "b", cta: str = "c"):
    from marketing.definitions import ChannelCopy, Source

    return ChannelCopy(
        channel_key=channel_key,
        headline=headline,
        body=body,
        cta=cta,
        sources=[Source(url="https://example.com/x", note="n")],
    )


def test_measure_uses_the_channels_own_ceiling_not_the_shared_one() -> None:
    """A 46-character Google headline is over its 30-character ceiling and must
    be recorded as such — with the ceiling it actually broke, since that number
    is rendered beside the line in the approval gate."""
    from marketing import pipeline

    channels = [_copy("google_search_paid", headline="x" * 46)]

    pipeline.measure_copy_length(
        channels, {"google_search_paid": copy_length_budget(motion="paid", ad_platform="google")}
    )

    (overage,) = channels[0].over_budget
    assert (overage.field, overage.length, overage.budget) == ("headline", 46, 30)


def test_the_same_copy_on_an_organic_channel_is_not_flagged() -> None:
    """The identical 46 characters are fine on a channel with no ad form —
    which is what makes this a per-channel ceiling rather than a tightening."""
    from marketing import pipeline

    channels = [_copy("blog", headline="x" * 46)]

    pipeline.measure_copy_length(
        channels, {"blog": copy_length_budget(motion="organic", ad_platform=None)}
    )

    assert channels[0].over_budget == []


def test_copy_written_before_this_change_is_judged_by_the_rule_it_was_written_under() -> None:
    """A pending artefact stashed before #173 has no `channel_budgets`. Judging
    its copy by the tighter per-channel ceiling would flag fields the model was
    never asked to fit, on a run nobody can go back and redo."""
    from marketing import pipeline

    channels = [_copy("google_search_paid", headline="x" * 46)]

    pipeline.measure_copy_length(channels, None)

    assert channels[0].over_budget == []


def test_the_fatal_ceiling_scales_with_the_channels_own_budget() -> None:
    """`COPY_LENGTH_CEILING_MULTIPLE` is a multiple of the budget that applies,
    so a Google headline is thrown out past 3x30 rather than 3x60. Otherwise the
    tighter ceiling would be advisory exactly where it matters most."""
    from marketing import pipeline

    budgets = {"google_search_paid": copy_length_budget(motion="paid", ad_platform="google")}

    with pytest.raises(pipeline.CopyTooLongError) as excinfo:
        pipeline.measure_copy_length([_copy("google_search_paid", headline="x" * 91)], budgets)

    assert "google_search_paid headline is 91 characters (budget 30)" in str(excinfo.value)

    # 89 is over the 30-char budget but inside 3x, so it is recorded, not fatal.
    survivable = [_copy("google_search_paid", headline="x" * 89)]
    pipeline.measure_copy_length(survivable, budgets)
    assert survivable[0].over_budget[0].length == 89


@pytest.mark.asyncio
async def test_the_agent_is_told_each_channels_ceiling_in_its_payload() -> None:
    """The half a prompt cannot do on its own (#128's lesson).

    The tool schema is built once per run and covers every channel, so a field
    description cannot say "30 for Google, 70 for LinkedIn". The numbers travel
    in the payload instead, and this asserts they arrive — a model told a
    ceiling above the real one writes to the ceiling it is given.
    """
    from marketing import pipeline

    class _Gateway:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def request_agent_run(self, *, input_payload, **_kw):
            self.payloads.append(input_payload)
            return "run-1"

    gateway = _Gateway()
    budgets = channel_copy_budgets(
        {"google_search_paid": "paid", "blog": "organic"},
        {"google_search_paid": "google", "blog": None},
    )

    await pipeline.start_copy(
        gateway,  # type: ignore[arg-type]
        positioning_body={},
        channel_plan_body={},
        channel_budgets=budgets,
    )

    limits = gateway.payloads[0]["channel_limits"]
    assert limits["google_search_paid"] == {"headline": 30, "body": 90, "cta": 30}
    assert limits["blog"] == dict(COPY_LENGTH_BUDGET)


def test_the_prompt_points_at_the_map_rather_than_restating_numbers() -> None:
    """The prompt must not carry a second copy of the per-channel ceilings —
    they are per run, and a hard-coded set in the instructions would be the
    stale-copy failure this estate keeps paying for. It has to name the key the
    payload actually uses, or the model cannot find them."""
    from marketing.definitions import COPY_INSTRUCTIONS

    assert "channel_limits" in COPY_INSTRUCTIONS

    # No platform-specific ceiling is written out in prose. A number quoted
    # here is a second copy of `AD_PLATFORM_LIMITS`, and it is the copy that
    # goes stale: change Google's limit and the payload says one thing while a
    # sentence three paragraphs up says another.
    #
    # It caught its author immediately — the first draft of this prompt
    # illustrated the point with "a 30-character box", which is Google's
    # headline limit transcribed by hand.
    #
    # Only values unique to the platform table are checked. The budget's own
    # numbers are quoted deliberately, and platform NAMES are not checked at
    # all: `COPY_INSTRUCTIONS` names LinkedIn as a tone example ("a LinkedIn
    # post and an Instagram caption should not read the same"), which is about
    # voice and carries no number to go stale.
    prompt_numbers = set(re.findall(r"\b\d+\b", COPY_INSTRUCTIONS))
    platform_only = {
        value for limits in AD_PLATFORM_LIMITS.values() for value in limits.values()
    } - set(COPY_LENGTH_BUDGET.values())

    transcribed = sorted(value for value in platform_only if str(value) in prompt_numbers)
    assert not transcribed, f"platform limits written into the prompt: {transcribed}"
