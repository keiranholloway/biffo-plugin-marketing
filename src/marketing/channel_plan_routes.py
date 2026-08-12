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
    # The closed set of URLs this run is being shown (issue #22): exactly the
    # approved positioning's own citations. Read at start time and stashed on
    # the pending artefact below for the same reason `taxonomy_motions` is —
    # it must be what THIS run saw, not what positioning has been re-run to by
    # the time the run completes. See `pipeline.extract_channel_plan`.
    allowed_source_urls = pipeline.citation_source_urls(approved_positioning.get("citations"))

    channels_resp = await admin_app._core("GET", f"{_INTERNAL_PREFIX}/channels", admin.token)
    channels_resp.raise_for_status()
    channel_rows = channels_resp.json() or []
    # What the agent is shown, and what it is later validated against
    # (#76 increment 2) — the SAME set, stored on the pending artefact below
    # rather than re-fetched at advance time, so a channel added to the
    # taxonomy between start and advance cannot retroactively legalise (or a
    # deleted one retroactively invalidate) a key this run was actually
    # shown. See `pipeline.start_channel_plan`'s own docstring.
    taxonomy = [
        {
            "channel_key": row["key"],
            "label": row["label"],
            "motion": row["motion"],
            "category": row["category"],
        }
        for row in channel_rows
    ]
    taxonomy_motions = {row["key"]: row["motion"] for row in channel_rows}

    causation_id, run_id = await pipeline.start_channel_plan(
        gateway, positioning_body=positioning_body, taxonomy=taxonomy
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
            # Pending-state payload, overwritten with the real result once
            # proposed — same shape `start_research_route` already uses for
            # `research_run_ids`.
            "body": json.dumps(
                {
                    "channel_taxonomy": taxonomy_motions,
                    "allowed_source_urls": allowed_source_urls,
                }
            ),
        },
    )
    created.raise_for_status()
    return created.json()
