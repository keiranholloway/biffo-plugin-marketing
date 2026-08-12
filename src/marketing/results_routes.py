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

## Clicks are measured directly; leads, conversions and cost go through the
## instance's own leads source (issue #31, option B)

`marketing_click` and `marketing_link` are this plugin's own generated-CRUD
tables (declared in `biffo.plugin.json`), reachable through the same
internal, SigV4-signed mount every other route in this file uses
(`admin_app._core`, `_INTERNAL_PREFIX`). Clicks per campaign, and the
paid/organic split, are real numbers computed from real rows this plugin
wrote itself, and stay that way regardless of anything below.

Leads (`public.demo_requests`, tabsii-platform) and cost
(`tabsii.lead_source_costs`, DDL module 049) are tabsii concepts this plugin
must never import directly — a marketing plugin installed on biffo-platform
has no such tables and never will. Issue #31's settled design (option B) is
that **the plugin declares it needs a leads source, and the instance
configures which endpoint answers it**: `MARKETING_LEADS_SOURCE_URL`
(delivered via `plugin_host_environment`, biffo-template#1534). When set,
this module calls

    GET <leads_source_url>?campaign_id=<uuid>&campaign_id=<uuid>...

and expects back `{"campaigns": [{campaign_id, leads: {value, measurable[,
reason]}, conversions: {...}, cost: {...}}]}` — see `_fetch_leads_source` and
`_metric_from_raw`. tabsii's implementation of that endpoint is a grouped
count over `demo_requests.utm_campaign`, `.status`, and a join to
`lead_source_costs`; another platform answering the same three questions from
entirely different tables is an equally valid implementation. None of that
lives here — this file knows only the wire contract.

**Unset is a first-class state, not an error.** A platform with no leads
source configured (every platform other than tabsii, today) renders
leads/conversions/cost as unmeasurable with the reason "no leads source is
configured for this deployment" — the honest answer, not a failure.

**The source failing must never take down results that already work.** A
timeout, a non-200, or a malformed body degrades every campaign's
leads/conversions/cost to unmeasurable-with-a-reason; clicks, computed
locally, keep rendering regardless (`_fetch_leads_source` never raises).

**A campaign the source doesn't know about is unmeasurable, not an error** —
see the "unknown campaign id" handling in `_metrics_for_campaign`.

**`measurable: false` is never rendered as a zero, and an upstream `reason`
is always preserved verbatim** when the source gives one — see
`_metric_from_raw`. A real zero (`value: 0, measurable: true`) stays
distinguishable throughout.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

import httpx
from aws_lambda_powertools import Logger
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import admin_app

logger = Logger(child=True)

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

#: issue #31's contract: the instance tells this plugin which endpoint
#: answers "leads, conversions and cost for these campaign ids" by setting
#: this environment variable (delivered via `plugin_host_environment`,
#: biffo-template#1534). The value is the full path/URL to call — this
#: module appends the `campaign_id` query params and calls it exactly as
#: given, through the same dual-auth transport (`admin_app._core`) every
#: other Core call in this file uses.
_LEADS_SOURCE_URL_ENV = "MARKETING_LEADS_SOURCE_URL"

#: A platform with no leads source configured is normal, not broken — every
#: platform other than tabsii sees exactly this today. Verbatim text from
#: issue #31's settled contract, since a caller reading only the JSON should
#: get the same words this docstring does.
_NOT_CONFIGURED_REASON = "no leads source is configured for this deployment"

#: The URL is configured but the call itself did not produce a usable
#: response — timeout, connection failure, or a non-200 status. Degrades to
#: unmeasurable rather than a 500 on the whole dashboard; see
#: `_fetch_leads_source`.
_SOURCE_UNREACHABLE_REASON = "the configured leads source could not be reached"

#: The URL answered, but the body wasn't the shape the contract promises
#: (not JSON, no `campaigns` list, or a per-metric object missing the
#: `value`/`measurable` the contract requires). Kept distinct from
#: `_SOURCE_UNREACHABLE_REASON` so an operator reading the reason can tell
#: "nothing answered" from "something answered wrong".
_SOURCE_MALFORMED_REASON = "the configured leads source returned data this plugin could not parse"

#: The call succeeded and the source is working — this specific campaign id
#: simply wasn't in its response. Per the contract: "a campaign the source
#: does not know about is simply unmeasurable, not a 500."
_UNKNOWN_TO_SOURCE_REASON = "the configured leads source has no data for this campaign"

#: `measurable: false` with no `reason` at all is itself a malformed
#: response — the contract requires one — but still degrades to unmeasurable
#: rather than failing the whole call. See `_metric_from_raw`.
_NO_REASON_GIVEN = "the configured leads source marked this unmeasurable but gave no reason"


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

    Still used verbatim by `paid_pack_routes.py` for its own, still-genuinely
    -unreachable `spend` field — unrelated to the leads/conversions/cost
    metrics below, which now go through `Metric` instead.
    """

    measurable: bool = False
    denominator: int | None = None
    reason: str = (
        "No route reachable from this plugin's internal Core transport exposes this yet "
        "(tracked in issue #31) — 'unmeasurable' means the transport does not exist, "
        "not that the count is zero."
    )


class Metric(BaseModel):
    """A leads/conversions/cost figure sourced from the instance-configured
    leads source (issue #31) — or the reason it could not be measured.

    Exactly one of two shapes, matching the wire contract:

    - `measurable=True`, `value` set to a real number (a real zero renders
      exactly this way — `value=0, measurable=True`, distinguishable from an
      unmeasurable metric by `measurable` alone, never by `value` being falsy).
    - `measurable=False`, `value=None`, `reason` set to why.

    `denominator` carries the same meaning as `UnmeasuredMetric.denominator`
    above (see that class) and is populated the same way regardless of
    whether this instance ended up measurable or not.
    """

    value: float | int | None = None
    measurable: bool
    denominator: int | None = None
    reason: str | None = None


class CampaignResults(BaseModel):
    campaign_id: str
    campaign_name: str
    clicks: ClickBreakdown
    leads: Metric
    conversions: Metric
    cost: Metric


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


class _LeadsSourceResult:
    """The outcome of the one call to the instance-configured leads source.

    Exactly one of the three states below holds — callers (`_metrics_for_campaign`)
    check `not_configured` first, then `failure`, then fall through to
    `by_campaign_id`:

    - `not_configured=True`: `MARKETING_LEADS_SOURCE_URL` is unset. A normal,
      first-class state (see module docstring), not a failure.
    - `failure` set: the URL is configured but the call did not produce a
      usable response (timeout, non-200, malformed body). Degrades to
      unmeasurable with `failure` as the reason for every campaign — never a
      500 on the results dashboard.
    - `by_campaign_id` populated (possibly empty): the call succeeded.
      A campaign id simply absent from it is unknown to the source, not an
      error — handled by `_metrics_for_campaign`, not here.
    """

    def __init__(
        self,
        *,
        not_configured: bool = False,
        failure: str | None = None,
        by_campaign_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.not_configured = not_configured
        self.failure = failure
        self.by_campaign_id = by_campaign_id or {}


async def _fetch_leads_source(campaign_ids: list[str], token: str) -> _LeadsSourceResult:
    """Call the instance-configured leads source for `campaign_ids`, or
    report why it couldn't be reached — never raises.

    Every failure mode (unset URL, timeout, connection error, non-200,
    unparseable body, wrong shape) is caught here and turned into a
    `_LeadsSourceResult` state rather than an exception, because a broken
    leads source must not take down `/results` — clicks are computed
    locally from this plugin's own tables and must keep rendering
    regardless of what this call does.
    """
    url = os.environ.get(_LEADS_SOURCE_URL_ENV, "").strip()
    if not url:
        return _LeadsSourceResult(not_configured=True)

    if not campaign_ids:
        # Nothing to ask about — same as a successful call that named no
        # campaigns; every campaign below will fall into the "unknown to
        # source" branch, which is the honest answer to "the source knows
        # nothing about a campaign we never asked it about".
        return _LeadsSourceResult(by_campaign_id={})

    query = urlencode([("campaign_id", campaign_id) for campaign_id in campaign_ids])
    full_path = f"{url}?{query}"

    try:
        resp = await admin_app._core("GET", full_path, token)
    except httpx.HTTPError:
        logger.warning("Leads source request failed", path=url, exc_info=True)
        return _LeadsSourceResult(failure=_SOURCE_UNREACHABLE_REASON)
    except Exception:  # noqa: BLE001 - a broken leads source must never 500 /results
        logger.warning("Leads source request raised unexpectedly", path=url, exc_info=True)
        return _LeadsSourceResult(failure=_SOURCE_UNREACHABLE_REASON)

    if resp.status_code != 200:
        logger.warning(
            "Leads source returned a non-200 status", path=url, status_code=resp.status_code
        )
        return _LeadsSourceResult(failure=_SOURCE_UNREACHABLE_REASON)

    try:
        body = resp.json()
    except ValueError:
        logger.warning("Leads source returned a non-JSON body", path=url)
        return _LeadsSourceResult(failure=_SOURCE_MALFORMED_REASON)

    if not isinstance(body, dict) or not isinstance(body.get("campaigns"), list):
        logger.warning("Leads source response missing a 'campaigns' list", path=url)
        return _LeadsSourceResult(failure=_SOURCE_MALFORMED_REASON)

    by_campaign_id: dict[str, dict[str, Any]] = {}
    for entry in body["campaigns"]:
        if isinstance(entry, dict) and isinstance(entry.get("campaign_id"), str):
            by_campaign_id[entry["campaign_id"]] = entry
    return _LeadsSourceResult(by_campaign_id=by_campaign_id)


def _metric_from_raw(raw: Any, *, denominator: int | None = None) -> Metric:
    """Parse one `{value, measurable[, reason]}` object from the leads
    source into a `Metric`, defensively — a malformed individual field
    degrades to unmeasurable rather than raising and taking the rest of the
    response down with it.
    """
    if not isinstance(raw, dict):
        return Metric(measurable=False, denominator=denominator, reason=_SOURCE_MALFORMED_REASON)

    measurable = raw.get("measurable")
    if measurable is True:
        value = raw.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Metric(
                measurable=False, denominator=denominator, reason=_SOURCE_MALFORMED_REASON
            )
        return Metric(value=value, measurable=True, denominator=denominator)

    if measurable is False:
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = _NO_REASON_GIVEN
        return Metric(measurable=False, denominator=denominator, reason=reason)

    # `measurable` missing or not a bool at all — the contract requires it.
    return Metric(measurable=False, denominator=denominator, reason=_SOURCE_MALFORMED_REASON)


def _metrics_for_campaign(
    campaign_id: str, source: _LeadsSourceResult, *, denominator: int
) -> tuple[Metric, Metric, Metric]:
    """`(leads, conversions, cost)` for one campaign, given the whole leads
    source call's outcome.

    `denominator` (the campaign's click count) is applied to `leads` only,
    in every branch — matching the pre-#31 behaviour, since a click-to-lead
    rate's population is this campaign's click count regardless of whether
    leads turned out measurable. `conversions`/`cost` never carry one: a
    conversion rate's population is a lead count this plugin does not
    independently verify, and cost is an amount, not a share.
    """
    if source.not_configured:
        reason = _NOT_CONFIGURED_REASON
    elif source.failure is not None:
        reason = source.failure
    else:
        entry = source.by_campaign_id.get(campaign_id)
        if entry is None:
            reason = _UNKNOWN_TO_SOURCE_REASON
        else:
            return (
                _metric_from_raw(entry.get("leads"), denominator=denominator),
                _metric_from_raw(entry.get("conversions")),
                _metric_from_raw(entry.get("cost")),
            )

    return (
        Metric(measurable=False, denominator=denominator, reason=reason),
        Metric(measurable=False, reason=reason),
        Metric(measurable=False, reason=reason),
    )


@router.get("/results")
async def results(admin: Any = Depends(admin_app.require_admin)) -> ResultsResponse:
    """Clicks, leads, conversions and cost, per campaign.

    Three list calls against this plugin's own tables — campaigns, links,
    clicks — each paged in full via `_list_all`, then joined in memory:
    `marketing_click.link_id` -> `marketing_link.is_paid` for the paid/organic
    split, and `marketing_click.campaign_id` -> `marketing_campaign.id` for
    the per-campaign grouping. Leads, conversions and cost come from one
    additional call to the instance-configured leads source (`_fetch_leads_source`),
    made once for every campaign id this call knows about — see the module
    docstring for the full contract.
    """
    campaigns = await _list_all(f"{_INTERNAL_PREFIX}/campaigns", admin.token, {})
    links = await _list_all(f"{_INTERNAL_PREFIX}/links", admin.token, {})
    clicks = await _list_all(f"{_INTERNAL_PREFIX}/clicks", admin.token, {})

    links_by_id = {link["id"]: link for link in links if link.get("id")}
    campaign_ids = [campaign["id"] for campaign in campaigns if campaign.get("id")]
    known_campaign_ids = set(campaign_ids)

    clicks_by_campaign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unattributed = 0
    for click in clicks:
        campaign_id = click.get("campaign_id")
        if campaign_id is not None and campaign_id in known_campaign_ids:
            clicks_by_campaign[campaign_id].append(click)
        else:
            unattributed += 1

    leads_source = await _fetch_leads_source(campaign_ids, admin.token)

    out: list[CampaignResults] = []
    for campaign in campaigns:
        campaign_id = campaign["id"]
        breakdown = _click_breakdown(clicks_by_campaign.get(campaign_id, []), links_by_id)
        leads, conversions, cost = _metrics_for_campaign(
            campaign_id, leads_source, denominator=breakdown.total
        )
        out.append(
            CampaignResults(
                campaign_id=campaign_id,
                campaign_name=campaign.get("name") or "",
                clicks=breakdown,
                leads=leads,
                conversions=conversions,
                cost=cost,
            )
        )

    return ResultsResponse(campaigns=out, unattributed_clicks=unattributed)
