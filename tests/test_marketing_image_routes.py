"""The still-image generation route (M6, issue #5), over fakes for the
provider, the internal (SigV4) Core client, and the bearer campaign client —
plus a patched `httpx.AsyncClient` for the one raw upload POST this route
makes directly to object storage.

Fakes rather than mocks, matching this repo's convention
(`test_marketing_mint_route.py`, `test_marketing_pipeline_routes.py`): what is
worth asserting is what got written to the ledger and to `marketing_asset`,
and a fake that records both says that directly.

A standalone app with only `image_routes.router` — not `admin_app.build_app()`
— so these tests do not depend on whatever else lands in `admin_app.py`
concurrently, and so overriding `image_routes.require_admin` cannot be
confused with `admin_app.require_admin` (two different dependency callables,
per the module's own docstring on why routes live here rather than there).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from biffo_plugin_sdk import BiffoAPIError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketing import image_routes
from marketing.image_provider import GeneratedImage, ImageProviderError

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ef"
_MEDIA_ID = "media-1"
_LEDGER_ID = "ledger-1"
_PRESIGN_URL = "https://s3.example.invalid/upload"


class _FakeImageProvider:
    """Proves issue #5's requirement 3: this is a second `ImageProvider`
    implementation, and the route needed no change to accept it."""

    def __init__(self, *, cost_usd: float | None = None, content: bytes = b"fake-bytes") -> None:
        self.cost_usd = cost_usd
        self.content = content
        self.prompts: list[str] = []

    async def generate_still(self, *, prompt: str) -> GeneratedImage:
        self.prompts.append(prompt)
        return GeneratedImage(
            content=self.content,
            content_type="image/png",
            filename="fake.png",
            provider="fake-provider",
            model="fake-model-1",
            units=1.0,
            unit_kind="image",
            cost_usd=self.cost_usd,
        )


class _FailingImageProvider:
    async def generate_still(self, *, prompt: str) -> GeneratedImage:
        raise ImageProviderError("the fake provider always fails")


class _FakeCoreClient:
    """Stands in for the SigV4-signed internal client: storage presign/
    confirm/url and the media-generations ledger."""

    def __init__(
        self,
        *,
        max_bytes: Any = 10_000_000,
        presign_status: int | None = None,
        ledger_status: int | None = None,
        ledger_missing_id: bool = False,
        confirm_missing_id: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._max_bytes = max_bytes
        self._presign_status = presign_status
        self._ledger_status = ledger_status
        self._ledger_missing_id = ledger_missing_id
        self._confirm_missing_id = confirm_missing_id

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        self.calls.append(("POST", path, json))
        if path == image_routes._LEDGER_PATH:
            if self._ledger_status is not None:
                raise BiffoAPIError(self._ledger_status, "ledger unavailable")
            if self._ledger_missing_id:
                return {"created_at": "2026-08-10T00:00:00Z"}
            return {"id": _LEDGER_ID, "created_at": "2026-08-10T00:00:00Z"}
        if path == f"{image_routes._STORAGE_PATH}/presign":
            if self._presign_status is not None:
                raise BiffoAPIError(self._presign_status, "storage unavailable")
            return {
                "key": "plugins/marketing/default/generated.png",
                "url": _PRESIGN_URL,
                "fields": {"key": "plugins/marketing/default/generated.png"},
                "max_bytes": self._max_bytes,
                "expires_in": 900,
            }
        if path == f"{image_routes._STORAGE_PATH}/confirm":
            if self._confirm_missing_id:
                return {
                    "owner_plugin": "system:marketing",
                    "storage_key": (json or {})["key"],
                    "filename": "generated.png",
                    "mime_type": "image/png",
                    "size_bytes": len(b"fake-bytes"),
                }
            return {
                "id": _MEDIA_ID,
                "owner_plugin": "system:marketing",
                "storage_key": (json or {})["key"],
                "filename": "generated.png",
                "mime_type": "image/png",
                "size_bytes": len(b"fake-bytes"),
            }
        raise AssertionError(f"unexpected POST {path}")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path == f"{image_routes._STORAGE_PATH}/{_MEDIA_ID}/url":
            return {"url": "https://s3.example.invalid/signed-get", "expires_in": 300}
        raise AssertionError(f"unexpected GET {path}")


class _FakeCampaignClient:
    """Stands in for the dual-auth (SigV4 + forwarded user token) client over
    the plugin's own generated-CRUD tables (`marketing_campaign`,
    `marketing_asset`) — Core's internal, per-plugin mount, per issue #27."""

    def __init__(
        self,
        *,
        exists: bool = True,
        fail_asset_creation: bool = False,
        asset_missing_id: bool = False,
    ) -> None:
        self.exists = exists
        self.fail_asset_creation = fail_asset_creation
        self.asset_missing_id = asset_missing_id
        self.created_assets: list[dict[str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"{image_routes._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}":
            if not self.exists:
                raise BiffoAPIError(404, "not found")
            return {"id": _CAMPAIGN}
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        if path == f"{image_routes._INTERNAL_PREFIX}/assets":
            if self.fail_asset_creation:
                raise BiffoAPIError(502, "asset service unavailable")
            row = {**(json or {})}
            if not self.asset_missing_id:
                row["id"] = f"asset-{len(self.created_assets) + 1}"
            self.created_assets.append(row)
            return row
        raise AssertionError(f"unexpected POST {path}")


def _admin_user() -> Any:
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def _app(
    *,
    provider,
    core_client: _FakeCoreClient,
    campaign_client: _FakeCampaignClient,
) -> FastAPI:
    app = FastAPI()
    app.include_router(image_routes.router)
    app.dependency_overrides[image_routes.require_admin] = _admin_user
    app.dependency_overrides[image_routes.get_image_provider] = lambda: provider
    app.dependency_overrides[image_routes.get_core_client] = lambda: core_client
    app.dependency_overrides[image_routes.get_campaign_client] = lambda: campaign_client
    return app


@pytest.fixture
def _s3_upload_ok(monkeypatch: pytest.MonkeyPatch):
    """Patches the module's `httpx.AsyncClient` so the one raw upload POST
    `_upload` makes lands on an in-memory transport rather than a socket —
    the s3-facing half of the flow that no `BiffoAPIClient` fake covers,
    since it is a plain `httpx` call by design (a presigned POST needs no
    SigV4 signature; S3 verifies the presign's own signature instead)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
    return calls


@pytest.fixture
def _s3_upload_failing(monkeypatch: pytest.MonkeyPatch):
    """As `_s3_upload_ok`, but S3 refuses the upload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="access denied")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)


