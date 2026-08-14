"""The campaign studio's admin-facing ASGI app (ADR-0021 ``admin_ingress``).

Mounted by the shared plugin host at ``/api/v1/plugins/marketing/admin``, gated
on the ``admin`` Cognito group before this app sees a request (ADR-0011 —
authorization is a core concern, never plugin code).

## Why this surface is the admin one, and not the user one

The plugin has two surfaces in its design, and they are the same machine pointed
at different audiences: ``admin_ingress`` is the operator marketing *the
platform*, ``user_ingress`` is a franchise unit marketing *its own services*.
**Both are now built** — ``user_app.py`` is Surface B, added after this
docstring first said "only the first is built" (now corrected: that sentence
was stale from the moment ``user_app.py`` shipped). There is no
platform-operator tier in Biffo — an instance's "platform admins" are simply
its own tenant admins — which is exactly why one plugin can serve both
Biffo-marketing-Biffo and Tabsii-marketing-Tabsii without a new capability.

## What is NOT here, and why

The five tables declare their CRUD in ``biffo.plugin.json``'s ``api_routes``, so
**Core** generates those and the host forwards them. **Not** "admin-only" any
more, and not gated on either surface's Cognito group at all: ``biffo-
template``'s ``plugin_host/forward.py`` places the declared-``api_routes``
forwarder **outside** the plugin's own group gate on purpose ("gating them
additionally on the plugin's user group... would reject the admin the route
exists for") — reachability is governed entirely by each table's own
``required_role`` in Core, checked against WHATEVER Cognito group the bearer
token belongs to, not by which mount the request came in through. Four of the
five tables (``marketing_campaign``/``artefact``/``asset``/``link``) opened
``list``/``read`` to ``required_role: []`` so ``user_app.py`` could reach
them — which means, as a direct consequence, ANY authenticated caller in the
tenant can reach them via ``/api/v1/plugins/marketing/campaigns`` etc.
directly, not only a ``founder``-group one (see ``user_app.py``'s own module
docstring for the full reasoning and the accepted limitation this creates).
``create``/``update``/``delete`` and every operation on ``marketing_click``
stay admin-only. The UI calls
``/api/v1/plugins/marketing/campaigns`` directly — one hop.

Routes live here only when they are *not* CRUD over one table: the pipeline
transitions, which read one row and write several, and the static configuration
the UI needs to render a form. Duplicating generated CRUD through here would add
a hop and a second copy of the permission decision.

## The mount ordering is load-bearing

``StaticFiles`` at ``/`` swallows every path registered after it. The SPA mount
is therefore **last in this file**, and ``tests/test_marketing_admin_app.py``
asserts the ordering rather than trusting this comment — both existing plugins
learned that the same way.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from biffo_plugin_sdk import BiffoAPIClient, BiffoAPIError, create_core_client
from biffo_plugin_sdk.user_serving import require_group
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pipeline, principal_client
from .config import public_base_url_for
from .definitions import ARTEFACT_KINDS, MEDIA_KINDS, PIPELINE_STAGES, PLACEMENTS
from .image_routes import router as image_router
from .links import destination_with_utms, mint_token, tracked_url

require_admin = require_group("admin")

#: Core's own base URL. The plugin calls Core directly rather than back through
#: the host: the host calling itself through the public path is three hops, and
#: the middle one is this same Lambda.
CORE_API_URL = os.environ.get("BIFFO_CORE_API_URL", "")

#: A 30s timeout, not httpx's default 5s. Core's cold start has been measured at
#: ~4.3s, so a 5s default expires on the first request after a quiet period —
#: which reads as a broken feature rather than a slow one.
_CORE_TIMEOUT = 30.0

#: Every `_core()` call goes to Core's internal, per-plugin CRUD mount —
#: `plugin_router.py`'s `path_prefix="/internal/plugins"` in biffo-template —
#: never the public `/api/v1/plugins/marketing/*` one the browser reaches,
#: which is unaddressable from a Lambda anyway (see `plugin_router.py`'s own
#: docstring on the #652 collision). Written out here, at each call site
#: below, rather than appended inside `_core()` itself: `tests/
#: test_marketing_core_paths_guard.py` statically inspects each call site's
#: own literal path argument, so the prefix has to be visible there, not
#: hidden behind a helper's plumbing (`principal_client`'s module docstring
#: explains the same choice from the other side).
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/config")
async def config() -> dict[str, Any]:
    """Static configuration the admin UI renders its forms from.

    Served rather than duplicated in TypeScript so the vocabulary has one
    definition. A form offering a placement the renderer does not know, or a
    stage the service will not accept, fails at submit — and the mismatch is
    invisible until someone tries it.
    """
    return {
        "media_kinds": list(MEDIA_KINDS),
        "placements": list(PLACEMENTS),
        "pipeline_stages": list(PIPELINE_STAGES),
    }


def _validated_campaign_id(campaign_id: str) -> str:
    """``campaign_id`` as a UUID, or a 404.

    **This is an SSRF guard, not tidiness.** ``campaign_id`` arrives from the
    URL path and is interpolated into the Core API URL by ``_core``. This
    Lambda SigV4-signs its own calls to Core's *internal* API as
    ``system:marketing`` (ADR-0021 §1a), so a value carrying ``../`` or a host
    separator does not merely 404 — it steers a credentialed request at an
    endpoint this plugin was never granted for. CodeQL flagged exactly this
    ("Partial server-side request forgery") on the first version of the mint
    route.

    Validating rather than escaping, because the set of legal values is known
    exactly: ``marketing_campaign.id`` is a ``String(36)`` UUID written by
    Core. Anything else is not a campaign this plugin could act on, so 404 is
    both the honest answer and the one that leaks least.
    """
    try:
        parsed = uuid.UUID(campaign_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found."
        ) from None
    # str(UUID) re-renders canonically, so nothing the caller wrote survives
    # into the URL even if it parsed.
    return str(parsed)


class MintRequest(BaseModel):
    """One link to mint: a channel key, and optionally a variant.

    No ``is_paid`` here (#84). It used to be a caller-supplied flag,
    independent of ``channel`` — which meant an operator could tick "Paid
    placement" for a channel whose own taxonomy row already says
    ``motion: organic``, or leave it unticked for one that is inherently
    paid, and nothing reconciled the two. Motion now lives on the channel
    (#76 increment 2), so ``mint_links`` derives ``is_paid`` from the
    selected channel's own ``motion`` and this model does not accept a
    second, independently-wrong answer to the same question. A legacy
    caller that still sends ``is_paid`` is not rejected for it — pydantic
    ignores unknown fields by default — it is simply not consulted.
    """

    channel: str = Field(min_length=1, max_length=64)
    variant: str | None = Field(default=None, max_length=64)


class MintBody(BaseModel):
    """A batch, because an operator mints a campaign's channels together.

    One request per channel would make a partially-minted campaign the normal
    outcome of a flaky network rather than an exceptional one.
    """

    links: list[MintRequest] = Field(min_length=1, max_length=50)


@router.post("/campaigns/{campaign_id}/links", status_code=status.HTTP_201_CREATED)
async def mint_links(
    campaign_id: str,
    body: MintBody,
    request: Request,
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Mint tracked links for a campaign, and return the URLs to publish.

    Not generated CRUD, which is why it lives here: minting reads one row and
    writes several, and the values it writes are **derived** rather than
    supplied. A caller who could POST a `marketing_link` directly could set its
    own `utm_campaign`, which would put back exactly the hand-typed string this
    milestone exists to remove.

    The token IS returned here, unlike at the public redirect where it never
    appears in a response body. The distinction is the audience: the operator
    has to publish this URL, so withholding it would make the feature useless,
    and they are an authenticated admin of this tenant. The public route's
    silence is about not confirming a *stranger's* guess.
    """
    base_url = public_base_url_for(request.headers.get("origin"), request.headers.get("referer"))
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No public base URL is configured for this deployment.",
        )

    campaign_id = _validated_campaign_id(campaign_id)

    campaign = await _core("GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", admin.token)
    if campaign.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign.raise_for_status()

    destination = (campaign.json() or {}).get("destination_url")
    if not destination:
        # Refused rather than defaulted: a link to nowhere is worse than no
        # link, because it still records clicks and still looks like it worked.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This campaign has no destination_url, so its links would lead nowhere.",
        )

    # #84: `marketing_link.channel` now holds taxonomy keys everywhere else
    # (`pack_routes._ensure_links`) — this was the one path still writing
    # whatever an operator typed. Validated here, against this tenant's own
    # `marketing_channel` rows, not against a hardcoded list: an
    # instance-added channel must mint exactly as well as a seeded one.
    channel_motions = await _channel_motions(admin.token)
    unrecognised = sorted(
        {spec.channel for spec in body.links if spec.channel not in channel_motions}
    )
    if unrecognised:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Not a recognised channel key: {', '.join(unrecognised)}. "
                f"Valid channel keys: {', '.join(sorted(channel_motions))}."
            ),
        )

    minted: list[dict[str, Any]] = []
    for spec in body.links:
        # Derived from the channel's own taxonomy row, not from anything the
        # caller sent (#84) — see `MintRequest`'s docstring for why there is
        # no independent `is_paid` on the request any more.
        is_paid = channel_motions[spec.channel] == "paid"
        token = mint_token()
        created = await _core(
            "POST",
            f"{_INTERNAL_PREFIX}/links",
            admin.token,
            json={
                "campaign_id": campaign_id,
                "token": token,
                "channel": spec.channel,
                "variant": spec.variant,
                "is_paid": is_paid,
                # Resolved HERE, once, and stored. The public redirect sends the
                # caller to exactly what is stored, so editing the campaign
                # tomorrow cannot rewrite a link published today.
                "destination_url": destination_with_utms(
                    destination,
                    campaign_id=campaign_id,
                    channel=spec.channel,
                    variant=spec.variant,
                    is_paid=is_paid,
                ),
            },
        )
        created.raise_for_status()
        row = created.json()
        minted.append(
            {
                "id": row.get("id"),
                "channel": spec.channel,
                "variant": spec.variant,
                "is_paid": is_paid,
                "url": tracked_url(base_url, token),
            }
        )

    return {"campaign_id": campaign_id, "links": minted}


