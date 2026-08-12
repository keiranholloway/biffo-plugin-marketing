"""Dual-auth transport to Core's internal, per-plugin CRUD mount (issue #27).

Core's internal API (`/api/v1/internal/*`) is IAM-authorized, not Cognito
(ADR-0009) — reaching it at all requires a SigV4 signature. But the per-plugin
CRUD routes under `/api/v1/internal/plugins/<name>/*` are gated a second way,
on top of that: `plugin_router.py`'s `build_plugin_router(guard_factory=
require_principal_crud_permission)` (read in `biffo-template`) still
authorises on the *calling admin's own role*, not merely on "this is a
SigV4-signed request from a trusted plugin". That guard accepts the user's
token from either transport — a Cognito bearer header on the public mount, or
`X-Biffo-User-Token` on the SigV4-signed internal one — and checks the same
thing either way. The two mounts differ only in *who can reach them*, never in
*what is allowed* once they do (see that module's docstring for the full
reasoning).

So a call through here needs BOTH credentials, together, not either alone:

- **SigV4**, or API Gateway never lets the request past the front door.
- **The admin's own forwarded token**, via `X-Biffo-User-Token`, or the
  request reaches Core with a valid signature and no user identity at all —
  which does not fail loudly. It reaches `require_principal_crud_permission`
  signed but tokenless, and that guard has nothing to authorise *against*, so
  the honest failure mode is a permission error, not a 404. Shipping a
  SigV4-only client here would look like a fix (the path resolves, Core
  responds) while actually trading a 404-everywhere bug for a silent
  authorization one — see this plugin's own `admin_app._core` before this
  module existed, and issue #27.

The transport is **one long-lived, principal-free `SignedCoreClient` per
`(base_url, timeout)`**, reused across every call in a warm execution
environment (issue #29), with the calling admin's token supplied *per call*
through `raw_request`'s `extra_signed_headers=` — the one SDK primitive that
signs a request AND carries an extra header through that signature. See
`_signed_client_for` below for the cache and why it is keyed that way.

**Why not the SDK's `PrincipalCoreClient` here.** `biffo-plugin-sdk` 1.3.0
(`biffo-template#1490`) added a `SignedCoreClient` subclass that binds the
user token to the *client* and folds it in by overriding `_sign`, so every
request path it exposes carries the token automatically. That shape came from
this module, and it is the right one when the client is built per admin
request — which its own docstring states as its assumption ("`user_token` is
bound once per client, matching every existing caller's lifecycle... never
shared across identities"). A reused client breaks exactly that assumption,
and not benignly: `PrincipalCoreClient._sign` sets
`merged[FORWARDED_USER_HEADER] = self._user_token` *after* merging the
per-call `extra`, so a cached instance would sign every later admin's calls
with the FIRST admin's token — one tenant acting as another, which is a far
worse defect than the per-call cost this module now avoids. Client reuse and
a client-bound identity cannot both be had; this module keeps the reuse and
moves the identity onto the call, where the SDK's `extra_signed_headers`
already puts it inside the signature. `tests/test_marketing_principal_client.
py::test_two_principals_never_share_a_forwarded_token` is the guard.

Two things below are deliberately NOT part of the SDK class — they were
judged per-plugin adapter concerns, not shared mechanism, when the SDK class
was written — and stay here:

- **`None`-valued query params are dropped**, not stringified. The SDK's
  `_send`/`raw_request` callers pass `urlencode(params)` straight through, so
  `{"a": None}` would render as the literal query text `a=None` — a real
  value that matches nothing, not the omitted filter a caller almost
  certainly means. No current caller passes one (verified), but a future
  optional filter reaching here should omit cleanly, not silently break.
- **The `httpx.Response` wrapper** (`request()`, below) over the SDK's raw
  `(status, body, content_type)` tuple, for `admin_app._core` and every
  existing call site that already does `.status_code` /
  `.raise_for_status()` / `.json()`.

Every path passed in here is expected to already be the FULL internal path
(`/api/v1/internal/plugins/<name>/...`), never a bare one — this module does
no prefixing of its own. That is deliberate: `tests/
test_marketing_core_paths_guard.py` walks each call site's own literal
argument text, so the `/api/v1/...` prefix has to be visible AT THE CALL
SITE, not hidden behind this module's plumbing. See that file's docstring.
"""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from biffo_plugin_sdk import FORWARDED_USER_HEADER, BiffoAPIClient, SignedCoreClient

