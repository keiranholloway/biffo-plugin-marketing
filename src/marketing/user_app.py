"""The campaign studio's franchise-unit-facing ASGI app (ADR-0021 ``user_ingress``).

Mounted by the shared plugin host at ``/api/v1/plugins/marketing``, gated on the
``founder`` Cognito group before this app sees a request (ADR-0011 —
authorization is a core concern, never plugin code). ``founder`` is the
estate's existing name for "an approved, ordinary product user" — both
``idea-scout`` and ``ideation`` gate their own ``user_ingress`` on it, and
``modules/cloud/aws/auth/main.tf`` describes the group itself as "Approved
product user (e.g. an invited founder). Access to user-facing modules." This
plugin reuses that convention rather than inventing a Tabsii-specific group
name for a capability the plugin contract already generalises over — see
``admin_app.py``'s own module docstring for why one manifest can serve both
Biffo-marketing-Biffo and Tabsii-marketing-Tabsii through the SAME two ingress
keys.

## Surface A vs. Surface B

``admin_app.py``'s docstring lays out the split: ``admin_ingress`` is the
operator marketing *the platform*; ``user_ingress`` — this file — is a
franchise unit marketing *its own services*. This change is deliberately the
smallest honest slice of the second surface: a unit can see which campaigns
are published and pull the material to actually promote one. Per-unit
localisation, unit-scoped visibility, and the full download-and-copy polish
the day-0 design describes are iteration 2
(``docs/design/marketing-plugin/campaign-studio.md`` §2, in
``tabsii-platform``) and are **not** built here.

## No built UI in this surface

Unlike ``admin_ingress``, the shared plugin host does not bake a
``StaticFiles`` mount into a ``user_ingress`` app at all — read
``services/_plugin-host/src/plugin_host/mount.py::build_host`` in
``biffo-template`` directly: only the ``/<name>/admin`` mount gets the
``_is_public_admin_asset`` exemption, and it is ``admin_app.py`` itself
(not the host) that mounts ``StaticFiles`` inside that app. A user-facing UI
is a separate manifest concept (``user_frontend``, hosted per ADR-0018) that
``idea-scout`` and ``ideation`` both declare and this plugin deliberately does
not yet: building and deploying a real SPA plus its own CloudFront
distribution is a materially bigger commitment than "declare the surface and
make it real", and biffo-template issue #558 (open) tracks consolidating that
whole mechanism into ADR-0021 rather than multiplying copies of it mid-
migration. So this surface is **JSON only** — a unit reaches it directly (or a
future UI calls it, the way ``web-admin/src/lib/api.ts`` calls
``admin_app.py``).

## Read-only, and why two tables' permissions had to change

Every route here reads ``marketing_campaign``, ``marketing_artefact`` (the
``copy`` kind only — see ``get_pack_route``) and ``marketing_asset``/
``marketing_link`` through the same dual-auth mount ``admin_app._core`` and
``principal_client.PrincipalCoreClient`` use (``principal_client``'s module
docstring has the full mechanism) — Core's own per-table ``required_role``
governs who can pass, regardless of which plugin surface forwarded the call.
Those tables' ``list``/``read`` permissions were ``["admin"]`` only; a
``founder``-group caller would 403 there even with a perfectly-formed
signed-and-forwarded request, because the host's group gate and Core's own
table permission are two independent checks (``forward.py``'s own docstring:
"Authorization for these routes is the table's own ADR-0004 permissions...
not the plugin's user_ingress.required_group"). So ``biffo.plugin.json``
opens ``list``/``read`` to ``required_role: []`` (any authenticated caller —
Core's ``dependencies.py``: "empty ``required_role`` authorises any
authenticated caller") on ``marketing_campaign``, ``marketing_artefact``,
``marketing_asset`` and ``marketing_link``. ``create``/``update``/``delete``
stay admin-only on every table — a unit never writes through this surface —
and ``marketing_click`` (raw per-click analytics: user agent, referrer) is
untouched in every operation, since no route here has a legitimate reason to
read it. ``[]`` is the established idiom for "any authenticated caller", not
a bespoke choice: idea-scout's own ``idea_scout_build_types``/
``idea_scout_model_catalog`` read permissions use it for exactly the same
reason.

**A real, accepted limitation this creates, broader than just this
surface.** The routes in *this file* run behind ``require_founder``, but the
table permission itself does not know that — Core's manifest-declared
``api_routes`` (``GET /api/v1/plugins/marketing/campaigns`` etc.) are
forwarded by the shared host **outside any group gate at all**
(``plugin_host/forward.py``'s own docstring: placed there deliberately so an
admin isn't rejected by a founder-only ``user_ingress`` gate). So opening
these four tables' ``list``/``read`` makes them reachable by **any
authenticated tenant caller** — admin, editor, viewer or founder alike — via
that generic CRUD path directly, not only by a ``founder``-group request
routed through this file. For ``marketing_artefact`` specifically that also
means every ``kind`` is reachable that way, not just the ``copy`` kind this
file's own routes request — a caller could read the internal
research/positioning/channel-plan artefacts too. Building either
per-caller-group or per-``kind`` permission granularity into Core's
permission model is out of scope for this change. Flagged here rather than
silently accepted, and tracked for follow-up rather than only documented —
this repo's issue #40.

## What "available" means, and what it deliberately does not check yet

A campaign is available to a unit once its ``status`` is ``ready`` or
``live``. The generic list route has no server-side filter for "one of a set
of values" (mirrors ``results_routes.py``'s own full-pagination-then-filter
precedent), so this fetches every campaign a founder can see and filters in
memory. There is no per-unit or per-locality scoping — every founder-group
caller sees every published campaign in the tenant, which matches ADR-0001's
current single-tenant reality and campaign-studio.md §2's own "Actors: one"
->"many, each with their own locality" line marking that as iteration 2's
job, not this one's.

## What the pack does NOT do, deliberately

Unlike the admin surface's ``pack_routes.py`` (a concurrent change in this
repo, deliberately not imported from or edited here to keep this change
independent of it), this route does not mint missing tracked links, does not
require a source creative to exist, and does not report
``missing_placements``. It only
assembles whatever already exists: existing assets resolved to fresh URLs,
existing links resolved to their tracked URL, and the latest **approved**
``copy`` artefact. A campaign whose admin has approved copy but not yet
generated any stills, or not yet minted every channel's link, produces a pack
with an empty ``assets`` or ``links`` list rather than a 404 or a write this
surface has no permission to make. That is an honest, minimal reading of "the
distribution pack material for one campaign" — not the richer, mutating
version the admin surface builds.
"""

