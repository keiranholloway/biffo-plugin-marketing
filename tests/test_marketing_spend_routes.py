"""Recording ad spend against a campaign (M9, issue #8) and reading it back
as an honest metric.

The two properties this file exists to pin, both of them the
"no data is not zero" discipline `results_routes.py` is built around:

1. **No spend recorded is NOT a measured zero.** This plugin cannot observe
   ad spend — nothing generates a `marketing_spend` row except a person
   typing one in — so "no rows" means "nobody has told us", which is
   unknown. Reporting `0` there would assert "we looked, and nothing was
   spent", a claim this plugin cannot make, and it would understate spend in
   a systematic direction (every ROI computed from it reads better than
   reality). See `test_no_recorded_spend_is_unmeasurable_not_a_zero`.
2. **A recorded `0.00` IS a measured zero**, distinguishable from (1) by
   `measurable` alone and never by `value` being falsy — the exact property
   issues #99 and #114 exist to protect. See
   `test_a_recorded_zero_is_a_real_measured_zero`.

That split is not this file's invention: DDL module 049's own header states
it as a requirement ("no row at all -> unknown; a row, amount 0 -> genuinely
free... the endpoints must never coerce one into the other"), and tabsii's
`campaign_results._cost_metric` already implements the reading half the same
way.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing import admin_app, principal_client, spend_routes

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ab"
_OTHER_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000cd"


#: `campaign=None` has to mean "Core 404s this campaign", so the "use the
#: default" case needs a sentinel of its own — matching
#: `test_marketing_paid_pack_routes._DEFAULT_CAMPAIGN` exactly.
_DEFAULT_CAMPAIGN = object()


class _FakeCore:
    """Stands in for `admin_app._core` — only the campaign lookup the record
    route makes before it writes anything."""

    def __init__(self, *, campaign: dict[str, Any] | None | object = _DEFAULT_CAMPAIGN) -> None:
        self.campaign = (
            {"id": _CAMPAIGN, "name": "C"} if campaign is _DEFAULT_CAMPAIGN else campaign
        )

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        prefix = admin_app._INTERNAL_PREFIX
        if method == "GET" and path.startswith(f"{prefix}/campaigns/"):
            if self.campaign is None:
                return httpx.Response(404, json={"detail": "not found"}, request=request)
            return httpx.Response(200, json=self.campaign, request=request)
        raise AssertionError(f"unexpected call {method} {path}")


class _FakeSpendClient:
    """The dual-auth generated-CRUD client, over `marketing_spend` rows.

    Pages the way Core's generic list route really does (`limit`/`offset`),
    so `test_totals_every_page_of_recorded_spend` exercises the paging rather
    than a fake that always returns everything.
    """

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.posted: list[dict[str, Any]] = []
        self._next_id = 0

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert path == f"{spend_routes._INTERNAL_PREFIX}/spends", path
        params = params or {}
        matching = [r for r in self.rows if r.get("campaign_id") == params.get("campaign_id")]
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or len(matching))
        return matching[offset : offset + limit]

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        assert path == f"{spend_routes._INTERNAL_PREFIX}/spends", path
        self._next_id += 1
        row = {"id": f"spend-{self._next_id}", **(json or {})}
        self.posted.append(row)
        self.rows.append(row)
        return row


def _as_client(fake: _FakeSpendClient) -> principal_client.PrincipalCoreClient:
    """`recorded_spend` is typed against the real dual-auth client, and these
    tests call it directly rather than through `dependency_overrides` — so the
    structural stand-in needs one explicit cast rather than a loosened
    signature on production code."""
    return cast(principal_client.PrincipalCoreClient, fake)


def _row(amount: float | None, *, currency: str = "USD", campaign_id: str = _CAMPAIGN) -> dict:
    return {
        "id": f"row-{amount}-{currency}",
        "campaign_id": campaign_id,
        "amount": amount,
        "currency": currency,
        "notes": None,
    }


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def _app(*, spend_client: _FakeSpendClient) -> FastAPI:
    app = FastAPI()
    app.include_router(spend_routes.router)
    app.dependency_overrides[spend_routes.require_admin] = _admin_user
    app.dependency_overrides[spend_routes.get_campaign_client] = lambda: spend_client
    return app


# ── recording ───────────────────────────────────────────────────────────────


def test_records_spend_against_the_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_app, "_core", _FakeCore())
    client_rows = _FakeSpendClient()
    client = TestClient(_app(spend_client=client_rows))

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/spend",
        json={"amount": 240.5, "currency": "USD", "notes": "Week 1, Meta"},
    )

    assert resp.status_code == 201
    assert client_rows.posted == [
        {
            "id": "spend-1",
            "campaign_id": _CAMPAIGN,
            "amount": 240.5,
            "currency": "USD",
            "notes": "Week 1, Meta",
        }
    ]


def test_404s_an_unknown_campaign_rather_than_writing_an_orphan_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spend row whose `campaign_id` names nothing is money recorded
    against nowhere — and this plugin has no route that would ever surface
    it again. Refused before the write, not after."""
    monkeypatch.setattr(admin_app, "_core", _FakeCore(campaign=None))
    client_rows = _FakeSpendClient()
    client = TestClient(_app(spend_client=client_rows))

    resp = client.post(f"/campaigns/{_CAMPAIGN}/spend", json={"amount": 10.0})

    assert resp.status_code == 404
    assert client_rows.posted == []