# ── the happy paths ──────────────────────────────────────────────────────────


def test_generates_stores_and_ledgers_an_unpriced_still(_s3_upload_ok) -> None:
    """Requirement 2 of issue #5, unpriced half: no price returned must be
    *visible* as `unpriced`, never a silent zero."""
    provider = _FakeImageProvider(cost_usd=None)
    core = _FakeCoreClient()
    campaign = _FakeCampaignClient()
    client = TestClient(_app(provider=provider, core_client=core, campaign_client=campaign))

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "a friendly logo, flat style"}
    )

    assert resp.status_code == 201
    body = resp.json()

    # Requirement 1: stored and retrievable by signed URL.
    assert body["url"] == "https://s3.example.invalid/signed-get"
    assert body["media"]["id"] == _MEDIA_ID

    # Requirement 2: ledgered, visibly unpriced.
    assert body["ledger"]["id"] == _LEDGER_ID
    assert body["ledger"]["cost_usd"] is None
    assert body["ledger"]["unpriced"] is True

    # The ONE approved creative, never a placement render.
    assert body["asset"]["is_source"] is True
    assert body["asset"]["media_id"] == _MEDIA_ID
    assert body["asset"]["campaign_id"] == _CAMPAIGN
    assert body["asset"]["placement"] is None
    assert campaign.created_assets == [body["asset"]]

    assert provider.prompts == ["a friendly logo, flat style"]

    ledger_call = next(c for c in core.calls if c[1] == image_routes._LEDGER_PATH)
    assert ledger_call[2]["media_kind"] == "image"
    assert ledger_call[2]["provider"] == "fake-provider"
    assert ledger_call[2]["model"] == "fake-model-1"
    assert ledger_call[2]["units"] == 1.0
    assert ledger_call[2]["unit_kind"] == "image"
    assert ledger_call[2]["cost_usd"] is None


def test_generates_stores_and_ledgers_a_priced_still(_s3_upload_ok) -> None:
    """Requirement 2, priced half — proves the ledger write carries a real
    cost through when the provider returns one, not just the unpriced path."""
    provider = _FakeImageProvider(cost_usd=0.04)
    core = _FakeCoreClient()
    campaign = _FakeCampaignClient()
    client = TestClient(_app(provider=provider, core_client=core, campaign_client=campaign))

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "a mountain at dusk"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["ledger"]["cost_usd"] == 0.04
    assert body["ledger"]["unpriced"] is False


