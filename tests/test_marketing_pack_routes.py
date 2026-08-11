"""The distribution pack (M5, issue #4), over fakes for the internal (SigV4)
storage client, the dual-auth asset client, and `admin_app._core`.

Fakes rather than mocks, matching this repo's convention
(`test_marketing_mint_route.py`, `test_marketing_image_routes.py`,
`test_marketing_pipeline_routes.py`): what is worth asserting is what got
written to `marketing_link` and what the pack response actually assembles,
and a fake that records both says that directly.

A standalone app with only `pack_routes.router` — not `admin_app.build_app()`
— for the same reason `test_marketing_image_routes.py` gives: these tests
should not depend on whatever else lands in `admin_app.py` concurrently.
`admin_app._core` is still reached through (patched via monkeypatch), since
`pack_routes.py` calls it for the campaign/artefact/link lookups it shares
with every other route module in this plugin.

**No S3-facing traffic in this file.** An earlier version of this route
rendered `PLACEMENTS` here — fetching the source creative back from object
storage and re-uploading each crop — which is what tripped CodeQL's
`py/full-ssrf` (see `pack_routes.py`'s module docstring for the full story
and the issue that now owns fixing it properly, at generation time in
`image_routes.py`). This route only ever resolves `marketing_asset` rows
that already exist to a GET url, so there is nothing here for a fake S3
transport to intercept.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing import admin_app, config, pack_routes
from marketing.definitions import PLACEMENTS

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000c5"
_SOURCE_MEDIA_ID = "media-source"
_BASE_URL = "https://dev.example.invalid"

_CHANNELS = [
    {
        "channel_key": "instagram_organic",
        "motion": "organic",
        "headline": "Run every site the same way, finally.",
        "body": "One dashboard, every location.",
        "cta": "See how it works",
        "sources": [{"url": "https://example.com/y", "note": "n"}],
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


def _placement_asset(placement: str, *, media_id: str) -> dict[str, Any]:
    return {
        "id": f"asset-{placement}",
        "campaign_id": _CAMPAIGN,
        "media_kind": "image",
        "placement": placement,
        "media_id": media_id,
        "is_source": False,
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


#: A sentinel distinguishing "use the default campaign" from "explicitly
#: `None`" (a 404 fixture) — `campaign=None` has to be a legal, distinct
#: argument, so the default cannot also be `None`.
_DEFAULT_CAMPAIGN = object()


class _FakeCore:
    """Stands in for `admin_app._core`: campaigns, artefacts and links — the
    same three things `admin_app._latest_artefact`/`mint_links` reach, since
    `pack_routes.py` shares that seam rather than inventing another."""

    def __init__(
        self,
        *,
        campaign: dict[str, Any] | None | object = _DEFAULT_CAMPAIGN,
        copy_artefact: dict[str, Any] | None = None,
        extra_artefacts: list[dict[str, Any]] | None = None,
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
        #: Additional `marketing_artefact` rows of any kind/status, alongside
        #: `copy_artefact` — the divergence test (issue #41) uses this to add
        #: a newer, still-`pending` `copy` row on top of an older `approved`
        #: one, the same way a real re-run would.
        self.extra_artefacts = list(extra_artefacts or [])
        self.links: list[dict[str, Any]] = []
        self._next_link_id = 0

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        request = httpx.Request(method, f"https://core.invalid{path}")
        prefix = admin_app._INTERNAL_PREFIX
        if method == "GET" and path == f"{prefix}/campaigns/{_CAMPAIGN}":
            if self.campaign is None:
                return httpx.Response(404, json={"detail": "not found"}, request=request)
            return httpx.Response(200, json=self.campaign, request=request)
        if method == "GET" and path == f"{prefix}/artefacts":
            params = kw.get("params") or {}
            candidates = ([self.copy_artefact] if self.copy_artefact else []) + self.extra_artefacts
            rows = [a for a in candidates if a.get("kind") == params.get("kind")]
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
    """Stands in for the dual-auth client over this plugin's own
    `marketing_asset` table — the same shape as
    `test_marketing_image_routes._FakeCampaignClient`."""

    def __init__(self, *, assets: list[dict[str, Any]] | None = None) -> None:
        self.assets: list[dict[str, Any]] = list(assets or [])

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"{pack_routes._INTERNAL_PREFIX}/assets":
            campaign_id = (params or {}).get("campaign_id")
            return [a for a in self.assets if a.get("campaign_id") == campaign_id]
        raise AssertionError(f"unexpected GET {path}")


class _FakeStorageClient:
    """Stands in for the SigV4-signed internal client: only the one route
    this file's route now calls — minting a GET url for an existing media id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
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
    app.include_router(pack_routes.router)
    app.dependency_overrides[pack_routes.require_admin] = _admin_user
    app.dependency_overrides[pack_routes.get_core_client] = lambda: core_client
    app.dependency_overrides[pack_routes.get_campaign_client] = lambda: campaign_client
    return app


