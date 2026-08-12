"""The paid brief pack (M9, issue #8) — everything a paid channel needs that
the organic pack (``pack_routes.py``, M5) does not already carry.

**This is the organic pack's shape, with paid-specific additions, not a
second assembler.** The two things a distribution pack always needs —
whatever ``marketing_asset`` rows already exist, resolved to a fresh GET url,
and tracked links minted through the same three pure functions
(``links.mint_token`` / ``destination_with_utms`` / ``tracked_url``) —
are reused directly from ``pack_routes`` (``_existing_assets``,
``_asset_with_url``, ``_ensure_links``) rather than re-implemented here. What
this module adds is the part that is genuinely paid-only:

1. **Ad copy at real platform character limits.** The approved copy
   artefact's channels already exist (``copy_routes.py``, M5); this module
   filters to the ``paid`` ones and trims headline/body/cta to the limit its
   guessed platform actually enforces, never mid-word.
2. **Targeting**, drawn from the approved positioning artefact's
   ``segments`` — the only audience description this pipeline has ever
   produced, and the plugin's own citation discipline (every segment carries
   ``sources``) already makes it a defensible brief rather than a guess. Only
   ``name``, ``description`` and a ``source_count`` cross into the brief
   (``_targeting_segment``): the full ``{url, note}`` pairs stay on the
   positioning artefact itself, which is where an operator reviews evidence.
   Carrying them into every paid brief too was the reported duplication —
   the same handful of research URLs and notes, repeated verbatim on every
   segment of every downstream artefact that touched them. An approved
   positioning with zero segments 404s rather than shipping an empty
   targeting brief silently — the same gap-must-be-visible discipline this
   file uses for ``paid_channels``.
3. **A budget recommendation.** This plugin calls no ad platform API and has
   no historical spend or performance data to optimise against, so this is a
   declared, fixed starting-point heuristic — not a bidding model — and says
   so in its own ``basis`` field.
4. **Spend, reported as explicitly unmeasurable.** Issue #31 closed the gap
   for ``results_routes.py``'s leads/conversions/cost — those now go through
   the instance-configured leads source — but this route asks a different
   question (spend for one campaign, inside a paid brief pack) that contract
   was never wired to answer, and still isn't. This module does not invent a
   call to a route that does not exist; it reuses
   ``results_routes.UnmeasuredMetric`` verbatim so "no data" reads the same
   way here as it does on the results dashboard, rather than a bespoke ``0``.

## What this module deliberately does NOT do

No platform API call, no app registration, no credential vault, anywhere in
this file — the entire Meta/TikTok gauntlet stays out of the critical path by
design (issue #8). The pack is executed by hand in Ads Manager or Google Ads;
nothing here submits anything anywhere. It also never re-fetches or re-crops
creative bytes — see ``pack_routes.py``'s own module docstring for exactly
why that shape is a CodeQL ``py/full-ssrf`` sink (issue #36 owns the real
fix, at generation time); this module inherits the same constraint by
calling the same two functions, not by re-deriving the reasoning.
"""

from __future__ import annotations

from typing import Any

from biffo_plugin_sdk import BiffoAPIClient, create_core_client
from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import admin_app, config, pack_routes, pipeline, principal_client
from .results_routes import UnmeasuredMetric

require_admin = admin_app.require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

#: Duplicated local constant, not a cross-module reference — see
#: `channel_plan_routes._INTERNAL_PREFIX` for exactly why:
#: `tests/test_marketing_core_paths_guard.py` resolves a module-level string
#: constant only within the SAME file's own AST, and its own disagreement
#: test asserts every copy (this one included) still agrees with the rest.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"


def get_core_client() -> BiffoAPIClient:
    """SigV4-signed by default (ADR-0009), matching
    `pack_routes.get_core_client` exactly — the internal storage routes are
    IAM-only, never Cognito."""
    return create_core_client()


def get_campaign_client(
    admin: Any = Depends(require_admin),
) -> principal_client.PrincipalCoreClient:
    """A dual-auth client for this plugin's own generated-CRUD tables
    (`marketing_asset`), matching `pack_routes.get_campaign_client` exactly."""
    return principal_client.PrincipalCoreClient(admin.token)


