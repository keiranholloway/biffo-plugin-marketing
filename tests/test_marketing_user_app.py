"""The unit (franchise) surface (ADR-0021 `user_ingress`, `user_app.py`).

Fakes rather than mocks, matching this repo's convention
(`test_marketing_image_routes.py`, `test_marketing_mint_route.py`): what is
worth asserting is what got requested and what came back, and a fake that
records both says that directly.

Two fakes stand in for the two client shapes `user_app.py` depends on,
matching `test_marketing_image_routes.py`'s own split:

- `_FakeCoreClient` — the SigV4-only, plugin-identity client
  (`get_core_client`) used for the `me` storage `/url` route.
- `_FakeCampaignClient` — the dual-auth client (`get_campaign_client`) over
  this plugin's own generated-CRUD tables, carrying the founder's own
  forwarded token.

`admin_app._core`/`admin_app._latest_artefact` are exercised for real (not
faked) via `monkeypatch.setattr(admin_app, "CORE_API_URL", ...)` plus a faked
`principal_client.SignedCoreClient` — mirrors
`test_marketing_dual_auth_wiring.py`'s own approach for the same reason: this
file reuses those two helpers directly (see `user_app.py`'s module docstring
for why that reuse is safe and established), so a fake at THAT seam is what
proves the real function bodies ran, not a mock of them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing import admin_app, principal_client, user_app

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000a1"
_CORE_API_URL = "https://core.invalid"
_BASE_URL = "https://dev.tabsii.com"


def _founder_user() -> Any:
    return type("U", (), {"sub": "unit-1", "groups": ["founder"], "token": "founder-jwt"})()


class _FakeCoreClient:
    """Stands in for `get_core_client()` — the SigV4-only storage-URL client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        if path.endswith("/url"):
            media_id = path.split("/")[-2]
            return {"url": f"https://s3.example.invalid/{media_id}"}
        raise AssertionError(f"unexpected GET {path}")


class _FakeCampaignClient:
    """Stands in for `get_campaign_client()` — the dual-auth
    `PrincipalCoreClient` over this plugin's own generated-CRUD tables."""

    def __init__(
        self,
        *,
        assets: list[dict[str, Any]] | None = None,
        links: list[dict[str, Any]] | None = None,
    ) -> None:
        self.assets = assets or []
        self.links = links or []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        if path == f"{user_app._INTERNAL_PREFIX}/assets":
            return self.assets
        if path == f"{user_app._INTERNAL_PREFIX}/links":
            return self.links
        raise AssertionError(f"unexpected GET {path}")


def _app(*, core_client: Any, campaign_client: _FakeCampaignClient) -> FastAPI:
    app = FastAPI()
    app.include_router(user_app.router)
    app.dependency_overrides[user_app.require_founder] = _founder_user
    app.dependency_overrides[user_app.get_core_client] = lambda: core_client
    app.dependency_overrides[user_app.get_campaign_client] = lambda: campaign_client
    return app


class _FakeSignedCoreClient:
    """The lowest seam `admin_app._core` reaches Core through — see
    `test_marketing_dual_auth_wiring.py`'s own docstring for why this is
    faked at `principal_client.SignedCoreClient` rather than mocking
    `admin_app._core` itself: it proves `_core`'s and `_latest_artefact`'s
    real bodies ran with the founder's own token, not a stand-in for them."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def raw_request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        extra_signed_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str]:
        self.calls.append(
            {"method": method, "path": path, "extra_signed_headers": extra_signed_headers}
        )
        for prefix, (status_code, body) in self._responses.items():
            if path.startswith(prefix):
                return status_code, body, "application/json"
        raise AssertionError(f"unexpected {method} {path}")

    async def aclose(self) -> None:
        return None


def _campaign_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": _CAMPAIGN,
        "name": "Summer offers",
        "status": "ready",
        "guidance": "Disclose the promotional partnership.",
        "starts_at": None,
        "ends_at": None,
    }
    row.update(overrides)
    return row


def _approved_copy_artefact(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "artefact-1",
        "campaign_id": _CAMPAIGN,
        "kind": "copy",
        "status": "approved",
        "body": json.dumps({"channels": [{"channel": "instagram", "text": "Great offer!"}]}),
    }
    row.update(overrides)
    return row


@pytest.fixture
def fake_signed_client(monkeypatch: pytest.MonkeyPatch):
    def _install(responses: dict[str, tuple[int, bytes]]) -> _FakeSignedCoreClient:
        client = _FakeSignedCoreClient(responses)
        monkeypatch.setattr(principal_client, "SignedCoreClient", lambda **kw: client)
        monkeypatch.setattr(admin_app, "CORE_API_URL", _CORE_API_URL)
        return client

    return _install


@pytest.fixture
def configured_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches `test_marketing_dual_auth_wiring.py`'s own approach exactly
    (`monkeypatch.setattr(admin_app, "public_base_url", lambda: _BASE_URL)`):
    `user_app.py` imports `public_base_url` by name from `config`, so
    patching `user_app`'s own reference to it is isolated from `config`'s
    process-lifetime cache — no other test's cached value can leak in, and
    this test cannot leak one out either."""
    monkeypatch.setattr(user_app, "public_base_url", lambda: _BASE_URL)