async def _channel_motions(admin_token: str) -> dict[str, str]:
    """``{channel_key: motion}`` for this tenant's channel taxonomy — what
    `mint_links` validates a requested channel against, and derives `is_paid`
    from (#84). Mirrors `paid_pack_routes._channel_ad_platforms`, which fetches
    the same table for the same reason on the pack side; kept as two small
    functions rather than one shared helper because the two callers want
    different projections of the same rows (`ad_platform` there, `motion`
    here) and a single "give me the taxonomy" helper would just push the
    projection back onto each call site anyway."""
    resp = await _core("GET", f"{_INTERNAL_PREFIX}/channels", admin_token)
    resp.raise_for_status()
    rows = resp.json() or []
    return {row["key"]: row["motion"] for row in rows}


async def _core(method: str, path: str, token: str, **kw: Any) -> httpx.Response:
    """Dual-auth call to Core's internal, per-plugin CRUD mount — SigV4-signed
    AND carrying the calling admin's own token, forwarded via
    `X-Biffo-User-Token` (`principal_client`'s module docstring has the full
    mechanism, and issue #27 the reasoning for why the fix is this and not a
    bare signed client). `path` must already carry the `_INTERNAL_PREFIX`
    every call site above supplies — see that constant's own comment for why
    the prefix lives at the call site rather than being added here.

    This is a thin wrapper over `principal_client.request`, kept so every
    existing call site's `.status_code` / `.raise_for_status()` / `.json()`
    usage — written against a plain `httpx.Response` from before this was
    signed at all — keeps working unchanged.

    `**kw` is narrower than it looks: it forwards to `principal_client.
    request`, which accepts only `params=`/`json=` (plus `timeout=`, already
    supplied above). The pre-fix `_core` forwarded `**kw` straight to
    `httpx.AsyncClient.request`, so `headers=`/`data=`/a per-call `timeout=`
    were all previously valid; none of that is a call site above needs
    today, but a future one reaching for it gets a `TypeError`, not a
    silently-ignored kwarg.
    """
    if not CORE_API_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Core API URL is not configured for this deployment.",
        )
    return await principal_client.request(
        method, path, token, base_url=CORE_API_URL, timeout=_CORE_TIMEOUT, **kw
    )