from __future__ import annotations

from typing import Any

from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from biffo_plugin_sdk.user_serving import require_group
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status

from . import admin_app, principal_client
from .config import public_base_url_for
from .links import tracked_url

require_founder = require_group("founder")

#: Every `_core`-shaped call site below must carry this literal prefix — see
#: `tests/test_marketing_core_paths_guard.py`'s module docstring for why it
#: is a local constant rather than an import (the guard resolves a named
#: constant only within the file that defines it), and that file's own
#: disagreement test for why every copy across the plugin is asserted equal.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: Matches `image_routes._STORAGE_PATH` — duplicated for the same reason,
#: not imported (see `admin_app._validated_campaign_id`'s docstring on why a
#: stateful/path-bearing helper is duplicated per file rather than shared).
_STORAGE_PATH = "/api/v1/internal/plugins/me/storage"

#: Core's generic list route pages at up to 200 rows
#: (`routing/crud_handlers.py`, tabsii-platform) — mirrored as a literal here
#: rather than imported, matching `results_routes._LIST_PAGE_SIZE`'s own
#: reasoning: this plugin has no dependency on Core's own package.
_LIST_PAGE_SIZE = 200

#: The only statuses that make a campaign "available" to a unit — see the
#: module docstring. Not `definitions.PIPELINE_STAGES` wholesale: a unit must
#: never see `draft`/`researching`/`planned`/`generating` work in progress.
_AVAILABLE_STATUSES = frozenset({"ready", "live"})

router = APIRouter(dependencies=[Depends(require_founder)])


def get_core_client() -> BiffoAPIClient:
    """SigV4-signed, plugin-identity-only client for Core's `me` storage
    route — never table-permission-gated, so it needs no forwarded token.
    Matches `image_routes.get_core_client`/`pack_routes.get_core_client`,
    the same mechanism used there for the same route."""
    return create_core_client()


def get_campaign_client(
    founder: Any = Depends(require_founder),
) -> principal_client.PrincipalCoreClient:
    """Dual-auth client for this plugin's own generated-CRUD tables, carrying
    THIS founder's own forwarded token — same mechanism as `admin_app._core`
    and `image_routes.get_campaign_client` (issue #27's fix), just built from
    the founder gate instead of the admin one."""
    return principal_client.PrincipalCoreClient(founder.token)


def _core_error(exc: BiffoAPIError) -> HTTPException:
    """Matches `image_routes`/`pack_routes`'s own mapping: storage's 503
    ("not configured here") passes through as-is; everything else is 502,
    since Core answered and what it said is not something retrying *this*
    request fixes."""
    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)


