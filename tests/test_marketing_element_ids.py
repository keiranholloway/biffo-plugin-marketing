"""Element identity + operator selection (issue #145) — the machinery behind
partial artefact approval, tested independently of the HTTP layer.

`approve_artefact_route` used to flip an artefact's whole body `proposed ->
approved` in one move, but every artefact is a LIST — an operator who wanted
8 of 9 channel recommendations had to accept the ninth or reject the whole
run. `pipeline.with_element_ids`/`known_element_ids`/`selected_body`/
`source_urls_from_body` are what make a partial approval real: a stable id
per element, a way to narrow a body to an approved subset, and a way to
recompute the closed citation set that subset actually supports. See
`tests/test_marketing_pipeline_routes.py`'s selection section for the same
behaviour proven end to end over the HTTP routes.
"""

from __future__ import annotations

from marketing.pipeline import (
    ELEMENT_LIST_KEYS,
    known_element_ids,
    selected_body,
    source_urls_from_body,
    with_element_ids,
)


def _source(url: str) -> dict[str, str]:
    return {"url": url, "note": "n"}


# ── with_element_ids ──────────────────────────────────────────────────────────


def test_with_element_ids_stamps_every_known_list() -> None:
    """One id per element, across every list this plugin's artefact kinds
    actually use — proven for all five at once rather than one at a time, so
    a future kind added to `ELEMENT_LIST_KEYS` without a matching branch here
    is caught the moment this test is extended, not silently skipped."""
    body = {
        "summary": "irrelevant to ids",
        "findings": [{"signal": "s", "sources": [_source("https://a.example")]}],
        "segments": [{"name": "seg", "sources": [_source("https://b.example")]}],
        "pillars": [{"pillar": "p", "sources": [_source("https://c.example")]}],
        "ctas": [{"text": "cta", "sources": [_source("https://d.example")]}],
        "channels": [{"channel_key": "x", "sources": [_source("https://e.example")]}],
    }

    stamped = with_element_ids(body)

    for key in ELEMENT_LIST_KEYS:
        (element,) = stamped[key]
        assert isinstance(element.get("id"), str) and element["id"], key
    # Non-element keys pass through untouched.
    assert stamped["summary"] == "irrelevant to ids"


def test_with_element_ids_assigns_distinct_ids() -> None:
    body = {"channels": [{"channel_key": "a"}, {"channel_key": "b"}, {"channel_key": "c"}]}

    stamped = with_element_ids(body)

    ids = [c["id"] for c in stamped["channels"]]
    assert len(set(ids)) == 3


def test_with_element_ids_is_not_positional() -> None:
    """The design explicitly settled against a positional id (#94's class):
    reordering the same elements must not just "happen" to produce the same
    ids by coincidence of list position — each element's id must travel with
    its own content, not its index. Proven here by shuffling the list and
    checking the id for a given `channel_key` is unaffected by where it sits."""
    original = with_element_ids({"channels": [{"channel_key": "a"}, {"channel_key": "b"}]})
    by_key = {c["channel_key"]: c["id"] for c in original["channels"]}

    reordered = {"channels": [dict(c) for c in reversed(original["channels"])]}
    # Re-stamping (idempotency) must not change an id that is already set,
    # regardless of position in the list.
    restamped = with_element_ids(reordered)

    for channel in restamped["channels"]:
        assert channel["id"] == by_key[channel["channel_key"]]


def test_with_element_ids_is_idempotent() -> None:
    once = with_element_ids({"channels": [{"channel_key": "a"}]})
    twice = with_element_ids(once)

    assert once["channels"][0]["id"] == twice["channels"][0]["id"]


def test_with_element_ids_is_a_noop_on_a_pending_placeholder_body() -> None:
    """Every "body" write in this plugin routes through this helper (issue
    #145), including the pending-state placeholders
    (`{"channel_taxonomy": ..., "allowed_source_urls": [...]}`) that carry no
    selectable elements at all. Must not raise, and must not invent an
    `ELEMENT_LIST_KEYS` entry that was never there."""
    placeholder = {"channel_taxonomy": {"x": "organic"}, "allowed_source_urls": ["https://a"]}

    assert with_element_ids(placeholder) == placeholder


# ── known_element_ids ──────────────────────────────────────────────────────────


def test_known_element_ids_collects_across_every_list() -> None:
    body = with_element_ids(
        {
            "segments": [{"name": "s"}],
            "pillars": [{"pillar": "p"}],
            "ctas": [{"text": "c"}],
        }
    )

    ids = known_element_ids(body)

    assert ids == {body["segments"][0]["id"], body["pillars"][0]["id"], body["ctas"][0]["id"]}