# ── Research + positioning (M3) ──────────────────────────────────────────────
#
# Not generated CRUD, for the same reason `mint_links` above is not: starting a
# run and advancing an artefact both read one row and write several, and the
# fan-out/approval logic lives in `pipeline.py` so it is testable without an
# ASGI app. What is left here is the thin part — reading/writing the
# `marketing_artefact` row through the same `_core` seam as every other route
# in this file, and building the one thing that IS new: a SigV4-signed client
# for Core's internal agent-run API (`/api/v1/internal/agent-runs`), which
# `_core`'s Cognito-bearer-token calls cannot reach (ADR-0009 gates it on IAM,
# not a JWT).

#: The kinds a pipeline stage exists for — an explicit, opt-in subset of
#: `definitions.ARTEFACT_KINDS` (which also lists table-level kinds that may
#: not have a wired stage yet, as `channel_plan` itself did not until M4).
#: Kept as its own tuple rather than reused directly so that adding a kind to
#: `ARTEFACT_KINDS` cannot, by itself, make routes below start dispatching to
#: it before a stage actually exists — the assertion just below is what stops
#: this tuple drifting to name a kind `ARTEFACT_KINDS` does not.
#:
#: Kind-specific code, all of it: `_advance_artefact`'s branch on `kind`
#: below, `pipeline.flatten_citations`'s dispatch on the result *type* (not
#: `kind` — it never sees the string), and each stage's own starter route
#: (`start_research_route`/`start_positioning_route` here,
#: `start_channel_plan_route` in `channel_plan_routes.py`, `start_copy_route`
#: in `copy_routes.py`). Everything else —
#: `get_artefact_route`/`approve_artefact_route`/`reject_artefact_route` —
#: is generic over any kind in this tuple.
_PIPELINE_ARTEFACT_KINDS = ("research", "positioning", "channel_plan", "copy")
if not set(_PIPELINE_ARTEFACT_KINDS) <= set(ARTEFACT_KINDS):
    raise RuntimeError(
        "_PIPELINE_ARTEFACT_KINDS must stay a subset of definitions.ARTEFACT_KINDS "
        f"(got {_PIPELINE_ARTEFACT_KINDS!r} against {ARTEFACT_KINDS!r})"
    )

_AGENT_RUNS_PATH = "/api/v1/internal/agent-runs"


