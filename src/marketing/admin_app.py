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

import os
from pathlib import Path
from typing import Any

import httpx
from biffo_plugin_sdk.user_serving import require_group
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles

from .definitions import MEDIA_KINDS, PIPELINE_STAGES, PLACEMENTS

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


def build_app() -> FastAPI:
    """The admin ASGI app.

    A factory rather than a module-level singleton so tests can build one
    without the static directory existing — the deployed layout and the repo
    layout put it in different places, and a module-level mount would make
    importing this module fail in whichever one is not current.
    """
    app = FastAPI(title="Marketing — campaign studio (admin)")
    app.include_router(router)

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
