"""Copy-generation (M5, issue #4) admin route — the pipeline half of the
milestone. The distribution-pack half lives in ``pack_routes.py``; this
module is only responsible for the fourth approval gate
(``pending -> proposed -> approved | rejected``), the same shape M3/M4 already
established for research, positioning and the channel plan.

Kept in its own module for the same reason ``channel_plan_routes.py`` is
(read that module's own docstring): several agents can be working in this
repo at once, and ``admin_app.py`` is exactly the kind of file more than one
of them touches. This module reaches back into ``admin_app`` for the handful
of helpers every route in that file already shares (``require_admin``,
``get_agent_gateway``, ``_core``, ``_validated_campaign_id``,
``_latest_artefact``) rather than duplicating them.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from . import admin_app, pipeline

router = APIRouter(dependencies=[Depends(admin_app.require_admin)])

#: Duplicated local constant, not a cross-module reference — see
#: ``channel_plan_routes._INTERNAL_PREFIX`` for exactly why: ``tests/
#: test_marketing_core_paths_guard.py`` resolves a module-level string
#: constant only within the SAME file's own AST.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"


@router.post("/campaigns/{campaign_id}/copy", status_code=status.HTTP_201_CREATED)
async def start_copy_route(
    campaign_id: str,
    gateway: pipeline.AgentGateway = Depends(admin_app.get_agent_gateway),
    admin: Any = Depends(admin_app.require_admin),
) -> dict[str, Any]:
    """Start the single copy agent, requiring BOTH an **approved** positioning
    artefact (for the message pillars/CTAs copy is grounded in) and an
    **approved** channel plan (for which channels to write for) — the same
    gate discipline as ``start_channel_plan_route``: ``proposed`` output from
    either upstream stage must not be usable here, or their approval steps
    are decorative."""
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    positioning = await admin_app._latest_artefact(campaign_id, "positioning", admin.token)
    if positioning is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No positioning artefact for this campaign yet.",
        )
    try:
        pipeline.require_approved(positioning.get("status") or "", what="The positioning artefact")
    except pipeline.ArtefactNotApprovedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    channel_plan = await admin_app._latest_artefact(campaign_id, "channel_plan", admin.token)
    if channel_plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No channel-plan artefact for this campaign yet.",
        )
    try:
        pipeline.require_approved(
            channel_plan.get("status") or "", what="The channel-plan artefact"
        )
    except pipeline.ArtefactNotApprovedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    def _body(artefact: dict[str, Any]) -> dict[str, Any]:
        raw = artefact.get("body")
        return json.loads(raw) if isinstance(raw, str) else (raw or {})

    causation_id, run_id = await pipeline.start_copy(
        gateway,
        positioning_body=_body(positioning),
        channel_plan_body=_body(channel_plan),
    )

    created = await admin_app._core(
        "POST",
        f"{_INTERNAL_PREFIX}/artefacts",
        admin.token,
        json={
            "campaign_id": campaign_id,
            "kind": "copy",
            "status": "pending",
            "causation_id": causation_id,
            "agent_run_id": run_id,
        },
    )
    created.raise_for_status()
    return created.json()