async def _channel_ad_platforms(admin_token: str) -> dict[str, str | None]:
    """`{channel_key: ad_platform}` for this tenant's channel taxonomy (#76
    increment 2) — what `_platform_for_channel` looks up, replacing the
    substring-keyword guess it used to be."""
    resp = await admin_app._core("GET", f"{_INTERNAL_PREFIX}/channels", admin_token)
    resp.raise_for_status()
    rows = resp.json() or []
    return {row["key"]: row.get("ad_platform") for row in rows}


# ── Ad copy at real platform character limits ───────────────────────────────

#: Character limits by ad platform, per field. Deliberately conservative,
#: published numbers (Meta/Google/TikTok/LinkedIn ad-copy guidance), not
#: fetched from anywhere — this milestone calls no platform API, so these are
#: static data, the same way `definitions.PLACEMENTS`'s aspect ratios are.
#: `"generic"` is the fallback for a channel with no (or an unrecognised)
#: `ad_platform`, sized to the narrowest of the known platforms so a gap
#: never overstates how much room the operator actually has.
#:
#: Keyed by `marketing_channel.ad_platform` (#76 increment 2) — "google"
#: covers both Google Search ads and YouTube ads, which is coarser than
#: ideal (search and video ad copy specs genuinely differ), but the taxonomy
#: does not carry a finer-grained platform today and these limits are already
#: declared conservative, static guidance rather than authoritative ones —
#: see the module docstring. Splitting it further is a taxonomy change, not
#: a lookup-table one.
_PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "meta": {"headline": 40, "body": 125, "cta": 20},
    "google": {"headline": 30, "body": 90, "cta": 30},
    "tiktok": {"headline": 100, "body": 100, "cta": 20},
    "linkedin": {"headline": 70, "body": 150, "cta": 20},
    "generic": {"headline": 30, "body": 90, "cta": 20},
}

_ELLIPSIS = "…"


def _platform_for_channel(channel_key: str, ad_platforms: dict[str, str | None]) -> str:
    """The ad platform this channel's spend and character limits belong to —
    a lookup on `marketing_channel.ad_platform` (#76 increment 2), replacing
    the substring-keyword guess this function used to be (matching "google"
    against a free-text channel name such as "Google Search ads", with no
    channel enum anywhere in the pipeline to check against instead). The
    keyword table itself is deleted, not kept as an unreachable fallback —
    a dead fallback is a thing a later author "fixes" back into use.

    Falls back to `"generic"`'s narrower limits when the channel has no
    `ad_platform` set, or names one this table does not (yet) carry: an
    unrecognised platform still deserves a pack, just a more conservative
    one."""
    platform = ad_platforms.get(channel_key)
    return platform if platform in _PLATFORM_LIMITS else "generic"


def _fit_to_limit(text: str, limit: int) -> tuple[str, bool]:
    """`text` trimmed to at most `limit` characters, breaking on the last
    whole word that fits and returning whether it had to be trimmed at all.

    Never truncates mid-word: this text is meant to be copied straight into
    an Ads Manager form by a person, and a word sheared in half reads as
    broken, not merely short. Returns `(text, False)` unchanged when it
    already fits — matching `render.render`'s own "already correct" no-op
    discipline for the same reason: an unnecessary rewrite is a chance to
    introduce a difference nobody asked for.
    """
    if len(text) <= limit:
        return text, False
    if limit <= len(_ELLIPSIS):
        return _ELLIPSIS[:limit], True

    budget = limit - len(_ELLIPSIS)
    candidate = text[:budget]
    # Only back off to the previous whole word when the cut ITSELF lands
    # mid-word (the character immediately after `candidate` is not a space).
    # A cut that already lands exactly on a word boundary must keep every
    # word it already has — checking `" " in candidate` alone (the earlier
    # version of this function) can't tell those two cases apart, so it
    # always discarded one extra whole word even when the boundary was
    # already clean, wasting characters the platform actually allows.
    if text[budget] != " " and " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    return f"{candidate.rstrip()}{_ELLIPSIS}", True


