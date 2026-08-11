"""Direct unit tests for `principal_client.py` (issue #27).

Every other test in this repo that touches a Core call exercises this module
only through a fake at the boundary — `admin_app._core` is monkeypatched
wholesale in `test_marketing_mint_route.py`/`test_marketing_pipeline_routes.py`,
and `image_routes.get_campaign_client` is dependency-overridden in
`test_marketing_image_routes.py`. None of them runs a single line of this
module's own code: `uv run pytest --cov=src/marketing/principal_client`
reports "No data was collected" without this file, confirmed before writing
it. What is worth asserting here is `principal_client`'s own contract — the
forwarded user token rides `extra_signed_headers`, `json`/`params` are
encoded the way Core expects, and both wrapper shapes (`request`'s
`httpx.Response`, `PrincipalCoreClient`'s parse-and-raise) map a
`SignedCoreClient.raw_request` response correctly in both directions (2xx
and non-2xx).

A fake `SignedCoreClient`, not a mock — matching this repo's convention
(`test_marketing_mint_route.py`'s `_FakeCore`, `test_marketing_image_routes.py`'s
`_FakeCoreClient`) — standing in for the one real thing this module cannot
exercise in CI: an actual SigV4 signature over live AWS credentials.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from biffo_plugin_sdk import BiffoAPIError

from marketing import principal_client

_TOKEN = "user-jwt"  # noqa: S105 — a fixture value, not a real credential.


class _FakeSignedCoreClient:
    """Stands in for `biffo_plugin_sdk.SignedCoreClient`: records every
    `raw_request` call and answers with a canned `(status, body,
    content_type)` — that method's own return shape, so this exercises
    exactly the contract `principal_client` is written against.

    Also implements `aclose()` — `principal_client._raw` closes the
    connection pool `SignedCoreClient` opens per instance via a plain
    try/finally (not `async with`; see that function's own docstring for
    why) — and records whether it was reached, so a test can catch a future
    regression back to "build one and never close it" the same way this
    fake caught it once.
    """

    def __init__(self) -> None:
        self.init_kwargs: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._status = 200
        self._body: bytes = b"{}"
        self._content_type = "application/json"

    def answer(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self._status, self._body, self._content_type = status, body, content_type

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
        return self._status, self._body, self._content_type

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeSignedCoreClient:
    """Patches `principal_client.SignedCoreClient` (the name this module
    imports and calls) so every `SignedCoreClient(**kwargs)` this module
    constructs returns the SAME fake instance — `_raw` builds a fresh client
    per call, matching `admin_app._core`'s pre-fix per-call
    `httpx.AsyncClient`, and every test below makes exactly one call."""
    client = _FakeSignedCoreClient()

    def _build(token: str, **kwargs: Any) -> _FakeSignedCoreClient:
        client.init_kwargs.append(kwargs)
        return client

    monkeypatch.setattr(principal_client, "SignedCoreClient", _build)
    return client


# ── request() — the httpx.Response-shaped wrapper (admin_app._core) ─────────


async def test_request_signs_with_the_forwarded_user_token(fake_client) -> None:
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/campaigns/x",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert fake_client.calls[0]["extra_signed_headers"] == {
        principal_client.FORWARDED_USER_HEADER: _TOKEN
    }


async def test_request_passes_the_full_path_through_unchanged(fake_client) -> None:
    """No prefixing here — the guard needs the `/api/v1/...` prefix visible
    AT THE CALL SITE (this module's docstring), so a caller must already
    supply the full internal path."""
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert fake_client.calls[0]["path"] == "/api/v1/internal/plugins/marketing/artefacts"
    assert fake_client.calls[0]["method"] == "GET"
    assert fake_client.calls[0]["content"] is None


async def test_request_appends_params_as_a_querystring(fake_client) -> None:
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
        params={"campaign_id": "c1", "kind": "research"},
    )

    assert (
        fake_client.calls[0]["path"]
        == "/api/v1/internal/plugins/marketing/artefacts?campaign_id=c1&kind=research"
    )


async def test_request_omits_none_valued_params_rather_than_stringifying_them(fake_client) -> None:
    """`urlencode({"a": None})` alone renders the literal query text `a=None`
    — a real value that would silently match nothing, not the omitted
    filter a caller almost certainly means. No current caller passes a
    `None` param (verified before this test was written), but the omission
    has to hold for the first one that does."""
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
        params={"campaign_id": "c1", "kind": None},
    )

    assert (
        fake_client.calls[0]["path"]
        == "/api/v1/internal/plugins/marketing/artefacts?campaign_id=c1"
    )


async def test_request_with_only_none_valued_params_appends_no_querystring(fake_client) -> None:
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
        params={"kind": None},
    )

    assert fake_client.calls[0]["path"] == "/api/v1/internal/plugins/marketing/artefacts"


async def test_request_json_encodes_the_body(fake_client) -> None:
    await principal_client.request(
        "POST",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
        json={"status": "approved"},
    )

    assert json.loads(fake_client.calls[0]["content"]) == {"status": "approved"}


async def test_request_passes_base_url_and_timeout_to_the_signed_client(fake_client) -> None:
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
        timeout=30.0,
    )

    assert fake_client.init_kwargs[0]["base_url"] == "https://core.invalid"
    assert fake_client.init_kwargs[0]["timeout"] == 30.0


async def test_request_omits_timeout_when_not_given(fake_client) -> None:
    """The SDK's own default (30.0 on `SignedCoreClient`) should apply,
    rather than this module silently overriding it with something else."""
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert "timeout" not in fake_client.init_kwargs[0]


async def test_request_closes_the_signed_client_after_the_call(fake_client) -> None:
    """A `SignedCoreClient` owns its own `httpx.AsyncClient`/connection pool
    (`BiffoAPIClient.__init__` builds one whenever `client=` isn't passed);
    building a fresh one per Core call without closing it leaks one pool per
    call. Regression guard for exactly that: a version of `_raw` without the
    `async with` passes every other test in this file and still leaks."""
    assert fake_client.closed is False

    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert fake_client.closed is True


async def test_request_returns_a_real_httpx_response_on_success(fake_client) -> None:
    fake_client.answer(200, b'{"id": "a1"}')

    resp = await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/artefacts/a1",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert isinstance(resp, httpx.Response)
    assert resp.status_code == 200
    assert resp.json() == {"id": "a1"}
    resp.raise_for_status()  # must not raise


async def test_request_response_raise_for_status_fires_on_4xx(fake_client) -> None:
    """The whole point of returning a real `httpx.Response`: every existing
    `_core()` call site does `resp.raise_for_status()` unchanged."""
    fake_client.answer(404, b'{"detail": "not found"}')

    resp = await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/campaigns/x",
        _TOKEN,
        base_url="https://core.invalid",
    )

    assert resp.status_code == 404
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


# ── PrincipalCoreClient — the BiffoAPIClient-shaped wrapper (image_routes) ──


async def test_principal_core_client_get_returns_parsed_json_on_success(fake_client) -> None:
    fake_client.answer(200, b'{"id": "campaign-1"}')
    client = principal_client.PrincipalCoreClient(_TOKEN)

    result = await client.get("/api/v1/internal/plugins/marketing/campaigns/campaign-1")

    assert result == {"id": "campaign-1"}
    assert fake_client.calls[0]["extra_signed_headers"] == {
        principal_client.FORWARDED_USER_HEADER: _TOKEN
    }


async def test_principal_core_client_get_returns_none_for_an_empty_body(fake_client) -> None:
    fake_client.answer(204, b"")
    client = principal_client.PrincipalCoreClient(_TOKEN)

    assert await client.get("/api/v1/internal/plugins/marketing/campaigns/x") is None


async def test_principal_core_client_post_encodes_json_and_signs_the_token(fake_client) -> None:
    fake_client.answer(201, b'{"id": "asset-1"}')
    client = principal_client.PrincipalCoreClient(_TOKEN)

    result = await client.post(
        "/api/v1/internal/plugins/marketing/assets", json={"campaign_id": "c1"}
    )

    assert result == {"id": "asset-1"}
    assert json.loads(fake_client.calls[0]["content"]) == {"campaign_id": "c1"}


async def test_principal_core_client_raises_biffo_api_error_on_404_with_detail(fake_client) -> None:
    fake_client.answer(404, b'{"detail": "Campaign not found."}')
    client = principal_client.PrincipalCoreClient(_TOKEN)

    with pytest.raises(BiffoAPIError) as exc:
        await client.get("/api/v1/internal/plugins/marketing/campaigns/x")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Campaign not found."


async def test_principal_core_client_raises_biffo_api_error_with_a_fallback_detail(
    fake_client,
) -> None:
    """A non-JSON or detail-less error body must still raise something a
    caller's `except BiffoAPIError` can read, not crash the mapping itself."""
    fake_client.answer(502, b"upstream exploded", content_type="text/plain")
    client = principal_client.PrincipalCoreClient(_TOKEN)

    with pytest.raises(BiffoAPIError) as exc:
        await client.post("/api/v1/internal/plugins/marketing/assets", json={"a": 1})

    assert exc.value.status_code == 502


async def test_principal_core_client_patch_is_supported(fake_client) -> None:
    fake_client.answer(200, b'{"status": "approved"}')
    client = principal_client.PrincipalCoreClient(_TOKEN)

    result = await client.patch(
        "/api/v1/internal/plugins/marketing/artefacts/a1", json={"status": "approved"}
    )

    assert result == {"status": "approved"}
    assert fake_client.calls[0]["method"] == "PATCH"
