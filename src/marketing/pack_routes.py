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

That same capability is why the assets-only read, ``list_assets_route``,
lives here too rather than next to ``image_routes.generate_still_route``:
it needs exactly the SigV4 client and the two helpers this module already
has, and ``image_routes`` cannot import this module (``pack_routes`` imports
``admin_app``, which includes ``image_routes``' router — a cycle). See that
route's own docstring for why it is not simply a call to ``/pack``.

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
) -> tuple[list[dict[str, Any]], list[str], int]:
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
    #64 deleted the single ``next((a for a in assets if a.get("is_source")),
    None)`` pick along with the 404 it guarded — this function no longer
    picks a source at all, which is issue #54's premise as originally filed:
    that literal call site is gone.

    **The invariant it guarded is not gone, though.**
    ``image_routes.generate_still_route`` writes a new ``is_source=True``
    row on every call and nothing supersedes the one it replaces (still
    true today — issue #54, and out of scope for this function: that write
    path belongs to a concurrent change, see ``image_routes.py``), so more
    than one can exist for a real campaign. Left unfiltered, the pack would
    show every one of them labelled "Source" (``PackParts.tsx``:
    ``a.is_source === true ? 'Source' : ...``), with no way for an operator
    to tell which is actually in effect, and no guarantee the set stays the
    same between two requests. So this function picks, deterministically,
    which single row gets to keep ``is_source: True`` in what it returns:
    **newest ``created_at`` wins** — the most recent generation is what an
    operator who just clicked "generate again" means by "the" creative, and
    the admin UI already reinforces that reading by rendering the newest
    generation first. Every other ``is_source=True`` row is dropped from the
    response outright (not merely relabelled — a superseded source with no
    placement carries nothing else worth showing). This is a presentation
    fix, pure function of ``assets`` and therefore byte-identical on repeat
    for the same underlying rows; it does not touch Core, so it cannot by
    itself restore "exactly one source per campaign" as a stored fact — only
    ``generate_still_route`` superseding the prior row can do that.

    The count dropped is returned as this function's third element rather
    than only logged: this module's own convention for "the API silently
    hid something" is response-level disclosure (``missing_placements``
    already does exactly this), not a log line only CloudWatch sees — and
    without it, every request looks clean regardless of how many duplicate
    source rows have piled up, so nothing short of reading the raw
    ``marketing_asset`` table would ever surface the underlying #54 gap to
    an operator or a future maintainer.
    """
    assets = await campaign_client.get(
        f"{_INTERNAL_PREFIX}/assets", params={"campaign_id": campaign_id}
    )
    assets = assets or []

    sources = [a for a in assets if a.get("is_source")]
    superseded_source_count = 0
    if len(sources) > 1:
        canonical_id = max(sources, key=lambda a: (a.get("created_at") or "", a.get("id") or ""))[
            "id"
        ]
        superseded_source_count = len(sources) - 1
        assets = [a for a in assets if not a.get("is_source") or a.get("id") == canonical_id]

    have_placements = {a["placement"] for a in assets if a.get("placement")}
    missing_placements = [p for p in PLACEMENTS if p not in have_placements]
    return assets, missing_placements, superseded_source_count


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


def _link_sort_key(link: dict[str, Any]) -> tuple[bool, str, str]:
    """A total order over a pack's ``links`` that does not depend on which
    of them were already in Core and which were just minted (issue #94).

    Before this, ``_ensure_links`` returned ``existing`` links (Core's own
    list-route order, which carries no ``ORDER BY`` and is not guaranteed
    stable) followed by whatever was minted just now (``channels`` iteration
    order) appended after them. The two orderings agree by coincidence at
    most once: the very first call that mints every link. Every later call
    is all "existing" and reflects Core's order instead, which can — and in
    the field, did — differ from the mint call's order. An operator refreshing
    the same pack then sees the same links reshuffle for no reason, and
    anything indexing ``links[n]`` positionally (this plugin does not, but a
    caller reasonably could) gets a different answer each time.

    Sorting the same key on every return path, regardless of whether a given
    entry came from ``existing`` or was just minted, means the two paths
    cannot diverge again — there is only one code path that decides order,
    not two that happen to agree.

    Organic before paid (`is_paid` ascending: `False`/`None` before `True`)
    is the "reading order the pack otherwise implies" the issue itself names
    — copy and creative are organic-first throughout this pack, so the links
    list matching that is what an operator actually expects on top. Within a
    motion, ``channel`` (a taxonomy ``channel_key``, short and stable — see
    ``_ensure_links``'s own docstring) breaks ties alphabetically; ``variant``
    (currently always ``None`` from this function, but not from generic CRUD
    ``create``, which any admin can call directly) breaks any further tie so
    the order stays fully deterministic even for two rows that otherwise
    look identical.
    """
    return (
        link.get("is_paid") is True,
        str(link.get("channel") or ""),
        str(link.get("variant") or ""),
    )


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

    Filtered to ``channels`` and keyed on ``(channel_key, is_paid)``, not
    the bare ``channel_key`` alone (issue #48). ``existing`` is every link
    Core has ever minted for the campaign — channel planning can be re-run,
    so it can hold links for channels this call's ``channels`` no longer
    names at all, and those must not leak into ``out``.
    ``paid_pack_routes.get_paid_pack_route`` used to filter its own result
    back down for exactly this reason; that workaround is gone now that the
    filter lives here, at the source, where every caller gets it for free.
    The dedup is motion-aware for the same reason: the seeded taxonomy gives
    an organic and a paid version of "the same" channel distinct
    ``channel_key``s (#76), but nothing in this plugin enforces that a
    tenant's own custom channel keeps a key motion-exclusive — a bare
    ``channel_key`` match would let an existing organic link suppress the
    mint of the paid one this call actually asked for.

    Both the "already have" and "is wanted" checks below go through
    :func:`_link_motions`/``_requested_key`` rather than a tuple literal
    repeated at each call site — two symmetric-looking expressions that are
    actually asymmetric (``channel``/``is_paid`` vs ``channel_key``/
    ``motion``) is exactly the shape that lets one future edit (e.g. adding
    ``variant`` to the key) update three of four sites and silently reopen
    #48. ``marketing_link.is_paid`` is nullable, and generic CRUD's own
    ``create`` is exposed to any admin, not only this function's own mint
    loop (which always writes a concrete bool) — so a ``NULL`` row is a real
    possibility, not a hypothetical. It is deliberately NOT folded to
    ``False``: ``results_routes.py`` already rejected that exact fold for
    this same column ("folding that into organic ... is exactly the 'share
    over classifiable inputs' mistake"), and doing it here would let one
    ambiguous row both trigger a duplicate mint (it would not satisfy the
    paid request) and vanish from a paid pack's own ``links`` (it would not
    match the paid filter either). A ``NULL`` row is instead treated as
    already covering *whichever* motion is asked for — "don't know" must
    never justify writing a second link, and must never make an existing
    one disappear from the pack that already shows it.
    """
    links_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/links", admin_token, params={"campaign_id": campaign_id}
    )
    links_resp.raise_for_status()
    existing = links_resp.json() or []

    def _requested_key(c: dict[str, Any]) -> tuple[Any, bool]:
        return c.get("channel_key"), c.get("motion") == "paid"

    def _link_motions(link: dict[str, Any]) -> set[bool]:
        is_paid = link.get("is_paid")
        return {True, False} if is_paid is None else {bool(is_paid)}

    requested = {_requested_key(c) for c in channels}

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
        if any((link["channel"], motion) in requested for motion in _link_motions(link))
    ]

    channel_motions: dict[Any, set[bool]] = {}
    for link in existing:
        channel_motions.setdefault(link["channel"], set()).update(_link_motions(link))

    missing = [
        c
        for c in channels
        if _requested_key(c)[1] not in channel_motions.get(_requested_key(c)[0], set())
    ]
    if missing:
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

    # Sorted once, on the way out, regardless of which entries came from
    # `existing` (Core's own unordered list) and which were just minted
    # (`channels` iteration order) — see `_link_sort_key`'s own docstring
    # for why a single shared key is what keeps the mint call and every
    # later fetch call in agreement (issue #94).
    return sorted(out, key=_link_sort_key)


