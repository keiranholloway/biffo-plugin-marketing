"""The results dashboard (M8, issue #7), over a fake Core.

Fakes rather than mocks, matching this repo's `test_marketing_mint_route.py`
convention: a fake that actually stores rows and answers `GET .../clicks`
with a real page of them proves the wiring (method, path, and — new here —
the `limit`/`offset` pagination params `_list_all` sends) rather than merely
asserting a call happened with some argument order a reviewer has to trust.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marketing import admin_app, results_routes

_CAMPAIGN_A = "b3f1c0de-0000-4000-8000-0000000000fa"
_CAMPAIGN_B = "b3f1c0de-0000-4000-8000-0000000000fb"
_DELETED_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000fc"


class _FakeCore:
    """An in-memory `marketing_campaign`/`marketing_link`/`marketing_click`
    store, paged the same way the real Core `list` route is (limit/offset,
    default order is whatever insertion order gave it — this fake doesn't
    need to match Core's `created_at DESC` ordering, since nothing under
    test depends on row order)."""

    def __init__(self) -> None:
        self.campaigns: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.clicks: list[dict[str, Any]] = []

    def _table(self, path: str) -> list[dict[str, Any]] | None:
        prefix = results_routes._INTERNAL_PREFIX
        return {
            f"{prefix}/campaigns": self.campaigns,
            f"{prefix}/links": self.links,
            f"{prefix}/clicks": self.clicks,
        }.get(path)

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        rows = self._table(path)
        if method != "GET" or rows is None:
            raise AssertionError(f"unexpected call {method} {path}")
        params = kw.get("params") or {}
        limit = params["limit"]
        offset = params["offset"]
        return httpx.Response(200, json=rows[offset : offset + limit], request=request)


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch):
    core = _FakeCore()
    monkeypatch.setattr(admin_app, "_core", core)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"}
    )()
    return TestClient(app), core


def _campaign(campaign_id: str, name: str) -> dict[str, Any]:
    return {"id": campaign_id, "name": name}


def _link(link_id: str, campaign_id: str, *, is_paid: bool | None) -> dict[str, Any]:
    return {"id": link_id, "campaign_id": campaign_id, "is_paid": is_paid}


def _click(click_id: str, campaign_id: str, link_id: str | None) -> dict[str, Any]:
    return {"id": click_id, "campaign_id": campaign_id, "link_id": link_id}


def test_clicks_are_split_paid_organic_and_unknown(ctx) -> None:
    """The one thing this endpoint can actually measure, measured correctly.

    `link-unknown` carries `is_paid=None` and `link-missing` isn't in the
    links table at all (a click whose link was later deleted) — both must
    land in `unknown_channel_type`, never silently folded into `organic`.
    """
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.links = [
        _link("link-paid", _CAMPAIGN_A, is_paid=True),
        _link("link-organic", _CAMPAIGN_A, is_paid=False),
        _link("link-unknown", _CAMPAIGN_A, is_paid=None),
    ]
    core.clicks = [
        _click("c1", _CAMPAIGN_A, "link-paid"),
        _click("c2", _CAMPAIGN_A, "link-paid"),
        _click("c3", _CAMPAIGN_A, "link-organic"),
        _click("c4", _CAMPAIGN_A, "link-unknown"),
        _click("c5", _CAMPAIGN_A, "link-missing"),
    ]

    resp = client.get("/results")
    assert resp.status_code == 200
    clicks = resp.json()["campaigns"][0]["clicks"]
    assert clicks == {"total": 5, "paid": 2, "organic": 1, "unknown_channel_type": 2}


def test_a_campaign_with_no_clicks_reports_a_real_verified_zero(ctx) -> None:
    """Contrast case for the next test: this IS zero, because clicks are
    actually measured — not `UnmeasuredMetric`, whose `measurable` is always
    `False`."""
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "No traffic yet")]

    resp = client.get("/results")
    clicks = resp.json()["campaigns"][0]["clicks"]
    assert clicks == {"total": 0, "paid": 0, "organic": 0, "unknown_channel_type": 0}


def test_leads_conversions_and_cost_are_unmeasurable_never_zero(ctx) -> None:
    """The milestone's actual claim: no reachable transport exists yet, so
    these must never render as a bare `0` — that would claim "we looked and
    nothing happened," which this plugin cannot say."""
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.links = [_link("link-1", _CAMPAIGN_A, is_paid=True)]
    core.clicks = [_click("c1", _CAMPAIGN_A, "link-1"), _click("c2", _CAMPAIGN_A, "link-1")]

    campaign = client.get("/results").json()["campaigns"][0]

    assert campaign["leads"]["measurable"] is False
    assert campaign["leads"]["denominator"] == 2, "the click count, once leads become reachable"
    assert "issue #31" in campaign["leads"]["reason"]

    assert campaign["conversions"]["measurable"] is False
    assert campaign["conversions"]["denominator"] is None, "a lead count this plugin can't see"

    assert campaign["cost"]["measurable"] is False
    assert campaign["cost"]["denominator"] is None