# ── /campaigns ────────────────────────────────────────────────────────────


def test_lists_only_ready_and_live_campaigns() -> None:
    campaign_client = _FakeCampaignClient()
    campaign_client.get = _paged_campaigns(  # type: ignore[method-assign]
        [
            _campaign_row(id="c-draft", status="draft"),
            _campaign_row(id="c-ready", status="ready"),
            _campaign_row(id="c-live", status="live"),
            _campaign_row(id="c-archived", status="archived"),
        ]
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=campaign_client))

    resp = client.get("/campaigns")

    assert resp.status_code == 200
    ids = {c["id"] for c in resp.json()}
    assert ids == {"c-ready", "c-live"}


def _paged_campaigns(rows: list[dict[str, Any]]):
    """A `.get` replacement that answers the `/campaigns` list path with
    `rows` and 404s anything else — the minimal shape `_list_all`'s single
    page needs (fewer than `_LIST_PAGE_SIZE` rows, so it returns after one
    call)."""

    async def get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"{user_app._INTERNAL_PREFIX}/campaigns":
            return rows if (params or {}).get("offset", 0) == 0 else []
        raise AssertionError(f"unexpected GET {path}")

    return get


def test_campaign_summary_omits_internal_fields() -> None:
    """Only id/name/status/starts_at/ends_at travel to a unit — not `brief`
    or `destination_url`, which are internal working fields."""
    internal_row = _campaign_row(
        brief="what the research agent should look for",
        destination_url="https://tabsii.com/apply",
    )
    campaign_client = _FakeCampaignClient()
    campaign_client.get = _paged_campaigns([internal_row])  # type: ignore[method-assign]
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=campaign_client))

    resp = client.get("/campaigns")

    assert resp.status_code == 200
    body = resp.json()[0]
    assert set(body) == {"id", "name", "status", "starts_at", "ends_at"}


# ── /campaigns/{id}/pack ─────────────────────────────────────────────────


def test_pack_assembles_copy_assets_and_links(fake_signed_client, configured_base_url) -> None:
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps([_approved_copy_artefact()]).encode(),
            ),
        }
    )
    core_client = _FakeCoreClient()
    campaign_client = _FakeCampaignClient(
        assets=[
            {
                "id": "asset-1",
                "campaign_id": _CAMPAIGN,
                "media_kind": "image",
                "placement": None,
                "media_id": "media-1",
                "is_source": True,
            }
        ],
        links=[
            {
                "id": "link-1",
                "campaign_id": _CAMPAIGN,
                "channel": "instagram",
                "variant": None,
                "is_paid": False,
                "token": "tok-abc123",
            }
        ],
    )
    client = TestClient(_app(core_client=core_client, campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_id"] == _CAMPAIGN
    assert body["campaign_name"] == "Summer offers"
    assert body["guidance"] == "Disclose the promotional partnership."
    assert body["copy"] == [{"channel": "instagram", "text": "Great offer!"}]
    assert body["assets"] == [
        {
            "id": "asset-1",
            "campaign_id": _CAMPAIGN,
            "media_kind": "image",
            "placement": None,
            "media_id": "media-1",
            "is_source": True,
            "url": "https://s3.example.invalid/media-1",
        }
    ]
    assert body["links"] == [
        {
            "channel": "instagram",
            "variant": None,
            "is_paid": False,
            "url": f"{_BASE_URL}/c/tok-abc123",
        }
    ]


def test_pack_survives_a_storage_response_with_no_url_key(fake_signed_client) -> None:
    """A 200 from storage with a body shaped differently than expected (no
    `url` key) must not 500 the whole pack — same resilience as the missing-
    `media_id` case, not just a docstring claim about it."""
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps([_approved_copy_artefact()]).encode(),
            ),
        }
    )

    class _MalformedStorageResponse:
        async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
            return {}  # no "url" key

    campaign_client = _FakeCampaignClient(
        assets=[
            {
                "id": "asset-1",
                "campaign_id": _CAMPAIGN,
                "media_kind": "image",
                "placement": None,
                "media_id": "media-1",
                "is_source": True,
            }
        ]
    )
    client = TestClient(
        _app(core_client=_MalformedStorageResponse(), campaign_client=campaign_client)
    )

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert resp.json()["assets"] == [
        {
            "id": "asset-1",
            "campaign_id": _CAMPAIGN,
            "media_kind": "image",
            "placement": None,
            "media_id": "media-1",
            "is_source": True,
            "url": None,
        }
    ]


