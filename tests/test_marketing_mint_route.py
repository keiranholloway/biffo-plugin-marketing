"""The mint route (M2), over a fake Core.

Fakes rather than mocks: the thing worth asserting is *what was written to
`marketing_link`*, and a fake that records the calls says that directly, where a
mock's assertion language says only that something was called.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from marketing import admin_app

_CAMPAIGN = "b3f1c0de-0000-4000-8000-000000000001"
# A token a caller tries to choose for themselves. Named rather than inlined so
# ruff's hardcoded-credential rule (S105) does not fire on a value whose entire
# purpose is to be REJECTED.
_ATTACKER_TOKEN = "attacker-chosen"  # noqa: S105
_BASE = "https://dev.tabsii.com"


class _FakeCore:
    """Records what the plugin asked Core to do, and answers plausibly."""

    def __init__(self, *, destination: str | None = "https://tabsii.com/intake/demo") -> None:
        self.destination = destination
        self.created: list[dict[str, Any]] = []

    async def __call__(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        # `request=` is not decoration: httpx refuses `raise_for_status()` on a
        # response with no request attached, and the route calls it.
        request = httpx.Request(method, f"https://core.invalid{path}")
        if method == "GET" and path.startswith("/campaigns/"):
            return httpx.Response(
                200,
                json={"id": _CAMPAIGN, "destination_url": self.destination},
                request=request,
            )
        if method == "POST" and path == "/links":
            body = kw["json"]
            self.created.append(body)
            return httpx.Response(
                201, json={**body, "id": f"link-{len(self.created)}"}, request=request
            )
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch):
    core = _FakeCore()
    monkeypatch.setattr(admin_app, "_core", core)
    monkeypatch.setattr(admin_app, "PUBLIC_BASE_URL", _BASE)

    app = admin_app.build_app()
    # The host gates on the Cognito group before this app sees a request
    # (ADR-0011); this stands in for the ForwardedUser it would pass through.
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"}
    )()
    return TestClient(app), core


def test_minting_returns_a_publishable_url_per_link(ctx) -> None:
    client, _ = ctx
    resp = client.post(
        f"/campaigns/{_CAMPAIGN}/links",
        json={"links": [{"channel": "linkedin"}, {"channel": "instagram", "variant": "b"}]},
    )

    assert resp.status_code == 201
    links = resp.json()["links"]
    assert len(links) == 2
    for link in links:
        assert link["url"].startswith(f"{_BASE}/c/")


def test_each_link_gets_its_own_token(ctx) -> None:
    """Two links sharing a token would merge two channels' clicks into one."""
    client, core = ctx
    client.post(
        f"/campaigns/{_CAMPAIGN}/links",
        json={"links": [{"channel": "linkedin"}, {"channel": "instagram"}]},
    )

    tokens = {row["token"] for row in core.created}
    assert len(tokens) == 2


def test_the_stored_destination_carries_the_campaign_id(ctx) -> None:
    """The whole milestone, asserted at the row that actually gets written."""
    client, core = ctx
    client.post(f"/campaigns/{_CAMPAIGN}/links", json={"links": [{"channel": "linkedin"}]})

    stored = core.created[0]["destination_url"]
    assert parse_qs(urlsplit(stored).query)["utm_campaign"] == [_CAMPAIGN]


def test_the_caller_cannot_supply_the_destination_or_the_token(ctx) -> None:
    """Both are derived, and that is the security property of this route.

    A caller who could set `destination_url` could point a campaign's tracked
    link anywhere; one who could set `token` could collide with, or guess, an
    existing link. Extra fields in the body must not reach Core.
    """
    client, core = ctx
    client.post(
        f"/campaigns/{_CAMPAIGN}/links",
        json={
            "links": [
                {
                    "channel": "linkedin",
                    "token": _ATTACKER_TOKEN,
                    "destination_url": "https://evil.example/",
                }
            ]
        },
    )

    stored = core.created[0]
    assert stored["token"] != _ATTACKER_TOKEN
    assert stored["destination_url"].startswith("https://tabsii.com/intake/demo")


def test_a_campaign_with_no_destination_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A link to nowhere is worse than no link.

    It still records clicks and still looks like it worked, so the failure
    surfaces as a campaign that mysteriously converts at zero rather than as an
    error anybody sees.
    """
    core = _FakeCore(destination=None)
    monkeypatch.setattr(admin_app, "_core", core)
    monkeypatch.setattr(admin_app, "PUBLIC_BASE_URL", _BASE)
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "a", "groups": ["admin"], "token": "t"}
    )()

    resp = TestClient(app).post(
        f"/campaigns/{_CAMPAIGN}/links", json={"links": [{"channel": "linkedin"}]}
    )

    assert resp.status_code == 422
    assert core.created == [], "nothing should have been written"


def test_an_unconfigured_deployment_says_so_rather_than_minting_a_broken_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a base URL the minted link would be published as `/c/<token>`."""
    monkeypatch.setattr(admin_app, "_core", _FakeCore())
    monkeypatch.setattr(admin_app, "PUBLIC_BASE_URL", "")
    app = admin_app.build_app()
    app.dependency_overrides[admin_app.require_admin] = lambda: type(
        "U", (), {"sub": "a", "groups": ["admin"], "token": "t"}
    )()

    resp = TestClient(app).post(
        f"/campaigns/{_CAMPAIGN}/links", json={"links": [{"channel": "linkedin"}]}
    )
    assert resp.status_code == 503


def test_an_empty_batch_is_rejected(ctx) -> None:
    client, _ = ctx
    assert client.post(f"/campaigns/{_CAMPAIGN}/links", json={"links": []}).status_code == 422