class _CoreAgentGateway:
    """`pipeline.AgentGateway` backed by Core's internal, SigV4-signed
    agent-run seam (ADR-0009, ADR-0014, ADR-0021 §1a).

    Signs with the shared plugin host's own IAM role and asserts the
    `marketing` plugin identity via the SDK's `acting_as_plugin` binding —
    already set by the host's `group_gate` before this app is dispatched to,
    so nothing here has to set it again. This is the platform's documented,
    intended mechanism for a plugin to reach Core's internal API (ADR-0021
    §1a: "the SDK's default `self.api` client... reads it to stamp the
    outbound `X-Biffo-Plugin` header"), and it is what idea-scout's own
    `CoreTransport` is built on (it subclasses this same `SignedCoreClient`).
    """

    def __init__(self, client: BiffoAPIClient) -> None:
        # Typed as the base `BiffoAPIClient`, not `SignedCoreClient`: that is
        # exactly what `create_core_client()` is declared to return (it may
        # build either, depending on `BIFFO_CORE_AUTH_MODE`), and everything
        # here only calls `.get`/`.post`, which the base class already
        # defines. Only the *default* build (SigV4) is correct against the
        # real internal mount; `get_agent_gateway` is what actually chooses it.
        self._client = client

    async def request_agent_run(
        self,
        *,
        agent_name: str,
        definition: dict[str, Any],
        output_tool: dict[str, Any],
        input_payload: dict[str, Any],
        causation_id: str,
    ) -> str:
        # output_tools rides on the definition snapshot, not `tools` — an
        # output tool offered as a registry tool fails the run as unknown
        # (ADR-0017 §5), same as idea-scout's adapter.
        snapshot = {**definition, "output_tools": [output_tool]}
        run = await self._client.post(
            _AGENT_RUNS_PATH,
            json={
                "agent_name": agent_name,
                "definition_snapshot": snapshot,
                "input_payload": input_payload,
                "causation_id": causation_id,
            },
        )
        return run["id"]

    async def find_chain_run(
        self, *, chain_id: str, agent_name: str
    ) -> pipeline.AgentRunView | None:
        rows = await self._client.get(
            _AGENT_RUNS_PATH, params={"causation_id": chain_id, "agent_name": agent_name}
        )
        if not rows:
            return None
        return await self.get_agent_run(run_id=rows[0]["id"])

    async def get_agent_run(self, *, run_id: str) -> pipeline.AgentRunView | None:
        try:
            run = await self._client.get(f"{_AGENT_RUNS_PATH}/{run_id}")
        except BiffoAPIError as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                return None
            raise
        return pipeline.AgentRunView(
            id=run["id"],
            status=run["status"],
            messages=run.get("messages") or [],
            # Core's `AgentRunResponse.annotations` (biffo-template#1528/#1530):
            # `run.get(...)` alone would collapse a genuinely absent key to the
            # same `None` as an explicit JSON `null`, which is exactly right
            # here — both mean "not known", never "confirmed zero". Do NOT
            # coerce to `[]`: that would make an unknown run read as a proven
            # empty retrieval, which is the exact ambiguity #82 exists to remove.
            annotations=run.get("annotations"),
            # What this run ACTUALLY ran with (issue #160). Core's
            # `AgentRunResponse.definition_snapshot` — carried across because
            # the research-synthesis run is fired by the orchestration engine
            # from a copy of this plugin's definition frozen into the seeded
            # workflow, and this is the only place the plugin can see that
            # copy. Same reasoning as `annotations` above: absent stays
            # `None` ("not known"), never `{}` ("checked, and it was empty"),
            # because `synthesis_config_drift` must not read an unknown
            # snapshot as a clean bill of health.
            definition_snapshot=run.get("definition_snapshot"),
        )


def get_agent_gateway() -> pipeline.AgentGateway:
    """One gateway per request. `create_core_client()` builds a SigV4-signing
    client by default (ADR-0009) — never an unsigned one, which would
    silently 403 against the internal mount."""
    return _CoreAgentGateway(create_core_client())


async def _artefacts_of_kind(campaign_id: str, kind: str, token: str) -> list[dict[str, Any]]:
    """Every artefact Core holds for this campaign/kind, unsorted and
    unfiltered by status.

    `campaign_id` and `kind` are both plain `String` columns, so Core's
    generic list route accepts them as equality filters without any manifest
    change (`filterable_columns` derives from column type, never a hardcoded
    list). Shared by `_latest_artefact` and `_latest_approved_artefact` so
    both read Core the same way and cannot drift from each other."""
    params = {"campaign_id": campaign_id, "kind": kind}
    resp = await _core("GET", f"{_INTERNAL_PREFIX}/artefacts", token, params=params)
    resp.raise_for_status()
    return resp.json() or []


async def _latest_artefact(campaign_id: str, kind: str, token: str) -> dict[str, Any] | None:
    """This campaign's most recent artefact of `kind`, **whatever its
    status**, or `None`.

    This answers "what is the latest attempt at this stage" — right for
    polling/advancing a possibly-still-running run (`get_artefact_route`) and
    for acting on whatever the newest attempt is (`approve_artefact_route`/
    `reject_artefact_route`, which must find a `pending` or freshly-run
    attempt to approve, not an already-approved older one). It is the WRONG
    question for any gate that requires an *approved* input — see
    `_latest_approved_artefact` (issue #41): a newer `pending`/`proposed` row
    sorts ahead of an older `approved` one here, so a caller that treats this
    return value as "the approved artefact" is reading the wrong row the
    moment someone starts a fresh attempt on a campaign that already has one
    approved.

    Sorted defensively rather than trusting insertion order — a fake Core in
    a test may not preserve it."""
    rows = await _artefacts_of_kind(campaign_id, kind, token)
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[0]


async def _latest_approved_artefact(
    campaign_id: str, kind: str, token: str
) -> dict[str, Any] | None:
    """This campaign's most recent **approved** artefact of `kind`, or
    `None` if none is approved yet — even when a newer, not-yet-approved
    attempt of the same kind exists.

    This is what every downstream-stage gate and pack route actually wants
    (issue #41): "latest" and "latest approved" coincide only until an admin
    starts a newer attempt on a campaign that already has an approved one.
    From that point, `_latest_artefact` returns the newer, unapproved row —
    which would silently hide a perfectly good approved artefact from a gate
    that conflated the two questions. Every caller of this function still
    needs its own `None` check: "no approved artefact yet" covers both "none
    exists at all" and "one exists but nothing is approved", and a caller
    that must tell those apart (for a 404 vs. 409, say) calls
    `_latest_artefact` too, for the informative case."""
    rows = await _artefacts_of_kind(campaign_id, kind, token)
    approved = [r for r in rows if r.get("status") == "approved"]
    if not approved:
        return None
    approved.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return approved[0]


