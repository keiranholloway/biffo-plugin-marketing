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

import io
import re
import urllib.error
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


# ── publish_url (#103b) ───────────────────────────────────────────────────


def test_every_channel_declares_a_publish_url_key() -> None:
    """`None` is a valid, deliberate value (most channels have no single
    composer) — the key must still be present so a caller can tell "checked,
    found none" from "this row predates the field"."""
    for channel in CHANNELS:
        assert "publish_url" in channel, channel["key"]


def test_publish_url_is_either_none_or_a_stable_https_link() -> None:
    """A wrong link is worse than no link (#103b's own instruction) — every
    non-`None` value must at least be shaped like a real, stable third-party
    URL: `https://`, no query-string credential/token, no bare IP."""
    for channel in CHANNELS:
        url = channel["publish_url"]
        if url is None:
            continue
        assert url.startswith("https://"), channel["key"]
        assert "@" not in url, f"{channel['key']!r} looks like it embeds credentials"


def test_trade_press_earned_has_no_publish_url() -> None:
    """#103's own framing: `trade_press_earned` is a pitch to a publication,
    not a composer — the channel most likely to be reached for a deep link
    that must not exist."""
    trade_press_earned = next(c for c in CHANNELS if c["key"] == "trade_press_earned")
    assert trade_press_earned["publish_url"] is None


def test_at_least_one_channel_per_named_platform_has_a_publish_url() -> None:
    """The load-bearing coverage check for #103b: enough of the taxonomy
    actually carries a link that the feature is not vacuously true. Every key
    here is asserted present above in `CHANNELS`."""
    have_links = {c["key"] for c in CHANNELS if c["publish_url"] is not None}
    expected = {"google_search_paid", "linkedin_organic", "linkedin_paid", "tiktok_organic"}
    assert expected <= have_links


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
    Records every method used and every PATCH body, so a test can assert both
    halves of "never clobbers an instance's own additions": the script never
    DELETEs or PUTs at all, and the only PATCH it may issue is a fill-if-null
    backfill of a DEFAULT channel's never-set field (#103b)."""

    def __init__(
        self, initial: list[dict[str, Any]] | None = None, *, fail_on: str | None = None
    ) -> None:
        self.rows: list[dict[str, Any]] = [dict(row) for row in (initial or [])]
        self.methods_used: list[str] = []
        #: (row_id, body) for every PATCH, so a test can assert exactly which
        #: rows were touched and with what — not merely that a PATCH happened.
        self.patches: list[tuple[str, dict]] = []
        #: A method ("GET"/"POST"/"PATCH") that should raise
        #: `urllib.error.HTTPError` instead of completing, standing in for
        #: Core rejecting or being unreachable for that call.
        self._fail_on = fail_on

    def request(self, method: str, url: str, token: str, body: dict | None = None) -> Any:
        self.methods_used.append(method)
        if method == self._fail_on:
            hdrs: Any = {}
            raise urllib.error.HTTPError(
                url, 502, "Bad Gateway", hdrs, io.BytesIO(b"upstream refused")
            )
        if method == "GET":
            return list(self.rows)
        if method == "POST":
            assert body is not None
            row = {**body, "id": f"row-{len(self.rows)}"}
            self.rows.append(row)
            return row
        if method == "PATCH":
            assert body is not None
            row_id = url.rsplit("/", 1)[-1]
            self.patches.append((row_id, dict(body)))
            for row in self.rows:
                if row.get("id") == row_id:
                    row.update(body)
                    return row
            raise AssertionError(f"PATCH against unknown row {row_id!r}")
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
    assert "PUT" not in fake.methods_used
    assert "DELETE" not in fake.methods_used
    # The seeder may now PATCH — but only to fill a DEFAULT channel's
    # never-set field (#103b). It must never touch an instance-added row,
    # whose key is absent from CHANNELS entirely.
    assert all(row_id != custom_channel["id"] for row_id, _ in fake.patches), (
        "an instance-added channel must never be PATCHed"
    )


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


def test_a_failed_list_call_reports_the_core_error_and_exits_one(monkeypatch, capsys) -> None:
    """`main()`'s FIRST `except urllib.error.HTTPError` — Core refusing (or
    being unreachable for) the initial GET that lists existing channels, the
    call every idempotency decision below it depends on. Must report what
    Core said and exit 1, not raise the raw `HTTPError` out of `main()`."""
    fake = _FakeCore(fail_on="GET")

    rc = _run_seed(monkeypatch, fake)

    assert rc == 1
    err = capsys.readouterr().err
    assert "Could not list channels" in err
    assert "502" in err


