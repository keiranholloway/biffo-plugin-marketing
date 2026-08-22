"""Recording what a campaign actually cost (M9, issue #8) — the one criterion
of the paid brief pack that was never built.

An operator executes the paid pack by hand in Ads Manager or Google Ads (see
``paid_pack_routes.py``: no platform API, no app registration, by design), so
the money leaves the building somewhere this deployment cannot observe. The
only way a spend figure can ever exist here is for the person who spent it to
type it in. This module is that surface, and the total it reads back.

## Why the spend row lives in this plugin's own table

The obvious-looking alternative is to write ``tabsii.lead_source_costs``
(DDL module 049) through Core's generic CRUD layer, so the figure lands in
the same ledger the results dashboard's ``cost`` already reads. That does not
work, for four independent reasons — any one of which is sufficient:

1. **This plugin must not name a tabsii table.** ``results_routes.py``'s
   module docstring settles that in as many words for the read side ("cost
   (``tabsii.lead_source_costs``, DDL module 049) [is a] tabsii concept this
   plugin must never import directly — a marketing plugin installed on
   biffo-platform has no such table and never will"), which is why issue
   #31's option B made the *reader* a configured endpoint rather than a
   hard-coded table. A writer hard-coding the same table would undo that.
2. **``brand_id`` is ``NOT NULL`` and this plugin has no brand.** The generic
   CRUD create handler injects ``tenant_id`` only; ``brand_id`` is an
   ordinary caller-supplied payload field, and there is no brand header and
   no default brand anywhere in Core. Writing ``lead_source_costs`` would
   therefore need a brand value this plugin has no way to produce — that is
   the mismatch on its own terms, independent of anything the reading side
   does with the column once it exists. (As of 2026-08-22, the reading side
   has stopped reading that table at all — ``tabsii-platform#892`` removed
   the ``lead_source_costs`` read from ``campaign_results.py`` entirely, so
   ``marketing_spend`` is now the sole cost ledger. That is a second,
   independent reason to prefer it, not the basis for this one.)
3. **The plugin's transport cannot reach that route.** Everything this
   plugin sends to Core goes through ``principal_client`` — SigV4 plus the
   admin's forwarded token — onto ``/api/v1/internal/plugins/marketing/*``.
   ``POST /api/v1/data/lead_source_costs`` is guarded by
   ``require_crud_permission`` -> ``require_auth``, which is Cognito-bearer
   only.
4. **No grant could fix (3).** ``fn_authorized`` resolves permissions only
   through a *user's* role assignments; a ``system:`` service principal has
   no user id and therefore no representable grant at all. It is not a
   missing row somebody can add.

So spend is a ``marketing_spend`` row: this plugin's own generated-CRUD
table, reached by the transport it already has, working identically on every
platform this plugin can be installed into. What that costs is stated in the
issue rather than hidden here — the results dashboard's ``cost`` metric still
comes from the instance-configured leads source and is a different number
answering a different question (what the instance's lead sources cost),
until an instance chooses to have its leads source read this table too.

## No spend recorded is NOT a measured zero

The single decision this module exists to get right. A campaign with no
``marketing_spend`` row is **unmeasurable**, not zero:

- Nothing in this deployment generates a spend row. Zero rows therefore
  means "nobody has told us", never "no money was spent". Reporting ``0``
  would assert "we looked, and nothing happened" — a claim this plugin
  cannot make — and it would be wrong in a systematic direction, since every
  cost-per-click or ROI computed from a fabricated zero reads better than
  reality. That is precisely the failure ``results_routes.py``'s module
  docstring opens with.
- A row with ``amount = 0`` **is** a real measured zero, and renders as one.

That split is not invented here. DDL module 049's own header requires it
("no row at all -> unknown ... a row, amount 0 -> genuinely free ... the
endpoints must return null for the first and 0 for the [second], and must
never coerce one into the other"), and tabsii's ``campaign_results.
_cost_metric`` already implements the reading half the same way.

**It does not contradict issue #99**, which ruled the opposite way for
*leads*: there, the configured source can see the entire lead population, so
an absent campaign id really is a real zero, and the caller vouches the
campaign exists. Spend has no such population — there is nothing to have
looked at. #99's rule is "a zero you can verify is a zero"; this is the same
rule, applied to a figure that cannot be verified.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import admin_app, principal_client
from .results_routes import Metric

require_admin = admin_app.require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

#: Duplicated local constant, not a cross-module reference — see
#: `channel_plan_routes._INTERNAL_PREFIX` and `paid_pack_routes` for exactly
#: why: `tests/test_marketing_core_paths_guard.py` resolves a module-level
#: string constant only within the SAME file's own AST.
_INTERNAL_PREFIX = "/api/v1/internal/plugins/marketing"

#: Core's generic list route caps a single page; spend is totalled across
#: every page rather than the first (`_all_spend_rows`). Matches
#: `results_routes._LIST_PAGE_SIZE` — under-counting money because a list
#: stopped at page one is the same defect that module pages to avoid, with
#: worse consequences.
_LIST_PAGE_SIZE = 200

#: The reason a campaign with no recorded spend carries. Deliberately
#: **actionable**, and deliberately not the pre-#8 wording ("no route
#: reachable from this plugin's internal Core transport exposes this yet
#: (tracked in issue #31)") — that reason was true when there was no way to
#: record spend at all, and repeating it now would tell an operator to wait
#: for a transport that already exists.
NO_SPEND_RECORDED_REASON = (
    "No spend has been recorded against this campaign yet. This plugin calls no ad "
    "platform API, so it can only report spend an operator enters here — no rows means "
    "nobody has entered any, which is not the same as having spent nothing."
)

#: Rows exist but Core handed back an `amount` that is not a number. Kept
#: distinct from `NO_SPEND_RECORDED_REASON` so an operator can tell "you have
#: not told us" from "what you told us came back unusable" — the same
#: distinction `results_routes` draws between its unreachable and malformed
#: reasons.
_UNUSABLE_ROW_REASON = (
    "Spend has been recorded for this campaign, but at least one row came back with an "
    "amount this plugin could not read, so the total would be wrong."
)


class SpendMetric(Metric):
    """`results_routes.Metric`, plus the currency the value is denominated in.

    A bare `240.5` is not a spend figure — 240.5 of what? — and this is the
    one metric in the plugin that is an amount of money rather than a count.
    `currency` is `None` exactly when `measurable` is `False`, for the same
    reason `value` is: there is no figure to denominate.
    """

    currency: str | None = None


class SpendEntry(BaseModel):
    """One spend entry an operator is recording.

    No date field. Core stamps `created_at` on every plugin table row, which
    is when the operator recorded it — and this milestone has no reporting
    that slices spend by period, so a second, hand-entered date would be a
    field nothing reads and every operator has to fill in. A period-scoped
    version of this belongs with whatever first needs to group by one.
    """

    #: `ge=0` rather than `gt=0`: a genuine zero is a recordable, meaningful
    #: figure (see the module docstring), while a negative is a data-entry
    #: slip — module 049 takes exactly this position in SQL
    #: (`CHECK (amount IS NULL OR amount >= 0)`).
    amount: float = Field(ge=0)
    #: ISO-4217-shaped, defaulting to the currency `paid_pack_routes.
    #: _budget_recommendation` states its budget in, so the recommendation
    #: and the actual are denominated the same way unless an operator says
    #: otherwise. Not validated against a currency list: this plugin holds no
    #: such list and inventing a partial one would refuse real currencies.
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        """`usd` and `USD` are one currency, not two.

        Left unnormalised they are two distinct strings, and `recorded_spend`
        would refuse to total them as "more than one currency" — a
        mixed-currency guard firing on a casing difference.
        """
        return value.upper()


def get_campaign_client(
    admin: Any = Depends(require_admin),
) -> principal_client.PrincipalCoreClient:
    """A dual-auth client for this plugin's own generated-CRUD tables,
    matching `pack_routes.get_campaign_client` / `paid_pack_routes.
    get_campaign_client` exactly."""
    return principal_client.PrincipalCoreClient(admin.token)


async def _all_spend_rows(
    campaign_id: str, *, campaign_client: principal_client.PrincipalCoreClient
) -> list[dict[str, Any]]:
    """Every `marketing_spend` row for this campaign — not just page one.

    Same explicit paging as `results_routes._list_all`, and for the same
    reason: Core's generic list route caps a page, so a campaign with more
    entries than one page would silently under-total.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = await campaign_client.get(
            f"{_INTERNAL_PREFIX}/spends",
            params={"campaign_id": campaign_id, "limit": _LIST_PAGE_SIZE, "offset": offset},
        )
        batch = batch or []
        rows.extend(batch)
        if len(batch) < _LIST_PAGE_SIZE:
            return rows
        offset += _LIST_PAGE_SIZE


