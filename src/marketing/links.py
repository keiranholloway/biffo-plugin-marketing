"""Link minting — the attribution spine's plugin half (M2).

Today ``utm_campaign`` is a string somebody types into a URL. Nothing
guarantees it corresponds to a real campaign, and nothing ever will while the
campaign exists only in someone's head. **This module makes the campaign's own
id the value that travels**, so `utm_campaign` becomes a foreign key by
construction rather than a hopeful string. That single change is what turns "we
store UTMs" into "everything is a campaign and is trackable".

## Resolved at mint time, deliberately

``destination_url`` is composed **once, here**, and stored on the row. The
public redirect (Core's ``public_clicks.py``) never composes or edits a URL — it
sends the caller to exactly what was stored. So editing a campaign's destination
tomorrow cannot rewrite a link that was published yesterday, and a link that is
already out in the world keeps meaning what it meant when it was minted. A link
is a promise made to a stranger; it should not change under them.

## The functions here are pure on purpose

Minting is the part with a security property (token entropy) and a correctness
property (UTM composition), and both are far easier to test as functions over
values than as assertions about an HTTP handler. The route in ``admin_app`` is
the thin part.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: 32 bytes of entropy, url-safe. The token is the *whole* of an anonymous
#: caller's authority at the public redirect — there is no account, no session
#: and no second factor behind it — so it is sized to be unguessable rather than
#: tidy. ``token_urlsafe(32)`` yields 43 characters, inside the column's 64.
_TOKEN_BYTES = 32

#: Paid and organic are the two things a channel plan ranks separately, and the
#: distinction has to survive into the analytics or the results dashboard cannot
#: tell bought traffic from earned. Stored as the UTM medium rather than
#: inferred later from spend records, which may not exist.
_MEDIUM_PAID = "paid"
_MEDIUM_ORGANIC = "organic"


def mint_token() -> str:
    """A fresh, unguessable link token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def tracked_url(base_url: str, token: str) -> str:
    """The URL an operator actually publishes.

    ``base_url`` is the instance's public origin; ``c/*`` is routed to the Core
    API by the shared CloudFront distribution (biffo-template#1443), which is
    why this is a short path on the marketing site rather than an API hostname.
    A link people paste into social posts should look like it belongs to the
    brand, not to a gateway.
    """
    return f"{base_url.rstrip('/')}/c/{token}"


def destination_with_utms(
    destination_url: str,
    *,
    campaign_id: str,
    channel: str,
    variant: str | None = None,
    is_paid: bool = False,
) -> str:
    """``destination_url`` with this link's attribution stamped onto it.

    **``utm_campaign`` is the campaign's id**, not its name. A name is edited,
    duplicated and translated; an id joins. This is the whole reason the plugin
    owns the campaign record rather than tracking someone else's spreadsheet.

    Existing query parameters on the destination are preserved — a campaign may
    legitimately point at a URL that already carries its own — but the five
    ``utm_*`` keys this function owns are **overwritten** rather than appended.
    Two values for one key is not a merge, it is an ambiguity every analytics
    tool resolves differently, and the whole point here is that the number is
    not arguable.
    """
    parts = urlsplit(destination_url)
    # keep_blank_values so a destination's own `?foo=` survives the round trip
    # rather than being silently dropped on its way through us.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _ours(k)]

    query.append(("utm_source", channel))
    query.append(("utm_medium", _MEDIUM_PAID if is_paid else _MEDIUM_ORGANIC))
    query.append(("utm_campaign", campaign_id))
    if variant:
        query.append(("utm_content", variant))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _ours(key: str) -> bool:
    """Whether this plugin claims the parameter, and will therefore replace it."""
    return key in ("utm_source", "utm_medium", "utm_campaign", "utm_content")
