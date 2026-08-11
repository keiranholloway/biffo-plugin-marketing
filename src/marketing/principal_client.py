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

`biffo_plugin_sdk.PrincipalCoreClient` — released in `biffo-plugin-sdk` 1.3.0
(`biffo-template#1490`) — now owns the dual-auth mechanism itself: it
overrides `SignedCoreClient._sign` to fold `X-Biffo-User-Token` in BEFORE
signing, so every request path it exposes (`get`/`post`/`patch`/`raw_request`/
...) carries the forwarded token inside the SigV4 signature automatically.
That class's shape — and its name — came from THIS module: before 1.3.0, this
file hand-built a `SignedCoreClient` per call and passed the token through
`raw_request`'s `extra_signed_headers=`, which is the one SDK primitive that
signs a request AND carries an extra header through that signature. The SDK
class now does the same thing structurally rather than at each call site, so
this module builds on it instead of re-implementing it.

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

import json as _json
from typing import Any
from urllib.parse import urlencode

import httpx
from biffo_plugin_sdk import FORWARDED_USER_HEADER, BiffoAPIClient
from biffo_plugin_sdk import PrincipalCoreClient as SignedCoreClient

__all__ = ["FORWARDED_USER_HEADER", "request", "PrincipalCoreClient"]


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

    Builds and closes its own `SignedCoreClient` (the SDK's
    `PrincipalCoreClient`, imported under its predecessor's name — see the
    module docstring) per call, deliberately matching the lifecycle
    discipline of the `httpx.AsyncClient` this replaced in `admin_app._core`
    (`async with httpx.AsyncClient(...) as client:`) — it owns exactly such a
    client (`BiffoAPIClient.__init__` builds one whenever `client=` isn't
    passed), so skipping the `async with` here would leak one connection pool
    per call rather than one per request, which is what a caller reading the
    code this replaced would reasonably expect not to happen.
    """
    # `None`-valued entries are dropped, not stringified — see module
    # docstring. The SDK's own `_send`/`raw_request` callers do not do this.
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        full_path = f"{path}?{urlencode(clean_params)}" if clean_params else path
    else:
        full_path = path
    content = _json.dumps(json).encode() if json is not None else None
    kwargs: dict[str, Any] = {"base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    # Not `async with SignedCoreClient(token, **kwargs) as client:` — the
    # SDK's `BiffoAPIClient.__aenter__` is declared `-> BiffoAPIClient`, not
    # `Self`, so pyright resolves `client`'s type through the base class and
    # loses `raw_request` (only defined on `SignedCoreClient`/
    # `PrincipalCoreClient`). A plain try/finally closes the same connection
    # pool `__aexit__` would, without depending on a return-type annotation
    # this module doesn't own.
    client = SignedCoreClient(token, **kwargs)
    try:
        # This client's own `_sign` override (see module docstring) already
        # folds the forwarded user token in before signing — passing it again
        # here via `extra_signed_headers` is redundant with that (`_sign`
        # merges by key, so the duplicate is a same-value no-op) but keeps
        # the token's presence observable at THIS call's boundary, which is
        # what `tests/test_marketing_principal_client.py`'s fake asserts on:
        # it stands in for the whole client, so it never runs `_sign` itself.
        return await client.raw_request(
            method,
            full_path,
            content=content,
            extra_signed_headers={FORWARDED_USER_HEADER: token},
        )
    finally:
        await client.aclose()


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
