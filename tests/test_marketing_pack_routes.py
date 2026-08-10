"""The distribution pack (M5, issue #4), over fakes for the internal (SigV4)
storage client, the dual-auth asset client, and `admin_app._core` — plus a
patched `httpx.AsyncClient` for the two raw S3-facing calls this route makes
directly (download the source creative, upload each placement render).

Fakes rather than mocks, matching this repo's convention
(`test_marketing_mint_route.py`, `test_marketing_image_routes.py`,
`test_marketing_pipeline_routes.py`): what is worth asserting is what got
written to `marketing_asset` / `marketing_link` and what the pack response
actually assembles, and a fake that records both says that directly.

A standalone app with only `pack_routes.router` — not `admin_app.build_app()`
— for the same reason `test_marketing_image_routes.py` gives: these tests
should not depend on whatever else lands in `admin_app.py` concurrently.
`admin_app._core` is still reached through (patched via monkeypatch), since
`pack_routes.py` calls it for the campaign/artefact/link lookups it shares
with every other route module in this plugin.
"""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from marketing import admin_app, pack_routes
from marketing.definitions import PLACEMENTS

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000c5"
_SOURCE_MEDIA_ID = "media-source"
_DOWNLOAD_URL = "https://bucket.s3.eu-west-1.amazonaws.com/signed-get-source"
_PRESIGN_URL = "https://s3.example.invalid/upload"
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


def _source_png_bytes() -> bytes:
    """A landscape image (1000x500, 2:1) that matches none of `PLACEMENTS`'
    target ratios exactly, so every placement forces a real crop rather than
    the deterministic no-op path — what is under test here is "was a render
    actually produced and stored", not the crop math itself (that is
    render.py's own suite, `test_marketing_render.py`)."""
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 500), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


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
        self._next_asset_id = 0

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"{pack_routes._INTERNAL_PREFIX}/assets":
            campaign_id = (params or {}).get("campaign_id")
            return [a for a in self.assets if a.get("campaign_id") == campaign_id]
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        if path == f"{pack_routes._INTERNAL_PREFIX}/assets":
            self._next_asset_id += 1
            row = {**(json or {}), "id": f"asset-new-{self._next_asset_id}"}
            self.assets.append(row)
            return row
        raise AssertionError(f"unexpected POST {path}")