async def recorded_spend(
    campaign_id: str, *, campaign_client: principal_client.PrincipalCoreClient
) -> SpendMetric:
    """Total recorded spend for one campaign, or the reason there isn't one.

    Four outcomes, all of them honest about which they are:

    - **No rows** -> unmeasurable, `NO_SPEND_RECORDED_REASON`. See the module
      docstring: this is the decision this file exists to make, and it is
      never a zero.
    - **Rows in one currency** -> `measurable=True`, the sum, and that
      currency. A single recorded `0.00` lands here, as a real measured zero.
    - **Rows in more than one currency** -> unmeasurable, naming them.
      `120 USD + 80 GBP` is not `200` of anything; this plugin holds no FX
      rate and must not invent one.
    - **A row with an unreadable `amount`** -> unmeasurable, rather than a
      total silently missing that row, and never a 500 on the whole pack.
    """
    rows = await _all_spend_rows(campaign_id, campaign_client=campaign_client)
    if not rows:
        return SpendMetric(measurable=False, reason=NO_SPEND_RECORDED_REASON)

    currencies = sorted({str(row.get("currency") or "").upper() or "?" for row in rows})
    if len(currencies) > 1:
        return SpendMetric(
            measurable=False,
            reason=(
                "Spend has been recorded for this campaign in more than one currency "
                f"({', '.join(currencies)}). This plugin holds no exchange rate, so it "
                "will not add them together."
            ),
        )

    total = 0.0
    for row in rows:
        amount = row.get("amount")
        # `bool` is an `int` in Python; a `True` here is a broken row, not 1.
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return SpendMetric(measurable=False, reason=_UNUSABLE_ROW_REASON)
        total += float(amount)

    return SpendMetric(value=total, measurable=True, currency=currencies[0])


