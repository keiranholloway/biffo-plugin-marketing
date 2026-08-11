"""Deployment configuration this plugin cannot be told directly.

`biffo plugin install` writes `plugins.generated.tf` from a fixed argument list
and regenerates it in full on the next install, so an **installed** plugin has
no channel for instance-specific configuration. Core plugins escape that only
because they are wired by hand in `plugins.core.tf` — which is how agent-runtime
receives its OpenRouter key. Filed upstream as keiranholloway/biffo-template#1456.

Until that closes, the value arrives out of band: Terraform grants read access
to one conventional SSM path built from values the module already has, and an
operator sets it once with `aws ssm put-parameter`. This module is the read
side of that arrangement.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from aws_lambda_powertools import Logger

from . import ssm

logger = Logger(child=True)

#: Set directly in local development and in tests. Takes precedence over SSM so
#: neither has to reach AWS to run.
_DIRECT_ENV = "BIFFO_PUBLIC_BASE_URL"

#: The parameter NAME, injected by this plugin's Terraform. Never the value:
#: Terraform reading the value would put it in state, and a base URL that
#: changes should not need an apply to take effect.
_PARAMETER_ENV = "BIFFO_PUBLIC_BASE_URL_PARAMETER"

#: Resolved once per Lambda container, not per request. A cold start pays one
#: SSM call; every warm invocation pays none. `None` means "not yet looked up",
#: which is distinct from `""` meaning "looked up, and there is nothing there" —
#: without that distinction an unconfigured deployment would re-query SSM on
#: every single request and fail slowly rather than quickly.
_cached: str | None = None


def public_base_url() -> str:
    """This deployment's public origin, or `""` when it is not configured.

    Returning empty rather than raising is deliberate: the caller turns it into
    a 503 that says what is missing. A link minted against a missing base URL
    would be published as a bare `/c/<token>`, which fails silently in whatever
    feed it was pasted into rather than at the moment it was created.

    A call that could not even reach SSM (issue #25) also returns `""` **for
    this call only** — see `_from_ssm` — without caching that as the answer,
    so a transient blip on one cold start does not read as "not configured"
    for the rest of that container's warm life.
    """
    global _cached

    if _cached is not None:
        return _cached

    direct = os.environ.get(_DIRECT_ENV, "").strip()
    if direct:
        _cached = direct.rstrip("/")
        return _cached

    parameter = os.environ.get(_PARAMETER_ENV, "").strip()
    if not parameter:
        logger.warning(
            "No public base URL configured: neither %s nor %s is set",
            _DIRECT_ENV,
            _PARAMETER_ENV,
        )
        _cached = ""
        return _cached

    result = _from_ssm(parameter)
    if result is None:
        # Could not ask (issue #25) — fail only this call. `_cached` stays
        # `None` so the next call retries rather than repeating a non-answer.
        return ""
    _cached = result
    return _cached


def _from_ssm(parameter: str) -> str | None:
    """Fetch `parameter`, normalised for a base URL.

    Delegates the "genuinely absent vs merely unreachable" classification to
    `ssm.read_parameter` (issue #25) — see that module's docstring for why the
    two cannot be collapsed into one `""`. Returns:

    - the value, trailing-slash stripped, when SSM answers.
    - `""` when SSM confirms the parameter does not exist — safe to cache.
    - `None` when the call itself failed — must NOT be cached; see
      `public_base_url` for what the caller does with that.
    """
    value = ssm.read_parameter(parameter)
    if value is None:
        return None
    return value.strip().rstrip("/")


def reset_cache() -> None:
    """Forget the resolved value. For tests only."""
    global _cached
    _cached = None


def public_base_url_for(origin: str | None, referer: str | None) -> str:
    """This deployment's public origin, preferring what the caller actually used.

    **Why the request, and not configuration.** A plugin's Terraform sets
    environment on the plugin's OWN Lambda, but an ``admin_ingress`` app runs on
    the SHARED PLUGIN HOST — a different function, with a different role and a
    different environment. So `BIFFO_PUBLIC_BASE_URL_PARAMETER` and the SSM read
    grant both landed somewhere this code never executes, and
    ``public_base_url()`` correctly reported "not configured" forever. That gap
    is keiranholloway/biffo-template#1456.

    The origin an operator is looking at IS the public base URL, by definition —
    they loaded this panel from it. CloudFront's `AllViewerExceptHostHeader`
    policy drops `Host` but forwards every other viewer header, so `Origin` (and
    `Referer` as a fallback) arrive intact.

    **Safe because of what this value is used for.** It composes only the URL
    *shown* to the operator. The link's stored ``destination_url`` is built from
    the campaign's own destination, server-side, and is never derived from a
    header — so a forged `Origin` can at worst show a caller a link on the
    origin they already control, and cannot redirect anyone anywhere.

    Falls back to the configured value, which is still right for any caller that
    sends neither header.
    """
    for candidate in (origin, referer):
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return public_base_url()