@router.get("/campaigns/{campaign_id}/assets")
async def list_assets_route(
    campaign_id: str,
    core_client: BiffoAPIClient = Depends(get_core_client),
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Every ``marketing_asset`` row this campaign already has, resolved to a
    fresh GET url — the assets half of the pack, on its own, with **no copy
    gate and no side effects**.

    This exists because of issue #102, which is a money defect rather than a
    cosmetic one. The admin UI's stills panel held generated stills in React
    state only, so a campaign whose creative was generated in any earlier
    session rendered "No stills generated yet this session" — indistinguishable
    from "this campaign has no creative". Image generation is the one
    irreversibly billable step in this plugin (``image_routes.py``'s own
    ordering rationale for issue #24), so an empty state that cannot tell
    those two apart actively invites a duplicate paid generation.

    **Why the frontend could not just call ``/pack`` for this.** The pack is
    a different question with two properties that make it wrong here:

    1. It is **gated on approved copy** — 404 with no copy artefact, 409 with
       one that is not approved yet. Generating a still before copy is
       approved is a perfectly ordinary order of work, and that is exactly
       the campaign whose operator most needs to see what already exists.
    2. It **mints tracked links as a side effect** (``_ensure_links``). A
       panel that loads on mount must not write rows, and must not depend on
       ``destination_url``/a public base URL being configured — both of which
       ``_ensure_links`` can legitimately 422/503 on.

    So this is a read: campaign existence, then ``_existing_assets`` and
    ``_asset_with_url``, the same two helpers ``get_pack_route`` uses — which
    is deliberately the whole implementation. Reusing ``_existing_assets``
    means the newest-source-wins dedup and the ``superseded_source_count``
    disclosure it returns are the same here as in the pack, rather than one
    surface hiding duplicate sources and another showing them.
    """
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    campaign_resp = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", admin.token
    )
    if campaign_resp.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign_resp.raise_for_status()

    assets, missing_placements, superseded_source_count = await _existing_assets(
        campaign_id, campaign_client=campaign_client
    )
    assets_with_urls = [await _asset_with_url(core_client, a) for a in assets]

    return {
        "campaign_id": campaign_id,
        "assets": assets_with_urls,
        "missing_placements": missing_placements,
        "superseded_source_count": superseded_source_count,
    }


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

    copy_body = admin_app._parse_artefact_body(approved_copy.get("body"))
    channels = copy_body.get("channels") or []
    try:
        pipeline.require_channel_keyed_copy(channels)
    except pipeline.StaleChannelPlanError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    assets, missing_placements, superseded_source_count = await _existing_assets(
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
        "superseded_source_count": superseded_source_count,
        "copy": channels,
        "links": links,
        "guidance": campaign.get("guidance") or "",
    }
