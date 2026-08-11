"""Still-image generation (M6, issue #5): prompt -> stored asset -> ledgered cost.

A new module rather than a route added to `admin_app.py`: other work lands in
that file concurrently, and `admin_app.py` gains exactly one line — the
`include_router` call below wires this in, ahead of its `StaticFiles` mount
(which must stay last — see `admin_app`'s module docstring).

The flow:

1. `image_provider.ImageProvider.generate_still` — behind the port, so a
   provider swap never touches the route, the storage upload, or the
   ledger-write payload below (issue #5's requirement 3). The one place this
   file is *meant* to know a choice exists is `get_image_provider()`, whose
   body is entirely `image_provider.create_image_provider()` — it names no
   vendor itself, only the generic call that resolves one (issue #63). **This
   is the only irreversible, billable step in this handler** — the provider
   has been paid before this function has written anything at all.
2. Record the generation in Core's media ledger (biffo-template#1439) —
   **immediately**, before storage or the asset row. Issue #24: every step
   after the charge is local bookkeeping Core can retry or reconcile, but the
   ledger row is the only durable evidence the charge happened, so it is
   written as early as it can be rather than last. See
   `generate_still_route`'s own docstring for exactly what this guarantees
   and what it does not.
3. Store the bytes via Core's plugin object-storage capability
   (biffo-template#1437): presign, upload, confirm with `head_object`. The
   documented flow is "browser PUT" because the capability's usual caller is
   a user uploading a file — there is no browser in a machine-generated
   flow, so this Lambda plays that role instead: a presigned POST is just an
   HTTP request, and nothing about it requires a browser to send it. Bytes
   still never pass through **Core's** Lambda, which only signs and later
   verifies with `head_object` — the property the precondition actually
   promises.

Then a `marketing_asset` row is created with `is_source=True`: the ONE
approved creative placements are later rendered from (`render.py`, M5) —
never a placement render itself. This route is the only path in this plugin
that calls a provider to *create* image bytes, so every asset it writes is,
by construction, a source.

Both internal Core routes (storage, ledger) are SigV4-signed as
`system:marketing` (ADR-0009) via the SDK's `create_core_client()` — the same
mechanism `admin_app._CoreAgentGateway` uses (M3). Reading/writing this
plugin's own `marketing_campaign` / `marketing_asset` tables uses the
ordinary Cognito-bearer path instead, via `biffo_plugin_sdk.BiffoAPIClient`
directly rather than `admin_app._core`: importing back from the module this
router is included INTO would be a circular import, and reaching into
`admin_app` for it would grow this module's edit of that file past the single
include line the boundary asks for.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from aws_lambda_powertools import Logger
from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from biffo_plugin_sdk.user_serving import require_group
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import principal_client
from .image_provider import (
    GeneratedImage,
    ImageProvider,
    ImageProviderError,
    create_image_provider,
)

logger = Logger(child=True)

require_admin = require_group("admin")

router = APIRouter(dependencies=[Depends(require_admin)])

_STORAGE_PATH = "/api/v1/internal/plugins/me/storage"
_LEDGER_PATH = "/api/v1/internal/media-generations"

#: Where `get_campaign_client()`'s dual-auth client reaches this plugin's own
#: generated-CRUD tables (`marketing_campaign`, `marketing_asset`) — Core's
#: internal, per-plugin mount, never the public one. See `admin_app.py`'s
#: `_INTERNAL_PREFIX` for why this is its own local constant rather than an
#: import: the guard test below resolves a named constant only within the
#: file that defines it.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: A plain client's own upload timeout, matching `admin_app._CORE_TIMEOUT`'s
#: reasoning: generous enough that a cold start or a slow provider does not
#: read as a broken feature.
_UPLOAD_TIMEOUT = 30.0


def _validated_campaign_id(campaign_id: str) -> str:
    """Identical guard to `admin_app._validated_campaign_id`, deliberately
    duplicated rather than imported (see the module docstring): `campaign_id`
    below is interpolated into an HTTP path this plugin calls with its own
    credentials, and CodeQL flagged exactly that shape as a partial SSRF on
    the first version of `admin_app.mint_links`. A pure UUID-parse-and-
    re-render has no logic that can drift between two copies the way a
    stateful helper could — see `admin_app`'s copy for the full reasoning.
    """
    try:
        parsed = uuid.UUID(campaign_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found."
        ) from None
    return str(parsed)


def get_image_provider() -> ImageProvider:
    """One provider per request, matching `admin_app.get_agent_gateway`'s
    shape. Which concrete class gets constructed is
    `image_provider.create_image_provider`'s call, driven by the
    `MARKETING_IMAGE_PROVIDER` env var — this function, and this file, still
    name no vendor directly. Tests override this dependency with a fake —
    proving issue #5's requirement 3 (a provider swap is an implementation
    change, not a rewrite) rather than merely asserting it in prose; the
    live `create_image_provider` choice (issue #63) is the same claim proven
    a second, independent way, because a factory naming which concrete
    implementation to build is the one place that is *meant* to know about
    them — the route handler below, the storage upload, and the ledger
    payload it writes still touch none of it.

    A misconfigured `MARKETING_IMAGE_PROVIDER` fails here, as a 502, rather
    than reaching the route body at all — the same mapping `ImageProviderError`
    gets everywhere else in this router (see `generate_still_route` below),
    not a bare 500 an operator would have to guess the cause of.
    """
    try:
        return create_image_provider()
    except ImageProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def get_core_client() -> BiffoAPIClient:
    """SigV4-signed by default (ADR-0009): the internal storage and ledger
    routes are IAM-only, never Cognito. See `admin_app._CoreAgentGateway` for
    the same mechanism, used there for the internal agent-run API instead.
    """
    return create_core_client()


def get_campaign_client(
    admin: Any = Depends(require_admin),
) -> principal_client.PrincipalCoreClient:
    """A dual-auth client for this plugin's own generated-CRUD tables —
    `marketing_campaign` and `marketing_asset` — SigV4-signed AND carrying
    the calling admin's own forwarded token, exactly like `admin_app._core`
    (see `principal_client`'s module docstring for why both are required
    together, not either alone: this hits the same
    `require_principal_crud_permission`-guarded internal mount `_core` does).
    """
    return principal_client.PrincipalCoreClient(admin.token)


class GenerateStillRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    #: Ties this generation to the agent chain that produced the prompt, if
    #: any — optional, because an operator can also type a prompt directly
    #: with no chain behind it at all.
    causation_id: str | None = Field(default=None, max_length=255)


class GenerateStillResponse(BaseModel):
    asset: dict[str, Any]
    media: dict[str, Any]
    url: str
    ledger: dict[str, Any]


def _core_error(exc: BiffoAPIError, *, not_found: str | None = None) -> HTTPException:
    """Map a Core failure to the HTTP status a caller should see.

    A 404 is only ever "the thing you named does not exist" when the caller
    passes `not_found` for exactly that lookup — reusing 404 for an
    unrelated Core failure would read as "campaign not found" when the real
    cause is something else entirely. Storage's own 503 ("not configured
    here") passes through as-is, matching `internal_plugin_storage.py`'s own
    posture in Core. Everything else is 502: Core answered, but not with
    something a retry of *this* request fixes — mirrors
    `admin_app._pipeline_error_to_http`.
    """
    if not_found is not None and exc.status_code == status.HTTP_404_NOT_FOUND:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)


def _required_field(payload: Any, key: str, *, context: str) -> Any:
    """``payload[key]``, or a 502 naming what broke instead of a bare
    `KeyError` (or worse, an unhandled `TypeError`).

    Absorbed from issue #26 (see issue #24's comments): every call site below
    reads this straight off a Core response with no guard, so a response-shape
    drift becomes an unhandled 500 rather than a diagnosable error — and on
    this route, every one of these reads happens **after** the provider has
    already been paid (issue #24), so losing the message here would also
    swallow the evidence (a ledger/media id) the caller needs to avoid a blind
    retry.

    ``payload`` is typed `Any`, not `dict[str, Any]`, and both `KeyError` and
    `TypeError` are caught: the SDK's own `_parse_json` returns `None` for any
    empty-body 2xx, and `None[key]` raises `TypeError`, not `KeyError` — a
    `dict`-only guard would miss exactly the response-shape drift this
    function exists to catch. Not currently reachable against Core's own
    routes (they all declare a `response_model`, so an empty body cannot pass
    validation), but nothing here should depend on that staying true.
    """
    try:
        return payload[key]
    except (KeyError, TypeError) as exc:
        logger.error("%s response had no %r: %r", context, key, payload)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{context} response had no '{key}'.",
        ) from exc


def _committed_note(*, charged: bool = False, **ids: str | None) -> str:
    """A human-readable list of what already committed, appended to an error
    raised after the provider has been charged (issue #24) — so an operator
    reading the failure knows a blind retry would double the charge rather
    than merely retry a step that never touched money.

    ``charged=True`` with no ``ids`` covers the one case where there is a
    charge but nothing to name yet: the ledger POST succeeded but its `id`
    could not be read out of the response. There is still no id to print, but
    "nothing to report" would be worse than silence — it would read as though
    the charge itself were still in doubt, when it is not.
    """
    parts = [f"{name} {value}" for name, value in ids.items() if value]
    if parts:
        return " Already recorded: " + ", ".join(parts) + ". Do not regenerate blindly."
    if charged:
        return (
            " The provider call already succeeded and WAS charged — its ledger id could "
            "not be read from Core's response, so check the media-generations ledger by "
            "hand before regenerating."
        )
    return ""


def _with_committed_note(
    exc: HTTPException, *, charged: bool = False, **ids: str | None
) -> HTTPException:
    """`exc`, with `_committed_note`'s context appended to its detail and its
    status code preserved."""
    note = _committed_note(charged=charged, **ids)
    if not note:
        return exc
    return HTTPException(status_code=exc.status_code, detail=f"{exc.detail}{note}")


async def _upload(core_client: BiffoAPIClient, image: GeneratedImage) -> dict[str, Any]:
    """presign -> direct upload -> confirm.

    This Lambda plays the role a browser plays for a user-supplied file (see
    the module docstring): the presigned POST Core mints is just an HTTP
    request, and nothing about it requires a browser to send it.

    Called *after* the ledger write (issue #24) — every failure raised here
    happens after the provider has already been paid and the charge is
    already durably recorded, so callers wrap what this raises with
    `_with_committed_note` rather than surfacing it bare.
    """
    try:
        presigned = await core_client.post(
            f"{_STORAGE_PATH}/presign",
            json={"filename": image.filename, "content_type": image.content_type},
        )
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    max_bytes = _required_field(presigned, "max_bytes", context="Presign")
    if len(image.content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Generated image is {len(image.content)} bytes, over this "
                f"deployment's {max_bytes}-byte ceiling."
            ),
        )

    # Every field read off `presigned` from here on is guarded the same way
    # as `max_bytes` above — a response missing any of these is exactly the
    # same class of Core response-shape drift, and by this point in the
    # module docstring's ordering (issue #24) the ledger has already been
    # written, so an unguarded `KeyError` here would still be a post-charge
    # failure worth a diagnosable message.
    presign_url = _required_field(presigned, "url", context="Presign")
    presign_fields = _required_field(presigned, "fields", context="Presign")
    presign_key = _required_field(presigned, "key", context="Presign")

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as upload_client:
        try:
            upload = await upload_client.post(
                presign_url,
                data=presign_fields,
                files={"file": (image.filename, image.content, image.content_type)},
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
        return await core_client.post(f"{_STORAGE_PATH}/confirm", json={"key": presign_key})
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc


@router.post("/campaigns/{campaign_id}/stills", status_code=status.HTTP_201_CREATED)
async def generate_still_route(
    campaign_id: str,
    body: GenerateStillRequest,
    provider: ImageProvider = Depends(get_image_provider),
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
) -> GenerateStillResponse:
    """Generate one still, ledger its cost, store it, and record it as this
    campaign's approved source creative.

    **Ordering is the fix for issue #24.** `provider.generate_still` is the
    only irreversible, billable step here — money has moved before this
    function has written anything at all. Everything after it (storage
    upload, the asset row) is local bookkeeping Core can retry or reconcile.
    The ledger write is the one exception: it is the only durable evidence
    the charge happened, so it is made **immediately** after the charge,
    before upload or the asset row — not last, as it was before this fix.

    **What this guarantees:** once the provider call succeeds, the very next
    thing this handler does is ledger it. If upload or asset creation then
    fails, the failure is raised with the ledger id (and the media id, once
    storage has succeeded) named in its detail, so an operator reading the
    error knows a blind retry would double the charge rather than merely
    retry a step that never touched money.

    **What this does NOT guarantee:** this is not exactly-once. Core's ledger
    route has no idempotency key, so a failure of the ledger POST *itself*
    (the one write that must happen first, and the one write nothing here can
    move earlier) still leaves a charge with no queryable record short of a
    log line, and a subsequent retry of the whole request would charge again.
    That gap is logged loudly (see below) but not closed — closing it needs
    an idempotency key Core's `/internal/media-generations` route does not
    yet accept, which is a Core-side change, not something this route can
    build around on its own.
    """
    campaign_id = _validated_campaign_id(campaign_id)

    try:
        await campaign_client.get(f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}")
    except BiffoAPIError as exc:
        raise _core_error(exc, not_found="Campaign not found.") from exc

    try:
        image = await provider.generate_still(prompt=body.prompt)
    except ImageProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # The provider has now been paid. Ledger it before anything else — see
    # the docstring above for why this cannot wait until after upload/asset
    # creation.
    try:
        ledger = await core_client.post(
            _LEDGER_PATH,
            json={
                "media_kind": "image",
                "provider": image.provider,
                "model": image.model,
                "units": image.units,
                "unit_kind": image.unit_kind,
                "cost_usd": image.cost_usd,
                "causation_id": body.causation_id,
            },
        )
    except BiffoAPIError as exc:
        # This is the one gap this fix cannot close (see the docstring): the
        # charge happened and could not even be ledgered. Log every field a
        # human would need to reconcile it by hand, since a log line is the
        # only record that will exist.
        logger.error(
            "Provider %s/%s charged campaign %s (units=%s unit_kind=%s cost_usd=%s "
            "causation_id=%s) but the ledger write failed: %s. A retry WILL double-bill "
            "unless this is reconciled by hand.",
            image.provider,
            image.model,
            campaign_id,
            image.units,
            image.unit_kind,
            image.cost_usd,
            body.causation_id,
            exc.detail,
        )
        raise _core_error(exc) from exc

    # The charge is already durably recorded at this point — see the
    # docstring above. Every failure from here on wraps its error with
    # `_with_committed_note` so a caller reading it sees what already
    # committed, never a bare failure that reads like nothing happened.
    try:
        ledger_id = _required_field(ledger, "id", context="Ledger")
    except HTTPException as exc:
        raise _with_committed_note(exc, charged=True) from exc

    try:
        media = await _upload(core_client, image)
    except HTTPException as exc:
        raise _with_committed_note(exc, ledger=ledger_id) from exc

    try:
        media_id = _required_field(media, "id", context="Storage confirm")
    except HTTPException as exc:
        raise _with_committed_note(exc, ledger=ledger_id) from exc

    try:
        asset = await campaign_client.post(
            f"{_INTERNAL_PREFIX}/assets",
            json={
                "campaign_id": campaign_id,
                "media_kind": "image",
                "placement": None,
                "media_id": media_id,
                "is_source": True,
            },
        )
    except BiffoAPIError as exc:
        raise _with_committed_note(_core_error(exc), ledger=ledger_id, media=media_id) from exc

    try:
        asset_id = _required_field(asset, "id", context="Asset")
    except HTTPException as exc:
        raise _with_committed_note(exc, ledger=ledger_id, media=media_id) from exc

    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{media_id}/url")
    except BiffoAPIError as exc:
        raise _with_committed_note(
            _core_error(exc), ledger=ledger_id, media=media_id, asset=asset_id
        ) from exc

    # Everything up to and including the asset row has committed by this
    # point — a failure minting the signed URL below is the one failure mode
    # left that touches nothing billable or durable; it is always safe to
    # retry on its own, never a reason to regenerate.
    try:
        url = _required_field(url_resp, "url", context="Signed URL")
    except HTTPException as exc:
        raise _with_committed_note(exc, ledger=ledger_id, media=media_id, asset=asset_id) from exc

    return GenerateStillResponse(
        asset=asset,
        media=media,
        url=url,
        ledger={
            "id": ledger_id,
            "cost_usd": image.cost_usd,
            "unpriced": image.cost_usd is None,
        },
    )
