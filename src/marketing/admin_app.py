"""The campaign studio's admin-facing ASGI app (ADR-0021 ``admin_ingress``).

Mounted by the shared plugin host at ``/api/v1/plugins/marketing/admin``, gated
on the ``admin`` Cognito group before this app sees a request (ADR-0011 —
authorization is a core concern, never plugin code).

## Why this surface is the admin one, and not the user one

The plugin has two surfaces in its design, and they are the same machine pointed
at different audiences: ``admin_ingress`` is the operator marketing *the
platform*, ``user_ingress`` is a franchise unit marketing *its own services*.
Only the first is built. There is no platform-operator tier in Biffo — an
instance's "platform admins" are simply its own tenant admins — which is exactly
why one plugin can serve both Biffo-marketing-Biffo and Tabsii-marketing-Tabsii
without a new capability.

## What is NOT here, and why

The five tables declare their CRUD in ``biffo.plugin.json``'s ``api_routes``, so
**Core** generates those and the host forwards them, authorised by each table's
own admin-only permissions. The UI calls
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
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pipeline
from .config import public_base_url
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
    """One link to mint: a channel, optionally a variant, organic or paid."""

    channel: str = Field(min_length=1, max_length=64)
    variant: str | None = Field(default=None, max_length=64)
    is_paid: bool = False


class MintBody(BaseModel):
    """A batch, because an operator mints a campaign's channels together.

    One request per channel would make a partially-minted campaign the normal
    outcome of a flaky network rather than an exceptional one.
    """

    links: list[MintRequest] = Field(min_length=1, max_length=50)


@router.post("/campaigns/{campaign_id}/links", status_code=status.HTTP_201_CREATED)
async def mint_links(
    campaign_id: str, body: MintBody, admin: Any = Depends(require_admin)
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
    base_url = public_base_url()
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No public base URL is configured for this deployment.",
        )

    campaign_id = _validated_campaign_id(campaign_id)

    campaign = await _core("GET", f"/campaigns/{campaign_id}", admin.token)
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

    minted: list[dict[str, Any]] = []
    for spec in body.links:
        token = mint_token()
        created = await _core(
            "POST",
            "/links",
            admin.token,
            json={
                "campaign_id": campaign_id,
                "token": token,
                "channel": spec.channel,
                "variant": spec.variant,
                "is_paid": spec.is_paid,
                # Resolved HERE, once, and stored. The public redirect sends the
                # caller to exactly what is stored, so editing the campaign
                # tomorrow cannot rewrite a link published today.
                "destination_url": destination_with_utms(
                    destination,
                    campaign_id=campaign_id,
                    channel=spec.channel,
                    variant=spec.variant,
                    is_paid=spec.is_paid,
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
                "is_paid": spec.is_paid,
                "url": tracked_url(base_url, token),
            }
        )

    return {"campaign_id": campaign_id, "links": minted}


async def _core(method: str, path: str, token: str, **kw: Any) -> httpx.Response:
    if not CORE_API_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Core API URL is not configured for this deployment.",
        )
    async with httpx.AsyncClient(timeout=_CORE_TIMEOUT) as client:
        return await client.request(
            method, f"{CORE_API_URL}{path}", headers={"Authorization": f"Bearer {token}"}, **kw
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
#: `start_channel_plan_route` in `channel_plan_routes.py`). Everything else
#: — `get_artefact_route`/`approve_artefact_route`/`reject_artefact_route` —
#: is generic over any kind in this tuple.
_PIPELINE_ARTEFACT_KINDS = ("research", "positioning", "channel_plan")
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
            id=run["id"], status=run["status"], messages=run.get("messages") or []
        )


def get_agent_gateway() -> pipeline.AgentGateway:
    """One gateway per request. `create_core_client()` builds a SigV4-signing
    client by default (ADR-0009) — never an unsigned one, which would
    silently 403 against the internal mount."""
    return _CoreAgentGateway(create_core_client())


async def _latest_artefact(campaign_id: str, kind: str, token: str) -> dict[str, Any] | None:
    """This campaign's most recent artefact of `kind`, or `None`.

    `campaign_id` and `kind` are both plain `String` columns, so Core's
    generic list route accepts them as equality filters without any manifest
    change (`filterable_columns` derives from column type, never a hardcoded
    list). Sorted defensively rather than trusting insertion order — a fake
    Core in a test may not preserve it."""
    params = {"campaign_id": campaign_id, "kind": kind}
    resp = await _core("GET", "/artefacts", token, params=params)
    resp.raise_for_status()
    rows = resp.json() or []
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[0]


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
#: cannot share this mapping's call shape. Every other stage is a single,
#: un-fanned-out run, and shares exactly the shape `advance(gateway,
#: run_id=...)`: adding one grows this dict by one line, not `_advance_artefact`
#: by a new branch.
_SINGLE_RUN_ADVANCERS: dict[str, Callable[..., Any]] = {
    "positioning": pipeline.advance_positioning,
    "channel_plan": pipeline.advance_channel_plan,
}


async def _advance_artefact(
    artefact: dict[str, Any], kind: str, gateway: pipeline.AgentGateway, token: str
) -> dict[str, Any]:
    """Advance a `pending` artefact if its agent run(s) have produced
    something, proposing it once they have. Reading is what advances the
    pipeline (mirrors idea-scout's `get_run`) — there is no background loop
    anywhere in this plugin."""
    if kind == "research":
        pending = json.loads(artefact.get("body") or "{}")
        research_run_ids = pending.get("research_run_ids") or []
        result = await pipeline.advance_research(
            gateway, chain_id=artefact["causation_id"], research_run_ids=research_run_ids
        )
    else:
        # `agent_run_id` is stamped on creation by each stage's own starter
        # route (`start_positioning_route` here, `start_channel_plan_route`
        # in `channel_plan_routes.py`). A row edited directly through
        # generated CRUD could lack it, and "there is no run to advance" is a
        # real, distinct failure from any pipeline error.
        run_id = artefact.get("agent_run_id")
        if not run_id:
            raise pipeline.MalformedOutputError(
                f"This {kind} artefact has no agent_run_id to advance."
            )
        result = await _SINGLE_RUN_ADVANCERS[kind](gateway, run_id=run_id)

    if result is None:
        return artefact  # still in flight; nothing to propose yet

    updated = await _core(
        "PATCH",
        f"/artefacts/{artefact['id']}",
        token,
        json={
            "status": "proposed",
            "body": json.dumps(result.model_dump()),
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

    campaign = await _core("GET", f"/campaigns/{campaign_id}", admin.token)
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
        "/artefacts",
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
            "body": json.dumps({"research_run_ids": research_run_ids}),
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


@router.post("/campaigns/{campaign_id}/artefacts/{kind}/approve")
async def approve_artefact_route(
    campaign_id: str, kind: str, admin: Any = Depends(require_admin)
) -> dict[str, Any]:
    """`proposed` -> `approved`. Only a human calls this route — there is no
    other caller in this plugin — which is the approval gate itself, not
    ceremony around it."""
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

    updated = await _core(
        "PATCH", f"/artefacts/{artefact['id']}", admin.token, json={"status": "approved"}
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
        "PATCH", f"/artefacts/{artefact['id']}", admin.token, json={"status": "rejected"}
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
    try:
        pipeline.require_approved(research.get("status") or "", what="The research artefact")
    except pipeline.ArtefactNotApprovedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    raw_body = research.get("body")
    research_body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})

    causation_id, run_id = await pipeline.start_positioning(gateway, research_body=research_body)

    created = await _core(
        "POST",
        "/artefacts",
        admin.token,
        json={
            "campaign_id": campaign_id,
            "kind": "positioning",
            "status": "pending",
            "causation_id": causation_id,
            "agent_run_id": run_id,
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
