"""`create_image_provider` (issue #63 follow-up) — which concrete
`ImageProvider` a deployment gets, chosen by `MARKETING_IMAGE_PROVIDER`.

Covers the selection itself (default, each accepted value, the unknown-value
failure) directly against `image_provider.create_image_provider`, plus one
route-level test proving `image_routes.get_image_provider` maps that failure
to a 502 rather than reaching the route body or crashing as a bare 500 —
without overriding `get_image_provider` itself, so this is the one test in
the suite that exercises the real factory through the real router.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import marketing.image_provider as image_provider
from marketing import image_routes
from marketing.image_provider import (
    ImageProviderError,
    OpenAIImageProvider,
    OpenRouterImageProvider,
    create_image_provider,
)

_CAMPAIGN = "b3f1c0de-0000-4000-8000-0000000000ef"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    """A configured API key (selection doesn't need one to succeed — only
    `generate_still` does) and no leftover `MARKETING_IMAGE_PROVIDER` from a
    previous test."""
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY", "sk-test-not-real")  # noqa: S105
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER", raising=False)
    image_provider.reset_api_key_cache()
    yield
    image_provider.reset_api_key_cache()


def test_default_is_openrouter() -> None:
    """Unset picks OpenRouter — the only implementation with a real cost,
    which is the whole reason issue #63 exists."""
    assert isinstance(create_image_provider(), OpenRouterImageProvider)


def test_explicit_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "openrouter")
    assert isinstance(create_image_provider(), OpenRouterImageProvider)


def test_explicit_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "openai")
    assert isinstance(create_image_provider(), OpenAIImageProvider)


def test_value_is_case_and_whitespace_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "  OpenAI  ")
    assert isinstance(create_image_provider(), OpenAIImageProvider)


def test_unknown_value_fails_loudly_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently pick the default (or anything else) — an
    operator would then be billed by a provider they never chose, with a
    cost field shaped differently from the one they expected."""
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "openrouetr")

    with pytest.raises(ImageProviderError, match="openrouetr"):
        create_image_provider()


def test_unknown_value_error_names_the_accepted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "bogus")

    with pytest.raises(ImageProviderError, match="openai") as exc_info:
        create_image_provider()
    assert "openrouter" in str(exc_info.value)


def _admin_user():
    return type("U", (), {"sub": "admin", "groups": ["admin"], "token": "admin-jwt"})()


def test_route_maps_an_unknown_provider_choice_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real router, with `get_image_provider` NOT overridden:
    proves the mapping lives where an operator actually hits it, not only at
    the unit level above."""
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER", "bogus")

    app = FastAPI()
    app.include_router(image_routes.router)
    app.dependency_overrides[image_routes.require_admin] = _admin_user
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(f"/campaigns/{_CAMPAIGN}/stills", json={"prompt": "anything"})

    assert response.status_code == 502
    assert "bogus" in response.json()["detail"]
