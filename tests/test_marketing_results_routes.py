"""The results dashboard (M8, issue #7), over a fake Core.

Fakes rather than mocks, matching this repo's `test_marketing_mint_route.py`
convention: a fake that actually stores rows and answers `GET .../clicks`
with a real page of them proves the wiring (method, path, and — new here —
the `limit`/`offset` pagination params `_list_all` sends) rather than merely
asserting a call happened with some argument order a reviewer has to trust.

Also covers issue #31's leads-source contract: the instance configures
`MARKETING_LEADS_SOURCE_URL`, and this plugin calls it and maps the response
into `leads`/`conversions`/`cost`. `_FakeCore` answers that call too, at a
fixed fake URL, so these tests exercise the real query-building and
response-parsing code rather than mocking `_fetch_leads_source` itself.

Issue #99 corrected #31's contract: "unknown campaign ids are omitted" is
withdrawn, replaced by "the source MUST return an entry for every requested
campaign_id". A requested id absent from the source's response is now
evidence the source did not conform, handled as a defensive fallback only —
see `test_leads_source_omitting_a_requested_campaign_degrades_without_fabricating_a_zero`
and `test_leads_source_reports_a_real_zero_for_a_campaign_with_no_leads_yet`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from marketing import admin_app, results_routes

_CAMPAIGN_A = "b3f1c0de-0000-4000-8000-0000000000fa"
_CAMPAIGN_B = "b3f1c0de-0000-4000-8000-0000000000fb"
_DELETED_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000fc"

#: Deliberately not `/api/v1/internal/plugins/marketing/...` — the whole
#: point of issue #31's contract is that the leads source lives on the
#: INSTANCE's own mount (tabsii's real value is
#: `/api/v1/internal/tabsii/campaign-results`), not this plugin's own.
_LEADS_URL = "/api/v1/internal/tabsii/campaign-results"


class _FakeCore:
    """An in-memory `marketing_campaign`/`marketing_link`/`marketing_click`
    store, paged the same way the real Core `list` route is (limit/offset,
    default order is whatever insertion order gave it — this fake doesn't
    need to match Core's `created_at DESC` ordering, since nothing under
    test depends on row order).

    Also stands in for the instance-configured leads source at `_LEADS_URL`:
    `leads_response` is returned as the 200 body, `leads_status` overrides
    the status code, and `leads_raises` — if set — is raised instead of
    returning at all (simulating a timeout/connection failure). Every path
    requested at `_LEADS_URL` is recorded in `leads_calls` so a test can
    assert on the exact query string sent.
    """

    def __init__(self) -> None:
        self.campaigns: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.clicks: list[dict[str, Any]] = []
        self.leads_response: dict[str, Any] = {"campaigns": []}
        self.leads_status: int = 200
        self.leads_raises: Exception | None = None
        #: When set, returned as the raw (non-JSON) response body instead of
        #: `leads_response` — simulates the leads source answering with
        #: something that isn't valid JSON at all.
        self.leads_raw_body: bytes | None = None
        self.leads_calls: list[str] = []

    def _table(self, path: str) -> list[dict[str, Any]] | None:
        prefix = results_routes._INTERNAL_PREFIX
        return {
            f"{prefix}/campaigns": self.campaigns,
            f"{prefix}/links": self.links,
            f"{prefix}/clicks": self.clicks,
        }.get(path)

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        base_path = path.split("?", 1)[0]
        if base_path == _LEADS_URL:
            self.leads_calls.append(path)
            if self.leads_raises is not None:
                raise self.leads_raises
            if self.leads_raw_body is not None:
                return httpx.Response(
                    self.leads_status, content=self.leads_raw_body, request=request
                )
            return httpx.Response(self.leads_status, json=self.leads_response, request=request)
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


def test_unset_leads_source_url_is_unmeasurable_not_an_error(ctx, monkeypatch) -> None:
    """Issue #31's contract: `MARKETING_LEADS_SOURCE_URL` unset is a
    first-class state, not a failure — every platform other than tabsii sees
    exactly this today. Must never render as a bare `0` — that would claim
    "we looked and nothing happened," which this plugin cannot say."""
    monkeypatch.delenv(results_routes._LEADS_SOURCE_URL_ENV, raising=False)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.links = [_link("link-1", _CAMPAIGN_A, is_paid=True)]
    core.clicks = [_click("c1", _CAMPAIGN_A, "link-1"), _click("c2", _CAMPAIGN_A, "link-1")]

    campaign = client.get("/results").json()["campaigns"][0]

    assert campaign["leads"]["measurable"] is False
    assert campaign["leads"]["value"] is None
    assert campaign["leads"]["denominator"] == 2, "the click count, once leads become reachable"
    assert campaign["leads"]["reason"] == "no leads source is configured for this deployment"

    assert campaign["conversions"]["measurable"] is False
    assert campaign["conversions"]["denominator"] is None, "a lead count this plugin can't see"
    assert campaign["conversions"]["reason"] == "no leads source is configured for this deployment"

    assert campaign["cost"]["measurable"] is False
    assert campaign["cost"]["denominator"] is None
    assert core.leads_calls == [], "no configured URL means no call is even attempted"


def test_leads_source_reports_a_real_zero_distinguishable_from_unmeasurable(
    ctx, monkeypatch
) -> None:
    """A campaign with genuinely no leads (`value: 0, measurable: true`) must
    render as a real, verified zero — not collapse into the same shape as
    "no data"."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "No leads yet")]
    core.leads_response = {
        "campaigns": [
            {
                "campaign_id": _CAMPAIGN_A,
                "leads": {"value": 0, "measurable": True},
                "conversions": {"value": 0, "measurable": True},
                "cost": {"value": 0, "measurable": True},
            }
        ]
    }

    campaign = client.get("/results").json()["campaigns"][0]
    assert campaign["leads"] == {"value": 0, "measurable": True, "denominator": 0, "reason": None}
    assert campaign["conversions"] == {
        "value": 0,
        "measurable": True,
        "denominator": None,
        "reason": None,
    }
    assert campaign["cost"]["value"] == 0
    assert campaign["cost"]["measurable"] is True


