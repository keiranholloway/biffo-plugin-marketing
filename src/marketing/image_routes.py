"""Still-image generation (M6, issue #5): prompt -> stored asset -> ledgered cost.

A new module rather than a route added to `admin_app.py`: other work lands in
that file concurrently, and `admin_app.py` gains exactly one line — the
`include_router` call below wires this in, ahead of its `StaticFiles` mount
(which must stay last — see `admin_app`'s module docstring).

The flow, in the order issue #5 asks for it:

1. `image_provider.ImageProvider.generate_still` — behind the port, so a
   provider swap never touches this file (issue #5's requirement 3).
2. Store the bytes via Core's plugin object-storage capability
   (biffo-template#1437): presign, upload, confirm with `head_object`. The
   documented flow is "browser PUT" because the capability's usual caller is
   a user uploading a file — there is no browser in a machine-generated
   flow, so this Lambda plays that role instead: a presigned POST is just an
   HTTP request, and nothing about it requires a browser to send it. Bytes
   still never pass through **Core's** Lambda, which only signs and later
   verifies with `head_object` — the property the precondition actually
   promises.
3. Record the generation in Core's media ledger (biffo-template#1439): a
   cost, or visibly `unpriced` — never silently zero.

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
from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from biffo_plugin_sdk.user_serving import require_group
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .image_provider import GeneratedImage, ImageProvider, ImageProviderError, OpenAIImageProvider

require_admin = require_group("admin")

router = APIRouter(dependencies=[Depends(require_admin)])

_STORAGE_PATH = "/api/v1/internal/plugins/me/storage"
_LEDGER_PATH = "/api/v1/internal/media-generations"

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
    shape. `OpenAIImageProvider` is the only production implementation this
    milestone ships; tests override this dependency with a fake — proving
    issue #5's requirement 3 (a provider swap is an implementation change,
    not a rewrite) rather than merely asserting it in prose.
    """
    return OpenAIImageProvider()


def get_core_client() -> BiffoAPIClient:
    """SigV4-signed by default (ADR-0009): the internal storage and ledger
    routes are IAM-only, never Cognito. See `admin_app._CoreAgentGateway` for
    the same mechanism, used there for the internal agent-run API instead.
    """
    return create_core_client()


def get_campaign_client(admin: Any = Depends(require_admin)) -> BiffoAPIClient:
    """A plain, bearer-token client for this plugin's own generated-CRUD
    tables — `marketing_campaign` and `marketing_asset` — scoped to the
    calling admin's own forwarded token, exactly like `admin_app._core`.
    """
    return BiffoAPIClient(token=admin.token)


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


async def _upload(core_client: BiffoAPIClient, image: GeneratedImage) -> dict[str, Any]:
    """presign -> direct upload -> confirm.

    This Lambda plays the role a browser plays for a user-supplied file (see
    the module docstring): the presigned POST Core mints is just an HTTP
    request, and nothing about it requires a browser to send it.
    """
    try:
        presigned = await core_client.post(
            f"{_STORAGE_PATH}/presign",
            json={"filename": image.filename, "content_type": image.content_type},
        )
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    if len(image.content) > presigned["max_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Generated image is {len(image.content)} bytes, over this "
                f"deployment's {presigned['max_bytes']}-byte ceiling."
            ),
        )

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as upload_client:
        try:
            upload = await upload_client.post(
                presigned["url"],
                data=presigned["fields"],
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
        return await core_client.post(f"{_STORAGE_PATH}/confirm", json={"key": presigned["key"]})
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc


@router.post("/campaigns/{campaign_id}/stills", status_code=status.HTTP_201_CREATED)
async def generate_still_route(
    campaign_id: str,
    body: GenerateStillRequest,
    provider: ImageProvider = Depends(get_image_provider),
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: BiffoAPIClient = Depends(get_campaign_client),
) -> GenerateStillResponse:
    """Generate one still, store it, ledger its cost (or its absence), and
    record it as this campaign's approved source creative.
    """
    campaign_id = _validated_campaign_id(campaign_id)

    try:
        await campaign_client.get(f"/campaigns/{campaign_id}")
    except BiffoAPIError as exc:
        raise _core_error(exc, not_found="Campaign not found.") from exc

    try:
        image = await provider.generate_still(prompt=body.prompt)
    except ImageProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    media = await _upload(core_client, image)

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
        raise _core_error(exc) from exc

    try:
        asset = await campaign_client.post(
            "/assets",
            json={
                "campaign_id": campaign_id,
                "media_kind": "image",
                "placement": None,
                "media_id": media["id"],
                "is_source": True,
            },
        )
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{media['id']}/url")
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc

    return GenerateStillResponse(
        asset=asset,
        media=media,
        url=url_resp["url"],
        ledger={
            "id": ledger["id"],
            "cost_usd": image.cost_usd,
            "unpriced": image.cost_usd is None,
        },
    )