def test_a_failed_create_reports_which_channel_and_exits_one(monkeypatch, capsys) -> None:
    """`main()`'s SECOND `except urllib.error.HTTPError` — a POST creating a
    still-missing default channel failing. Distinct from the GET failure
    above: the listing succeeded, so the script knows exactly which channel
    it was trying to create, and the error must name it."""
    fake = _FakeCore(fail_on="POST")

    rc = _run_seed(monkeypatch, fake)

    assert rc == 1
    err = capsys.readouterr().err
    assert "Failed on" in err
    assert "502" in err
    assert CHANNELS[0]["key"] in err


def test_a_failed_backfill_patch_is_reported_and_skipped_not_fatal(monkeypatch, capsys) -> None:
    """`_backfill_null_fields`'s own `except urllib.error.HTTPError` — a
    single channel's fill-if-null PATCH failing must not abort the whole
    seeding run (every channel already exists at this point; the run's own
    job is done). It logs the failure and moves on to the next channel,
    rather than losing every OTHER channel's backfill over one PATCH."""
    seeded = [
        {**channel, "id": f"row-{i}", "publish_url": None} for i, channel in enumerate(CHANNELS)
    ]
    fake = _FakeCore(initial=seeded, fail_on="PATCH")

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0, "a backfill failure must not fail the whole run"
    err = capsys.readouterr().err
    assert "Could not backfill" in err
    assert "502" in err
    assert fake.patches == [], "the fake raises before recording the PATCH as applied"


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


def test_a_field_added_to_a_default_channel_reaches_an_already_seeded_install(
    monkeypatch,
) -> None:
    """The gap this backfill closes, and the reason the feature reading it
    shipped dead.

    `publish_url` (#103b) was added to `CHANNELS` after every existing
    instance had already seeded its taxonomy. Because the seeder is
    insert-only — it skips any key it already has — those rows kept NULL for
    ever: the migration added the column, all 32 rows held NULL, and the UI
    reading it rendered no links while its code, its tests and its CI all
    passed. Observed exactly that on tabsii dev: 0 of 32 rows populated.
    """
    seeded = [
        {**channel, "id": f"row-{i}", "publish_url": None} for i, channel in enumerate(CHANNELS)
    ]
    fake = _FakeCore(initial=seeded)

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0
    expected = {c["key"]: c["publish_url"] for c in CHANNELS if c.get("publish_url")}
    assert expected, "this test is vacuous unless some default channel has a publish_url"
    for row in fake.rows:
        if row["key"] in expected:
            assert row["publish_url"] == expected[row["key"]], (
                f"{row['key']} should have been backfilled"
            )


def test_backfill_never_overwrites_a_value_an_operator_already_set(monkeypatch) -> None:
    """Fill-if-null, never overwrite — the same rule that makes the seeder
    insert-only in the first place, applied at field level rather than row
    level. An operator who points a channel at their own composer keeps it."""
    target = next(c for c in CHANNELS if c.get("publish_url"))
    operator_value = "https://intranet.example.test/our-own-publishing-tool"
    seeded = [
        {
            **channel,
            "id": f"row-{i}",
            "publish_url": operator_value if channel["key"] == target["key"] else None,
        }
        for i, channel in enumerate(CHANNELS)
    ]
    fake = _FakeCore(initial=seeded)

    rc = _run_seed(monkeypatch, fake)

    assert rc == 0
    kept = next(r for r in fake.rows if r["key"] == target["key"])
    assert kept["publish_url"] == operator_value, "an operator's own value must survive"
    assert all(row_id != kept["id"] for row_id, _ in fake.patches), (
        "a row whose field is already set must not be PATCHed at all"
    )


def test_backfill_leaves_a_channel_with_no_publish_url_alone(monkeypatch) -> None:
    """A default channel whose `publish_url` is deliberately None — a pitch to
    a publication, not a composer — must not be PATCHed with a null, which
    would be a write that changes nothing and muddies the audit trail."""
    seeded = [
        {**channel, "id": f"row-{i}", "publish_url": None} for i, channel in enumerate(CHANNELS)
    ]
    fake = _FakeCore(initial=seeded)

    _run_seed(monkeypatch, fake)

    no_url_keys = {c["key"] for c in CHANNELS if c.get("publish_url") is None}
    by_id = {r["id"]: r for r in fake.rows}
    for row_id, _ in fake.patches:
        assert by_id[row_id]["key"] not in no_url_keys, (
            "a channel with no publish_url must never be PATCHed"
        )
