"""The default channel taxonomy (#67, #76 increment 1), and the seeder's
idempotency/preservation guarantees — asserted rather than trusted, same
rationale as `test_marketing_seed_fan_in_workflow.py` and idea-scout's
`test_idea_scout_seed_business_models.py`.

Three things this suite exists to prove, because the issue is explicit that
nothing else proves them:

1. Seeding twice is a no-op.
2. Re-seeding never touches (never PATCHes, never DELETEs) a row it did not
   just create itself — which is what keeps an instance-added channel alive
   across an upgrade that adds new default channels.
3. Every seeded `key` is short enough, by construction, that the #75 overflow
   (a 121-character agent-generated channel name landing in a `String(64)`
   column) cannot recur through a channel referenced by key.
"""

from __future__ import annotations

import re
from typing import Any

from _scripts import load_script

seed = load_script("seed_marketing_channels")
CHANNELS = seed.CHANNELS
CATEGORIES = seed.CATEGORIES

#: #67's own list, verbatim, so a typo'd or invented category in `CHANNELS`
#: fails here rather than silently becoming a twelfth category nobody decided
#: on.
_EXPECTED_CATEGORIES = frozenset(
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

#: The categories #67 calls "offline/field" — the breadth this taxonomy exists
#: to cover beyond a digital-only list.
_OFFLINE_FIELD_CATEGORIES = frozenset({"events", "trade_press", "direct_mail", "local_field"})

#: A digital sample, so "covers digital and offline/field" is checked as a
#: conjunction rather than assumed from the offline half alone.
_DIGITAL_CATEGORIES = frozenset({"search", "social", "video", "email"})


# ── The taxonomy itself ──────────────────────────────────────────────────────


def test_categories_are_exactly_hashtag_67s_set() -> None:
    assert CATEGORIES == _EXPECTED_CATEGORIES


def test_every_channel_declares_a_known_category() -> None:
    for channel in CHANNELS:
        assert channel["category"] in CATEGORIES, channel["key"]


def test_taxonomy_covers_digital_and_offline_field_breadth() -> None:
    """The load-bearing coverage test: #67 exists specifically because a
    digital-only list would exclude the routes franchise (and other
    field-heavy) operators convert through most. Both halves must be
    present, not just declared as valid categories."""
    used = {channel["category"] for channel in CHANNELS}
    assert _OFFLINE_FIELD_CATEGORIES <= used, used
    assert _DIGITAL_CATEGORIES <= used, used


def test_every_channel_has_a_motion_of_organic_or_paid() -> None:
    for channel in CHANNELS:
        assert channel["motion"] in ("organic", "paid"), channel["key"]


def test_motion_lives_on_the_channel_not_only_on_a_recommendation() -> None:
    """#67's settled design: LinkedIn-organic and LinkedIn-paid (and the same
    pair for every other network with both) are separate channels, not one
    channel with a motion asserted elsewhere."""
    keys = {channel["key"] for channel in CHANNELS}
    for network in ("facebook", "instagram", "linkedin", "tiktok", "x"):
        assert f"{network}_organic" in keys, network
        assert f"{network}_paid" in keys, network


def test_every_channel_has_a_non_empty_operator_facing_label() -> None:
    for channel in CHANNELS:
        assert channel["label"].strip(), channel["key"]


def test_ad_platform_is_either_none_or_a_real_lowercase_slug() -> None:
    for channel in CHANNELS:
        platform = channel["ad_platform"]
        assert platform is None or (platform == platform.lower() and platform.strip())


def test_taxonomy_has_no_duplicate_keys() -> None:
    keys = [channel["key"] for channel in CHANNELS]
    assert len(set(keys)) == len(keys)


def test_taxonomy_contains_nothing_platform_or_vertical_specific() -> None:
    """This plugin installs on every Biffo platform. Franchise operators are
    the motivating example for breadth (#67), not a reason to encode
    franchise vocabulary — the same leak class as #46 (a product-specific
    Cognito group reaching a plugin meant for all of them), one schema level
    up."""
    banned = ("franchise", "tabsii", "biffo")
    for channel in CHANNELS:
        haystack = f"{channel['key']} {channel['label']}".lower()
        for word in banned:
            assert word not in haystack, f"{channel['key']!r} mentions {word!r}"


# ── Keys: short and stable by construction (the #75 guard) ──────────────────


def test_keys_are_short_stable_machine_safe_slugs() -> None:
    """`key` is a `String(64)` column — the same width `marketing_link.channel`
    was when a 121-character agent-generated name overflowed it (#75). Every
    seeded key is asserted well under half that ceiling, so the overflow class
    cannot recur through a channel referenced by key rather than by whatever
    string an agent chose to write."""
    for channel in CHANNELS:
        key = channel["key"]
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key
        assert len(key) <= 32, f"{key!r} is {len(key)} chars — nowhere near #75's 121"


def test_the_75_case_itself_cannot_occur() -> None:
    """The exact failure from #75, replayed against this table's shape rather
    than against `marketing_link` directly (that migration is #76 increment
    2's job): the longest key in the default taxonomy is nowhere near a
    121-character overflow of a 64-character column."""
    overflowing_name = (
        "Google Search ads (non-brand: terms like 'franchise management "
        "software UK', 'franchise operations pricing per location')"
    )
    assert len(overflowing_name) == 121
    assert all(len(channel["key"]) < len(overflowing_name) for channel in CHANNELS)


# ── The seed script's behaviour ──────────────────────────────────────────────


class _FakeCore:
    """A minimal in-memory stand-in for the tenant's `marketing_channel` rows,
    behind the exact seam (`seed._request`) the real script calls through.
    Records every method used, so a test can assert the script never issues a
    PATCH or DELETE — the structural half of "never clobbers an instance's own
    additions"."""

    def __init__(self, initial: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = [dict(row) for row in (initial or [])]
        self.methods_used: list[str] = []

    def request(self, method: str, url: str, token: str, body: dict | None = None) -> Any:
        self.methods_used.append(method)
        if method == "GET":
            return list(self.rows)
        if method == "POST":
            assert body is not None
            row = {**body, "id": f"row-{len(self.rows)}"}
            self.rows.append(row)
            return row
        raise AssertionError(f"unexpected method {method!r} — the seeder must never call this")


def _run_seed(monkeypatch, fake: _FakeCore) -> int:
    monkeypatch.setattr(seed, "_request", fake.request)
    monkeypatch.setenv("CORE_API_URL", "https://example.test")
    monkeypatch.setenv("ADMIN_BEARER_TOKEN", "a-real-token")
    monkeypatch.setattr("sys.argv", ["seed_marketing_channels.py"])
    return seed.main()


def test_seeding_from_empty_creates_every_default_channel(monkeypatch) -> None:
    fake = _FakeCore()
    rc = _run_seed(monkeypatch, fake)
    assert rc == 0
    assert {row["key"] for row in fake.rows} == {c["key"] for c in CHANNELS}


def test_seeding_twice_is_a_no_op(monkeypatch) -> None:
    fake = _FakeCore()
    _run_seed(monkeypatch, fake)
    first_run_rows = len(fake.rows)

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0
    assert len(fake.rows) == first_run_rows, "second run must create nothing new"


def test_reseeding_preserves_an_instance_added_channel(monkeypatch) -> None:
    """The other half of the idempotency requirement: an instance that added
    its own channel (not in the default taxonomy) must still have it, byte
    for byte, after a re-seed — including after an upgrade that adds a new
    default channel this instance never had before."""
    custom_channel = {
        "key": "regional_billboard_ads",
        "label": "Regional billboard advertising",
        "motion": "paid",
        "category": "local_field",
        "ad_platform": None,
        "id": "existing-custom-row",
    }
    fake = _FakeCore(initial=[custom_channel])

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0
    assert custom_channel in fake.rows, "the instance-added channel must survive re-seeding"
    assert "PATCH" not in fake.methods_used
    assert "PUT" not in fake.methods_used
    assert "DELETE" not in fake.methods_used


def test_an_upgrade_adding_a_default_channel_reaches_an_existing_install(monkeypatch) -> None:
    """Simulates the scenario #67/#76 name explicitly: an install that already
    ran this script, then a later plugin version adds one more default
    channel to `CHANNELS`. Re-running must create only the new one."""
    already_seeded = [{**c, "id": f"seeded-{c['key']}"} for c in CHANNELS]
    fake = _FakeCore(initial=already_seeded)

    new_channel = {
        "key": "podcast_sponsorship",
        "label": "Podcast sponsorship",
        "motion": "paid",
        "category": "content",
        "ad_platform": None,
    }
    monkeypatch.setattr(seed, "CHANNELS", [*CHANNELS, new_channel])

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0
    keys_after = {row["key"] for row in fake.rows}
    assert "podcast_sponsorship" in keys_after
    assert keys_after == {c["key"] for c in CHANNELS} | {"podcast_sponsorship"}


def test_missing_credentials_is_a_clear_failure_not_a_silent_noop(monkeypatch) -> None:
    monkeypatch.delenv("CORE_API_URL", raising=False)
    monkeypatch.delenv("ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["seed_marketing_channels.py"])
    assert seed.main() == 2


def test_dry_run_prints_without_network(capsys, monkeypatch) -> None:
    """`--dry-run` must not require `CORE_API_URL`/a token — it is how an
    operator reviews the taxonomy before touching an environment."""
    monkeypatch.delenv("CORE_API_URL", raising=False)
    monkeypatch.delenv("ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["seed_marketing_channels.py", "--dry-run"])

    assert seed.main() == 0
    assert "google_search_paid" in capsys.readouterr().out


def test_it_hits_the_same_public_crud_mount_the_other_marketing_tables_use() -> None:
    """Pinned rather than assumed. `admin_app.py`'s own module docstring
    records that the UI reaches `marketing_campaign` etc. at
    `/api/v1/plugins/marketing/campaigns` directly, and that mount is live in
    production — unlike `seed_fan_in_workflow.py`'s orchestration endpoint,
    which had never once existed (#60). This script targets the same,
    already-proven mount, not a new or guessed one."""
    assert seed._CHANNELS_PATH == "/api/v1/plugins/marketing/channels"