def test_pack_404s_a_campaign_that_is_not_ready_or_live(fake_signed_client) -> None:
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row(status="draft")).encode(),
            ),
        }
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404


def test_pack_404s_an_unknown_campaign(fake_signed_client) -> None:
    fake_signed_client(
        {f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (404, b'{"detail":"not found"}')}
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404


def test_pack_404s_a_malformed_campaign_id() -> None:
    """The SSRF guard (`admin_app._validated_campaign_id`, reused here): not
    a UUID must 404 before any Core call is attempted."""
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    resp = client.get("/campaigns/not-a-uuid-at-all/pack")

    assert resp.status_code == 404


def test_pack_404s_when_copy_is_not_yet_approved(fake_signed_client) -> None:
    """A `proposed` copy artefact must not reach a unit — collapsed to the
    same 404 as "no copy yet", never a 409 (see `get_pack_route`'s own
    docstring for why: a founder cannot act on that distinction)."""
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps([_approved_copy_artefact(status="proposed")]).encode(),
            ),
        }
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 404


def test_pack_serves_approved_copy_even_with_a_newer_pending_re_run(fake_signed_client) -> None:
    """The divergence case issue #41 is about — the "pack still serving"
    shape, this surface's half: a founder-facing pack that was serving fine
    must not start 404ing purely because an admin started a copy re-run. The
    newer `pending` row (later `created_at`, no `created_at` column needed
    here since both rows are returned together) must not hide the older
    `approved` one that is still perfectly good."""
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps(
                    [
                        _approved_copy_artefact(
                            id="artefact-1",
                            status="approved",
                            created_at="2026-08-10T00:00:01Z",
                        ),
                        _approved_copy_artefact(
                            id="artefact-2",
                            status="pending",
                            body=json.dumps({"channels": []}),
                            created_at="2026-08-10T00:00:99Z",
                        ),
                    ]
                ).encode(),
            ),
        }
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert resp.json()["copy"] == [{"channel": "instagram", "text": "Great offer!"}]


def test_pack_forwards_the_founders_own_token(fake_signed_client) -> None:
    """`admin_app._core`/`_latest_artefact`, reused here, must carry THIS
    founder's token — not a hardcoded or admin one — through to Core."""
    fake = fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps([_approved_copy_artefact()]).encode(),
            ),
        }
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()))

    client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert fake.calls, "no Core calls were made"
    for call in fake.calls:
        assert call["extra_signed_headers"] == {
            principal_client.FORWARDED_USER_HEADER: "founder-jwt"
        }


def test_pack_omits_link_urls_when_no_public_base_url_is_configured(
    fake_signed_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link with no base URL configured still appears — with `url: null` —
    rather than the whole pack failing, since assets/copy may still be
    useful with no base URL wired up yet."""
    monkeypatch.setattr(user_app, "public_base_url", lambda: "")
    fake_signed_client(
        {
            f"{user_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}": (
                200,
                json.dumps(_campaign_row()).encode(),
            ),
            f"{user_app._INTERNAL_PREFIX}/artefacts": (
                200,
                json.dumps([_approved_copy_artefact()]).encode(),
            ),
        }
    )
    campaign_client = _FakeCampaignClient(
        links=[
            {
                "id": "link-1",
                "campaign_id": _CAMPAIGN,
                "channel": "instagram",
                "variant": None,
                "is_paid": False,
                "token": "tok-abc123",
            }
        ]
    )
    client = TestClient(_app(core_client=_FakeCoreClient(), campaign_client=campaign_client))

    resp = client.get(f"/campaigns/{_CAMPAIGN}/pack")

    assert resp.status_code == 200
    assert resp.json()["links"] == [
        {"channel": "instagram", "variant": None, "is_paid": False, "url": None}
    ]


def test_the_manifest_app_path_resolves() -> None:
    """`user_ingress.app` is "marketing.user_app:app" — assert that resolves,
    same discipline as `test_marketing_admin_app.test_the_manifest_app_path_resolves`."""
    module_name, _, attr = "marketing.user_app:app".partition(":")
    module = __import__(module_name, fromlist=[attr])
    assert getattr(module, attr) is not None
