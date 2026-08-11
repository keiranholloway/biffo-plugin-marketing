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
2. **Targeting**, drawn straight from the approved positioning artefact's
   ``segments`` — the only audience description this pipeline has ever
   produced, and the plugin's own citation discipline (every segment carries
   ``sources``) already makes it a defensible brief rather than a guess.
3. **A budget recommendation.** This plugin calls no ad platform API and has
   no historical spend or performance data to optimise against, so this is a
   declared, fixed starting-point heuristic — not a bidding model — and says
   so in its own ``basis`` field.
4. **Spend, reported as explicitly unmeasurable.** ``lead_source_costs``
   (``tabsii.lead_source_costs``, DDL module 049) is not reachable from this
   plugin today — no ``/api/v1/internal/*`` route is registered for it, and
   it sits behind tabsii-CRM RBAC codes unrelated to this plugin's own admin
   identity (issue #31, the exact gap ``results_routes.py`` already reports
   leads/conversions/cost against). This module does not invent a call to a
   route that does not exist; it reuses ``results_routes.UnmeasuredMetric``
   verbatim so "no data" reads the same way here as it does on the results
   dashboard, rather than a bespoke ``0``.

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

import json
from typing import Any

from biffo_plugin_sdk import BiffoAPIClient, create_core_client
from fastapi import APIRouter, Depends, HTTPException, status

from . import admin_app, pack_routes, pipeline, principal_client
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


# ── Ad copy at real platform character limits ───────────────────────────────

#: Character limits by guessed platform, per field. Deliberately conservative,
#: published numbers (Meta/Google/TikTok/LinkedIn ad-copy guidance), not
#: fetched from anywhere — this milestone calls no platform API, so these are
#: static data, the same way `definitions.PLACEMENTS`'s aspect ratios are.
#: `"generic"` is the fallback for a channel this table does not recognise,
#: sized to the narrowest of the known platforms so a guess never overstates
#: how much room the operator actually has.
_PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "meta": {"headline": 40, "body": 125, "cta": 20},
    "google_search": {"headline": 30, "body": 90, "cta": 30},
    "tiktok": {"headline": 100, "body": 100, "cta": 20},
    "linkedin": {"headline": 70, "body": 150, "cta": 20},
    "generic": {"headline": 30, "body": 90, "cta": 20},
}

#: Ordered so a more specific keyword (e.g. "google") is checked before a
#: channel name that happens to also mention a competitor in passing —
#: not currently ambiguous with real channel names, but order is still
#: significant and worth being explicit about.
_PLATFORM_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("meta", ("facebook", "instagram", "meta")),
    ("google_search", ("google",)),
    ("tiktok", ("tiktok",)),
    ("linkedin", ("linkedin",)),
)

_ELLIPSIS = "…"


def _platform_for_channel(channel: str) -> str:
    """The best-guess ad platform for a free-text channel name (e.g.
    "Instagram Reels", "Google Search ads") — there is no channel enum
    anywhere in this pipeline (`ChannelRecommendation.channel` is free text
    the channel-plan agent names itself), so this is a keyword guess, not a
    lookup. Falls back to `"generic"`'s narrower limits rather than raising:
    an unrecognised channel still deserves a pack, just a more conservative
    one."""
    lowered = channel.lower()
    for platform, keywords in _PLATFORM_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return platform
    return "generic"


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
    if " " in text[: budget + 1] and " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    return f"{candidate.rstrip()}{_ELLIPSIS}", True


def _ad_copy_variant(channel_copy: dict[str, Any]) -> dict[str, Any]:
    """One paid channel's copy, trimmed to its guessed platform's limits.

    The original headline/body/cta are never returned alongside the trimmed
    ones — this pack is meant to be pasted straight into Ads Manager, and a
    second, longer copy sitting next to the one that actually fits is an
    invitation to paste the wrong one. Each field's own `_limit` and
    `_truncated` flag says what happened, so the trim is never silent.
    """
    channel = channel_copy.get("channel") or ""
    platform = _platform_for_channel(channel)
    limits = _PLATFORM_LIMITS[platform]

    headline, headline_truncated = _fit_to_limit(
        channel_copy.get("headline") or "", limits["headline"]
    )
    body, body_truncated = _fit_to_limit(channel_copy.get("body") or "", limits["body"])
    cta, cta_truncated = _fit_to_limit(channel_copy.get("cta") or "", limits["cta"])

    return {
        "channel": channel,
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


@router.get("/campaigns/{campaign_id}/paid-pack")
async def get_paid_pack_route(
    campaign_id: str,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Assemble this campaign's paid brief pack: ad copy at platform
    character limits, creative, targeting, a budget recommendation, tracked
    links, and spend (reported as unmeasurable — see the module docstring).

    Requires an **approved** `copy` artefact carrying at least one `paid`
    channel, and an **approved** `positioning` artefact for `targeting` — the
    same "an unreviewed artefact must never reach an operator" discipline
    `pack_routes.get_pack_route` already enforces for the organic pack.
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
    try:
        pipeline.require_approved(copy_artefact.get("status") or "", what="The copy artefact")
    except pipeline.ArtefactNotApprovedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    raw_copy_body = copy_artefact.get("body")
    copy_body = (
        json.loads(raw_copy_body) if isinstance(raw_copy_body, str) else (raw_copy_body or {})
    )
    paid_channels = [c for c in (copy_body.get("channels") or []) if c.get("motion") == "paid"]
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
    try:
        pipeline.require_approved(positioning.get("status") or "", what="The positioning artefact")
    except pipeline.ArtefactNotApprovedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    raw_positioning_body = positioning.get("body")
    positioning_body = (
        json.loads(raw_positioning_body)
        if isinstance(raw_positioning_body, str)
        else (raw_positioning_body or {})
    )
    targeting = positioning_body.get("segments") or []

    assets, missing_placements = await pack_routes._existing_assets(
        campaign_id, campaign_client=campaign_client
    )
    assets_with_urls = [await pack_routes._asset_with_url(core_client, a) for a in assets]

    links = await pack_routes._ensure_links(
        campaign_id, paid_channels, campaign=campaign, admin_token=admin.token
    )

    return {
        "campaign_id": campaign_id,
        "ad_copy": [_ad_copy_variant(c) for c in paid_channels],
        "assets": assets_with_urls,
        "missing_placements": missing_placements,
        "targeting": targeting,
        "budget": _budget_recommendation(len(paid_channels)),
        "links": links,
        "guidance": campaign.get("guidance") or "",
        "spend": UnmeasuredMetric(),
    }