def _parse_artefact_body(raw: Any) -> dict[str, Any]:
    """An artefact's ``body`` normalised to a dict, whether it arrived as
    Core's persisted JSON *text* or as a dict some caller already parsed (or
    never serialised at all, e.g. a pending-state placeholder body built and
    read back in the same request).

    This exact ternary (``json.loads(raw) if isinstance(raw, str) else (raw
    or {})``) was duplicated near-verbatim at every artefact-reading call
    site across the plugin — ``pack_routes.py``, ``paid_pack_routes.py``
    (twice), ``copy_routes.py``, ``channel_plan_routes.py``, this module's
    own ``start_positioning_route``, and ``user_app.py`` (issue #49). One
    implementation here; every call site below imports it rather than
    re-deriving it."""
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _artefact_selection(artefact: dict[str, Any]) -> list[str] | None:
    """The `element_ids` an operator approved this artefact with, or `None`
    for "everything" (issue #145) — `marketing_artefact.approved_selection`,
    read tri-shaped exactly like `body`/`citations` (Core's persisted JSON
    *text*, an already-parsed value, or genuinely absent — see
    `_parse_artefact_body`'s own docstring for why every artefact-reading
    route needs the same normalisation).

    `NULL`/unreadable both read as `None` — "everything" — rather than
    raising or defaulting to "nothing": every artefact approved before this
    column existed has no selection at all, and must keep behaving exactly as
    it did before this change, not lose every element the moment it ships.
    """
    raw = artefact.get("approved_selection")
    if isinstance(raw, str):
        if not raw:
            return None
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, list) else None


def _require_known_kind(kind: str) -> None:
    if kind not in _PIPELINE_ARTEFACT_KINDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown artefact kind.")


def _pipeline_error_to_http(exc: pipeline.PipelineError) -> HTTPException:
    """Every pipeline failure the caller can hit while advancing a run maps to
    502: Core answered, an agent ran, and what it produced (or failed to
    produce) is not something retrying the *request* fixes — the operator
    re-runs the stage instead. Kept as one mapping so a new pipeline error
    type cannot silently fall through as a 500."""
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


#: `research` is fanned out and handled as its own branch below — its advance
#: function takes `chain_id`/`research_run_ids`, not a single `run_id`, so it
#: cannot share this mapping's call shape. `channel_plan` and `copy` are ALSO
#: their own branches below (#76 increment 2): both need extra context
#: (the taxonomy/plan they were started against) read back from the pending
#: artefact's own `body`, the same way `research_run_ids` already is — a
#: shape this dict's call cannot carry. `positioning` is the one stage left
#: whose only extra context is the one EVERY downstream stage needs, the
#: approved parent's citation URLs (issue #22), passed uniformly below:
#: adding another like it grows this dict by one line, not `_advance_artefact`
#: by a new branch.
_SINGLE_RUN_ADVANCERS: dict[str, Callable[..., Any]] = {
    "positioning": pipeline.advance_positioning,
}