def test_pagination_gathers_every_click_not_just_the_first_page(ctx) -> None:
    """`_list_all` must page through Core's `limit`/`offset` list route
    rather than trusting one response — proven here with more clicks than
    one page (`_LIST_PAGE_SIZE`), not asserted against a mock that would
    stay green even if the pagination loop were deleted.
    """
    client, core = ctx
    total_clicks = results_routes._LIST_PAGE_SIZE + 37
    core.campaigns = [_campaign(_CAMPAIGN_A, "High traffic")]
    core.links = [_link("link-1", _CAMPAIGN_A, is_paid=True)]
    core.clicks = [_click(f"c{i}", _CAMPAIGN_A, "link-1") for i in range(total_clicks)]

    clicks = client.get("/results").json()["campaigns"][0]["clicks"]
    assert clicks["total"] == total_clicks
    assert clicks["paid"] == total_clicks


def test_clicks_for_a_deleted_campaign_are_counted_not_silently_dropped(ctx) -> None:
    """A click naming a campaign this call can't see (deleted after the
    click landed) must not just vanish from the numbers — it has to show up
    somewhere, or the total the dashboard implies is quietly wrong."""
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Still live")]
    core.clicks = [
        _click("c1", _CAMPAIGN_A, None),
        _click("c2", _DELETED_CAMPAIGN, None),
        _click("c3", _DELETED_CAMPAIGN, None),
    ]

    body = client.get("/results").json()
    assert body["unattributed_clicks"] == 2
    assert body["campaigns"][0]["clicks"]["total"] == 1
    assert len(body["campaigns"]) == 1, "the deleted campaign itself must not appear"


def test_multiple_campaigns_do_not_leak_clicks_into_each_other(ctx) -> None:
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "A"), _campaign(_CAMPAIGN_B, "B")]
    core.links = [
        _link("link-a", _CAMPAIGN_A, is_paid=True),
        _link("link-b", _CAMPAIGN_B, is_paid=False),
    ]
    core.clicks = [
        _click("c1", _CAMPAIGN_A, "link-a"),
        _click("c2", _CAMPAIGN_B, "link-b"),
        _click("c3", _CAMPAIGN_B, "link-b"),
    ]

    campaigns = {c["campaign_id"]: c for c in client.get("/results").json()["campaigns"]}
    assert campaigns[_CAMPAIGN_A]["clicks"]["total"] == 1
    assert campaigns[_CAMPAIGN_B]["clicks"]["total"] == 2


def test_no_campaigns_at_all_is_an_empty_list_not_an_error(ctx) -> None:
    client, _core = ctx
    resp = client.get("/results")
    assert resp.status_code == 200
    assert resp.json() == {"campaigns": [], "unattributed_clicks": 0}


def test_results_requires_the_admin_group() -> None:
    """The host gates on the Cognito group before this app sees a request
    (ADR-0011); asserted here the same way every other route in this repo
    asserts it — no dependency override, so `require_admin` runs for real
    and rejects the missing bearer token."""
    app = admin_app.build_app()
    resp = TestClient(app).get("/results")
    assert resp.status_code == 401