def test_causation_id_travels_to_the_ledger(_s3_upload_ok) -> None:
    core = _FakeCoreClient()
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/stills",
        json={"prompt": "anything", "causation_id": "chain-42"},
    )

    assert resp.status_code == 201
    ledger_call = next(c for c in core.calls if c[1] == image_routes._LEDGER_PATH)
    assert ledger_call[2]["causation_id"] == "chain-42"


def test_swapping_the_provider_needs_no_route_change(_s3_upload_ok) -> None:
    """Requirement 3 of issue #5, made concrete: a *second* `ImageProvider`
    implementation (this test module's own `_FakeImageProvider`, distinct
    from `OpenAIImageProvider`) drives the same route through the same
    storage/ledger/asset flow with no change to `image_routes.py`."""
    provider = _FakeImageProvider(cost_usd=None, content=b"a completely different fake payload")
    client = TestClient(
        _app(
            provider=provider, core_client=_FakeCoreClient(), campaign_client=_FakeCampaignClient()
        )
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "swap me in"})

    assert resp.status_code == 201
    assert resp.json()["asset"]["is_source"] is True


# ── failure paths ────────────────────────────────────────────────────────────


def test_404s_an_unknown_campaign() -> None:
    client = TestClient(
        _app(
            provider=_FakeImageProvider(),
            core_client=_FakeCoreClient(),
            campaign_client=_FakeCampaignClient(exists=False),
        )
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 404


def test_404s_a_malformed_campaign_id() -> None:
    """The SSRF guard: not-a-UUID must 404, never reach a Core call built
    from the raw path segment (`admin_app`'s module docstring names the
    CodeQL finding this mirrors)."""
    campaign_client = _FakeCampaignClient()
    client = TestClient(
        _app(
            provider=_FakeImageProvider(),
            core_client=_FakeCoreClient(),
            campaign_client=campaign_client,
        )
    )

    resp = client.post("/campaigns/not-a-uuid-at-all/stills", json={"prompt": "anything"})

    assert resp.status_code == 404
    # And the guard fired before any Core call was made with the raw value.
    assert campaign_client.created_assets == []


def test_502s_when_the_provider_fails() -> None:
    client = TestClient(
        _app(
            provider=_FailingImageProvider(),
            core_client=_FakeCoreClient(),
            campaign_client=_FakeCampaignClient(),
        )
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    assert "always fails" in resp.json()["detail"]


def test_502s_when_the_generated_image_exceeds_the_storage_ceiling() -> None:
    core = _FakeCoreClient(max_bytes=4)  # smaller than any real payload
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    assert "byte ceiling" in resp.json()["detail"]


def test_502s_when_the_upload_is_refused(_s3_upload_failing) -> None:
    client = TestClient(
        _app(
            provider=_FakeImageProvider(),
            core_client=_FakeCoreClient(),
            campaign_client=_FakeCampaignClient(),
        )
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502


def test_storage_unavailable_passes_through_as_503() -> None:
    """`internal_plugin_storage.py`'s own posture in Core: an unconfigured
    bucket is a deployment that has not been wired, not a bug in this
    request — 503, not 502."""
    core = _FakeCoreClient(presign_status=503)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 503


# ── issue #24: a partial failure must not orphan a billed generation ────────


def test_the_charge_is_ledgered_before_upload_is_even_attempted(_s3_upload_ok) -> None:
    """The core of the fix: the ledger POST is the very next Core call after
    the provider is paid, ahead of storage — not last, as it was before.

    Fails before the fix: the old handler called `_upload` first, so the
    ledger path would not appear before the presign path in `core.calls`.
    """
    core = _FakeCoreClient()
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 201
    post_paths = [path for method, path, _ in core.calls if method == "POST"]
    ledger_index = post_paths.index(image_routes._LEDGER_PATH)
    presign_index = post_paths.index(f"{image_routes._STORAGE_PATH}/presign")
    assert ledger_index < presign_index, f"ledger must precede presign, got {post_paths}"


def test_the_charge_survives_an_upload_failure_and_the_error_names_the_ledger(
    _s3_upload_failing,
) -> None:
    """Issue #24's central claim: a failure between the provider call and the
    asset row must not orphan the charge, and the resulting error must let a
    caller tell this apart from a first attempt.

    Fails before the fix: the old handler ledgered *after* upload, so an
    upload failure meant no ledger row was ever written — `core.calls` would
    contain no POST to `_LEDGER_PATH` at all, and the 502 would say nothing
    about a charge having already happened.
    """
    core = _FakeCoreClient()
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    ledger_calls = [c for c in core.calls if c[1] == image_routes._LEDGER_PATH]
    assert len(ledger_calls) == 1, "the charge must be ledgered even though upload failed"
    detail = resp.json()["detail"]
    assert _LEDGER_ID in detail, (
        f"the error must name the ledger entry so a retry is not blind: {detail!r}"
    )


def test_the_charge_and_media_survive_an_asset_failure_and_the_error_names_both(
    _s3_upload_ok,
) -> None:
    """A failure creating the `marketing_asset` row must not discard the
    evidence that the charge AND the upload already happened."""
    core = _FakeCoreClient()
    campaign = _FakeCampaignClient(fail_asset_creation=True)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=campaign)
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    ledger_calls = [c for c in core.calls if c[1] == image_routes._LEDGER_PATH]
    confirm_calls = [c for c in core.calls if c[1] == f"{image_routes._STORAGE_PATH}/confirm"]
    assert len(ledger_calls) == 1
    assert len(confirm_calls) == 1
    detail = resp.json()["detail"]
    assert _LEDGER_ID in detail
    assert _MEDIA_ID in detail


def test_a_ledger_failure_is_loud_even_though_it_cannot_be_prevented(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one gap the fix does not close (see the route's own docstring): if
    the ledger write itself fails, there is nothing to name in the error
    response, because nothing durable was written. That case must at least
    be loud in the logs, with everything a human needs to reconcile it by
    hand — not silently swallowed the way it would be by a bare `_core_error`
    mapping."""
    core = _FakeCoreClient(ledger_status=502)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    with caplog.at_level("ERROR"):
        resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    assert any("double-bill" in record.message for record in caplog.records)


def test_a_malformed_ledger_response_still_reports_the_charge() -> None:
    """The ledger POST can succeed (the charge IS recorded) while its
    response is missing `id` — Core response-shape drift, not a failed
    write. The 502 that follows must still say a charge happened, even
    though there is no id left to name.

    Fails before this round's fix: `ledger_id = _required_field(...)` was
    not wrapped in `_with_committed_note`, so this case raised a bare
    "Ledger response had no 'id'." with no mention that money had already
    moved — the exact blind-retry risk issue #24 exists to prevent, at the
    point closest to the charge.
    """
    core = _FakeCoreClient(ledger_missing_id=True)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "charged" in detail.lower(), f"the error must say a charge already happened: {detail!r}"


def test_a_malformed_confirm_response_names_the_ledger(_s3_upload_ok) -> None:
    """As above, one step later: storage confirms the upload but its
    response is missing `id`. The ledger is already known at this point, so
    the error must name it.

    Fails before this round's fix: `media_id = _required_field(...)` was not
    wrapped in `_with_committed_note`, so this case's 502 said nothing about
    the ledger entry that already existed.
    """
    core = _FakeCoreClient(confirm_missing_id=True)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert _LEDGER_ID in detail, f"the error must name the ledger entry: {detail!r}"


def test_a_non_numeric_max_bytes_is_a_diagnosable_502_not_a_crash() -> None:
    """`presigned["max_bytes"]` is read straight into a numeric comparison
    (`len(image.content) > max_bytes`). If Core's response carries the key
    but with the wrong type — `null`, a JSON string — a bare `TypeError`
    would raise straight out of that comparison, past every `except
    HTTPException` guard in the route, as an unstyled 500 with no
    committed-state note — even though the charge has already happened by
    this point (issue #24).

    Fails before this round's fix: `max_bytes` was read via `_required_field`
    alone, which only guards presence, not type.
    """
    core = _FakeCoreClient(max_bytes="not-a-number")
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=_FakeCampaignClient())
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "max_bytes" in detail
    assert _LEDGER_ID in detail, f"the error must name the ledger entry: {detail!r}"


def test_a_malformed_asset_response_does_not_fail_an_otherwise_successful_generation(
    _s3_upload_ok,
) -> None:
    """A missing `id` on the asset response is Core response-shape drift,
    but by the time it is read the charge, the upload AND the asset row have
    all already committed — `asset_id` from here on is used only to enrich a
    LATER failure's error message, never to build a request. Hard-failing an
    otherwise fully successful generation over a field that is purely
    cosmetic at this point would be worse than the gap it closes, so this
    must still return 201.
    """
    core = _FakeCoreClient()
    campaign = _FakeCampaignClient(asset_missing_id=True)
    client = TestClient(
        _app(provider=_FakeImageProvider(), core_client=core, campaign_client=campaign)
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert resp.status_code == 201
    assert "id" not in resp.json()["asset"]


def test_rejects_an_empty_prompt() -> None:
    client = TestClient(
        _app(
            provider=_FakeImageProvider(),
            core_client=_FakeCoreClient(),
            campaign_client=_FakeCampaignClient(),
        )
    )

    resp = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": ""})

    assert resp.status_code == 422
