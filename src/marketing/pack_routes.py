"""The distribution pack (M5, issue #4) — the milestone's actual deliverable,
and what makes M5 "the first genuinely usable release" (M1-M4 only produce an
approved plan; nothing an operator can publish comes out of them).

A pack is four things:

1. **The assets** — whatever ``marketing_asset`` rows already exist for this
   campaign, each resolved to a fresh GET url. **Placement rendering is not
   done here, and is not yet done anywhere in this plugin** — see "What this
   module does NOT do" below; ``missing_placements`` in the response says so
   explicitly rather than silently pretending they exist.
2. **The copy** — the latest **approved** ``copy`` artefact's body
   (``copy_routes.py``, pipeline plumbing for the same stage).
3. **The tracked links** — one per channel in the approved copy, minted with
   the exact same three pure functions ``admin_app.mint_links`` uses
   (``links.mint_token`` / ``links.destination_with_utms`` /
   ``links.tracked_url``) and reused on every later call rather than
   re-minted, so repeatedly opening the pack does not spawn a fresh token per
   visit.
4. **``guidance``** — ``marketing_campaign.guidance``, verbatim. Disclosure
   and music-licensing obligations belong to whoever presses publish, which
   is the operator, not this plugin — the column exists so the pack can
   surface that text next to what it applies to, not so this module can
   reason about it.

Its own module for the same reason ``image_routes.py`` is: resolving a
``media_id`` to a fresh GET url needs the SigV4-signed internal client for
object storage, which ``admin_app._core``'s Cognito-bearer path cannot reach.

## What this module deliberately does NOT do — placement rendering

An earlier version of this route rendered ``PLACEMENTS`` here: fetch the
source creative's bytes back from object storage via a presigned URL, crop
each placement with ``render.render``, upload the result. CodeQL's
``py/full-ssrf`` correctly flagged that fetch — a server-side request to a
URL read out of an HTTP response is exactly the shape the query exists to
catch, and four different mitigation shapes (a host-allowlist check, wrapped
in a helper; the same check inlined before the sink; the same plus an inline
``codeql[py/full-ssrf]`` suppression comment; a positive-branch guard
matching CodeQL's own documented barrier example) all left the finding
unchanged. Investigation traced why: the query's taint source is
``core_client`` itself — a ``Depends()``-injected value, per CodeQL's FastAPI
model — so everything read back through it is treated as attacker-influenced
regardless of what checks run on the derived string.

That investigation surfaced the real defect the query was pointing at:
``marketing_asset`` already models one row **per placement** (its own column
description: "the placements are DETERMINISTIC RENDERS of \\[the source]"),
but nothing in this plugin ever writes one. ``image_routes.py``'s
``generate_still_route`` writes only the source row (``is_source=True``,
``placement=None``); this module was compensating for that gap by fetching
the source creative back and re-cropping it — and that re-fetch is the SSRF
sink. The correct fix renders each placement **inside**
``generate_still_route``, from the bytes the image provider already returned
in memory, before they are ever uploaded — no fetch, no presigned URL, no
outbound request, no sink. That is also what "render once, never
regenerate" (``definitions.py``, ``render.py``) was already asking for: the
render belongs at generation time, not reconstructed later from storage.

``image_routes.py`` is owned by a different, concurrent change in this repo,
so that fix is **not** made here — it is filed as issue #36 rather than
reached into unilaterally. Until it lands, this route can only serve
whatever assets already exist, which today is the source creative alone;
``missing_placements`` in the response makes that gap
visible to a caller rather than a route that quietly renders nothing and
says nothing.

**A caveat that issue also has to answer, not this module:** the fix above
assumes the plugin holds the source creative's bytes in memory at generation
time, which is true of ``generate_still_route``'s provider-generated path.
If a "publish an existing creative" upload path is ever added — one that
never routes the bytes through this plugin — the fetch problem returns in a
different shape, and most likely needs Core to grow an internal "read
bytes" endpoint so this plugin never talks to S3 directly at all. No such
path exists in this plugin today (verified: ``image_routes.py`` is the only
place a ``marketing_asset`` row with ``is_source=True`` is ever created).

It does not build a download or copy-to-clipboard UI either — that is
``web-admin/``, out of scope for this change (a concurrent agent is expected
to build the frontend against this API). The day-0 design's two device
warnings therefore stay **unverified by this change**: a clipboard write
silently failing on mobile Safari, and iOS ``<a download>`` on a video
opening a preview instead of saving. Neither is reachable from here — there
is no device in this loop — and neither should be asserted fixed until
someone checks on a real phone.
"""

from __future__ import annotations

import json
from typing import Any