def _ad_copy_variant(
    channel_copy: dict[str, Any], ad_platforms: dict[str, str | None]
) -> dict[str, Any]:
    """One paid channel's copy, trimmed to its actual platform's limits —
    looked up from the taxonomy (`ad_platforms`, #76 increment 2), not
    guessed from the channel's name.

    The original headline/body/cta are never returned alongside the trimmed
    ones — this pack is meant to be pasted straight into Ads Manager, and a
    second, longer copy sitting next to the one that actually fits is an
    invitation to paste the wrong one. Each field's own `_limit` and
    `_truncated` flag says what happened, so the trim is never silent.
    """
    channel_key = channel_copy.get("channel_key") or ""
    platform = _platform_for_channel(channel_key, ad_platforms)
    limits = _PLATFORM_LIMITS[platform]

    headline, headline_truncated = _fit_to_limit(
        channel_copy.get("headline") or "", limits["headline"]
    )
    body, body_truncated = _fit_to_limit(channel_copy.get("body") or "", limits["body"])
    cta, cta_truncated = _fit_to_limit(channel_copy.get("cta") or "", limits["cta"])

    return {
        # Outward key kept as "channel" (not renamed to "channel_key") for
        # admin-UI backward compatibility — `PaidPack.tsx` already renders
        # `.channel`/`.platform`, and this dict is one this plugin builds
        # itself, not a verbatim artefact-body passthrough. The value is now
        # a channel_key rather than a free-text label — see
        # `pack_routes._ensure_links`'s identical choice.
        "channel": channel_key,
        "platform": platform,
        "headline": headline,
        "headline_limit": limits["headline"],
        "headline_truncated": headline_truncated,
        "body": body,
        "body_limit": limits["body"],
        "body_truncated": body_truncated,
        "cta": cta,
        "cta_limit": limits["cta"],
        "cta_truncated": cta_truncated,
    }


# ── Budget recommendation ────────────────────────────────────────────────────

#: A fixed starting-point figure, not a bid-optimisation output — see the
#: module docstring's point 3. Changing this is a product decision, not a
#: bugfix, which is why it is named rather than inlined.
_DEFAULT_DAILY_TEST_BUDGET_USD = 20.0
_DEFAULT_TEST_WINDOW_DAYS = 7


def _budget_recommendation(paid_channel_count: int) -> dict[str, Any]:
    """A flat per-channel daily test budget for `paid_channel_count` paid
    channels, over `_DEFAULT_TEST_WINDOW_DAYS` days.

    Deliberately not a function of anything this plugin cannot see —
    audience size, expected CPM, historical performance — because it cannot
    see any of them: no platform API is called, and no spend history is
    reachable yet (issue #31). `basis` states that limitation in the
    response itself, not only in this docstring, so a caller reading only
    the JSON still gets the real reasoning.
    """
    per_channel_daily = _DEFAULT_DAILY_TEST_BUDGET_USD
    total_daily = per_channel_daily * paid_channel_count
    return {
        "currency": "USD",
        "channel_count": paid_channel_count,
        "per_channel_daily": per_channel_daily,
        "total_daily": total_daily,
        "test_window_days": _DEFAULT_TEST_WINDOW_DAYS,
        "total_test_budget": total_daily * _DEFAULT_TEST_WINDOW_DAYS,
        "basis": (
            f"A fixed starting-point heuristic (${per_channel_daily:.0f}/day per paid "
            f"channel for a {_DEFAULT_TEST_WINDOW_DAYS}-day test window) — not a "
            "bid-optimisation model. This plugin calls no ad platform API and holds no "
            "historical spend or performance data to optimise against; reassess once "
            "real spend and conversion data exist (see `spend` below)."
        ),
    }


def _targeting_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """One positioning segment, shaped for the targeting brief.

    Deliberately drops the segment's full `sources` (each a `{url, note}`
    pair) down to a bare count. The full citations already live on the
    positioning artefact this pack was built from — this brief is meant to be
    read by a person setting up ad targeting, not to re-litigate the
    evidence, and repeating every source's URL and note on every segment here
    was the exact duplication reported against the campaign studio: the same
    handful of research URLs, verbatim, on every artefact that touches a
    segment. `source_count` keeps the "this is evidenced, not a guess" signal
    (the module docstring's own reasoning for using segments here) without
    reprinting the evidence itself.
    """
    sources = segment.get("sources") or []
    return {
        "name": segment.get("name"),
        "description": segment.get("description"),
        "source_count": len(sources),
    }