async def _advance_artefact(
    artefact: dict[str, Any], kind: str, gateway: pipeline.AgentGateway, token: str
) -> dict[str, Any]:
    """Advance a `pending` artefact if its agent run(s) have produced
    something, proposing it once they have. Reading is what advances the
    pipeline (mirrors idea-scout's `get_run`) — there is no background loop
    anywhere in this plugin."""

    # `agent_run_id` is stamped on creation by each stage's own starter route
    # (`start_positioning_route` here, `start_channel_plan_route` in
    # `channel_plan_routes.py`, `start_copy_route` in `copy_routes.py`). A row
    # edited directly through generated CRUD could lack it, and "there is no
    # run to advance" is a real, distinct failure from any pipeline error —
    # checked once here since every branch below except `research` needs it.
    def _require_run_id() -> str:
        run_id = artefact.get("agent_run_id")
        if not run_id:
            raise pipeline.MalformedOutputError(
                f"This {kind} artefact has no agent_run_id to advance."
            )
        return run_id

    # Everything each stage's starter route stashed for its own advance step,
    # read back once here rather than per-branch. `research_run_ids`,
    # `channel_taxonomy` and `channel_plan_channels` were already carried this
    # way; `allowed_source_urls` (issue #22) joins them for the same reason —
    # it must be the approved parent's citations as they stood when THIS run
    # was started, not a fresh read of a parent that may have been re-run
    # since. Absent (a legacy row, or an artefact started before #22 shipped)
    # reads as `None`, i.e. "not known", which skips the provenance check
    # rather than failing every in-flight run — see
    # `pipeline._extract_cited_artefact` for why the two must not be collapsed.
    pending = json.loads(artefact.get("body") or "{}")
    allowed_source_urls = pending.get("allowed_source_urls")

    if kind == "research":
        # `research` is the one stage with no closed prior source set — the
        # web is its source — so it takes no `allowed_source_urls` at all.
        research_run_ids = pending.get("research_run_ids") or []
        result = await pipeline.advance_research(
            gateway, chain_id=artefact["causation_id"], research_run_ids=research_run_ids
        )
    elif kind == "channel_plan":
        # `channel_taxonomy` is `{channel_key: motion}` for exactly what
        # `start_channel_plan_route` showed this run — stashed in `body`
        # while pending, same pattern as `research_run_ids` above (#76
        # increment 2). See `pipeline.extract_channel_plan` for why it must
        # be what the run was shown, not a fresh fetch.
        taxonomy = pending.get("channel_taxonomy") or {}
        # `allowed_motions` is the campaign's motion as it stood when this run
        # started (#67), carried the same way and for the same reason.
        # Absent — a run started before #67 shipped — reads as `None`, i.e.
        # "this run was never motion-constrained", which skips the check
        # rather than failing an in-flight artefact for a rule it was never
        # shown. Exactly the distinction `allowed_source_urls` draws above;
        # `or None` rather than `or []` because an empty list would read as
        # "no motion is allowed" and reject everything.
        allowed_motions = pending.get("allowed_motions") or None
        advance = await pipeline.advance_channel_plan(
            gateway,
            # `agent_run_id` is the GROUNDING run (#65): the stage starts with
            # the `:online` run that retrieves, and the planning run it feeds
            # is started from a later poll of this very function. Its id lands
            # in `channel_plan_run_id` below, and is passed back here so the
            # transition happens exactly once.
            evidence_run_id=_require_run_id(),
            plan_run_id=pending.get("channel_plan_run_id"),
            plan_input=pending.get("plan_input"),
            # Carried purely so this stage's retrieval-breadth line (#65 — the
            # instrument #101 built for research, now pointed here too) can be
            # joined to the rest of this campaign's runs, and so both runs of
            # the stage share one chain.
            causation_id=artefact.get("causation_id"),
            taxonomy=taxonomy,
            allowed_motions=allowed_motions,
            allowed_source_urls=allowed_source_urls,
        )
        if advance.started_plan_run_id is not None:
            # The stage is half done: the grounding run finished and its
            # evidence has just been handed to a freshly-started planning run.
            # Recording that id is not bookkeeping — without it the next poll
            # would see the same terminal grounding run and start (and bill
            # for) a second planning run. The artefact stays `pending`; the
            # next poll advances the planning run.
            started = await _core(
                "PATCH",
                f"{_INTERNAL_PREFIX}/artefacts/{artefact['id']}",
                token,
                json={
                    "body": json.dumps(
                        pipeline.with_element_ids(
                            {**pending, "channel_plan_run_id": advance.started_plan_run_id}
                        )
                    )
                },
            )
            started.raise_for_status()
            return started.json()
        result = advance.plan
    elif kind == "copy":
        # `channel_plan_channels` is `{channel_key: motion}` for the approved
        # plan's real entries this run was started against — same pattern,
        # see `pipeline.extract_copy`.
        channel_plan_channels = pending.get("channel_plan_channels") or {}
        result = await pipeline.advance_copy(
            gateway,
            run_id=_require_run_id(),
            channel_plan_channels=channel_plan_channels,
            allowed_source_urls=allowed_source_urls,
        )
    else:
        result = await _SINGLE_RUN_ADVANCERS[kind](
            gateway, run_id=_require_run_id(), allowed_source_urls=allowed_source_urls
        )

    if result is None:
        return artefact  # still in flight; nothing to propose yet

    updated = await _core(
        "PATCH",
        f"{_INTERNAL_PREFIX}/artefacts/{artefact['id']}",
        token,
        json={
            "status": "proposed",
            # `with_element_ids` (issue #145) — server-assigned ids, stamped
            # here because this is the one place every artefact kind's real
            # content is actually persisted (`_advance_artefact`'s docstring).
            "body": json.dumps(pipeline.with_element_ids(result.model_dump())),
            "citations": json.dumps(pipeline.flatten_citations(result)),
        },
    )
    updated.raise_for_status()
    return updated.json()


