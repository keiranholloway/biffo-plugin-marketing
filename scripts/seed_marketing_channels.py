#!/usr/bin/env python3
"""Seed the default channel taxonomy (#67, #76 increment 1).

Modelled directly on `biffo-plugin-idea-scout`'s `scripts/seed_business_models.py`
— same mechanism, same idempotency shape — **not** on this repo's own
`seed_fan_in_workflow.py`, which posts to a Core orchestration endpoint that has
never existed (#60) and had therefore never once worked. This script instead
hits the plugin's own public generic-CRUD mount, `/api/v1/plugins/marketing/*`,
which is the same mount `marketing_campaign`/`marketing_link`/etc. already use in
production (see `admin_app.py`'s module docstring: "The UI calls
`/api/v1/plugins/marketing/campaigns` directly — one hop") — a live, exercised
mechanism, unlike the fan-in workflow's endpoint.

## Idempotent and re-runnable, without clobbering an instance's own additions

Generic CRUD has no upsert — every POST creates a row — so a blind re-run would
duplicate every channel. This script lists what the tenant already has, then
creates only the default channels whose `key` is missing. Concretely:

- **Running it twice is a no-op the second time**: nothing to create, so
  nothing is created.
- **An instance-added channel is never touched.** This script only ever POSTs
  a channel from `CHANNELS` below whose `key` is absent; it never PATCHes or
  DELETEs an existing row, default or custom. A one-way overwrite would delete
  exactly the customisation this design exists to allow (the same trap
  `shared-files.json` documents at estate level for `mustBeUniform`).
- **An upgrade that adds a new default channel reaches an existing install**:
  re-running this script after `CHANNELS` grows creates only the newly-added
  keys — every previously-seeded or instance-added row is untouched.

## Tenant scoping

`marketing_channel` is a generic-CRUD table (`owner-scoped-tables`), so every
row carries the caller's `tenant_id` automatically — there is no single global
row shared across tenants. Run this once per tenant that needs the default
taxonomy, with an admin token for that tenant. (There is no per-install,
no-token seeding path here: `BiffoPluginBase.on_install()` is never invoked —
see `biffo_plugin_sdk.plugin`'s module docstring — and the SDK's other
self-seeding path, `POST /internal/plugins/me/config/seed`, is for the
plugin's own config, not tenant-scoped table rows.)

Usage:
    CORE_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com \
    ADMIN_BEARER_TOKEN=<a real Cognito admin id/access token> \
    python scripts/seed_marketing_channels.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

_CHANNELS_PATH = "/api/v1/plugins/marketing/channels"

#: The default taxonomy (#67): broad on purpose, digital AND offline/field, and
#: deliberately free of any platform-, vertical- or tenant-specific vocabulary —
#: this plugin installs on every Biffo platform. `key` values are short,
#: lower-case, underscore-separated slugs; the picker/label copy is what an
#: operator actually reads. `ad_platform` is set only where a channel's spend
#: and character limits genuinely belong to one platform's ad product; it is
#: `None` for organic-only, owned, or platform-agnostic channels.
#:
#: `category` values are exactly #67's eleven: search, social, video, email,
#: content, communities, partner, events, trade_press, direct_mail, local_field
#: — asserted by `tests/test_marketing_seed_marketing_channels.py`.
CHANNELS: list[dict[str, Any]] = [
    # ── Search ───────────────────────────────────────────────────────────────
    {
        "key": "search_organic",
        "label": "Organic search (SEO)",
        "motion": "organic",
        "category": "search",
        "ad_platform": None,
    },
    {
        "key": "google_search_paid",
        "label": "Google Search ads",
        "motion": "paid",
        "category": "search",
        "ad_platform": "google",
    },
    # ── Social — organic and paid are separate channels (#67) ──────────────────
    {
        "key": "facebook_organic",
        "label": "Facebook — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "facebook_paid",
        "label": "Facebook ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "meta",
    },
    {
        "key": "instagram_organic",
        "label": "Instagram — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "instagram_paid",
        "label": "Instagram ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "meta",
    },
    {
        "key": "linkedin_organic",
        "label": "LinkedIn — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "linkedin_paid",
        "label": "LinkedIn ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "linkedin",
    },
    {
        "key": "tiktok_organic",
        "label": "TikTok — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "tiktok_paid",
        "label": "TikTok ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "tiktok",
    },
    {
        "key": "x_organic",
        "label": "X (Twitter) — organic",
        "motion": "organic",
        "category": "social",
        "ad_platform": None,
    },
    {
        "key": "x_paid",
        "label": "X (Twitter) ads",
        "motion": "paid",
        "category": "social",
        "ad_platform": "x",
    },
    # ── Video — short-form and long-form (#67) ──────────────────────────────
    {
        "key": "youtube_organic",
        "label": "YouTube — organic (long-form)",
        "motion": "organic",
        "category": "video",
        "ad_platform": None,
    },
    {
        "key": "youtube_paid",
        "label": "YouTube ads",
        "motion": "paid",
        "category": "video",
        "ad_platform": "google",
    },
    {
        "key": "short_form_video_organic",
        "label": "Short-form video — organic (Reels/Shorts/clips)",
        "motion": "organic",
        "category": "video",
        "ad_platform": None,
    },
    # ── Email ────────────────────────────────────────────────────────────────
    {
        "key": "email_owned_list",
        "label": "Email — owned list",
        "motion": "organic",
        "category": "email",
        "ad_platform": None,
    },
    {
        "key": "email_newsletter_sponsorship",
        "label": "Newsletter sponsorship",
        "motion": "paid",
        "category": "email",
        "ad_platform": None,
    },
    # ── Content ──────────────────────────────────────────────────────────────
    {
        "key": "blog_seo_content",
        "label": "Blog / SEO content",
        "motion": "organic",
        "category": "content",
        "ad_platform": None,
    },
    {
        "key": "guest_content",
        "label": "Guest content (guest posts, cross-posting)",
        "motion": "organic",
        "category": "content",
        "ad_platform": None,
    },
    {
        "key": "webinar",
        "label": "Webinar",
        "motion": "organic",
        "category": "content",
        "ad_platform": None,
    },
    # ── Communities ──────────────────────────────────────────────────────────
    {
        "key": "online_communities",
        "label": "Online communities (forums, groups)",
        "motion": "organic",
        "category": "communities",
        "ad_platform": None,
    },
    # ── Partner / affiliate ──────────────────────────────────────────────────
    {
        "key": "partner_comarketing",
        "label": "Partner co-marketing",
        "motion": "organic",
        "category": "partner",
        "ad_platform": None,
    },
    {
        "key": "affiliate_referral",
        "label": "Affiliate / referral",
        "motion": "paid",
        "category": "partner",
        "ad_platform": None,
    },
    # ── Events ───────────────────────────────────────────────────────────────
    {
        "key": "trade_show_presence",
        "label": "Trade show / expo presence",
        "motion": "organic",
        "category": "events",
        "ad_platform": None,
    },
    {
        "key": "own_event",
        "label": "Own event",
        "motion": "organic",
        "category": "events",
        "ad_platform": None,
    },
    {
        "key": "event_sponsorship",
        "label": "Event sponsorship",
        "motion": "paid",
        "category": "events",
        "ad_platform": None,
    },
    # ── Trade press ──────────────────────────────────────────────────────────
    {
        "key": "trade_press_earned",
        "label": "Trade press — earned coverage",
        "motion": "organic",
        "category": "trade_press",
        "ad_platform": None,
    },
    {
        "key": "trade_press_paid",
        "label": "Trade press — paid placement",
        "motion": "paid",
        "category": "trade_press",
        "ad_platform": None,
    },
    # ── Direct mail ──────────────────────────────────────────────────────────
    {
        "key": "direct_mail",
        "label": "Direct mail",
        "motion": "paid",
        "category": "direct_mail",
        "ad_platform": None,
    },
    # ── Local / field ────────────────────────────────────────────────────────
    {
        "key": "local_field_activity",
        "label": "Local / field activity (territory, in-person)",
        "motion": "organic",
        "category": "local_field",
        "ad_platform": None,
    },
    {
        "key": "local_paid_advertising",
        "label": "Local paid advertising (press, radio, out-of-home)",
        "motion": "paid",
        "category": "local_field",
        "ad_platform": None,
    },
]

#: #67's closed set of categories. Kept here, next to `CHANNELS`, so a typo'd
#: category in a new entry fails the test that checks every row against it
#: rather than silently adding a twelfth category nobody decided on.
CATEGORIES: frozenset[str] = frozenset(
    {
        "search",
        "social",
        "video",
        "email",
        "content",
        "communities",
        "partner",
        "events",
        "trade_press",
        "direct_mail",
        "local_field",
    }
)


def _request(method: str, url: str, token: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read() or b"null")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(CHANNELS, indent=2))
        return 0

    api = os.environ.get("CORE_API_URL", "").rstrip("/")
    token = os.environ.get("ADMIN_BEARER_TOKEN", "")
    if not api or not token:
        print("CORE_API_URL and ADMIN_BEARER_TOKEN are both required.", file=sys.stderr)
        return 2

    url = f"{api}{_CHANNELS_PATH}"
    try:
        existing = _request("GET", url, token) or []
    except urllib.error.HTTPError as exc:
        print(f"Could not list channels: {exc.code} {exc.reason}", file=sys.stderr)
        return 1

    # Idempotent by key, and never touches a row it did not just create — see
    # the module docstring's "never clobbers an instance's own additions".
    have = {row.get("key") for row in existing}
    created = 0
    for channel in CHANNELS:
        if channel["key"] in have:
            continue
        try:
            _request("POST", url, token, channel)
        except urllib.error.HTTPError as exc:
            print(f"Failed on {channel['key']}: {exc.code} {exc.reason}", file=sys.stderr)
            return 1
        created += 1

    skipped = len(CHANNELS) - created
    print(f"Created {created} channel(s); {skipped} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
