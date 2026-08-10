"""The distribution pack (M5, issue #4) — the milestone's actual deliverable,
and what makes M5 "the first genuinely usable release" (M1-M4 only produce an
approved plan; nothing an operator can publish comes out of them).

A pack is four things, assembled from what earlier milestones already built —
this module writes no new generation logic of its own, only the render-once
and mint-once wiring around it:

1. **The assets** — the one approved source creative plus its three
   ``PLACEMENTS`` renders (``render.py``, M5's other half, already merged).
   Rendered, never regenerated: ``render.render`` is deterministic and
   effectively free, so :func:`_ensure_placements` below renders a placement
   at most once per campaign and reuses the stored ``marketing_asset`` row on
   every later call.
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

Its own module for the same reason ``image_routes.py`` is: rendering needs
the SigV4-signed internal client for object storage (presign / confirm /
mint a GET url), which ``admin_app._core``'s Cognito-bearer path cannot
reach.

## What this module deliberately does NOT do

It does not build a download or copy-to-clipboard UI — that is
``web-admin/``, out of scope for this change (a concurrent agent is expected
to build the frontend against this API). The day-0 design's two device
warnings therefore stay **unverified by this change**: a clipboard write
silently failing on mobile Safari, and iOS ``<a download>`` on a video
opening a preview instead of saving. Neither is reachable from here — there
is no device in this loop — and neither should be asserted fixed until
someone checks on a real phone.
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from fastapi import APIRouter, Depends, HTTPException, status
from PIL import Image

from . import admin_app, pipeline, principal_client
from .config import public_base_url
from .definitions import PLACEMENTS
from .links import destination_with_utms, mint_token, tracked_url
from .render import render

require_admin = admin_app.require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

#: Duplicated local constant — see ``channel_plan_routes._INTERNAL_PREFIX``
#: for why: the core-paths guard resolves a module-level string constant
#: only within the file that defines it.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: Matches ``image_routes._STORAGE_PATH`` — duplicated for the same reason,
#: not imported: see that module's own comment on ``_validated_campaign_id``.
_STORAGE_PATH = "/api/v1/internal/plugins/me/storage"

#: Generous timeouts, matching ``admin_app._CORE_TIMEOUT`` /
#: ``image_routes._UPLOAD_TIMEOUT``'s own reasoning: a cold start should read
#: as slow, never as broken.
_TRANSFER_TIMEOUT = 30.0

#: Pillow's own ``Image.format`` values, mapped to what this module needs to
#: upload a rendered placement honestly. ``render.render`` always writes PNG
#: except its exact-match no-op path, which returns the source's original
#: bytes untouched (see that module's docstring) — so the source's real
#: format has to be read back, not assumed.
_CONTENT_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}
_DEFAULT_FORMAT = "PNG"


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


#: `plugin_storage.presign_download` (biffo-template's `services/api/src/
#: api/plugin_storage.py`) mints its URL from a bare `boto3.client("s3")` —
#: no custom endpoint configured — so a legitimate download URL is always
#: this host family. CodeQL flags the `client.get(...)` below in `_download`
#: as a full server-side request forgery: the URL text is data-flow-tainted
#: by `campaign_id`, several hops upstream, and nothing before this line
#: proved the string is safe to fetch. This is that proof — reject anything
#: that is not an https URL to a real S3 host before ever making the
#: request, so a malformed or unexpected Core response cannot steer this
#: Lambda at an arbitrary origin.
_ALLOWED_DOWNLOAD_HOST_SUFFIX = ".amazonaws.com"


def _validate_download_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(_ALLOWED_DOWNLOAD_HOST_SUFFIX):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Core returned a download URL for an unexpected host.",
        )
    return url


def _core_error(exc: BiffoAPIError) -> HTTPException:
    """Matches ``image_routes``'s own mapping: storage's 503 ("not
    configured here") passes through as-is; everything else is 502, since
    Core answered and what it said is not something retrying *this* request
    fixes."""
    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)


async def _download(core_client: BiffoAPIClient, media_id: str) -> bytes:
    """The bytes behind one of this plugin's own stored objects — mint a
    short-lived GET url, then fetch it directly. There is no server-side
    download route (``internal_plugin_storage.py`` only ever mints a URL and
    lets the caller move the bytes); this Lambda plays the role a browser
    plays for ``image_routes._upload``'s presigned POST, one direction
    earlier in the same flow."""
    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{media_id}/url")
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    async with httpx.AsyncClient(timeout=_TRANSFER_TIMEOUT) as client:
        try:
            resp = await client.get(_validate_download_url(url_resp["url"]))
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not download the source creative: {exc}",
            ) from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not download the source creative: {resp.status_code}",
        )
    return resp.content


async def _upload(
    core_client: BiffoAPIClient, *, content: bytes, filename: str, content_type: str
) -> dict[str, Any]:
    """presign -> direct upload -> confirm. Mirrors ``image_routes._upload``
    exactly, generalised to take raw bytes rather than an ``ImageProvider``'s
    ``GeneratedImage`` — a rendered placement has no provider behind it."""
    try:
        presigned = await core_client.post(
            f"{_STORAGE_PATH}/presign", json={"filename": filename, "content_type": content_type}
        )
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    if len(content) > presigned["max_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Rendered placement is {len(content)} bytes, over this "
                f"deployment's {presigned['max_bytes']}-byte ceiling."
            ),
        )

    async with httpx.AsyncClient(timeout=_TRANSFER_TIMEOUT) as upload_client:
        try:
            upload = await upload_client.post(
                presigned["url"],
                data=presigned["fields"],
                files={"file": (filename, content, content_type)},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upload to object storage failed: {exc}",
            ) from exc

    if upload.status_code not in (200, 201, 204):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upload to object storage failed: {upload.status_code}",
        )

    try:
        return await core_client.post(f"{_STORAGE_PATH}/confirm", json={"key": presigned["key"]})
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc


async def _ensure_placements(
    campaign_id: str,
    *,
    core_client: BiffoAPIClient,
    campaign_client: principal_client.PrincipalCoreClient,
) -> list[dict[str, Any]]:
    """The campaign's source creative plus every ``PLACEMENTS`` render,
    rendering and storing only the ones that do not exist yet. Renders at
    most once per placement per campaign — every later call reuses the
    stored ``marketing_asset`` row, which is what makes "byte-identical on
    repeat" (the issue's own "done when") true of the whole pack, not just of
    ``render.render`` in isolation.

    Raises 404 if this campaign has no approved source creative
    (``is_source=True``) yet — there is nothing to render from.
    """
    assets = await campaign_client.get(
        f"{_INTERNAL_PREFIX}/assets", params={"campaign_id": campaign_id}
    )
    source = next((a for a in (assets or []) if a.get("is_source")), None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No approved source creative for this campaign yet.",
        )

    existing_by_placement = {
        a["placement"]: a for a in (assets or []) if not a.get("is_source") and a.get("placement")
    }

    out = [source]
    source_bytes: bytes | None = None
    for placement in PLACEMENTS:
        existing = existing_by_placement.get(placement)
        if existing is not None:
            out.append(existing)
            continue

        if source_bytes is None:
            source_bytes = await _download(core_client, source["media_id"])

        rendered = render(source_bytes, placement)
        with Image.open(io.BytesIO(rendered)) as image:
            fmt = image.format or _DEFAULT_FORMAT
        content_type = _CONTENT_TYPES.get(fmt, "application/octet-stream")
        extension = _EXTENSIONS.get(fmt, "png")

        media = await _upload(
            core_client,
            content=rendered,
            filename=f"{campaign_id}-{placement}.{extension}",
            content_type=content_type,
        )
        asset = await campaign_client.post(
            f"{_INTERNAL_PREFIX}/assets",
            json={
                "campaign_id": campaign_id,
                "media_kind": source.get("media_kind") or "image",
                "placement": placement,
                "media_id": media["id"],
                "is_source": False,
            },
        )
        out.append(asset)
    return out


async def _asset_with_url(core_client: BiffoAPIClient, asset: dict[str, Any]) -> dict[str, Any]:
    """One asset row plus a freshly-minted GET url — minted per request, per
    ``image_routes``' own docstring on why a URL is never stored."""
    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{asset['media_id']}/url")
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc
    return {**asset, "url": url_resp["url"]}


async def _ensure_links(
    campaign_id: str, channels: list[dict[str, Any]], *, campaign: dict[str, Any], admin_token: str
) -> list[dict[str, Any]]:
    """One tracked link per channel in the approved copy — minted once and
    reused on every later pack request, never re-minted, so opening the pack
    repeatedly does not spawn a fresh token per channel per visit.

    Mints with the exact same three pure functions
    ``admin_app.mint_links`` uses (``mint_token`` / ``destination_with_utms``
    / ``tracked_url``) and writes through the same ``admin_app._core`` seam,
    rather than composing a URL some fourth way.
    """
    links_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/links", admin_token, params={"campaign_id": campaign_id}
    )
    links_resp.raise_for_status()
    existing = links_resp.json() or []
    have_channels = {link["channel"] for link in existing}

    base_url = public_base_url()
    out = [
        {
            "channel": link["channel"],
            "variant": link.get("variant"),
            "is_paid": link.get("is_paid"),
            "url": tracked_url(base_url, link["token"]) if base_url else None,
        }
        for link in existing
    ]

    missing = [c for c in channels if c.get("channel") not in have_channels]
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
        channel = c["channel"]
        is_paid = c.get("motion") == "paid"
        token = mint_token()
        created = await admin_app._core(
            "POST",
            f"{_INTERNAL_PREFIX}/links",
            admin_token,
            json={
                "campaign_id": campaign_id,
                "token": token,
                "channel": channel,
                "variant": None,
                "is_paid": is_paid,
                "destination_url": destination_with_utms(
                    destination,
                    campaign_id=campaign_id,
                    channel=channel,
                    variant=None,
                    is_paid=is_paid,
                ),
            },
        )
        created.raise_for_status()
        out.append(
            {
                "channel": channel,
                "variant": None,
                "is_paid": is_paid,
                "url": tracked_url(base_url, token),
            }
        )
    return out


@router.get("/campaigns/{campaign_id}/pack")
async def get_pack_route(
    campaign_id: str,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Assemble this campaign's distribution pack: assets, copy, tracked
    links and guidance. Requires an **approved** ``copy`` artefact — a
    ``proposed`` one must not reach an operator's pack, or the copy approval
    gate is decorative, same as every other gate in this pipeline."""
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

    raw_body = copy_artefact.get("body")
    copy_body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})
    channels = copy_body.get("channels") or []

    assets = await _ensure_placements(
        campaign_id, core_client=core_client, campaign_client=campaign_client
    )
    assets_with_urls = [await _asset_with_url(core_client, a) for a in assets]

    links = await _ensure_links(campaign_id, channels, campaign=campaign, admin_token=admin.token)

    return {
        "campaign_id": campaign_id,
        "assets": assets_with_urls,
        "copy": channels,
        "links": links,
        "guidance": campaign.get("guidance") or "",
    }