from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import admin_app, pipeline, principal_client
from .config import public_base_url_for
from .definitions import PLACEMENTS
from .links import destination_with_utms, mint_token, tracked_url

require_admin = admin_app.require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

#: Duplicated local constant — see ``channel_plan_routes._INTERNAL_PREFIX``
#: for why: the core-paths guard resolves a module-level string constant
#: only within the file that defines it.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: Matches ``image_routes._STORAGE_PATH`` — duplicated for the same reason,
#: not imported: see that module's own comment on ``_validated_campaign_id``.
_STORAGE_PATH = "/api/v1/internal/plugins/me/storage"


def get_core_client() -> BiffoAPIClient:
    """SigV4-signed by default (ADR-0009), matching
    ``image_routes.get_core_client`` exactly — the internal storage routes
    are IAM-only, never Cognito."""
    return create_core_client()


def get_campaign_client(
    admin: Any = Depends(require_admin),
) -> principal_client.PrincipalCoreClient:
    """A dual-auth client for this plugin's own generated-CRUD tables
    (``marketing_asset``), matching ``image_routes.get_campaign_client``
    exactly (issue #27)."""
    return principal_client.PrincipalCoreClient(admin.token)


def _core_error(exc: BiffoAPIError) -> HTTPException:
    """Matches ``image_routes``'s own mapping: storage's 503 ("not
    configured here") passes through as-is; everything else is 502, since
    Core answered and what it said is not something retrying *this* request
    fixes."""
    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)


async def _existing_assets(
    campaign_id: str, *, campaign_client: principal_client.PrincipalCoreClient
) -> tuple[list[dict[str, Any]], list[str]]:
    """Whatever ``marketing_asset`` rows already exist for this campaign,
    plus the subset of ``PLACEMENTS`` that has no row yet. Never renders —
    see the module docstring for why generation time, not pack time, is
    where that belongs.

    Returns ``([], PLACEMENTS)`` when nothing exists yet — a campaign whose
    still was never generated (image generation not yet run, or #63's
    missing provider key) is a valid, assemblable pack: the gap belongs in
    ``missing_placements``, exactly as the module docstring already promises
    ("missing_placements... says so explicitly rather than silently
    pretending they exist"), not behind a 404 raised here.

    **This used to require an ``is_source=True`` row and 404 when none
    existed** ("No approved source creative for this campaign yet."). That
    was issue #64: a campaign with copy fully approved still 404d on
    ``/pack`` and ``/paid-pack``, because this check runs *after* the copy
    gate in ``get_pack_route``/``get_paid_pack_route`` — a third 404 site in
    those functions that #64's own two-site reading of the source missed.
    """
    assets = await campaign_client.get(
        f"{_INTERNAL_PREFIX}/assets", params={"campaign_id": campaign_id}
    )
    assets = assets or []

    have_placements = {a["placement"] for a in assets if a.get("placement")}
    missing_placements = [p for p in PLACEMENTS if p not in have_placements]
    return assets, missing_placements


async def _asset_with_url(core_client: BiffoAPIClient, asset: dict[str, Any]) -> dict[str, Any]:
    """One asset row plus a freshly-minted GET url — minted per request, per
    ``image_routes``' own docstring on why a URL is never stored."""
    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{asset['media_id']}/url")
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc
    return {**asset, "url": url_resp["url"]}


