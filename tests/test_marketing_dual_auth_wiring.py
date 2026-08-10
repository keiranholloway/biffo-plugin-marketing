"""The seam between `admin_app`/`image_routes` and `principal_client` —
issue #27's actual security property, end to end.

Every other test that touches a Core call mocks `admin_app._core` wholesale
(`test_marketing_mint_route.py`, `test_marketing_pipeline_routes.py`) or
dependency-overrides `image_routes.get_campaign_client`
(`test_marketing_image_routes.py`). That is the right boundary for testing
each ROUTE's own logic, but it means none of those tests ever runs `_core`'s
or `get_campaign_client`'s own bodies — the code that threads `method`,
`path` and the calling admin's `token` through to `principal_client`, in
that order, with that specific token. Proven empirically before this file
was written: swapping `_core`'s delegation to
`principal_client.request(method, token, path, ...)` (path and token
transposed), and separately replacing `get_campaign_client`'s
`PrincipalCoreClient(admin.token)` with a hardcoded wrong string, both leave
the full suite at `226 passed`. The second sabotage is the exact failure
mode this module's own docstring (`principal_client.py`) warns about: a
signed-but-tokenless (or wrong-token) call reaches Core and fails as a
silent authorization error, not a loud one — so no route-level test
(asserting a 200/404/503) would ever catch it either.

So this file fakes ONLY `principal_client.SignedCoreClient` — the lowest
seam both `admin_app._core` and `image_routes.get_campaign_client` share —
and drives real HTTP requests through the real route handlers, with a
deliberately distinctive admin token so a wrong-token bug cannot hide behind
a coincidental match.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketing import admin_app, image_routes, principal_client

#: Deliberately distinctive — not "admin-jwt" or "token", which several
#: other test files already use, so a bug that forwards the WRONG-but-also-
#: plausible-looking token cannot coincidentally satisfy an assertion here.
_REAL_ADMIN_TOKEN = "the-genuine-signed-in-admins-jwt"  # noqa: S105
_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000f1"
_BASE_URL = "https://dev.tabsii.com"
_CORE_API_URL = "https://core.invalid"


class _FakeSignedCoreClient:
    """Stands in for `biffo_plugin_sdk.SignedCoreClient` at the one seam
    every dual-auth call site shares. Records every `raw_request` call —
    what is asserted here is the `extra_signed_headers` token and the
    `path`/`method`, never the response shape (that is
    `test_marketing_principal_client.py`'s job)."""

    def __init__(self) -> None:
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
            {
                "method": method,
                "path": path,
                "content": content,
                "extra_signed_headers": extra_signed_headers,
            }
        )
        # A generically-shaped 2xx JSON body: every route this file drives
        # only needs SOME campaign/artefact/asset-looking dict back, not a
        # specific one — the assertions below are about what was SENT.
        return (
            200,
            b'{"id": "x", "destination_url": "https://tabsii.com/intake/demo"}',
            ("application/json"),
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_signed_client(monkeypatch: pytest.MonkeyPatch) -> _FakeSignedCoreClient:
    client = _FakeSignedCoreClient()
    monkeypatch.setattr(principal_client, "SignedCoreClient", lambda **kw: client)
    return client


# ── admin_app._core, driven for real through a route ─────────────────────────


def test_mint_links_forwards_the_real_admins_token_through_core(
    fake_signed_client: _FakeSignedCoreClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_core`'s own body — not a mock of it — signs both of `mint_links`'
    Core calls with the SAME token the request arrived with."""
    monkeypatch.setattr(admin_app, "CORE_API_URL", _CORE_API_URL)
    monkeypatch.setattr(admin_app, "public_base_url", lambda: _BASE_URL)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": _REAL_ADMIN_TOKEN}
    )()

    resp = TestClient(app).post(
        f"/campaigns/{_CAMPAIGN}/links", json={"links": [{"channel": "linkedin"}]}
    )

    assert resp.status_code == 201
    assert len(fake_signed_client.calls) == 2  # GET campaign, POST links
    for call in fake_signed_client.calls:
        assert call["extra_signed_headers"] == {
            principal_client.FORWARDED_USER_HEADER: _REAL_ADMIN_TOKEN
        }


def test_mint_links_calls_the_real_internal_paths_not_the_token(
    fake_signed_client: _FakeSignedCoreClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards specifically against an argument-order regression in `_core`'s
    delegation to `principal_client.request` — if `method`/`path`/`token`
    were ever transposed, the token (a JWT-shaped string, never starting
    with `/`) would arrive here as the `path`."""
    monkeypatch.setattr(admin_app, "CORE_API_URL", _CORE_API_URL)
    monkeypatch.setattr(admin_app, "public_base_url", lambda: _BASE_URL)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": _REAL_ADMIN_TOKEN}
    )()

    TestClient(app).post(f"/campaigns/{_CAMPAIGN}/links", json={"links": [{"channel": "linkedin"}]})

    paths = {call["path"] for call in fake_signed_client.calls}
    assert paths == {
        f"{admin_app._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}",
        f"{admin_app._INTERNAL_PREFIX}/links",
    }
    assert _REAL_ADMIN_TOKEN not in paths


def test_approve_artefact_forwards_the_real_admins_token(
    fake_signed_client: _FakeSignedCoreClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second, independent route through `_core` (PATCH, not GET/POST) —
    `_latest_artefact` then `approve_artefact_route`'s own PATCH — so this
    is not proving the property for only one HTTP method."""
    monkeypatch.setattr(admin_app, "CORE_API_URL", _CORE_API_URL)

    class _ProposedThenApproved:
        """The fake Core answers `GET .../artefacts` with one `proposed`
        row, so `approve_artefact_route`'s status check passes and its own
        PATCH is what this test observes."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def raw_request(self, method, path, *, content=None, extra_signed_headers=None):
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "content": content,
                    "extra_signed_headers": extra_signed_headers,
                }
            )
            if method == "GET":
                import json as _json

                row = {
                    "id": "artefact-1",
                    "campaign_id": _CAMPAIGN,
                    "kind": "research",
                    "status": "proposed",
                    "created_at": "2026-08-10T00:00:00Z",
                }
                return 200, _json.dumps([row]).encode(), "application/json"
            return 200, b'{"id": "artefact-1", "status": "approved"}', "application/json"

        async def aclose(self) -> None:
            return None

    fake = _ProposedThenApproved()
    monkeypatch.setattr(principal_client, "SignedCoreClient", lambda **kw: fake)

    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": _REAL_ADMIN_TOKEN}
    )()

    resp = TestClient(app).post(f"/campaigns/{_CAMPAIGN}/artefacts/research/approve")

    assert resp.status_code == 200
    patch_calls = [c for c in fake.calls if c["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0]["extra_signed_headers"] == {
        principal_client.FORWARDED_USER_HEADER: _REAL_ADMIN_TOKEN
    }
    assert patch_calls[0]["path"] == f"{admin_app._INTERNAL_PREFIX}/artefacts/artefact-1"


# ── image_routes.get_campaign_client, driven for real through a route ───────


class _FakeImageProvider:
    async def generate_still(self, *, prompt: str) -> Any:
        from marketing.image_provider import GeneratedImage

        return GeneratedImage(
            content=b"fake-bytes",
            content_type="image/png",
            filename="fake.png",
            provider="fake-provider",
            model="fake-model-1",
            units=1.0,
            unit_kind="image",
            cost_usd=None,
        )


class _FakeCoreClient:
    """Stands in for `image_routes.get_core_client()` (Class 1 — plugin
    identity only, no forwarded token) so this test can isolate the ONE
    thing it cares about: `get_campaign_client()`'s dual-auth wiring."""

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        if path.endswith("/presign"):
            return {
                "key": "k",
                "url": "https://s3.example.invalid/upload",
                "fields": {},
                "max_bytes": 10_000_000,
                "expires_in": 900,
            }
        if path.endswith("/confirm"):
            return {"id": "media-1", "storage_key": "k", "filename": "f", "mime_type": "image/png"}
        return {"id": "ledger-1"}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return {"url": "https://s3.example.invalid/signed-get"}


def test_generate_still_forwards_the_real_admins_token_to_the_campaign_client(
    fake_signed_client: _FakeSignedCoreClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get_campaign_client()` is NOT dependency-overridden here — it runs
    for real, building a real `PrincipalCoreClient(admin.token)` that must
    carry the SAME token the request arrived with through to the (faked)
    `SignedCoreClient`."""
    import httpx
    from fastapi import FastAPI

    # `_upload`'s one raw `httpx.AsyncClient` POST to object storage — same
    # `MockTransport` pattern as `test_marketing_image_routes.py`'s own
    # `_s3_upload_ok` fixture, so this test isolates the ONE new thing it
    # exists to check (the campaign_client wiring) rather than reinventing
    # S3-upload faking.
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)

    app = FastAPI()
    app.include_router(image_routes.router)
    app.dependency_overrides[image_routes.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": _REAL_ADMIN_TOKEN}
    )()
    app.dependency_overrides[image_routes.get_image_provider] = lambda: _FakeImageProvider()
    app.dependency_overrides[image_routes.get_core_client] = lambda: _FakeCoreClient()
    # Deliberately NOT overriding get_campaign_client — see docstring above.

    resp = TestClient(app).post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "a logo"})

    assert resp.status_code == 201
    assert len(fake_signed_client.calls) == 2  # GET campaign, POST assets
    for call in fake_signed_client.calls:
        assert call["extra_signed_headers"] == {
            principal_client.FORWARDED_USER_HEADER: _REAL_ADMIN_TOKEN
        }
    paths = {call["path"] for call in fake_signed_client.calls}
    assert paths == {
        f"{image_routes._INTERNAL_PREFIX}/campaigns/{_CAMPAIGN}",
        f"{image_routes._INTERNAL_PREFIX}/assets",
    }
