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

Since issue #29 the module also owns a *lifecycle* contract, tested in its
own section below: one shared, principal-free `SignedCoreClient` per
`(base_url, timeout)` rather than one per call, with the caller's identity
moved onto each call. That is a performance change with a security failure
mode — a client that remembered a principal would serve one admin's identity
to the next — so the tests there assert both halves: that the client is built
once, and that two principals still never share a forwarded token.

A fake `SignedCoreClient`, not a mock — matching this repo's convention
(`test_marketing_mint_route.py`'s `_FakeCore`, `test_marketing_image_routes.py`'s
`_FakeCoreClient`) — standing in for the one real thing this module cannot
exercise in CI: an actual SigV4 signature over live AWS credentials.
"""

from __future__ import annotations

import asyncio
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

    Counts its own constructions (`init_kwargs`, appended by the fixture's
    factory) as well as its calls, because since issue #29 the number of
    clients built is itself part of this module's contract: one per
    `(base_url, timeout)` for the life of the process, not one per call.
    """

    def __init__(self) -> None:
        self.init_kwargs: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
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
        return None


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeSignedCoreClient:
    """Patches `principal_client.SignedCoreClient` (the name this module
    imports and calls) so every client this module builds is the SAME fake
    instance, and each construction is recorded on it.

    The module caches one real client per `(base_url, timeout)` for the life
    of the process, so this fixture would otherwise leak its fake into later
    tests; `conftest.py`'s autouse `_reset_principal_client_cache` empties
    that cache around every test, which is also what lets each test below
    assert on `init_kwargs` from a known-empty starting point."""
    client = _FakeSignedCoreClient()

    def _build(**kwargs: Any) -> _FakeSignedCoreClient:
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


# ── The shared client: built once, never principal-specific (issue #29) ────


async def test_repeated_calls_reuse_one_signed_client(fake_client) -> None:
    """Each `SignedCoreClient` resolves AWS credentials once and owns one
    `httpx.AsyncClient` pool, so a per-call build threw both away every call —
    `mint_links` with a 50-link batch paid 51 credential resolutions for one
    HTTP request (issue #29)."""
    for _ in range(5):
        await principal_client.request(
            "GET",
            "/api/v1/internal/plugins/marketing/artefacts",
            _TOKEN,
            base_url="https://core.invalid",
        )

    assert len(fake_client.calls) == 5
    assert len(fake_client.init_kwargs) == 1


async def test_concurrent_calls_build_one_client(fake_client) -> None:
    """The fan-out case, which is the one that actually happens: `_raw`'s
    cache lookup and its cache write must not be separated by an `await`, or
    N tasks each find an empty cache and each build a client — the exact
    shape of bug a serial-only test passes over. Ten concurrent calls, one
    client."""
    await asyncio.gather(
        *(
            principal_client.request(
                "POST",
                f"/api/v1/internal/plugins/marketing/links/{i}",
                _TOKEN,
                base_url="https://core.invalid",
                json={"i": i},
            )
            for i in range(10)
        )
    )

    assert len(fake_client.calls) == 10
    assert len(fake_client.init_kwargs) == 1


async def test_two_principals_never_share_a_forwarded_token(fake_client) -> None:
    """The reason the shared client is a plain `SignedCoreClient` and not the
    SDK's `PrincipalCoreClient` (module docstring): that class binds the user
    token to the CLIENT and its `_sign` overrides the per-call header with
    it, so caching one would sign the second admin's call with the first
    admin's token — one tenant acting as another. Here the identity rides
    each call, so a reused client cannot carry it across."""
    first, second = "admins-own-jwt", "a-different-admins-jwt"  # noqa: S105

    await principal_client.request(
        "GET", "/api/v1/internal/plugins/marketing/campaigns/a", first, base_url="https://c.invalid"
    )
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/campaigns/b",
        second,
        base_url="https://c.invalid",
    )

    # Same client (the point of the cache), different forwarded identity.
    assert len(fake_client.init_kwargs) == 1
    assert fake_client.calls[0]["extra_signed_headers"] == {
        principal_client.FORWARDED_USER_HEADER: first
    }
    assert fake_client.calls[1]["extra_signed_headers"] == {
        principal_client.FORWARDED_USER_HEADER: second
    }