def test_known_element_ids_of_a_body_with_no_ids_is_empty() -> None:
    """A legacy body — persisted before #145, never re-stamped — has no `id`
    on anything. `approve_artefact_route`'s unknown-id check must reject
    every requested element_id against a body like this, not raise."""
    legacy = {"segments": [{"name": "s", "sources": []}]}

    assert known_element_ids(legacy) == set()


# ── selected_body ────────────────────────────────────────────────────────────


def test_selected_body_of_none_returns_everything_unfiltered() -> None:
    """NULL/None selection means "everything" — the backwards-compatible
    reading every artefact approved before #145 gets (issue #145)."""
    body = with_element_ids({"channels": [{"channel_key": "a"}, {"channel_key": "b"}]})

    assert selected_body(body, None) == body


def test_selected_body_narrows_to_the_requested_ids() -> None:
    body = with_element_ids(
        {"channels": [{"channel_key": "a"}, {"channel_key": "b"}, {"channel_key": "c"}]}
    )
    keep = body["channels"][0]["id"]

    narrowed = selected_body(body, [keep])

    assert [c["channel_key"] for c in narrowed["channels"]] == ["a"]


def test_selected_body_drops_a_legacy_element_with_no_id() -> None:
    """An element with no `id` cannot have been named in a selection, so it
    is dropped rather than kept when a non-None selection is applied."""
    body = {"channels": [{"channel_key": "legacy"}]}  # never stamped

    narrowed = selected_body(body, ["some-id"])

    assert narrowed["channels"] == []


def test_selected_body_leaves_non_element_keys_untouched() -> None:
    body = with_element_ids({"summary": "kept verbatim", "findings": [{"signal": "s"}]})

    narrowed = selected_body(body, [])

    assert narrowed["summary"] == "kept verbatim"
    assert narrowed["findings"] == []


# ── source_urls_from_body ────────────────────────────────────────────────────


def test_source_urls_from_body_collects_and_dedupes_in_first_seen_order() -> None:
    body = {
        "segments": [
            {"sources": [_source("https://b.example"), _source("https://a.example")]},
            {"sources": [_source("https://a.example")]},  # duplicate, dropped
        ]
    }

    assert source_urls_from_body(body) == ["https://b.example", "https://a.example"]


def test_source_urls_from_body_narrows_when_a_finding_is_dropped() -> None:
    """The straightforward case: a URL cited only by a dropped element is not
    in the result once the body has been narrowed."""
    body = with_element_ids(
        {
            "findings": [
                {"signal": "kept", "sources": [_source("https://kept.example")]},
                {"signal": "dropped", "sources": [_source("https://dropped.example")]},
            ]
        }
    )
    keep_id = body["findings"][0]["id"]

    narrowed = selected_body(body, [keep_id])

    assert source_urls_from_body(narrowed) == ["https://kept.example"]


def test_source_urls_from_body_keeps_a_url_two_elements_share() -> None:
    """The subtle case the issue calls out by name: if two elements cite the
    SAME url and only one of them is dropped, the surviving element's
    legitimate citation of that url must not be revoked. This is what makes
    `source_urls_from_body` a fresh union over what survives, never a
    subtraction of the dropped element's own sources from the parent's full
    citation set — subtracting would fail this exact case."""
    body = with_element_ids(
        {
            "findings": [
                {
                    "signal": "kept",
                    "sources": [
                        _source("https://shared.example"),
                        _source("https://only-kept.example"),
                    ],
                },
                {"signal": "dropped", "sources": [_source("https://shared.example")]},
            ]
        }
    )
    keep_id = body["findings"][0]["id"]

    narrowed = selected_body(body, [keep_id])

    assert source_urls_from_body(narrowed) == [
        "https://shared.example",
        "https://only-kept.example",
    ]


def test_source_urls_from_body_of_a_full_unfiltered_body_matches_the_old_citations_reading() -> (
    None
):
    """NULL selection (`selected_body(body, None)`) must produce the exact
    same allowed set the pre-#145 code computed from the `citations` column
    — the equivalence that makes NULL a genuinely backwards-compatible
    reading, not just an unenforced default."""
    from marketing.definitions import Positioning
    from marketing.pipeline import citation_source_urls, flatten_citations

    result = Positioning.model_validate(
        {
            "segments": [{"name": "s", "description": "d", "sources": [_source("https://a")]}],
            "pillars": [],
            "ctas": [{"text": "c", "rationale": "r", "sources": [_source("https://b")]}],
        }
    )
    body = with_element_ids(result.model_dump())
    citations = flatten_citations(result)

    assert source_urls_from_body(selected_body(body, None)) == citation_source_urls(citations)