@router.post("/campaigns/{campaign_id}/research", status_code=status.HTTP_201_CREATED)
async def start_research_route(
    campaign_id: str,
    gateway: pipeline.AgentGateway = Depends(get_agent_gateway),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Fan out the two research agents for this campaign on one causation
    chain, and record the `research` artefact that will hold their
    reconciled output once the engine's fan-in fires
    (`scripts/seed_fan_in_workflow.py` — without that seeded once per
    environment this hangs in `pending` forever, silently)."""
    campaign_id = _validated_campaign_id(campaign_id)

    campaign = await _core("GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", admin.token)
    if campaign.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign.raise_for_status()

    brief = (campaign.json() or {}).get("brief")
    if not brief:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This campaign has no brief, so there is nothing to research.",
        )

    chain_id, research_run_ids = await pipeline.start_research(
        gateway, brief={"campaign_id": campaign_id, "brief": brief}
    )

    created = await _core(
        "POST",
        f"{_INTERNAL_PREFIX}/artefacts",
        admin.token,
        json={
            "campaign_id": campaign_id,
            "kind": "research",
            "status": "pending",
            "causation_id": chain_id,
            # research_run_ids travels here only while pending — advance_research
            # needs them to detect "every research agent failed, so the engine
            # never fired synthesis" (idea-scout's own reasoning for tracking
            # them). Overwritten with the real synthesis output once proposed.
            "body": json.dumps(pipeline.with_element_ids({"research_run_ids": research_run_ids})),
        },
    )
    created.raise_for_status()
    return created.json()


@router.get("/campaigns/{campaign_id}/artefacts/{kind}")
async def get_artefact_route(
    campaign_id: str,
    kind: str,
    gateway: pipeline.AgentGateway = Depends(get_agent_gateway),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """This campaign's latest artefact of `kind`, advancing it first if it is
    still `pending`."""
    _require_known_kind(kind)
    campaign_id = _validated_campaign_id(campaign_id)

    artefact = await _latest_artefact(campaign_id, kind, admin.token)
    if artefact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind} artefact for this campaign yet.",
        )
    if artefact.get("status") != "pending":
        return artefact

    try:
        return await _advance_artefact(artefact, kind, gateway, admin.token)
    except pipeline.PipelineError as exc:
        raise _pipeline_error_to_http(exc) from exc


class ApproveArtefactBody(BaseModel):
    """The operator's chosen subset of a `proposed` artefact's elements
    (issue #145).

    `element_ids=None` — the default, and what a request with no body at all
    parses to — means approve everything, preserving the pre-#145 behaviour
    exactly: today's UI, and every other existing caller, sends no body and
    must keep working unchanged.
    """

    element_ids: list[str] | None = Field(
        default=None,
        description=(
            "The element ids to approve out of this artefact's body. Omit entirely to "
            "approve everything (today's behaviour). An empty list is rejected — see "
            "approve_artefact_route's docstring — because 'nothing is usable' is what the "
            "reject route is for, not an empty selection here."
        ),
    )


@router.post("/campaigns/{campaign_id}/artefacts/{kind}/approve")
async def approve_artefact_route(
    campaign_id: str,
    kind: str,
    body: ApproveArtefactBody | None = Body(default=None),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """`proposed` -> `approved`, optionally narrowed to a subset of the
    artefact's elements (issue #145). Only a human calls this route — there
    is no other caller in this plugin — which is the approval gate itself,
    not ceremony around it.

    **An empty `element_ids` is rejected outright, not treated as "approve
    nothing".** Every artefact is a list, so an operator who genuinely finds
    none of it usable has a distinct action already — `reject_artefact_route`
    — and letting an empty selection through here would make "approve
    nothing" silently equivalent to "reject", without the operator having
    chosen reject, and without whatever downstream already read this
    artefact's status noticing the difference.

    **Unknown ids are rejected too**, against the artefact's OWN persisted
    element ids (`pipeline.known_element_ids`) — not the ids a stale admin
    page happened to render. A page that has been open since before a
    re-run must not be able to silently approve nothing (every requested id
    unknown) or a different element than the one the operator actually
    looked at.
    """
    _require_known_kind(kind)
    campaign_id = _validated_campaign_id(campaign_id)

    artefact = await _latest_artefact(campaign_id, kind, admin.token)
    if artefact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind} artefact for this campaign yet.",
        )
    if artefact.get("status") != "proposed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only a 'proposed' artefact can be approved (status: {artefact.get('status')})."
            ),
        )

    element_ids = body.element_ids if body is not None else None
    update: dict[str, Any] = {"status": "approved"}
    if element_ids is not None:
        if not element_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "An empty selection is not a valid approval — it is indistinguishable "
                    "from approving nothing by accident. Use the reject route if none of "
                    "this artefact is usable."
                ),
            )
        known = pipeline.known_element_ids(_parse_artefact_body(artefact.get("body")))
        unknown = sorted(set(element_ids) - known)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Unknown element id(s): {', '.join(unknown)}. This selection may be "
                    "stale — refresh the artefact and try again."
                ),
            )
        update["approved_selection"] = json.dumps(element_ids)

    updated = await _core(
        "PATCH",
        f"{_INTERNAL_PREFIX}/artefacts/{artefact['id']}",
        admin.token,
        json=update,
    )
    updated.raise_for_status()
    return updated.json()


@router.post("/campaigns/{campaign_id}/artefacts/{kind}/reject")
async def reject_artefact_route(
    campaign_id: str, kind: str, admin: Any = Depends(require_admin)
) -> dict[str, Any]:
    """`proposed` -> `rejected`."""
    _require_known_kind(kind)
    campaign_id = _validated_campaign_id(campaign_id)

    artefact = await _latest_artefact(campaign_id, kind, admin.token)
    if artefact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind} artefact for this campaign yet.",
        )
    if artefact.get("status") != "proposed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only a 'proposed' artefact can be rejected (status: {artefact.get('status')})."
            ),
        )

    updated = await _core(
        "PATCH",
        f"{_INTERNAL_PREFIX}/artefacts/{artefact['id']}",
        admin.token,
        json={"status": "rejected"},
    )
    updated.raise_for_status()
    return updated.json()


@router.post("/campaigns/{campaign_id}/positioning", status_code=status.HTTP_201_CREATED)
async def start_positioning_route(
    campaign_id: str,
    gateway: pipeline.AgentGateway = Depends(get_agent_gateway),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Start the single positioning agent, requiring an **approved** research
    artefact. The gate enforcement itself: `proposed` research must not be
    usable here, or the approval step above is decorative."""
    campaign_id = _validated_campaign_id(campaign_id)

    research = await _latest_artefact(campaign_id, "research", admin.token)
    if research is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No research artefact for this campaign yet.",
        )
    # Gated on the latest APPROVED research, not the latest attempt overall
    # (issue #41) — a newer pending/proposed re-run must not hide an older
    # approved one. `research` above is used only for the 404-vs-409 split
    # and, on failure, to report the newest attempt's real status.
    approved_research = await _latest_approved_artefact(campaign_id, "research", admin.token)
    if approved_research is None:
        try:
            pipeline.require_approved(research.get("status") or "", what="The research artefact")
        except pipeline.ArtefactNotApprovedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # require_approved raises whenever status != "approved"; it is only
        # possible to reach this line if research.status == "approved" while
        # approved_research is None, which cannot happen — the row
        # _latest_artefact returned would then be one of the rows
        # _latest_approved_artefact filters for. Kept so a broken invariant
        # is loud rather than silently falling through to `approved_research`.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The research artefact must be approved before this can proceed.",
        )

    # Narrowed to the operator's approved subset (issue #145) — `None` means
    # "everything", the backwards-compatible reading every pre-#145 approved
    # research artefact gets. This is BOTH what the agent is shown (a dropped
    # finding must not reach it as input) and what the citation check below is
    # derived from — the two must agree, or a run could cite something it was
    # never actually shown.
    research_body = pipeline.selected_body(
        _parse_artefact_body(approved_research.get("body")),
        _artefact_selection(approved_research),
    )

    # The closed set of URLs this run is actually being shown (issue #22),
    # recomputed from the approved SUBSET rather than read off the parent's
    # unfiltered `citations` column — see `pipeline.source_urls_from_body` for
    # why a fresh union, not a subtraction, is what makes this narrow
    # correctly when two findings share a source. Read HERE, at start time,
    # and stashed below — not re-read when the run completes, since research
    # can be re-run and re-approved in between, and validating against a set
    # this run never saw would fail an honest positioning run (and pass a
    # dishonest one).
    allowed_source_urls = pipeline.source_urls_from_body(research_body)

    causation_id, run_id = await pipeline.start_positioning(gateway, research_body=research_body)

    created = await _core(
        "POST",
        f"{_INTERNAL_PREFIX}/artefacts",
        admin.token,
        json={
            "campaign_id": campaign_id,
            "kind": "positioning",
            "status": "pending",
            "causation_id": causation_id,
            "agent_run_id": run_id,
            # Pending-state payload, overwritten with the real positioning
            # once proposed — the same shape `start_research_route` uses for
            # `research_run_ids` and `start_channel_plan_route` for
            # `channel_taxonomy`.
            "body": json.dumps(
                pipeline.with_element_ids({"allowed_source_urls": allowed_source_urls})
            ),
        },
    )
    created.raise_for_status()
    return created.json()