__all__ = [
    "FORWARDED_USER_HEADER",
    "PrincipalCoreClient",
    "request",
    "reset_signed_clients_for_tests",
]


@dataclass
class _CachedClient:
    """A cached signed client plus the event loop its connection pool belongs
    to — see `_signed_client_for` for why the loop is part of the entry."""

    client: Any
    loop: asyncio.AbstractEventLoop


#: One signed client per `(base_url, timeout)`, reused for the life of the
#: process. NOT keyed by anything identifying a user: see `_signed_client_for`.
_SIGNED_CLIENTS: dict[tuple[str | None, float | None], _CachedClient] = {}


def _signed_client_for(base_url: str | None, timeout: float | None) -> Any:
    """The shared `SignedCoreClient` for this `(base_url, timeout)`, built once.

    **What the key is, and why that is safe.** The key is exactly the two
    things that change what the client *is* — where it points and how long it
    waits. It contains nothing about *who* is calling, because the client
    contains nothing about who is calling: the admin's forwarded token is
    passed per call to `raw_request(..., extra_signed_headers=...)` in `_raw`
    and never stored on the client. So there is no per-principal state a wrong
    key could hand to the wrong tenant — the isolation is structural, not a
    property of the key being chosen carefully. (This is also why the SDK's
    `PrincipalCoreClient`, which binds the token to the client, is not what is
    cached here — the module docstring has that reasoning in full.)

    **What is actually saved.** Each `SignedCoreClient` resolves AWS
    credentials once and memoises them on `self` (`_get_credentials`), and
    owns one `httpx.AsyncClient` connection pool
    (`BiffoAPIClient.__init__`). Building one per call — the previous
    behaviour — threw both away after a single request, so `mint_links` with
    a 50-link batch paid 51 `botocore.session.get_session().get_credentials()`
    resolutions and opened 51 pools for one HTTP request (issue #29). The
    reference implementation this module's docstring cites,
    `plugin_host/app.py::core_sender()` in biffo-template, has always built
    its client once at composition time; the per-call build here was the
    anomaly.

    **Credential expiry is botocore's job, not ours.** In Lambda,
    `get_credentials()` returns a `RefreshableCredentials`, and `SigV4Auth`
    calls `get_frozen_credentials()` on it at every signature, which refreshes
    in place when the role's credentials are near expiry. Holding the
    credentials object for the life of the process is therefore *how* botocore
    is designed to be used — a client that outlives one set of temporary
    credentials keeps signing with valid ones.

    **No lock is needed for concurrent callers.** Everything between the cache
    lookup and the cache write below is synchronous — there is no `await`, so
    the event loop cannot interleave another task inside it, and N concurrent
    calls (a `mint_links` fan-out, an `asyncio.gather`) cannot race to build
    two clients. Guarded by
    `tests/test_marketing_principal_client.py::test_concurrent_calls_build_one_client`.

    **A new event loop invalidates the entry.** `httpx`'s pooled connections
    are bound to the loop that opened them, so reusing a pool on a different
    loop fails with "Event loop is closed" — the exact bug `main.py`'s
    module-level `_loop` exists to avoid, on the same reasoning. `main.py`
    keeps one loop across warm invocations, so this normally never fires; when
    a loop *is* replaced (that file's `if _loop.is_closed()` path), the entry
    is rebuilt rather than reused. The stale client is dropped without
    `aclose()` — closing it would have to run on the loop that is already
    gone.
    """
    key = (base_url, timeout)
    loop = asyncio.get_running_loop()
    cached = _SIGNED_CLIENTS.get(key)
    if cached is not None and cached.loop is loop:
        return cached.client
    kwargs: dict[str, Any] = {"base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    client = SignedCoreClient(**kwargs)
    _SIGNED_CLIENTS[key] = _CachedClient(client=client, loop=loop)
    return client


def reset_signed_clients_for_tests() -> None:
    """Drop every cached client — the test-isolation seam for the cache above.

    A process-lifetime cache and a test suite that monkeypatches
    `principal_client.SignedCoreClient` per test are in direct conflict
    without this: the first test to make a call caches ITS fake, `monkeypatch`
    then restores the real name at teardown, and every later test in the run
    keeps talking to the first test's fake. `tests/conftest.py` calls this
    around every test so the leak cannot happen; the same seam is why
    `web-admin/src/lib/auth.ts` exposes `__resetUserPoolForTests`.

    Deliberately synchronous, so a non-async test (`test_marketing_dual_auth_
    wiring.py` drives routes through a sync `TestClient`) can call it too.
    Entries are dropped, not closed: the only clients this ever discards in
    practice are test fakes holding no real socket, and a real pool is
    released with the process.
    """
    _SIGNED_CLIENTS.clear()


async def _raw(
    method: str,
    path: str,
    token: str,
    *,
    base_url: str | None,
    params: dict[str, Any] | None,
    json: dict[str, Any] | None,
    timeout: float | None,
) -> tuple[int, bytes, str]:
    """The one signed, dual-credential send every helper below builds on.

    Borrows the shared `SignedCoreClient` for this `(base_url, timeout)` — it
    is not built here and not closed here; `_signed_client_for` owns its
    lifecycle and documents why it is shared. What this function owns is the
    other half of the pair: the caller's identity, which rides the CALL rather
    than the client (`extra_signed_headers` below), and is what makes sharing
    the client safe in the first place.
    """
    # `None`-valued entries are dropped, not stringified — see module
    # docstring. The SDK's own `_send`/`raw_request` callers do not do this.
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        full_path = f"{path}?{urlencode(clean_params)}" if clean_params else path
    else:
        full_path = path
    content = _json.dumps(json).encode() if json is not None else None
    client = _signed_client_for(base_url, timeout)
    # `extra_signed_headers` is the ONLY place the calling admin's identity
    # enters this request, and it is load-bearing rather than belt-and-braces:
    # the shared client is a plain `SignedCoreClient` with no token bound to
    # it (see `_signed_client_for`), so dropping this line would send a
    # signed, tokenless request — which does not fail loudly, it reaches
    # `require_principal_crud_permission` with nothing to authorise against
    # (module docstring). `SignedCoreClient._sign` merges these in BEFORE
    # signing, so the token is covered by the SigV4 signature rather than
    # appended to an already-signed request.
    return await client.raw_request(
        method,
        full_path,
        content=content,
        extra_signed_headers={FORWARDED_USER_HEADER: token},
    )


async def request(
    method: str,
    path: str,
    token: str,
    *,
    base_url: str | None = None,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """The dual-auth call, wrapped back into a real `httpx.Response`.

    For callers written against that interface (`admin_app._core`, whose
    every existing call site already reads `.status_code` /
    `.raise_for_status()` / `.json()`) — this lets that interface keep
    working unchanged rather than every call site being rewritten around the
    SDK's raw `(status, body, content_type)` tuple.
    """
    status_code, body, content_type = await _raw(
        method, path, token, base_url=base_url, params=params, json=json, timeout=timeout
    )
    # A real (if synthetic) Request, purely so `.raise_for_status()` has one to
    # reference — httpx requires it and never inspects it for anything else here.
    dummy_request = httpx.Request(method, f"https://core.internal{path}")
    return httpx.Response(
        status_code, content=body, headers={"content-type": content_type}, request=dummy_request
    )


class PrincipalCoreClient:
    """The same dual-auth call, shaped like `BiffoAPIClient` instead —
    `.get`/`.post`/`.patch` returning parsed JSON and raising `BiffoAPIError`
    on a non-2xx, for callers already written against that interface
    (`image_routes.get_campaign_client`, whose call sites already do
    `except BiffoAPIError`).

    Not simply an alias for the SDK's own `PrincipalCoreClient.get`/`post`/
    `patch` (which return parsed JSON the same way): those go through
    `_send`, which does not drop `None`-valued query params (see module
    docstring) — so this class stays on top of `request()`/`_raw` above,
    which does.
    """

    def __init__(
        self, token: str, *, base_url: str | None = None, timeout: float | None = None
    ) -> None:
        self._token = token
        self._base_url = base_url
        self._timeout = timeout

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._call("GET", path, params=params)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._call("POST", path, json=json)

    async def patch(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._call("PATCH", path, json=json)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # Routed through `request()` (this module's own httpx.Response-shaped
        # wrapper) rather than `_raw` directly, specifically so the
        # non-2xx-to-BiffoAPIError mapping below can reuse `BiffoAPIClient`'s
        # OWN static helpers instead of a second, independent copy of them —
        # an earlier version of this method hand-rolled that mapping and had
        # already drifted from the SDK's own fallback-detail text on first
        # write (`f"HTTP {status}"` vs. the SDK's `response.reason_phrase`).
        response = await request(
            method,
            path,
            self._token,
            base_url=self._base_url,
            params=params,
            json=json,
            timeout=self._timeout,
        )
        BiffoAPIClient._raise_if_error(response)
        return BiffoAPIClient._parse_json(response)