def test_leads_source_unmeasurable_metric_preserves_the_upstream_reason(ctx, monkeypatch) -> None:
    """The exact example from issue #31's settled contract: a mix of
    measured values and one explicitly unmeasurable metric with its own
    reason, which must survive verbatim rather than being replaced or
    dropped."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.leads_response = {
        "campaigns": [
            {
                "campaign_id": _CAMPAIGN_A,
                "leads": {"value": 12, "measurable": True},
                "conversions": {"value": 3, "measurable": True},
                "cost": {
                    "value": None,
                    "measurable": False,
                    "reason": "no spend recorded for this campaign",
                },
            }
        ]
    }

    campaign = client.get("/results").json()["campaigns"][0]
    assert campaign["leads"]["value"] == 12
    assert campaign["leads"]["measurable"] is True
    assert campaign["conversions"]["value"] == 3
    assert campaign["conversions"]["measurable"] is True
    assert campaign["cost"]["measurable"] is False
    assert campaign["cost"]["value"] is None
    assert campaign["cost"]["reason"] == "no spend recorded for this campaign"


def test_leads_source_omitting_a_requested_campaign_degrades_without_fabricating_a_zero(
    ctx, monkeypatch
) -> None:
    """Issue #99's corrected contract: the source MUST return an entry for
    every requested campaign_id, so an id genuinely absent from its response
    means the source did not conform — not "unknown campaign" (that rule was
    withdrawn; see the module docstring). This is now purely a defensive
    fallback: the plugin cannot tell a broken source from a real zero it
    dropped, so it must degrade to unmeasurable rather than guess `0`."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Omitted by a non-conforming source")]
    core.leads_response = {"campaigns": []}  # source answered but omitted a requested id

    resp = client.get("/results")
    assert resp.status_code == 200
    campaign = resp.json()["campaigns"][0]
    assert campaign["leads"]["measurable"] is False
    assert campaign["leads"]["value"] is None, "never fabricate a zero for an omitted campaign"
    assert campaign["leads"]["reason"] == (
        "the configured leads source did not return an entry for this campaign, "
        "though the contract requires one for every requested campaign_id"
    )
    assert campaign["conversions"]["measurable"] is False
    assert campaign["cost"]["measurable"] is False


