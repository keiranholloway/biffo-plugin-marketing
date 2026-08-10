"""Results dashboard (M8, issue #7): clicks, leads, conversions and cost per
campaign.

## The measurement discipline is the feature

Every share this endpoint renders carries its denominator and an explicit
unmeasurable count. "No data" is a different, structurally distinct thing
from "zero" throughout — a confident number that is wrong in a systematic
direction is worse than a missing one. This estate has already paid for that
specific mistake once: a share computed over only the "classifiable" inputs
read 80% where the true figure, once the excluded slice was counted, was
47.1%. Nothing here computes a share over a filtered population and reports
it as if it covered the whole one.

This also never blurs pack engagement with publishing. This plugin calls no
platform APIs, so impressions, reach and engagement are unmeasurable **by
construction** — not absent because nobody wired them up yet, but because
there is nothing this deployment could ever call to learn them. Nothing below
renders a click, a mint, or a download count as if it said anything about
who saw a post.

## Clicks are measured; leads, conversions and cost are not — yet

`marketing_click` and `marketing_link` are this plugin's own generated-CRUD
tables (declared in `biffo.plugin.json`), reachable through the same
internal, SigV4-signed mount every other route in this file uses
(`admin_app._core`, `_INTERNAL_PREFIX`). Clicks per campaign, and the
paid/organic split, are real numbers computed from real rows this plugin
wrote itself.

Leads (`public.demo_requests`, tabsii-platform) and cost
(`tabsii.lead_source_costs`, DDL module 049) are **not** reachable from here
today, and this is a genuinely missing capability rather than an oversight in
this module. Both surfaces exist and are queried in `tabsii-platform`, but
neither is registered under `/api/v1/internal/*` — the only tree a
SigV4-signed plugin call can reach at all (`services/api/src/api/main.py`'s
full router-registration list has no `internal_*` router for either table).
`demo_requests` sits behind Core's own `/api/v1/admin/demo-requests`, gated
by `require_admin`, which is built on `require_auth`
(`services/api/src/api/middleware/auth.py`) — bearer-`Authorization`-only,
with no forwarded-token acceptance the way `require_principal` (the guard
`require_principal_crud_permission` uses) has. `lead_source_costs` sits
behind tabsii-CRM's `/api/v1/data/lead_source_costs` and
`/api/v1/analytics/pipeline/*`, gated on RBAC permission codes
(`leads.read`, `lead_source_costs.read`) that have nothing to do with the
"admin" Cognito group this plugin's own admin surface authorises on. Even if
either surface were reachable, `demo_requests.status` has no mutation path
yet (`demo_requests_admin.py`'s own docstring: "there's no mutation surface
yet"), so "conversion" has no signal to read regardless of transport.

So "no data" here is not a placeholder for effort not yet spent on a query —
it is the honest, current state of a real transport gap, filed as issue #31.
Until that closes, every campaign's leads, conversions and cost render as
**unmeasurable**, never as zero: reporting zero would claim "we looked, and
nothing happened," which is not a claim this plugin can make today.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import admin_app

router = APIRouter(dependencies=[Depends(admin_app.require_admin)])

#: Not `admin_app._INTERNAL_PREFIX` — `tests/test_marketing_core_paths_guard.py`
#: resolves a module-level string constant only within the SAME file's own
#: AST, so a cross-module reference would read as unresolvable and be
#: silently skipped rather than checked. Written out as its own local
#: constant, matching `channel_plan_routes.py`/`image_routes.py`; that guard
#: file's own disagreement test asserts every copy stays equal, since the
#: AST walk itself only checks "starts with /api/v1/", never "the copies
#: agree with each other or the real mounted path".
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: Core's generic `list` route defaults to 50 rows a page and caps a single
#: page at 200 (`routing/crud_handlers.py`, tabsii-platform). Mirrored here
#: as a literal rather than imported — this plugin has no dependency on
#: Core's own package — and used as the page size `_list_all` below asks
#: for explicitly, so a campaign with more clicks than one page cannot
#: silently under-count.
_LIST_PAGE_SIZE = 200

#: Why leads/conversions/cost cannot be computed from here today — see the
#: module docstring for the full mechanism. Surfaced verbatim in the
#: response so a caller reading only the JSON, not this file, still gets the
#: real reason rather than a bare `false`.
_UNMEASURABLE_REASON = (
    "No route reachable from this plugin's internal Core transport exposes this yet "
    "(tracked in issue #31) — 'unmeasurable' means the transport does not exist, "
    "not that the count is zero."
)


class ClickBreakdown(BaseModel):
    """Clicks for one campaign, with their own denominator discipline.

    `total` is the denominator; `paid` + `organic` + `unknown_channel_type`
    always sum to it. `marketing_link.is_paid` is nullable — a link minted
    before this column mattered, or a future caller of the mint route that
    never sets it — and folding that into `organic` (a falsy `None`) would
    be exactly the "share over classifiable inputs" mistake this milestone
    exists to avoid. `unknown_channel_type` is what keeps that count visible
    instead of silently disappearing into a bucket that looks like an
    answer.
    """

    total: int
    paid: int
    organic: int
    unknown_channel_type: int


class UnmeasuredMetric(BaseModel):
    """A metric this endpoint cannot compute at all today.

    Deliberately not a bare `null` or a `0`: `measurable` is always `False`
    on every instance this module constructs, and `reason` says why, so a
    caller reading the JSON — not just this file's docstring — can tell "no
    data" from "zero" without guessing. `denominator` is the population this
    metric would be a share of once it becomes measurable (e.g. total clicks,
    for a click-to-lead rate) when that population is itself known here;
    `None` when it isn't (e.g. a conversion rate's denominator is a lead
    count this plugin cannot see either).
    """

    measurable: bool = False
    denominator: int | None = None
    reason: str = _UNMEASURABLE_REASON


class CampaignResults(BaseModel):
    campaign_id: str
    campaign_name: str
    clicks: ClickBreakdown
    leads: UnmeasuredMetric
    conversions: UnmeasuredMetric
    cost: UnmeasuredMetric


class ResultsResponse(BaseModel):
    campaigns: list[CampaignResults]
    #: Clicks whose `campaign_id` names no campaign this call could see (the
    #: campaign was deleted after the click landed, or — defensively — a row
    #: this plugin did not expect). Counted rather than silently dropped from
    #: every campaign's total: an omission here is exactly the kind of
    #: invisible exclusion the module docstring's 80%-vs-47.1% example warns
    #: about, just one level up from a single campaign's own numbers.
    unattributed_clicks: int


async def _list_all(path: str, token: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row Core holds for `path`/`params` — not just the first page.

    Core's generic list route defaults to 50 rows and caps a single page at
    `_LIST_PAGE_SIZE` (see that constant). A campaign with more clicks than
    one page would silently under-count if this stopped after the first
    response; paged explicitly here for the same reason nothing below
    collapses "don't know" into a bucket that looks like a real answer.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = await admin_app._core(
            "GET", path, token, params={**params, "limit": _LIST_PAGE_SIZE, "offset": offset}
        )
        resp.raise_for_status()
        batch = resp.json() or []
        rows.extend(batch)
        if len(batch) < _LIST_PAGE_SIZE:
            return rows
        offset += _LIST_PAGE_SIZE


def _click_breakdown(
    clicks: list[dict[str, Any]], links_by_id: dict[str, dict[str, Any]]
) -> ClickBreakdown:
    paid = organic = unknown = 0
    for click in clicks:
        link = links_by_id.get(click.get("link_id") or "")
        is_paid = link.get("is_paid") if link is not None else None
        if is_paid is None:
            unknown += 1
        elif is_paid:
            paid += 1
        else:
            organic += 1
    return ClickBreakdown(
        total=len(clicks), paid=paid, organic=organic, unknown_channel_type=unknown
    )


@router.get("/results")
async def results(admin: Any = Depends(admin_app.require_admin)) -> ResultsResponse:
    """Clicks, leads, conversions and cost, per campaign.

    Three list calls against this plugin's own tables — campaigns, links,
    clicks — each paged in full via `_list_all`, then joined in memory:
    `marketing_click.link_id` -> `marketing_link.is_paid` for the paid/organic
    split, and `marketing_click.campaign_id` -> `marketing_campaign.id` for
    the per-campaign grouping. See the module docstring for why leads,
    conversions and cost are reported as `UnmeasuredMetric` rather than
    computed: no reachable Core surface carries them yet.
    """
    campaigns = await _list_all(f"{_INTERNAL_PREFIX}/campaigns", admin.token, {})
    links = await _list_all(f"{_INTERNAL_PREFIX}/links", admin.token, {})
    clicks = await _list_all(f"{_INTERNAL_PREFIX}/clicks", admin.token, {})

    links_by_id = {link["id"]: link for link in links if link.get("id")}
    known_campaign_ids = {campaign["id"] for campaign in campaigns if campaign.get("id")}

    clicks_by_campaign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unattributed = 0
    for click in clicks:
        campaign_id = click.get("campaign_id")
        if campaign_id is not None and campaign_id in known_campaign_ids:
            clicks_by_campaign[campaign_id].append(click)
        else:
            unattributed += 1

    out: list[CampaignResults] = []
    for campaign in campaigns:
        campaign_id = campaign["id"]
        breakdown = _click_breakdown(clicks_by_campaign.get(campaign_id, []), links_by_id)
        out.append(
            CampaignResults(
                campaign_id=campaign_id,
                campaign_name=campaign.get("name") or "",
                clicks=breakdown,
                # `denominator=breakdown.total`: once leads become reachable,
                # a click-to-lead rate's population is exactly this
                # campaign's click count. `conversions`/`cost` have no known
                # denominator here — a conversion rate is a share of leads,
                # which this plugin cannot see either, and cost is an amount
                # rather than a share at all.
                leads=UnmeasuredMetric(denominator=breakdown.total),
                conversions=UnmeasuredMetric(),
                cost=UnmeasuredMetric(),
            )
        )

    return ResultsResponse(campaigns=out, unattributed_clicks=unattributed)
