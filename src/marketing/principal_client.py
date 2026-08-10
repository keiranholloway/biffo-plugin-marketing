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

`SignedCoreClient.raw_request(method, path, content=..., extra_signed_headers=
...)` is the one SDK method that signs a request AND carries an extra header
through that signature (so the header can't be stripped or altered between
signing and arrival) — released in `biffo-plugin-sdk` 1.2.0
(keiranholloway/biffo-template#1480; PyPI had only 1.1.0, which lacked it,
when issue #27 was filed). It is also exactly what the shared plugin host's
own forwarder uses to relay a plugin's declared `api_routes` to Core
(`services/_plugin-host/src/plugin_host/app.py::core_sender`, read directly
rather than guessed at) — this module is the same shape, for this plugin's
own bespoke (non-generated-CRUD) routes.

Built directly on `SignedCoreClient`, not `create_core_client()`: the latter
can build either a signing or a plain client depending on
`BIFFO_CORE_AUTH_MODE`, and its declared return type is the plain base
(`BiffoAPIClient`), which has no `raw_request` at all. `core_sender()` makes
the same choice for the same reason.

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
from biffo_plugin_sdk import BiffoAPIClient, SignedCoreClient

#: Matches `plugin_host.forward.FORWARDED_USER_HEADER` exactly (biffo-template)
#: — the header `require_principal_crud_permission` reads a user token from
#: when the transport is SigV4 rather than a bearer `Authorization` header.
FORWARDED_USER_HEADER = "X-Biffo-User-Token"


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

    Builds and closes its own `SignedCoreClient` per call, deliberately
    matching the lifecycle discipline of the `httpx.AsyncClient` this
    replaced in `admin_app._core` (`async with httpx.AsyncClient(...) as
    client:`) — a `SignedCoreClient` owns exactly such a client
    (`BiffoAPIClient.__init__` builds one whenever `client=` isn't passed),
    so skipping the `async with` here would leak one connection pool per
    call rather than one per request, which is what a caller reading the
    code this replaced would reasonably expect not to happen.
    """
    # `None`-valued entries are dropped, not stringified: `urlencode` alone
    # would render `{"a": None}` as the literal query text `a=None`, sending
    # Core a filter that matches the string "None" instead of omitting the
    # filter — no current caller passes one (verified), but a future
    # optional filter reaching here should omit cleanly, not silently break.
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        full_path = f"{path}?{urlencode(clean_params)}" if clean_params else path
    else:
        full_path = path
    content = _json.dumps(json).encode() if json is not None else None
    kwargs: dict[str, Any] = {"base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    # Not `async with SignedCoreClient(**kwargs) as client:` — the SDK's
    # `BiffoAPIClient.__aenter__` is declared `-> BiffoAPIClient`, not
    # `Self`, so pyright resolves `client`'s type through the base class and
    # loses `raw_request` (only defined on `SignedCoreClient`). A plain
    # try/finally closes the same connection pool `__aexit__` would, without
    # depending on a return-type annotation this module doesn't own.
    client = SignedCoreClient(**kwargs)
    try:
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