async def _create_link_or_422(
    channel_key: str, admin_token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST one ``marketing_link`` row, translating any write Core rejects
    into a 4xx that NAMES the offending channel, rather than letting Core's
    raw failure reach the operator as an undiagnosable bare 500.

    This is exactly #75's shape: a too-long value (then, a 121-character
    agent-generated channel name against ``marketing_link.channel``'s
    ``String(64)``) surfaced as ``Server error '500 Internal Server Error'
    for url .../links`` with an ``asyncpg.StringDataRightTruncationError``
    visible only in Core's own logs, not in anything this plugin returned.
    Channel values are short by construction now (#76 increment 2 — a
    ``channel_key`` from the taxonomy, not agent-authored free text), so this
    exact overflow should not recur through this path — but the translation
    is kept anyway: it is general over *any* field Core rejects the write
    for, not specific to `channel`, and the next unbounded field (``variant``,
    ``destination_url``) gets the same diagnosability for free.
    """
    response = await admin_app._core("POST", f"{_INTERNAL_PREFIX}/links", admin_token, json=payload)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Could not create a tracked link for channel {channel_key!r}: "
                "Core rejected the write."
            ),
        )
    return response.json()


async def _ensure_links(
    campaign_id: str,
    channels: list[dict[str, Any]],
    *,
    campaign: dict[str, Any],
    admin_token: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """One tracked link per channel in the approved copy — minted once and
    reused on every later pack request, never re-minted, so opening the pack
    repeatedly does not spawn a fresh token per channel per visit.

    ``channels`` entries carry ``channel_key`` (#76 increment 2), not the
    free-text ``channel`` this function used to key on — see
    ``pipeline.require_channel_keyed_copy`` for the guard that rejects a
    pre-migration copy artefact before it ever reaches here. The
    ``marketing_link.channel`` DB column itself is unchanged (still
    ``String(64)``, unrenamed — a live column already holding production
    rows); what changes is that every value written into it now comes from a
    validated ``channel_key`` rather than whatever the channel-plan agent
    typed, which is what actually closes #75.

    Mints with the exact same three pure functions
    ``admin_app.mint_links`` uses (``mint_token`` / ``destination_with_utms``
    / ``tracked_url``) and writes through the same ``admin_app._core`` seam,
    rather than composing a URL some fourth way. No SSRF-shaped surface
    here: both calls are to Core's own internal CRUD mount, never to a URL
    read out of a response.
    """
    links_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/links", admin_token, params={"campaign_id": campaign_id}
    )
    links_resp.raise_for_status()
    existing = links_resp.json() or []
    have_channels = {link["channel"] for link in existing}

    # The response's own "channel" key is kept (not renamed to "channel_key")
    # for admin-UI backward compatibility — `PackParts.tsx`/`PaidPack.tsx`
    # already render `.channel` and this dict is one this plugin builds
    # itself, not a verbatim artefact-body passthrough. The value is now a
    # channel_key rather than a free-text label; a follow-up UI change to
    # show the taxonomy's human `label` alongside it is not done here.
    out = [
        {
            "channel": link["channel"],
            "variant": link.get("variant"),
            "is_paid": link.get("is_paid"),
            "url": tracked_url(base_url, link["token"]) if base_url else None,
        }
        for link in existing
    ]

    missing = [c for c in channels if c.get("channel_key") not in have_channels]
    if not missing:
        return out

    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No public base URL is configured for this deployment.",
        )
    destination = campaign.get("destination_url")
    if not destination:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This campaign has no destination_url, so its links would lead nowhere.",
        )

    for c in missing:
        channel_key = c["channel_key"]
        is_paid = c.get("motion") == "paid"
        token = mint_token()
        # The created row itself is not needed — the token minted it from is.
        await _create_link_or_422(
            channel_key,
            admin_token,
            {
                "campaign_id": campaign_id,
                "token": token,
                "channel": channel_key,
                "variant": None,
                "is_paid": is_paid,
                "destination_url": destination_with_utms(
                    destination,
                    campaign_id=campaign_id,
                    channel=channel_key,
                    variant=None,
                    is_paid=is_paid,
                ),
            },
        )
        out.append(
            {
                "channel": channel_key,
                "variant": None,
                "is_paid": is_paid,
                "url": tracked_url(base_url, token),
            }
        )
    return out


@router.get("/campaigns/{campaign_id}/pack")
async def get_pack_route(
    campaign_id: str,
    request: Request,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Assemble this campaign's distribution pack: assets, copy, tracked
    links and guidance. Requires an **approved** ``copy`` artefact — a
    ``proposed`` one must not reach an operator's pack, or the copy approval
    gate is decorative, same as every other gate in this pipeline.

    ``assets`` is whatever ``marketing_asset`` rows already exist —
    ``missing_placements`` names any of ``PLACEMENTS`` this pack could not
    include, per the module docstring's "What this module does NOT do".
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
    # (issue #41) — a newer pending/proposed re-run must not make a pack
    # that was serving fine start 404ing/409ing the moment someone starts an
    # edit. `copy_artefact` above is used only for the 404-vs-409 split and,
    # on failure, to report the newest attempt's real status.
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

    raw_body = approved_copy.get("body")
    copy_body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})
    channels = copy_body.get("channels") or []
    try:
        pipeline.require_channel_keyed_copy(channels)
    except pipeline.StaleChannelPlanError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    assets, missing_placements = await _existing_assets(
        campaign_id, campaign_client=campaign_client
    )
    assets_with_urls = [await _asset_with_url(core_client, a) for a in assets]

    links = await _ensure_links(
        campaign_id,
        channels,
        campaign=campaign,
        admin_token=admin.token,
        base_url=public_base_url_for(request.headers.get("origin"), request.headers.get("referer")),
    )

    return {
        "campaign_id": campaign_id,
        "assets": assets_with_urls,
        "missing_placements": missing_placements,
        "copy": channels,
        "links": links,
        "guidance": campaign.get("guidance") or "",
    }