async def _list_all(
    client: principal_client.PrincipalCoreClient, path: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every row Core holds for `path`/`params`, paged in full — mirrors
    `results_routes._list_all` exactly. Duplicated rather than imported:
    that module is admin-only (`router = APIRouter(dependencies=
    [Depends(admin_app.require_admin)])`) and this file must not gain an
    admin dependency by way of a shared helper."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_params = {**params, "limit": _LIST_PAGE_SIZE, "offset": offset}
        batch = await client.get(path, params=page_params)
        batch = batch or []
        rows.extend(batch)
        if len(batch) < _LIST_PAGE_SIZE:
            return rows
        offset += _LIST_PAGE_SIZE


def _campaign_summary(row: dict[str, Any]) -> dict[str, Any]:
    """The fields a unit needs to decide whether to open a campaign — not a
    passthrough of the whole row. Deliberately narrow so a future column
    added to `marketing_campaign` for admin/pipeline use does not reach a
    unit's browser by default."""
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "status": row.get("status") or "",
        "starts_at": row.get("starts_at"),
        "ends_at": row.get("ends_at"),
    }


@router.get("/campaigns")
async def list_campaigns_route(
    client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
) -> list[dict[str, Any]]:
    """Every campaign this founder can promote right now — `ready` or `live`
    only. See the module docstring for why draft/in-flight work never
    appears here."""
    rows = await _list_all(client, f"{_INTERNAL_PREFIX}/campaigns", {})
    return [_campaign_summary(r) for r in rows if (r.get("status") or "") in _AVAILABLE_STATUSES]


async def _resolve_asset_url(core_client: BiffoAPIClient, asset: dict[str, Any]) -> dict[str, Any]:
    """One asset row plus a freshly-minted GET url — minted per request, per
    `image_routes`' own docstring on why a URL is never stored. Skips (does
    not raise for) an asset row with no `media_id`, or a storage response
    with no `url` in it, rather than failing the whole pack on one malformed
    row — the second case mirrors the first: a response shaped differently
    than expected is not something retrying *this whole request* fixes any
    more than a missing `media_id` is."""
    media_id = asset.get("media_id")
    if not media_id:
        return {**asset, "url": None}
    try:
        url_resp = await core_client.get(f"{_STORAGE_PATH}/{media_id}/url")
    except BiffoAPIError as exc:
        raise _core_error(exc) from exc
    return {**asset, "url": url_resp.get("url")}


@router.get("/campaigns/{campaign_id}/pack")
async def get_pack_route(
    campaign_id: str,
    request: Request,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    founder: Any = Depends(require_founder),
) -> dict[str, Any]:
    """The material a unit needs to actually publish this campaign: its
    guidance text, the latest **approved** copy, every existing asset
    resolved to a real URL, and every already-minted tracked link. Nothing
    here is generated or minted — that stays an admin action
    (`admin_app.mint_links`, `image_routes.generate_still_route`); this route
    only assembles what already exists. See the module docstring's "What the
    pack does NOT do" section for how this differs from the admin surface's
    own pack.
    """
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    campaign_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", founder.token
    )
    if campaign_resp.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign_resp.raise_for_status()
    campaign = campaign_resp.json() or {}

    if (campaign.get("status") or "") not in _AVAILABLE_STATUSES:
        # Same 404 as "does not exist" — a unit has no legitimate way to
        # learn a draft/in-flight campaign exists at all.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")

    # `_latest_approved_artefact`, not `_latest_artefact` (issue #41): a
    # newer pending/proposed copy re-run must not make a pack that was
    # serving fine start 404ing the moment an admin starts an edit — the
    # previously-approved copy is still sitting one row back and this is
    # exactly what a unit should still be served.
    copy_artefact = await admin_app._latest_approved_artefact(campaign_id, "copy", founder.token)
    if copy_artefact is None:
        # Collapsed to one outcome rather than admin_app's 409-for-proposed:
        # a founder cannot approve anything, so "exists but not approved yet"
        # and "does not exist yet" carry the same actionable information —
        # none — and 409 would surface internal pipeline state for nothing.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No approved copy for this campaign yet.",
        )
    copy_body = admin_app._parse_artefact_body(copy_artefact.get("body"))
    channels = copy_body.get("channels") or []

    asset_rows = await _list_all(
        campaign_client, f"{_INTERNAL_PREFIX}/assets", {"campaign_id": campaign_id}
    )
    assets = [await _resolve_asset_url(core_client, a) for a in asset_rows]

    link_rows = await _list_all(
        campaign_client, f"{_INTERNAL_PREFIX}/links", {"campaign_id": campaign_id}
    )
    base_url = public_base_url_for(request.headers.get("origin"), request.headers.get("referer"))
    links = [
        {
            "channel": link.get("channel") or "",
            "variant": link.get("variant"),
            "is_paid": bool(link.get("is_paid")),
            "url": tracked_url(base_url, link["token"]) if base_url and link.get("token") else None,
        }
        for link in link_rows
    ]

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign.get("name") or "",
        "guidance": campaign.get("guidance") or "",
        "copy": channels,
        "assets": assets,
        "links": links,
    }


def build_app() -> FastAPI:
    """The user (founder) ASGI app — JSON only, no static mount. See the
    module docstring's "No built UI in this surface" section for why."""
    app = FastAPI(title="Marketing — campaign studio (unit)")
    app.include_router(router)
    return app


app = build_app()