async def test_a_different_base_url_or_timeout_gets_its_own_client(fake_client) -> None:
    """`(base_url, timeout)` is the whole key, and both halves count: a client
    is bound to where it points and how long it waits, so a call with
    different values must not be answered by the cached one."""
    await principal_client.request(
        "GET", "/api/v1/internal/plugins/marketing/x", _TOKEN, base_url="https://one.invalid"
    )
    await principal_client.request(
        "GET", "/api/v1/internal/plugins/marketing/x", _TOKEN, base_url="https://two.invalid"
    )
    await principal_client.request(
        "GET",
        "/api/v1/internal/plugins/marketing/x",
        _TOKEN,
        base_url="https://two.invalid",
        timeout=5.0,
    )

    assert [kw.get("base_url") for kw in fake_client.init_kwargs] == [
        "https://one.invalid",
        "https://two.invalid",
        "https://two.invalid",
    ]
    assert fake_client.init_kwargs[2]["timeout"] == 5.0


def test_a_cached_client_is_not_reused_on_a_new_event_loop(fake_client) -> None:
    """`httpx` pools connections on the loop that opened them, so reusing a
    cached client on a different loop fails with "Event loop is closed" —
    what `main.py`'s module-level `_loop` exists to avoid, and what its
    `if _loop.is_closed()` path would otherwise walk into. The cache entry
    records its loop and is rebuilt when the running loop differs."""

    async def _call() -> None:
        await principal_client.request(
            "GET", "/api/v1/internal/plugins/marketing/x", _TOKEN, base_url="https://core.invalid"
        )

    asyncio.run(_call())
    # A second, independent loop — as a warm Lambda invocation gets after
    # `main.py` replaces a closed one.
    asyncio.run(_call())

    assert len(fake_client.init_kwargs) == 2


def test_the_sdks_principal_client_would_override_a_per_call_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the SDK behaviour this module's design rests on — the reason the
    cached client is a plain `SignedCoreClient` and not the SDK's
    `PrincipalCoreClient` (module docstring).

    `PrincipalCoreClient._sign` merges the per-call `extra` first and then
    assigns its own bound `_user_token` over the top, so a *cached* instance
    of it would sign every later admin's call with the first admin's token no
    matter what the call site passed. Asserted against the real SDK class (the
    base `_sign` is stubbed only to keep botocore and AWS credentials out of
    it), so an SDK release that changed this precedence fails here rather than
    quietly making the docstring above wrong."""
    from biffo_plugin_sdk import PrincipalCoreClient as SdkPrincipalCoreClient
    from biffo_plugin_sdk import SignedCoreClient as SdkSignedCoreClient

    monkeypatch.setattr(
        SdkSignedCoreClient,
        "_sign",
        lambda self, method, url, body, extra=None: dict(extra or {}),
    )
    # `client=` supplied so the SDK does not open a real `httpx.AsyncClient`
    # pool for a test that never sends anything; nothing here touches it.
    client = SdkPrincipalCoreClient(
        "first-admins-jwt", base_url="https://core.invalid", client=object()
    )

    headers = client._sign(  # noqa: SLF001 — the precedence IS the thing under test.
        "GET", "https://core.invalid/x", None, {principal_client.FORWARDED_USER_HEADER: _TOKEN}
    )

    assert headers[principal_client.FORWARDED_USER_HEADER] == "first-admins-jwt"


def test_reset_signed_clients_for_tests_empties_the_cache(fake_client) -> None:
    """The seam `conftest.py` uses so a per-test fake cannot outlive its test
    (the module's own docstring for `reset_signed_clients_for_tests`).
    Synchronous, so a `TestClient`-driven test can call it too."""

    async def _call() -> None:
        await principal_client.request(
            "GET", "/api/v1/internal/plugins/marketing/x", _TOKEN, base_url="https://core.invalid"
        )

    asyncio.run(_call())
    assert len(fake_client.init_kwargs) == 1

    principal_client.reset_signed_clients_for_tests()
    asyncio.run(_call())

    assert len(fake_client.init_kwargs) == 2


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