def test_leads_source_reports_a_real_zero_for_a_campaign_with_no_leads_yet(
    ctx, monkeypatch
) -> None:
    """The case issue #99 exists to make reachable: a campaign that has been
    clicked but has produced no leads yet — the single most common campaign
    state — must render as a real, verified zero, not unmeasurable. Distinct
    from `test_leads_source_reports_a_real_zero_distinguishable_from_unmeasurable`,
    which proves the shape survives parsing; this proves the specific
    scenario #99 reported (an entry present with a zero value) is reachable
    at all now that the source is required to include every requested id."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Published and clicked, zero leads so far")]
    core.links = [_link("link-1", _CAMPAIGN_A, is_paid=True)]
    core.clicks = [_click("c1", _CAMPAIGN_A, "link-1"), _click("c2", _CAMPAIGN_A, "link-1")]
    core.leads_response = {
        "campaigns": [
            {
                "campaign_id": _CAMPAIGN_A,
                "leads": {"value": 0, "measurable": True},
                "conversions": {"value": 0, "measurable": True},
                "cost": {"value": 0, "measurable": True},
            }
        ]
    }

    campaign = client.get("/results").json()["campaigns"][0]
    assert campaign["leads"]["value"] == 0
    assert campaign["leads"]["measurable"] is True
    assert campaign["leads"]["denominator"] == 2


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(lambda core: setattr(core, "leads_status", 500), id="non_200"),
        pytest.param(
            lambda core: setattr(core, "leads_response", {"not": "the contract"}),
            id="malformed_body",
        ),
        pytest.param(
            lambda core: setattr(core, "leads_raises", httpx.ConnectTimeout("simulated timeout")),
            id="timeout",
        ),
        pytest.param(
            lambda core: setattr(core, "leads_raises", RuntimeError("boom")),
            id="unexpected_non_http_error",
        ),
        pytest.param(
            lambda core: setattr(core, "leads_raw_body", b"not json at all"), id="non_json_body"
        ),
    ],
)
def test_a_failing_leads_source_degrades_to_unmeasurable_not_a_500(
    ctx, monkeypatch, configure
) -> None:
    """Every named failure mode (non-200, malformed body, timeout/connection
    failure) must degrade to unmeasurable-with-a-reason — never a 500 on the
    whole results dashboard. Clicks, computed locally, must keep rendering
    regardless of what the leads source does."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.links = [_link("link-1", _CAMPAIGN_A, is_paid=True)]
    core.clicks = [_click("c1", _CAMPAIGN_A, "link-1"), _click("c2", _CAMPAIGN_A, "link-1")]
    configure(core)

    resp = client.get("/results")
    assert resp.status_code == 200
    campaign = resp.json()["campaigns"][0]
    assert campaign["clicks"] == {"total": 2, "paid": 2, "organic": 0, "unknown_channel_type": 0}
    assert campaign["leads"]["measurable"] is False
    assert campaign["leads"]["reason"]
    assert campaign["conversions"]["measurable"] is False
    assert campaign["cost"]["measurable"] is False


@pytest.mark.parametrize(
    "leads_field",
    [
        pytest.param("not a dict at all", id="not_a_dict"),
        pytest.param({"value": 12}, id="measurable_missing"),
        pytest.param({"value": "twelve", "measurable": True}, id="value_wrong_type"),
        pytest.param({"measurable": False}, id="unmeasurable_no_reason_given"),
    ],
)
def test_a_malformed_individual_metric_degrades_without_failing_the_whole_campaign(
    ctx, monkeypatch, leads_field
) -> None:
    """One malformed metric inside an otherwise well-formed response must not
    crash the request — `_metric_from_raw` degrades just that field, per the
    contract's "malformed body ... must degrade to unmeasurable" requirement
    applied at the per-metric level too."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "Spring launch")]
    core.leads_response = {
        "campaigns": [
            {
                "campaign_id": _CAMPAIGN_A,
                "leads": leads_field,
                "conversions": {"value": 3, "measurable": True},
                "cost": {"value": 0, "measurable": True},
            }
        ]
    }

    resp = client.get("/results")
    assert resp.status_code == 200
    campaign = resp.json()["campaigns"][0]
    assert campaign["leads"]["measurable"] is False
    assert campaign["leads"]["value"] is None
    assert campaign["leads"]["reason"]
    # The rest of the entry is unaffected by one bad field.
    assert campaign["conversions"] == {
        "value": 3,
        "measurable": True,
        "denominator": None,
        "reason": None,
    }
    assert campaign["cost"]["value"] == 0


def test_leads_source_is_queried_with_one_repeated_campaign_id_param_per_campaign(
    ctx, monkeypatch
) -> None:
    """The exact wire contract: `?campaign_id=<uuid>&campaign_id=<uuid>...`
    — one call, all campaign ids, as repeated query params rather than any
    other encoding (a list, a comma-joined string, ...)."""
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx
    core.campaigns = [_campaign(_CAMPAIGN_A, "A"), _campaign(_CAMPAIGN_B, "B")]

    client.get("/results")

    assert len(core.leads_calls) == 1
    path = core.leads_calls[0]
    assert path.startswith(f"{_LEADS_URL}?")
    pairs = parse_qsl(path.split("?", 1)[1])
    campaign_id_values = [v for k, v in pairs if k == "campaign_id"]
    assert set(campaign_id_values) == {_CAMPAIGN_A, _CAMPAIGN_B}
    assert len(campaign_id_values) == 2, "one param per campaign, not a single joined value"


def test_leads_source_is_not_called_when_there_are_no_campaigns(ctx, monkeypatch) -> None:
    monkeypatch.setenv(results_routes._LEADS_SOURCE_URL_ENV, _LEADS_URL)
    client, core = ctx

    resp = client.get("/results")

    assert resp.status_code == 200
    assert core.leads_calls == []


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