def build_app() -> FastAPI:
    """The admin ASGI app.

    A factory rather than a module-level singleton so tests can build one
    without the static directory existing — the deployed layout and the repo
    layout put it in different places, and a module-level mount would make
    importing this module fail in whichever one is not current.
    """
    app = FastAPI(title="Marketing — campaign studio (admin)")
    app.include_router(router)
    # Still-image generation (M6, issue #5) — routes live in image_routes.py,
    # not here, so this file gains only this one line. See that module's
    # docstring for why.
    app.include_router(image_router)

    # Channel-plan (M4) routes live in their own module, imported here —
    # rather than at this module's top level — so this file's own diff stays
    # small: `channel_plan_routes` reaches back into this module for the
    # shared `require_admin`/`get_agent_gateway`/`_core`/`_latest_artefact`
    # helpers, and importing it at top level would be a real circular import
    # (those names do not exist yet at that point in this module's own
    # execution). By the time `build_app()` runs, they do.
    from .channel_plan_routes import router as channel_plan_router

    app.include_router(channel_plan_router)

    # Results dashboard (M8, issue #7) — same reasoning as channel_plan_router
    # above: a lazy, in-function import keeps this file's diff to one include
    # line while `results_routes` reaches back into this module for
    # `require_admin`/`_core`.
    from .results_routes import router as results_router

    app.include_router(results_router)

    # Copy generation (M5, issue #4) — same reasoning and same lazy-import
    # shape as channel_plan_router just above: `copy_routes` reaches back
    # into this module for the same shared helpers.
    from .copy_routes import router as copy_router

    app.include_router(copy_router)

    # The distribution pack (M5, issue #4) — renders placements from the
    # approved source creative, mints any tracked links the approved copy's
    # channels still need, and assembles the whole pack. Its own module for
    # the same reason `image_routes.py` is: it needs the SigV4-signed
    # internal client for object storage, which this file's `_core` cannot
    # reach (that is Cognito-bearer only).
    from .pack_routes import router as pack_router

    app.include_router(pack_router)

    # The paid brief pack (M9, issue #8) — the paid-specific additions on top
    # of the same pack shape `pack_router` above assembles: ad copy at
    # platform character limits, targeting from the approved positioning, a
    # budget recommendation, and spend reported as explicitly unmeasurable
    # (issue #31) rather than a silent zero. Same lazy-import reasoning as
    # every other route module included above.
    from .paid_pack_routes import router as paid_pack_router

    app.include_router(paid_pack_router)

    # Recording what a campaign actually cost (M9, issue #8) — the paid
    # pack's own `spend` figure has to come from somewhere, and an operator
    # typing it in is the only source this deployment can ever have (no ad
    # platform API, by design). Its own module for the same reason every
    # other route module above is, and see that file's docstring for why the
    # row lives in this plugin's own table rather than tabsii's.
    from .spend_routes import router as spend_router

    app.include_router(spend_router)

    # LAST. See the module docstring: a StaticFiles mount at "/" swallows
    # everything registered after it, so anything below this line is unreachable.
    static = _static_dir()
    if static is not None:
        app.mount("/", StaticFiles(directory=str(static), html=True), name="admin-ui")
    return app


def _static_dir() -> Path | None:
    """Where the built admin SPA lives, or ``None`` if it has not been built.

    Two layouts, and a fixed relative parent count cannot satisfy both: the repo
    has ``src/marketing/admin_app.py`` with ``web-admin/dist`` at the root, while
    the deployed Lambda flattens ``src/`` into the task root and the host unzips
    plugins under ``BIFFO_PLUGINS_ROOT``. Resolved by asking, not by counting.

    Returns ``None`` rather than raising when absent: a local run with no built
    UI should serve the API and 404 the SPA, not fail to import.
    """
    root = os.environ.get("BIFFO_PLUGINS_ROOT")
    if root:
        candidate = Path(root) / "marketing" / "web-admin" / "dist"
        if candidate.is_dir():
            return candidate
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web-admin" / "dist"
        if candidate.is_dir():
            return candidate
    return None


app = build_app()