@router.post("/campaigns/{campaign_id}/spend", status_code=status.HTTP_201_CREATED)
async def record_spend_route(
    campaign_id: str,
    body: SpendEntry,
    campaign_client: principal_client.PrincipalCoreClient = Depends(get_campaign_client),
    admin: Any = Depends(require_admin),
) -> dict[str, Any]:
    """Record one spend entry against this campaign.

    Additive rather than replacing: an operator records what they spent this
    week, and the paid pack totals every entry. Correcting a mistake is not
    yet possible from this surface — `marketing_spend` declares no update or
    delete route (issue #8 scoped this to recording), which is stated here
    rather than left to be discovered.

    The campaign is confirmed to exist **before** anything is written. A
    spend row whose `campaign_id` names no campaign is money recorded against
    nowhere, and no route in this plugin would ever surface it again — it
    would simply be invisible, which is worse than a 404.
    """
    campaign_id = admin_app._validated_campaign_id(campaign_id)

    campaign = await admin_app._core(
        "GET", f"{_INTERNAL_PREFIX}/campaigns/{campaign_id}", admin.token
    )
    if campaign.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    campaign.raise_for_status()

    return await campaign_client.post(
        f"{_INTERNAL_PREFIX}/spends",
        json={
            "campaign_id": campaign_id,
            "amount": body.amount,
            "currency": body.currency,
            "notes": body.notes,
        },
    )
