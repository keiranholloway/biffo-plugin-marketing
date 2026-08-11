"""Channel-plan (M4, issue #3) admin route — the one thing that is new for
this milestone beyond what `admin_app.py` already dispatches generically
(``get_artefact_route``/``approve_artefact_route``/``reject_artefact_route``
already cover ``channel_plan`` once it is a known kind).

Kept in its own module, deliberately, rather than added inline in
``admin_app.py`` next to ``start_positioning_route``: several agents can be
working in this repo at once, and ``admin_app.py`` is exactly the kind of file
more than one of them touches. This module reaches back into ``admin_app`` for
the handful of helpers every route in that file already shares
(``require_admin``, ``get_agent_gateway``, ``_core``, ``_validated_campaign_id``,
``_latest_artefact``) rather than duplicating them — see ``admin_app.build_app``
for why the include is a lazy, in-function import rather than a top-level one.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from . import admin_app, pipeline

router = APIRouter(dependencies=[Depends(admin_app.require_admin)])

#: Not `admin_app._INTERNAL_PREFIX` — `tests/test_marketing_core_paths_guard.py`
#: resolves a module-level string constant only within the SAME file's own
#: AST, so a cross-module reference would read as unresolvable and be
#: silently skipped rather than checked. Written out as its own local
#: constant for the same reason `admin_app._validated_campaign_id` is
#: duplicated rather than imported (see that function's docstring) — and
#: `tests/test_marketing_core_paths_guard.py`'s own disagreement test
#: asserts this copy stays equal to the other two, since the guard itself
#: only checks "starts with /api/v1/", never "the three copies agree".
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"


@router.post("/campaigns/{campaign_id}/channel-plan", status_code=status.HTTP_201_CREATED)
async def start_channel_plan_route(
    campaign_id: str,
    gateway: pipeline.AgentGateway = Depends(admin_app.get_agent_gateway),
    admin: Any = Depends(admin_app.require_admin),
) -> dict[str, Any]:
    """Start the single channel-plan agent, requiring an **approved**
    positioning artefact — mirrors ``start_positioning_route``'s gate
    exactly: `proposed` positioning must not be usable here, or the approval
    step for positioning is decorative."""
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    positioning = await admin_app._latest_artefact(campaign_id, "positioning", admin.token)
    if positioning is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No positioning artefact for this campaign yet.",
        )
    # Gated on the latest APPROVED positioning, not the latest attempt
    # overall (issue #41) — a newer pending/proposed re-run must not hide an
    # older approved one. `positioning` above is used only for the
    # 404-vs-409 split and, on failure, to report the newest attempt's real
    # status — see `admin_app.start_positioning_route` for the identical
    # reasoning this mirrors.
    approved_positioning = await admin_app._latest_approved_artefact(
        campaign_id, "positioning", admin.token
    )
    if approved_positioning is None:
        try:
            pipeline.require_approved(
                positioning.get("status") or "", what="The positioning artefact"
            )
        except pipeline.ArtefactNotApprovedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The positioning artefact must be approved before this can proceed.",
        )

    raw_body = approved_positioning.get("body")
    positioning_body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})

    causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body=positioning_body
    )

    created = await admin_app._core(
        "POST",
        f"{_INTERNAL_PREFIX}/artefacts",
        admin.token,
        json={
            "campaign_id": campaign_id,
            "kind": "channel_plan",
            "status": "pending",
            "causation_id": causation_id,
            "agent_run_id": run_id,
        },
    )
    created.raise_for_status()
    return created.json()
