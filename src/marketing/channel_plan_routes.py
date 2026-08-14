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

from . import admin_app, definitions, pipeline

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


async def _targeting(campaign_id: str, token: str) -> tuple[str, list[str]]:
    """This campaign's motion and selected channel keys (#67), or a 4xx
    naming the decision that has not been taken yet.

    Both are operator decisions the channel-plan stage cannot make for
    itself, and both are refused rather than defaulted. A default motion of
    `both` and a default selection of "the whole taxonomy" would each be a
    silent decision taken on the operator's behalf, which is exactly the
    state #67 exists to end — the agent choosing the channel set with the
    operator's first involvement being approve-or-reject on a finished plan.
    So a campaign that predates this feature stops here with a sentence
    saying what to set, rather than quietly running as it used to.
    """
    campaign = await admin_app._core("GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", token)
    if campaign.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign.raise_for_status()
    row = campaign.json() or {}

    motion = (row.get("motion") or "").strip()
    if motion not in definitions.CAMPAIGN_MOTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This campaign has no motion set. Choose organic, paid or both before "
                "planning channels."
            ),
        )

    # Order preserved, duplicates dropped — the operator's selection is a set,
    # but a stable order keeps the taxonomy the agent is shown (and every
    # test asserting on it) deterministic.
    selected = list(
        dict.fromkeys(key.strip() for key in (row.get("target_channel_keys") or "").split(","))
    )
    selected = [key for key in selected if key]
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This campaign has no target channels selected. Choose the channels it "
                "should run on before planning."
            ),
        )
    return motion, selected


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

    # The operator's two pre-plan decisions (#67), read BEFORE the approval
    # gate work below so a campaign missing either is told which decision is
    # missing rather than having an agent run started for it.
    campaign_motion, selected_keys = await _targeting(campaign_id, admin.token)

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

    # Narrowed to the operator's approved subset (issue #145) — `None` means
    # "everything", the backwards-compatible reading every pre-#145 approved
    # positioning artefact gets. This is BOTH what the agent is shown (a
    # dropped segment/pillar/CTA must not reach it as input) and what the
    # citation check below is derived from.
    positioning_body = pipeline.selected_body(
        admin_app._parse_artefact_body(approved_positioning.get("body")),
        admin_app._artefact_selection(approved_positioning),
    )
    # The closed set of URLs this run is being shown (issue #22), recomputed
    # from the approved SUBSET rather than read off the parent's unfiltered
    # `citations` column — see `pipeline.source_urls_from_body` for why a
    # fresh union, not a subtraction, is what makes this narrow correctly when
    # two elements share a source. Read at start time and stashed on the
    # pending artefact below for the same reason `taxonomy_motions` is — it
    # must be what THIS run saw, not what positioning has been re-run to by
    # the time the run completes. See `pipeline.extract_channel_plan`.
    allowed_source_urls = pipeline.source_urls_from_body(positioning_body)

    channels_resp = await admin_app._core("GET", f"{_INTERNAL_PREFIX}/channels", admin.token)
    channels_resp.raise_for_status()
    channel_rows = channels_resp.json() or []

    # The operator's two decisions, applied to the agent's INPUT (#67).
    #
    # This is what makes them constraints rather than requests. A channel the
    # operator did not select, or whose motion this campaign does not run, is
    # simply not in the list the agent is given — and `extract_channel_plan`
    # rejects any `channel_key` outside exactly this snapshot (#76 increment
    # 2), so there is no path by which one reaches the plan. Nothing here
    # relies on the model choosing to comply; #128 is this estate's evidence
    # that a constraint the model is merely asked to respect is not one.
    #
    # A selected key that is no longer in the taxonomy is skipped rather than
    # rejected: the selection is a filter over the taxonomy, never a source of
    # channels of its own, and an admin deleting a channel must not brick
    # every campaign that had selected it.
    allowed_motions = definitions.motions_allowed_by(campaign_motion)
    selected = set(selected_keys)
    channel_rows = [
        row for row in channel_rows if row["key"] in selected and row["motion"] in allowed_motions
    ]
    if not channel_rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"None of this campaign's selected channels are {campaign_motion} channels "
                "this platform still offers. Adjust its motion or its channel selection "
                "before planning."
            ),
        )

    # What the agent is shown, and what it is later validated against
    # (#76 increment 2) — the SAME set, stored on the pending artefact below
    # rather than re-fetched at advance time, so a channel added to the
    # taxonomy between start and advance cannot retroactively legalise (or a
    # deleted one retroactively invalidate) a key this run was actually
    # shown. See `pipeline.start_channel_plan`'s own docstring. Since #67 the
    # campaign's motion is stashed the same way and for the same reason: an
    # operator widening a campaign from organic to both while a run is in
    # flight must not retroactively legalise a paid channel that run was
    # never allowed to pick.
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
        gateway,
        positioning_body=positioning_body,
        taxonomy=taxonomy,
        campaign_motion=campaign_motion,
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
                pipeline.with_element_ids(
                    {
                        "channel_taxonomy": taxonomy_motions,
                        "allowed_motions": sorted(allowed_motions),
                        "allowed_source_urls": allowed_source_urls,
                    }
                )
            ),
        },
    )
    created.raise_for_status()
    return created.json()