def test_404s_a_campaign_id_that_is_not_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin_app._validated_campaign_id`'s SSRF guard, on this route too —
    the path segment is interpolated into a credentialed Core URL."""
    monkeypatch.setattr(admin_app, "_core", _FakeCore())
    client_rows = _FakeSpendClient()
    client = TestClient(_app(spend_client=client_rows))

    resp = client.post("/campaigns/..%2Fadmin/spend", json={"amount": 10.0})

    assert resp.status_code == 404
    assert client_rows.posted == []


def test_rejects_negative_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative spend is a data-entry slip, not a refund model — module 049's
    own `CHECK (amount IS NULL OR amount >= 0)` takes the same position."""
    monkeypatch.setattr(admin_app, "_core", _FakeCore())
    client_rows = _FakeSpendClient()
    client = TestClient(_app(spend_client=client_rows))

    resp = client.post(f"/campaigns/{_CAMPAIGN}/spend", json={"amount": -1.0})

    assert resp.status_code == 422
    assert client_rows.posted == []


def test_normalises_the_currency_code_to_upper_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """`usd` and `USD` are the same currency. Left unnormalised they are two,
    and `recorded_spend` would refuse to sum them as "more than one
    currency" — a mixed-currency guard firing on a casing difference."""
    monkeypatch.setattr(admin_app, "_core", _FakeCore())
    client_rows = _FakeSpendClient()
    client = TestClient(_app(spend_client=client_rows))

    resp = client.post(f"/campaigns/{_CAMPAIGN}/spend", json={"amount": 5.0, "currency": "usd"})

    assert resp.status_code == 201
    assert client_rows.posted[0]["currency"] == "USD"


# ── reading it back ─────────────────────────────────────────────────────────


async def test_no_recorded_spend_is_unmeasurable_not_a_zero() -> None:
    """The whole point of this milestone's spend field.

    Zero rows means nobody has recorded anything, which is unknown — not
    "£0 was spent". Reporting a measured zero here would be the systematic
    understatement `results_routes.py`'s module docstring exists to prevent.
    """
    metric = await spend_routes.recorded_spend(
        _CAMPAIGN, campaign_client=_as_client(_FakeSpendClient())
    )

    assert metric.measurable is False
    assert metric.value is None
    assert metric.reason == spend_routes.NO_SPEND_RECORDED_REASON
    # The reason must be actionable now that a transport exists — it is no
    # longer "issue #31, there is no route".
    assert "31" not in metric.reason


async def test_a_recorded_zero_is_a_real_measured_zero() -> None:
    """The other half of the same distinction: an operator who really did
    spend nothing can say so, and it renders as a measured `0`."""
    client = _FakeSpendClient(rows=[_row(0.0)])

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.measurable is True
    assert metric.value == 0
    assert metric.reason is None


async def test_totals_every_page_of_recorded_spend() -> None:
    """Under-counting money because a list stopped at page one is the same
    defect `results_routes._list_all` exists to prevent, and worse here."""
    rows = [_row(1.0) for _ in range(spend_routes._LIST_PAGE_SIZE + 3)]
    client = _FakeSpendClient(rows=rows)

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.measurable is True
    assert metric.value == pytest.approx(float(spend_routes._LIST_PAGE_SIZE + 3))


async def test_only_totals_this_campaigns_rows() -> None:
    client = _FakeSpendClient(rows=[_row(10.0), _row(99.0, campaign_id=_OTHER_CAMPAIGN)])

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.value == pytest.approx(10.0)


async def test_mixed_currencies_are_unmeasurable_rather_than_a_wrong_sum() -> None:
    """`120 USD + 80 GBP` is not `200` of anything. This plugin holds no FX
    rate and must not invent one, so it says so instead of adding the
    numbers — the same "a confident number that is wrong in a systematic
    direction is worse than a missing one" rule as everywhere else here."""
    client = _FakeSpendClient(rows=[_row(120.0, currency="USD"), _row(80.0, currency="GBP")])

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.measurable is False
    assert metric.value is None
    assert metric.reason is not None
    assert "GBP" in metric.reason and "USD" in metric.reason


async def test_the_measured_metric_carries_the_currency_it_is_denominated_in() -> None:
    """A bare number is not a spend figure — `240.5` of what?"""
    client = _FakeSpendClient(rows=[_row(240.5, currency="GBP")])

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.measurable is True
    assert metric.currency == "GBP"


async def test_a_row_with_an_unusable_amount_degrades_rather_than_raising() -> None:
    """A row Core hands back with a non-numeric `amount` must not 500 the
    whole paid pack — same defensive posture as `_metric_from_raw`."""
    client = _FakeSpendClient(rows=[_row("not a number")])  # type: ignore[arg-type]

    metric = await spend_routes.recorded_spend(_CAMPAIGN, campaign_client=_as_client(client))

    assert metric.measurable is False
    assert metric.value is None
    assert metric.reason