@router.get("/campaigns/{campaign_id}/paid-pack")
async def get_paid_pack_route(
    campaign_id: str,
    request: Request,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Assemble this campaign's paid brief pack: ad copy at platform
    character limits, creative, targeting, a budget recommendation, tracked
    links, and spend (reported as unmeasurable — see the module docstring).

    Requires an **approved** `copy` artefact carrying at least one `paid`
    channel, and an **approved** `positioning` artefact with at least one
    `segment` for `targeting` — the same "an unreviewed or empty artefact
    must never reach an operator" discipline `pack_routes.get_pack_route`
    already enforces for the organic pack's copy gate.
    """
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    campaign_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", admin.token
    )
    if campaign_resp.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign_resp.raise_for_status()
    campaign = campaign_resp.json() or {}

    copy_artefact = await admin_app._latest_artefact(campaign_id, "copy", admin.token)
    if copy_artefact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No copy artefact for this campaign yet."
        )
    # Gated on the latest APPROVED copy, not the latest attempt overall
    # (issue #41 — this call site was not named in the issue but shares the
    # exact same shape as `pack_routes.get_pack_route`'s copy gate): a newer
    # pending/proposed re-run must not hide an older approved one.
    # `copy_artefact` above is used only for the 404-vs-409 split and, on
    # failure, to report the newest attempt's real status.
    approved_copy = await admin_app._latest_approved_artefact(campaign_id, "copy", admin.token)
    if approved_copy is None:
        try:
            pipeline.require_approved(copy_artefact.get("status") or "", what="The copy artefact")
        except pipeline.ArtefactNotApprovedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The copy artefact must be approved before this can proceed.",
        )

    copy_body = admin_app._parse_artefact_body(approved_copy.get("body"))
    channels = copy_body.get("channels") or []
    try:
        pipeline.require_channel_keyed_copy(channels)
    except pipeline.StaleChannelPlanError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    paid_channels = [c for c in channels if c.get("motion") == "paid"]
    if not paid_channels:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This campaign's approved copy has no paid channels.",
        )

    positioning = await admin_app._latest_artefact(campaign_id, "positioning", admin.token)
    if positioning is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No positioning artefact for this campaign yet.",
        )
    # Same reasoning as the copy gate just above.
    approved_positioning = await admin_app._latest_approved_artefact(
        campaign_id, "positioning", admin.token
    )
    if approved_positioning is None:
        try:
            pipeline.require_approved(
                positioning.get("status") or "", what="The positioning artefact"
            )
        except pipeline.ArtefactNotApprovedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The positioning artefact must be approved before this can proceed.",
        )

    positioning_body = admin_app._parse_artefact_body(approved_positioning.get("body"))
    raw_segments = positioning_body.get("segments") or []
    if not raw_segments:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The approved positioning artefact has no segments to target.",
        )
    targeting = [_targeting_segment(s) for s in raw_segments]

    ad_platforms = await _channel_ad_platforms(admin.token)

    assets, missing_placements, superseded_source_count = await pack_routes._existing_assets(
        campaign_id, campaign_client=campaign_client
    )
    assets_with_urls = [await pack_routes._asset_with_url(core_client, a) for a in assets]

    # `_ensure_links` filters to the `paid_channels` subset passed in, and
    # dedups motion-aware, at the source — issue #48. This route used to
    # filter the result back down again itself; that workaround is gone.
    links = await pack_routes._ensure_links(
        campaign_id,
        paid_channels,
        campaign=campaign,
        admin_token=admin.token,
        base_url=config.public_base_url_for(
            request.headers.get("origin"), request.headers.get("referer")
        ),
    )

    return {
        "campaign_id": campaign_id,
        "ad_copy": [_ad_copy_variant(c, ad_platforms) for c in paid_channels],
        "assets": assets_with_urls,
        "missing_placements": missing_placements,
        "superseded_source_count": superseded_source_count,
        "targeting": targeting,
        "budget": _budget_recommendation(len(paid_channels)),
        "links": links,
        "guidance": campaign.get("guidance") or "",
        "spend": UnmeasuredMetric(),
    }