@pytest.fixture(autouse=True)
def _base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pack_routes, "public_base_url_for", lambda *_: _BASE_URL)


# ── failure paths ────────────────────────────────────────────────────────────


def test_404s_an_unknown_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(campaign=None)
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404


def test_404s_when_no_copy_artefact_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(copy_artefact=None)
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404
    assert "copy" in resp.json()["detail"].lower()


def test_409s_when_copy_is_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate enforcement itself: `proposed` copy must never reach an
    operator's pack, or the copy approval gate is decorative."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS, status="proposed"))
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 409


def test_409s_a_pre_migration_copy_artefact_with_no_channel_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing-dev-data answer (#76 increment 2): a copy artefact
    approved BEFORE the taxonomy migration existed is the old free-text
    shape — `{"channel": ..., "motion": ...}`, no `channel_key` on any
    entry — and never passed through `start_copy_route`'s own guard (it
    predates the code that checks it). Opening its pack must surface a
    clear, actionable 409 naming the real cause, not a bare `KeyError`/500
    out of `_ensure_links` reaching for a field that was never there. This
    is the literal #75 shape: the free-text channel that overflowed
    `marketing_link.channel` was 121 characters, from a campaign whose copy
    would have looked exactly like this."""
    pre_migration_channels = [
        {
            "channel": (
                "Google Search ads (non-brand: terms like 'franchise management "
                "software UK', 'franchise operations pricing per location')"
            ),
            "motion": "paid",
            "headline": "h",
            "body": "b",
            "cta": "c",
            "sources": [{"url": "https://example.com/y", "note": "n"}],
        }
    ]
    core = _FakeCore(copy_artefact=_copy_artefact(pre_migration_channels))
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 409
    assert "channel_key" in resp.json()["detail"]
    assert "re-run" in resp.json()["detail"].lower()


def test_serves_the_approved_pack_even_with_a_newer_pending_copy_re_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The divergence case issue #41 is about — the "pack still serving"
    shape: a founder-facing pack that was serving fine must not start
    404ing/409ing purely because an admin started a copy re-run. The newer
    `pending` row must not hide the older `approved` one that is still
    perfectly good."""
    newer_pending = _copy_artefact(_CHANNELS, status="pending")
    newer_pending = {**newer_pending, "id": "artefact-copy-2", "created_at": "2026-08-10T00:00:99Z"}
    core = _FakeCore(
        copy_artefact=_copy_artefact(_CHANNELS),  # approved, created_at ...01Z
        extra_artefacts=[newer_pending],
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert resp.json()["copy"] == _CHANNELS


def test_assembles_with_no_assets_reporting_every_placement_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #64: a campaign with copy approved but no `marketing_asset` rows
    at all — image generation never run, e.g. #63's missing provider key —
    must still assemble a pack. `_existing_assets` used to require an
    `is_source=True` row and 404 ("No approved source creative for this
    campaign yet.") when none existed, a THIRD 404 site in `get_pack_route`
    the issue's own two-site read of the function missed: it fires after the
    copy-approval gate has already passed, so a fully-approved campaign with
    no image generated still 404s, contradicting the module docstring's own
    "missing_placements... rather than silently pretending they exist"."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient(assets=[]))
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    body = resp.json()
    assert body["assets"] == []
    assert set(body["missing_placements"]) == set(PLACEMENTS)
    assert body["copy"] == _CHANNELS


def test_422s_when_the_campaign_has_no_destination_for_a_channel_that_needs_minting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _FakeCore(
        campaign={"id": _CAMPAIGN, "destination_url": None, "guidance": None},
        copy_artefact=_copy_artefact(_CHANNELS),
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 422
    assert "destination_url" in resp.json()["detail"]


# ── the happy path: assemble from what already exists ───────────────────────


def test_assembles_the_pack_from_the_source_asset_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's honest baseline: only the source creative exists (nothing in
    this plugin writes a placement row yet — see the module docstring), so
    every `PLACEMENTS` entry is reported as missing rather than silently
    absent."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    storage = _FakeStorageClient()
    client = TestClient(_app(core_client=storage, campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    body = resp.json()

    # 1. Assets: only the source exists today, carrying a URL.
    assert len(body["assets"]) == 1
    assert body["assets"][0]["is_source"] is True
    assert body["assets"][0]["url"].startswith("https://bucket.s3.eu-west-1.amazonaws.com/")

    # The gap is visible, not silent.
    assert set(body["missing_placements"]) == set(PLACEMENTS)

    # 2. Copy: the approved artefact's channels, verbatim.
    assert body["copy"] == _CHANNELS

    # 3. Links: one minted per channel in the copy, using the real UTM
    # composition (links.py), never a hand-built URL.
    assert len(body["links"]) == 1
    link = body["links"][0]
    assert link["channel"] == "instagram_organic"
    assert link["url"].startswith(f"{_BASE_URL}/c/")
    assert core.links[0]["destination_url"].startswith("https://example.com/landing?")
    assert "utm_campaign=" + _CAMPAIGN in core.links[0]["destination_url"]

    # 4. Guidance: the campaign's own column, verbatim.
    assert body["guidance"] == "Disclose #ad. Licensed music only."

    # No render/upload traffic — only GET .../url calls for existing assets.
    assert all(call[0] == "GET" and call[1].endswith("/url") for call in storage.calls)


def test_includes_placement_assets_that_already_exist_and_reports_no_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once something (a future generation-time change) writes placement
    rows, this route must pick them up as ordinary assets rather than
    ignoring them — `missing_placements` should then be empty."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    assets = [_source_asset()] + [_placement_asset(p, media_id=f"media-{p}") for p in PLACEMENTS]
    campaign_client = _FakeCampaignClient(assets=assets)
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    body = resp.json()
    placements_present = {a["placement"] for a in body["assets"] if a["placement"]}
    assert placements_present == set(PLACEMENTS)
    assert body["missing_placements"] == []


def test_does_not_remint_a_link_for_a_channel_that_already_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    core.links.append(
        {
            "id": "link-existing",
            "campaign_id": _CAMPAIGN,
            "token": "existing-token",
            "channel": "instagram_organic",
            "variant": None,
            "is_paid": False,
            "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
        }
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert len(resp.json()["links"]) == 1
    assert resp.json()["links"][0]["url"] == f"{_BASE_URL}/c/existing-token"
    # No new link written.
    assert core.links == [core.links[0]]


def test_link_urls_come_from_the_request_origin_not_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real derivation, with nothing patched over it.

    Every other test here patches `public_base_url_for`, so none of them can
    tell whether the route calls it at all. That is exactly how #72 shipped:
    `_ensure_links` still called the **config-only** `public_base_url()`, which
    returns nothing on the shared plugin host where this app actually runs, and
    the deployed route 503'd ("No public base URL is configured for this
    deployment") while every test here stayed green.

    So this one deliberately does **not** use the `_base_url` fixture's patch —
    it sends a real `Origin` header and asserts the minted URL was built from
    it. It fails if anyone reverts a call site to the configured value, because
    no configuration is set in this process.
    """
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    core.links.append(
        {
            "id": "link-existing",
            "campaign_id": _CAMPAIGN,
            "token": "existing-token",
            "channel": "instagram_organic",
            "variant": None,
            "is_paid": False,
            "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
        }
    )
    monkeypatch.setattr(admin_app, "_core", core)
    # Undo the autouse fixture: this test is about the un-patched path.
    monkeypatch.setattr(pack_routes, "public_base_url_for", config.public_base_url_for)
    # And make sure a configured value cannot rescue it if the origin is ignored.
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)

    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient())
    )

    resp = client.get(
        f"/campaigns/{_CAMPAIGN}/pack",
        headers={"origin": "https://tenant.example.test"},
    )

    assert resp.status_code == 200
    assert resp.json()["links"][0]["url"] == "https://tenant.example.test/c/existing-token"


# ── issue #48: leaked links, motion-blind dedup ──────────────────────────────


def test_does_not_leak_stale_channel_links_and_mints_paid_when_only_organic_shares_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #48, both halves, against the real taxonomy shape rather than a
    hand-rolled short channel name.

    `existing` can hold more than the current copy's channels ever asked
    about — channel planning gets re-run, and old links are never deleted —
    so a stale `search_organic` link (from an earlier plan that no longer
    survives into this approved copy) must not leak into a pack that no
    longer requests it. And the seeded taxonomy gives an organic and a paid
    version of "the same" channel distinct `channel_key`s (#76), but nothing
    in this plugin enforces that a tenant's own custom channel keeps a key
    motion-exclusive — so an existing ORGANIC link for `event_sponsorship`
    must not suppress minting the PAID one this copy actually asks for.
    """
    awkward_channels = [
        {
            "channel_key": "event_sponsorship",
            "motion": "paid",
            "headline": "Meet us on the show floor this quarter",
            "body": "Live demos, no queue, real answers from the people who built it.",
            "cta": "Book a slot",
            "sources": [{"url": "https://example.com/e", "note": "n"}],
        }
    ]
    core = _FakeCore(copy_artefact=_copy_artefact(awkward_channels))
    core.links.extend(
        [
            {
                "id": "link-stale-unrelated-channel",
                "campaign_id": _CAMPAIGN,
                "token": "stale-token",
                "channel": "search_organic",
                "variant": None,
                "is_paid": False,
                "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
            },
            {
                "id": "link-same-key-organic-motion",
                "campaign_id": _CAMPAIGN,
                "token": "organic-token",
                "channel": "event_sponsorship",
                "variant": None,
                "is_paid": False,
                "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
            },
        ]
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    links = resp.json()["links"]

    # Neither the unrelated stale link nor the same-key organic one leaked;
    # exactly the requested paid channel comes back.
    assert len(links) == 1
    assert links[0]["channel"] == "event_sponsorship"
    assert links[0]["is_paid"] is True
    assert links[0]["url"] != f"{_BASE_URL}/c/organic-token"

    # The dedup did not skip minting because an organic link already "had"
    # the channel_key — a new paid link was actually written.
    assert core.links[-1]["channel"] == "event_sponsorship"
    assert core.links[-1]["is_paid"] is True


# ── issue #54: source creative selection ─────────────────────────────────────


def test_picks_the_newest_source_deterministically_when_more_than_one_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #54: `image_routes.generate_still_route` writes a new
    `is_source=True` row on every call and nothing supersedes the one it
    replaces, so more than one can exist for a campaign — the admin UI's
    `ImageGenerator` makes "generate again" the obvious, one-click thing to
    do. The pack must return the same single source on every request, not
    whichever row Core happens to list first: newest `created_at` wins (the
    most recent generation is what an operator who just re-generated means
    by "the" creative — the UI already renders it first for the same
    reason), and the superseded row must not still appear labelled "Source"
    alongside it.

    Fed in both list orders so the assertion cannot pass by accident of
    Core's own ordering — the whole point is that it must not matter.
    """
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    older = {
        **_source_asset(),
        "id": "asset-source-older",
        "media_id": "media-older",
        "created_at": "2026-08-01T00:00:00Z",
    }
    newer = {
        **_source_asset(),
        "id": "asset-source-newer",
        "media_id": "media-newer",
        "created_at": "2026-08-05T00:00:00Z",
    }

    for ordered_assets in ([older, newer], [newer, older]):
        campaign_client = _FakeCampaignClient(assets=list(ordered_assets))
        client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

        resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["assets"]) == 1
        assert body["assets"][0]["is_source"] is True
        assert body["assets"][0]["media_id"] == "media-newer"

        # The drop is disclosed, not silent — same discipline as
        # `missing_placements`: an operator or future maintainer reading
        # only the response still learns a source was superseded.
        assert body["superseded_source_count"] == 1


def test_reports_zero_superseded_sources_when_at_most_one_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disclosure field itself must not read as "something was hidden"
    when nothing was — a bare source-only pack (the common case today) must
    report 0, not omit the field or report a stale count."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert resp.json()["superseded_source_count"] == 0


def test_does_not_remint_or_drop_a_link_with_null_is_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    """`marketing_link.is_paid` is nullable, and generic CRUD's own `create`
    is exposed to any admin — not only `_ensure_links`'s own mint loop,
    which always writes a concrete bool — so a `NULL` row is a real
    possibility. `results_routes.py` already refuses to fold that NULL into
    `organic`/`False` for this same column; `_ensure_links` must not either,
    or an ambiguous existing link both gets duplicate-minted (it satisfies
    neither "already have paid" nor "already have organic") and vanishes
    from the pack's own `links` (it matches neither filter). A NULL row
    must be treated as already covering whichever motion is actually asked
    for."""
    awkward_channels = [
        {
            "channel_key": "event_sponsorship",
            "motion": "paid",
            "headline": "Meet us on the show floor this quarter",
            "body": "Live demos, no queue, real answers from the people who built it.",
            "cta": "Book a slot",
            "sources": [{"url": "https://example.com/e", "note": "n"}],
        }
    ]
    core = _FakeCore(copy_artefact=_copy_artefact(awkward_channels))
    core.links.append(
        {
            "id": "link-null-is-paid",
            "campaign_id": _CAMPAIGN,
            "token": "null-is-paid-token",
            "channel": "event_sponsorship",
            "variant": None,
            "is_paid": None,
            "destination_url": "https://example.com/landing?utm_campaign=" + _CAMPAIGN,
        }
    )
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(_app(core_client=_FakeStorageClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    links = resp.json()["links"]

    # The existing NULL-is_paid link is treated as already covering the
    # paid request: no second link minted, and the existing one still shows.
    assert len(links) == 1
    assert links[0]["url"] == f"{_BASE_URL}/c/null-is-paid-token"
    assert core.links == [core.links[0]]
