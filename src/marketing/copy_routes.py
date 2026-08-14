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

    # Both gates below follow `admin_app.start_positioning_route`'s pattern
    # exactly: gated on the latest APPROVED artefact of each kind, not the
    # latest attempt overall (issue #41) — a newer pending/proposed re-run of
    # either upstream stage must not hide an older approved one. The bare
    # `_latest_artefact` call is used only for the 404-vs-409 split and, on
    # failure, to report the newest attempt's real status.
    positioning = await admin_app._latest_artefact(campaign_id, "positioning", admin.token)
    if positioning is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No positioning artefact for this campaign yet.",
        )
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

    channel_plan = await admin_app._latest_artefact(campaign_id, "channel_plan", admin.token)
    if channel_plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No channel-plan artefact for this campaign yet.",
        )
    approved_channel_plan = await admin_app._latest_approved_artefact(
        campaign_id, "channel_plan", admin.token
    )
    if approved_channel_plan is None:
        try:
            pipeline.require_approved(
                channel_plan.get("status") or "", what="The channel-plan artefact"
            )
        except pipeline.ArtefactNotApprovedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The channel-plan artefact must be approved before this can proceed.",
        )

    # Narrowed to each parent's own approved subset (issue #145) — `None`
    # means "everything", the backwards-compatible reading every pre-#145
    # approved artefact gets. Both are used as agent input below AND as the
    # basis for the citation check, so a dropped element neither reaches the
    # model nor stays cite-able.
    positioning_body = pipeline.selected_body(
        admin_app._parse_artefact_body(approved_positioning.get("body")),
        admin_app._artefact_selection(approved_positioning),
    )
    channel_plan_body = pipeline.selected_body(
        admin_app._parse_artefact_body(approved_channel_plan.get("body")),
        admin_app._artefact_selection(approved_channel_plan),
    )
    # `{channel_key: motion}` for the plan's real, APPROVED entries (#76
    # increment 2, narrowed per #145) — excludes any suggested_label-only
    # proposal, since it is not a real channel yet, and now also excludes any
    # channel the operator did not approve. Stored on the pending artefact
    # below and re-read at advance time so `extract_copy` validates against
    # exactly this set, not whatever the plan has become by the time the run
    # completes.
    #
    # Raises 409 when the approved plan predates the taxonomy migration —
    # every entry the old free-text shape, none carrying a channel_key — the
    # explicit "existing dev data" answer (#76 increment 2): rather than
    # silently producing copy with no channel to reference, or crashing on a
    # missing key, this campaign must have channel planning re-run before
    # copy can be generated for it.
    # Asked of the plan as GENERATED, not as narrowed (issue #152). Staleness
    # means "this plan predates the taxonomy" — a fact about the artefact the
    # agent produced, which an operator's selection cannot change. Asking it of
    # the narrowed body told an operator who legitimately kept only a
    # `suggested_label`-only proposal that their current plan was stale and had
    # to be re-run, with no way to generate copy for the entry they approved.
    try:
        pipeline.channel_plan_channel_map(
            admin_app._parse_artefact_body(approved_channel_plan.get("body"))
        )
    except pipeline.StaleChannelPlanError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # The mapping itself comes from the operator's approved subset, and may
    # legitimately be empty — that is what keeping only an outside-taxonomy
    # proposal means, and it is not a stale plan.
    channel_plan_channels = pipeline.channel_key_motions(channel_plan_body)

    # The closed set of URLs this run is being shown (issue #22), recomputed
    # from each approved SUBSET rather than read off either parent's
    # unfiltered `citations` column — see `pipeline.source_urls_from_body` for
    # why a fresh union, not a subtraction, is what makes this narrow
    # correctly when two elements share a source. Copy is started against TWO
    # approved artefacts, so its legitimate source set is the union of both —
    # the plan's are a subset of the positioning's in practice, but only
    # because this same check holds one stage up, which is not something this
    # stage should assume. Read at start time and stashed below for the same
    # reason `channel_plan_channels` is.
    allowed_source_urls = list(
        dict.fromkeys(
            [
                *pipeline.source_urls_from_body(positioning_body),
                *pipeline.source_urls_from_body(channel_plan_body),
            ]
        )
    )

    causation_id, run_id = await pipeline.start_copy(
        gateway,
        positioning_body=positioning_body,
        channel_plan_body=channel_plan_body,
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
            # Pending-state payload, overwritten with the real result once
            # proposed — same pattern as `channel_plan_routes.py`'s own.
            "body": json.dumps(
                pipeline.with_element_ids(
                    {
                        "channel_plan_channels": channel_plan_channels,
                        "allowed_source_urls": allowed_source_urls,
                    }
                )
            ),
        },
    )
    created.raise_for_status()
    return created.json()
