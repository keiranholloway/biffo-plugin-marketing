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

from marketing import admin_app, pack_routes
from marketing.definitions import PLACEMENTS

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000c5"
_SOURCE_MEDIA_ID = "media-source"
_BASE_URL = "https://dev.example.invalid"

_CHANNELS = [
    {
        "channel": "Instagram Reels",
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
            is_copy = params.get("kind") == "copy" and self.copy_artefact
            rows = [self.copy_artefact] if is_copy else []
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
    monkeypatch.setattr(pack_routes, "public_base_url", lambda: _BASE_URL)


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


def test_404s_when_no_source_creative_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    client = TestClient(
        _app(core_client=_FakeStorageClient(), campaign_client=_FakeCampaignClient(assets=[]))
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404
    assert "source creative" in resp.json()["detail"].lower()


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
    assert link["channel"] == "Instagram Reels"
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
            "channel": "Instagram Reels",
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
