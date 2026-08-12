"""The paid brief pack (M9, issue #8), over the same fakes
`test_marketing_pack_routes.py` uses for the organic pack — the internal
(SigV4) storage client, the dual-auth asset client, and `admin_app._core` —
plus one addition: the fake `_core` also has to answer a `positioning`
artefact lookup, since `targeting` is drawn from it.

Fixture UUIDs deliberately do not end in twelve digits (gitleaks'
`biffo-aws-account-id` rule matches any bare run of twelve).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing import admin_app, pack_routes, paid_pack_routes
from marketing.definitions import PLACEMENTS

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ab"
_SOURCE_MEDIA_ID = "media-source"
_BASE_URL = "https://dev.example.invalid"

_ORGANIC_CHANNEL = {
    "channel_key": "instagram_organic",
    "motion": "organic",
    "headline": "Run every site the same way, finally.",
    "body": "One dashboard, every location.",
    "cta": "See how it works",
    "sources": [{"url": "https://example.com/y", "note": "n"}],
}

_PAID_CHANNEL_META = {
    "channel_key": "facebook_paid",
    "motion": "paid",
    "headline": ("Run every one of your sites exactly the same way, finally, at last, for good"),
    "body": (
        "One dashboard controls every location's marketing, so nothing drifts between "
        "sites and nobody has to remember to do the same thing five times over."
    ),
    "cta": "Book a demo today",
    "sources": [{"url": "https://example.com/z", "note": "n"}],
}

_PAID_CHANNEL_GOOGLE = {
    "channel_key": "google_search_paid",
    "motion": "paid",
    "headline": "Short headline",
    "body": "Short body",
    "cta": "Go",
    "sources": [{"url": "https://example.com/w", "note": "n"}],
}

#: The taxonomy rows behind the channel_keys above (#76 increment 2) — what
#: `_channel_ad_platforms` fetches from `GET /channels` and
#: `_platform_for_channel` looks `ad_platform` up in.
_CHANNEL_TAXONOMY = [
    {
        "key": "instagram_organic",
        "label": "Instagram — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "facebook_paid",
        "label": "Facebook ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "meta",
    },
    {
        "key": "google_search_paid",
        "label": "Google Search ads",
        "motion": "paid",
        "category": "search",
        "ad_platform": "google",
    },
]

_SEGMENTS = [
    {
        "name": "Multi-site operators",
        "description": "Operators running more than one location.",
        "sources": [{"url": "https://example.com/seg", "note": "n"}],
    }
]


def _source_asset() -> dict[str, Any]:
    return {
        "id": "asset-source",
        "campaign_id": _CAMPAIGN,
        "media_kind": "image",
        "placement": None,
        "media_id": _SOURCE_MEDIA_ID,
        "is_source": True,
    }


def _copy_artefact(channels: list[dict[str, Any]], *, status: str = "approved") -> dict[str, Any]:
    return {
        "id": "artefact-copy-1",
        "campaign_id": _CAMPAIGN,
        "kind": "copy",
        "status": status,
        "body": json.dumps({"channels": channels}),
        "created_at": "2026-08-10T00:00:01Z",
    }


def _positioning_artefact(
    segments: list[dict[str, Any]], *, status: str = "approved"
) -> dict[str, Any]:
    return {
        "id": "artefact-positioning-1",
        "campaign_id": _CAMPAIGN,
        "kind": "positioning",
        "status": status,
        "body": json.dumps({"segments": segments, "pillars": [], "ctas": []}),
        "created_at": "2026-08-10T00:00:00Z",
    }


_DEFAULT_CAMPAIGN = object()


class _FakeCore:
    """Stands in for `admin_app._core`: campaigns, artefacts (copy AND
    positioning — the paid pack needs both) and links."""

    def __init__(
        self,
        *,
        campaign: dict[str, Any] | None | object = _DEFAULT_CAMPAIGN,
        copy_artefact: dict[str, Any] | None = None,
        positioning_artefact: dict[str, Any] | None = None,
    ) -> None:
        self.campaign = (
            {
                "id": _CAMPAIGN,
                "destination_url": "https://example.com/landing",
                "guidance": "Disclose #ad. Licensed music only.",
            }
            if campaign is _DEFAULT_CAMPAIGN
            else campaign
        )
        self.copy_artefact = copy_artefact
        self.positioning_artefact = positioning_artefact
        self.links: list[dict[str, Any]] = []
        self.channels: list[dict[str, Any]] = list(_CHANNEL_TAXONOMY)
        self._next_link_id = 0

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        prefix = admin_app._INTERNAL_PREFIX
        if method == "GET" and path == f"{prefix}/campaigns/{_CAMPAIGN}":
            if self.campaign is None:
                return httpx.Response(404, json={"detail": "not found"}, request=request)
            return httpx.Response(200, json=self.campaign, request=request)
        if method == "GET" and path == f"{prefix}/channels":
            return httpx.Response(200, json=self.channels, request=request)
        if method == "GET" and path == f"{prefix}/artefacts":
            params = kw.get("params") or {}
            kind = params.get("kind")
            if kind == "copy":
                rows = [self.copy_artefact] if self.copy_artefact else []
            elif kind == "positioning":
                rows = [self.positioning_artefact] if self.positioning_artefact else []
            else:
                rows = []
            return httpx.Response(200, json=rows, request=request)
        if method == "GET" and path == f"{prefix}/links":
            return httpx.Response(200, json=self.links, request=request)
        if method == "POST" and path == f"{prefix}/links":
            self._next_link_id += 1
            row = {"id": f"link-{self._next_link_id}", **kw["json"]}
            self.links.append(row)
            return httpx.Response(201, json=row, request=request)
        raise AssertionError(f"unexpected call {method} {path}")


class _FakeCampaignClient:
    def __init__(self, *, assets: list[dict[str, Any]] | None = None) -> None:
        self.assets: list[dict[str, Any]] = list(assets or [])

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"{paid_pack_routes._INTERNAL_PREFIX}/assets":
            campaign_id = (params or {}).get("campaign_id")
            return [a for a in self.assets if a.get("campaign_id") == campaign_id]
        raise AssertionError(f"unexpected GET {path}")


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        # The literal storage path `pack_routes._asset_with_url` (reused by
        # this route) resolves URLs through — matching
        # `test_marketing_pack_routes._FakeStorageClient` exactly.
        prefix = f"{pack_routes._STORAGE_PATH}/"
        if path.startswith(prefix) and path.endswith("/url"):
            media_id = path[len(prefix) : -len("/url")]
            return {
                "url": f"https://bucket.s3.eu-west-1.amazonaws.com/signed-get-{media_id}",
                "expires_in": 300,
            }
        raise AssertionError(f"unexpected GET {path}")


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def _app(*, core_client: _FakeStorageClient, campaign_client: _FakeCampaignClient) -> FastAPI:
    app = FastAPI()
    app.include_router(paid_pack_routes.router)
    app.dependency_overrides[paid_pack_routes.require_admin] = _admin_user
    app.dependency_overrides[paid_pack_routes.get_core_client] = lambda: core_client
    app.dependency_overrides[paid_pack_routes.get_campaign_client] = lambda: campaign_client
    return app


@pytest.fixture(autouse=True)
def _base_url(monkeypatch: pytest.MonkeyPatch):
    # `_ensure_links` (reused from `pack_routes`) reads `public_base_url`
    # through that module's own imported name — patched there, matching
    # `test_marketing_pack_routes.py`'s own fixture exactly.
    monkeypatch.setattr(paid_pack_routes.config, "public_base_url_for", lambda *_: _BASE_URL)


# ── failure paths ────────────────────────────────────────────────────────────


def test_404s_an_unknown_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(campaign=None)
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 404


def test_404s_when_no_copy_artefact_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(copy_artefact=None)
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 404
    assert "copy" in resp.json()["detail"].lower()


def test_409s_when_copy_is_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(copy_artefact=_copy_artefact([_PAID_CHANNEL_META], status="proposed"))
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 409


def test_404s_when_the_approved_copy_has_no_paid_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Organic-only copy must not produce a paid pack — there is nothing paid
    to brief."""
    core = _FakeCore(
        copy_artefact=_copy_artefact([_ORGANIC_CHANNEL]),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 404
    assert "paid" in resp.json()["detail"].lower()


def test_404s_when_no_positioning_artefact_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(copy_artefact=_copy_artefact([_PAID_CHANNEL_META]), positioning_artefact=None)
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 404
    assert "positioning" in resp.json()["detail"].lower()


def test_409s_when_positioning_is_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(
        copy_artefact=_copy_artefact([_PAID_CHANNEL_META]),
        positioning_artefact=_positioning_artefact(_SEGMENTS, status="proposed"),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 409


def test_404s_when_the_approved_positioning_has_no_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved positioning with an empty `segments` list must not ship a
    silently-empty targeting brief — same "the gap is visible, not silent"
    discipline as the missing-paid-channels and missing-source-creative
    cases above."""
    core = _FakeCore(
        copy_artefact=_copy_artefact([_PAID_CHANNEL_META]),
        positioning_artefact=_positioning_artefact([]),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 404
    assert "segments" in resp.json()["detail"].lower()


def test_assembles_with_no_assets_reporting_every_placement_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #64, same root cause as `test_marketing_pack_routes.py`'s own
    regression test: `pack_routes._existing_assets` (reused here) used to
    404 ("No approved source creative for this campaign yet.") whenever no
    `marketing_asset` row existed at all, even with copy and positioning both
    fully approved — a campaign whose still was never generated (#63)."""
    core = _FakeCore(
        copy_artefact=_copy_artefact([_PAID_CHANNEL_META]),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient(assets=[]))
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 200
    body = resp.json()
    assert body["assets"] == []
    assert set(body["missing_placements"]) == set(PLACEMENTS)


def test_409s_a_pre_migration_copy_artefact_with_no_channel_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard as `test_marketing_pack_routes.py`'s equivalent, the paid
    half: a copy artefact approved before the taxonomy migration existed
    must not reach `_ad_copy_variant` at all."""
    pre_migration_channels = [
        {
            "channel": "Google Search ads (non-brand, long-tail intent)",
            "motion": "paid",
            "headline": "h",
            "body": "b",
            "cta": "c",
            "sources": [{"url": "https://example.com/y", "note": "n"}],
        }
    ]
    core = _FakeCore(
        copy_artefact=_copy_artefact(pre_migration_channels),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 409
    assert "channel_key" in resp.json()["detail"]


# ── the happy path ───────────────────────────────────────────────────────────


def test_assembles_the_paid_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(
        copy_artefact=_copy_artefact([_ORGANIC_CHANNEL, _PAID_CHANNEL_META, _PAID_CHANNEL_GOOGLE]),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    storage = _FakeStorageClient()
    client = TestClient(_app(core_client=storage, campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 200
    body = resp.json()

    # 1. Ad copy: only the PAID channels, never the organic one, each fit to
    # its ACTUAL platform's limits — looked up from the taxonomy's
    # `ad_platform` (#76 increment 2), not guessed from the channel's name.
    assert {c["channel"] for c in body["ad_copy"]} == {
        "facebook_paid",
        "google_search_paid",
    }
    meta_copy = next(c for c in body["ad_copy"] if c["channel"] == "facebook_paid")
    assert meta_copy["platform"] == "meta"
    assert len(meta_copy["headline"]) <= meta_copy["headline_limit"] == 40
    assert len(meta_copy["body"]) <= meta_copy["body_limit"] == 125
    assert meta_copy["headline_truncated"] is True
    assert meta_copy["headline"].endswith("…")
    assert " " not in meta_copy["headline"][-2:]  # never cut mid-word

    google_copy = next(c for c in body["ad_copy"] if c["channel"] == "google_search_paid")
    assert google_copy["platform"] == "google"
    # Short enough already: untouched, not truncated.
    assert google_copy["headline"] == "Short headline"
    assert google_copy["headline_truncated"] is False

    # 2. Creative: same shape as the organic pack — whatever assets already
    # exist, with the gap named rather than silently absent.
    assert len(body["assets"]) == 1
    assert body["assets"][0]["is_source"] is True
    assert set(body["missing_placements"]) == set(PLACEMENTS)

    # 3. Targeting: name + description from the approved positioning's
    # segments, plus a source count — never the full {url, note} pairs.
    # Those stay on the positioning artefact; repeating them here (the same
    # handful of research URLs and notes on every downstream artefact) was
    # the reported duplication.
    assert body["targeting"] == [
        {
            "name": "Multi-site operators",
            "description": "Operators running more than one location.",
            "source_count": 1,
        }
    ]
    assert "sources" not in body["targeting"][0]

    # 4. Budget: a stated, non-fabricated heuristic over the paid channel
    # count (2 here), not a bid-optimisation number.
    assert body["budget"]["channel_count"] == 2
    assert body["budget"]["per_channel_daily"] == 20.0
    assert body["budget"]["total_daily"] == 40.0
    assert "heuristic" in body["budget"]["basis"].lower()

    # 5. Links: one per PAID channel, both `is_paid`.
    assert len(body["links"]) == 2
    assert all(link["is_paid"] for link in body["links"])
    assert all("utm_medium=paid" in core.links[i]["destination_url"] for i in range(2))

    # 6. Spend: explicitly unmeasurable, never a fabricated zero.
    assert body["spend"]["measurable"] is False
    assert "issue #31" in body["spend"]["reason"] or "31" in body["spend"]["reason"]

    # 7. Guidance carried through unchanged, same as the organic pack.
    assert body["guidance"] == "Disclose #ad. Licensed music only."


def test_does_not_leak_an_existing_organic_link_into_the_paid_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pack_routes._ensure_links` (reused here) returns every link Core
    holds for the campaign, not just the requested `channels` (issue #48) —
    an organic link for a channel this paid pack never asked about must not
    show up in its `links`."""
    core = _FakeCore(
        copy_artefact=_copy_artefact([_ORGANIC_CHANNEL, _PAID_CHANNEL_META]),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    core.links.append(
        {
            "id": "link-organic",
            "campaign_id": _CAMPAIGN,
            "token": "organic-token",
            "channel": _ORGANIC_CHANNEL["channel_key"],
            "variant": None,
            "is_paid": False,
            "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
        }
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 200
    links = resp.json()["links"]
    assert {link["channel"] for link in links} == {_PAID_CHANNEL_META["channel_key"]}
    assert all(link["is_paid"] for link in links)


def test_does_not_remint_a_paid_link_for_a_channel_that_already_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _FakeCore(
        copy_artefact=_copy_artefact([_PAID_CHANNEL_META]),
        positioning_artefact=_positioning_artefact(_SEGMENTS),
    )
    core.links.append(
        {
            "id": "link-existing",
            "campaign_id": _CAMPAIGN,
            "token": "existing-token",
            "channel": "facebook_paid",
            "variant": None,
            "is_paid": True,
            "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
        }
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/paid-pack")

    assert resp.status_code == 200
    assert len(resp.json()["links"]) == 1
    assert resp.json()["links"][0]["url"] == f"{_BASE_URL}/c/existing-token"
    assert core.links == [core.links[0]]


# ── pure-function unit tests ─────────────────────────────────────────────────


def test_fit_to_limit_leaves_short_text_untouched() -> None:
    text, truncated = paid_pack_routes._fit_to_limit("Book a demo", 40)
    assert text == "Book a demo"
    assert truncated is False


def test_fit_to_limit_never_cuts_mid_word() -> None:
    text, truncated = paid_pack_routes._fit_to_limit("Run every one of your sites the same way", 20)
    assert truncated is True
    assert len(text) <= 20
    assert text.endswith("…")
    assert not text[:-1].endswith(" ")  # no trailing space before the ellipsis
    # Every word in the trimmed text is a whole word from the source.
    words = text[:-1].split()
    source_words = "Run every one of your sites the same way".split()
    assert words == source_words[: len(words)]


def test_fit_to_limit_keeps_the_full_word_when_the_cut_already_lands_on_a_boundary() -> None:
    """When `budget` characters happen to land exactly on a space, the whole
    word up to that point must be kept — backing off one further word (the
    bug this regresses) wastes characters the platform actually allows."""
    text, truncated = paid_pack_routes._fit_to_limit("Hello wonderful world", 16)
    assert truncated is True
    assert text == "Hello wonderful…"
    assert len(text) == 16


def test_fit_to_limit_handles_a_limit_smaller_than_the_ellipsis() -> None:
    text, truncated = paid_pack_routes._fit_to_limit("Hello world", 1)
    assert truncated is True
    assert len(text) == 1


@pytest.mark.parametrize(
    ("channel_key", "ad_platforms", "expected"),
    [
        # A real, recognised platform is a straight lookup (#76 increment 2 —
        # no more keyword-guessing a channel's name).
        ("facebook_paid", {"facebook_paid": "meta"}, "meta"),
        ("instagram_paid", {"instagram_paid": "meta"}, "meta"),
        ("google_search_paid", {"google_search_paid": "google"}, "google"),
        ("youtube_paid", {"youtube_paid": "google"}, "google"),
        ("tiktok_paid", {"tiktok_paid": "tiktok"}, "tiktok"),
        ("linkedin_paid", {"linkedin_paid": "linkedin"}, "linkedin"),
        # No ad_platform set on the channel (organic, or a platform-agnostic
        # medium like direct mail) falls back to generic.
        ("direct_mail", {"direct_mail": None}, "generic"),
        # A platform this table does not (yet) carry limits for also falls
        # back to generic, same as an unrecognised one used to.
        ("x_paid", {"x_paid": "x"}, "generic"),
        # A channel_key missing from the map entirely (defensive) — also generic.
        ("unknown_channel", {}, "generic"),
    ],
)
def test_platform_for_channel(
    channel_key: str, ad_platforms: dict[str, str | None], expected: str
) -> None:
    assert paid_pack_routes._platform_for_channel(channel_key, ad_platforms) == expected


def test_platform_keyword_table_is_gone() -> None:
    """The substring-keyword table this function used to guess from is
    DELETED (#76 increment 2), not kept as an unreachable fallback — a dead
    fallback is a thing a later author "fixes" back into use."""
    assert not hasattr(paid_pack_routes, "_PLATFORM_KEYWORDS")


def test_budget_recommendation_scales_with_channel_count() -> None:
    one = paid_pack_routes._budget_recommendation(1)
    three = paid_pack_routes._budget_recommendation(3)
    assert one["total_daily"] == paid_pack_routes._DEFAULT_DAILY_TEST_BUDGET_USD
    assert three["total_daily"] == paid_pack_routes._DEFAULT_DAILY_TEST_BUDGET_USD * 3
    assert three["total_test_budget"] == (
        three["total_daily"] * paid_pack_routes._DEFAULT_TEST_WINDOW_DAYS
    )