class _FakeStorageClient:
    """Stands in for the SigV4-signed internal client: storage
    presign/confirm/url, the same three routes `image_routes` uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._next_media = 0

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path == f"{pack_routes._STORAGE_PATH}/{_SOURCE_MEDIA_ID}/url":
            return {"url": _DOWNLOAD_URL, "expires_in": 300}
        prefix = f"{pack_routes._STORAGE_PATH}/"
        if path.startswith(prefix) and path.endswith("/url"):
            media_id = path[len(prefix) : -len("/url")]
            return {
                "url": f"https://bucket.s3.eu-west-1.amazonaws.com/signed-get-{media_id}",
                "expires_in": 300,
            }
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        self.calls.append(("POST", path, json))
        if path == f"{pack_routes._STORAGE_PATH}/presign":
            self._next_media += 1
            key = f"plugins/marketing/default/render-{self._next_media}.png"
            return {
                "key": key,
                "url": _PRESIGN_URL,
                "fields": {"key": key},
                "max_bytes": 10_000_000,
                "expires_in": 900,
            }
        if path == f"{pack_routes._STORAGE_PATH}/confirm":
            return {
                "id": f"media-render-{self._next_media}",
                "owner_plugin": "system:marketing",
                "storage_key": (json or {})["key"],
                "filename": "render.png",
                "mime_type": "image/png",
                "size_bytes": 123,
            }
        raise AssertionError(f"unexpected POST {path}")


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def _app(*, core_client: _FakeStorageClient, campaign_client: _FakeCampaignClient) -> FastAPI:
    app = FastAPI()
    app.include_router(pack_routes.router)
    app.dependency_overrides[pack_routes.require_admin] = _admin_user
    app.dependency_overrides[pack_routes.get_core_client] = lambda: core_client
    app.dependency_overrides[pack_routes.get_campaign_client] = lambda: campaign_client
    return app


@pytest.fixture
def _s3(monkeypatch: pytest.MonkeyPatch):
    """Patches `httpx.AsyncClient` so the raw download (GET the presigned
    URL) and the raw upload (POST the presigned form) land on an in-memory
    transport, matching `test_marketing_image_routes._s3_upload_ok`'s own
    approach for the upload half."""
    calls: list[httpx.Request] = []
    source_bytes = _source_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=source_bytes)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
    return calls


@pytest.fixture(autouse=True)
def _base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pack_routes, "public_base_url", lambda: _BASE_URL)


# ── failure paths that need no S3 traffic at all ────────────────────────────


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


# ── SSRF guard on the download URL (CodeQL: full server-side request forgery) ──


def test_validate_download_url_accepts_a_real_s3_host() -> None:
    url = "https://bucket.s3.eu-west-1.amazonaws.com/key"
    assert pack_routes._validate_download_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://bucket.s3.eu-west-1.amazonaws.com/key",  # not https
        "https://evil.example.com/key",  # not an S3 host at all
        "https://amazonaws.com.evil.example.com/key",  # suffix-match trick
    ],
)
def test_validate_download_url_rejects_anything_else(url: str) -> None:
    with pytest.raises(HTTPException) as exc:
        pack_routes._validate_download_url(url)
    assert exc.value.status_code == 502


def test_502s_when_core_returns_a_download_url_for_an_unexpected_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route-level proof, not just the unit check: a storage response
    naming a URL outside the S3 host family must never reach `httpx.get` —
    this is what CodeQL's "full server-side request forgery" finding on
    `_download` actually asks for."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)

    class _MaliciousStorageClient(_FakeStorageClient):
        async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
            if path == f"{pack_routes._STORAGE_PATH}/{_SOURCE_MEDIA_ID}/url":
                return {"url": "https://internal.example.invalid/steal", "expires_in": 300}
            return await super().get(path, params)

    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    client = TestClient(
        _app(core_client=_MaliciousStorageClient(), campaign_client=campaign_client)
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 502


# ── the happy path: render, upload, mint, assemble ──────────────────────────


def test_renders_every_placement_and_returns_the_whole_pack(
    _s3: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    campaign_client = _FakeCampaignClient(assets=[_source_asset()])
    storage = _FakeStorageClient()
    client = TestClient(_app(core_client=storage, campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    body = resp.json()

    # 1. Assets: the source plus every placement, each carrying a URL.
    placements_present = {a["placement"] for a in body["assets"] if a["placement"]}
    assert placements_present == set(PLACEMENTS)
    assert any(a["is_source"] for a in body["assets"])
    assert all(a["url"] for a in body["assets"])

    # A new marketing_asset row exists for each placement, is_source=False.
    new_placement_rows = [a for a in campaign_client.assets if a.get("placement")]
    assert {a["placement"] for a in new_placement_rows} == set(PLACEMENTS)
    assert all(a["is_source"] is False for a in new_placement_rows)

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

    # The source was downloaded exactly once and reused for all three crops.
    downloads = [r for r in _s3 if r.method == "GET"]
    assert len(downloads) == 1
    uploads = [r for r in _s3 if r.method == "POST"]
    assert len(uploads) == 3


def test_does_not_rerender_placements_that_already_exist(
    _s3: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency, and the whole point of "byte-identical on repeat" at the
    pack level: a placement rendered once must not be rendered — or
    downloaded, or re-uploaded — again on a later pack request."""
    core = _FakeCore(copy_artefact=_copy_artefact(_CHANNELS))
    monkeypatch.setattr(admin_app, "_core", core)
    assets = [_source_asset()] + [_placement_asset(p, media_id=f"media-{p}") for p in PLACEMENTS]
    campaign_client = _FakeCampaignClient(assets=assets)
    storage = _FakeStorageClient()
    client = TestClient(_app(core_client=storage, campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    # No new asset rows — every placement was already there.
    assert len(campaign_client.assets) == len(assets)
    # No download, no upload — nothing needed re-rendering.
    assert _s3 == []
    # Storage was still asked for a GET url per asset (to build the pack's
    # own URLs), never for presign/confirm.
    assert all(call[1].endswith("/url") for call in storage.calls)


def test_does_not_remint_a_link_for_a_channel_that_already_has_one(
    _s3: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
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


def test_422s_when_the_campaign_has_no_destination_for_a_channel_that_needs_minting(
    _s3: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
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
