"""Link minting (M2) — the attribution spine's plugin half.

These are the two properties the whole feature rests on: a token nobody can
guess, and a destination whose `utm_campaign` **is** the campaign's id. Both are
tested as functions over values rather than through the HTTP handler, because
that is where the properties actually live.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from marketing.links import destination_with_utms, mint_token, tracked_url

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ab"


def test_a_token_is_unguessable_and_fits_its_column() -> None:
    """The token is the whole of an anonymous caller's authority.

    There is no account behind it and no second factor, so entropy is the only
    thing protecting a campaign's traffic from being forged or enumerated.
    """
    token = mint_token()

    # token_urlsafe(32) is 43 chars; marketing_link.token is String(64).
    assert 40 <= len(token) <= 64
    assert token.strip() == token


def test_every_token_is_distinct() -> None:
    """A repeated token would attribute one campaign's clicks to another.

    1000 draws is not a statistical proof of entropy — it is a guard against
    the failure that actually happens, which is somebody replacing
    `secrets.token_urlsafe` with something seeded or sequential.
    """
    tokens = {mint_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_the_published_url_routes_through_the_c_path() -> None:
    """`c/*` is routed to the Core API by the shared CloudFront distribution."""
    assert tracked_url("https://dev.tabsii.com", "abc123") == "https://dev.tabsii.com/c/abc123"


def test_a_trailing_slash_on_the_base_does_not_double_up() -> None:
    assert tracked_url("https://dev.tabsii.com/", "abc") == "https://dev.tabsii.com/c/abc"


def test_utm_campaign_is_the_campaign_id_not_its_name() -> None:
    """**The point of the entire milestone.**

    A name is edited, duplicated and translated; an id joins. Carrying the id is
    what makes attribution a foreign key by construction rather than a string
    that happens to match.
    """
    url = destination_with_utms(
        "https://tabsii.com/intake/demo", campaign_id=_CAMPAIGN, channel="linkedin"
    )
    assert parse_qs(urlsplit(url).query)["utm_campaign"] == [_CAMPAIGN]


def test_paid_and_organic_are_distinguishable_in_the_medium() -> None:
    """The results dashboard cannot tell bought traffic from earned otherwise.

    Recorded as the UTM medium rather than inferred later from spend records,
    which for an organic campaign do not exist at all.
    """
    organic = destination_with_utms(
        "https://tabsii.com/x", campaign_id=_CAMPAIGN, channel="instagram"
    )
    paid = destination_with_utms(
        "https://tabsii.com/x", campaign_id=_CAMPAIGN, channel="instagram", is_paid=True
    )

    assert parse_qs(urlsplit(organic).query)["utm_medium"] == ["organic"]
    assert parse_qs(urlsplit(paid).query)["utm_medium"] == ["paid"]


def test_the_variant_travels_as_utm_content_when_there_is_one() -> None:
    with_variant = destination_with_utms(
        "https://tabsii.com/x", campaign_id=_CAMPAIGN, channel="email", variant="b"
    )
    without = destination_with_utms("https://tabsii.com/x", campaign_id=_CAMPAIGN, channel="email")

    assert parse_qs(urlsplit(with_variant).query)["utm_content"] == ["b"]
    assert "utm_content" not in parse_qs(urlsplit(without).query)


def test_the_destinations_own_query_parameters_survive() -> None:
    """A campaign may legitimately point at a URL that already carries params.

    Dropping them would break the destination page rather than the tracking,
    which is the more expensive half to debug.
    """
    url = destination_with_utms(
        "https://tabsii.com/intake/demo?ref=partner&plan=growth",
        campaign_id=_CAMPAIGN,
        channel="linkedin",
    )
    query = parse_qs(urlsplit(url).query)

    assert query["ref"] == ["partner"]
    assert query["plan"] == ["growth"]
    assert query["utm_campaign"] == [_CAMPAIGN]


def test_a_utm_already_on_the_destination_is_replaced_not_duplicated() -> None:
    """Two values for one key is an ambiguity, not a merge.

    Every analytics tool resolves a repeated `utm_campaign` differently, and the
    entire point of this milestone is that the number is not arguable. So the
    five keys this plugin owns are overwritten.
    """
    url = destination_with_utms(
        "https://tabsii.com/x?utm_campaign=hand-typed&utm_source=stale",
        campaign_id=_CAMPAIGN,
        channel="linkedin",
    )
    query = parse_qs(urlsplit(url).query)

    assert query["utm_campaign"] == [_CAMPAIGN], "the hand-typed value must not survive"
    assert query["utm_source"] == ["linkedin"]
    assert len(query["utm_campaign"]) == 1, "a repeated key is an ambiguity, not a merge"


def test_the_fragment_is_preserved() -> None:
    """A destination anchoring to a section keeps doing so."""
    url = destination_with_utms(
        "https://tabsii.com/x#pricing", campaign_id=_CAMPAIGN, channel="linkedin"
    )
    assert urlsplit(url).fragment == "pricing"
