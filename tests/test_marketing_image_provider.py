"""`OpenAIImageProvider` (M6) — over `httpx.MockTransport`, never the network.

Exercises the adapter's own contract: parse a still out of OpenAI's response
shape, and turn every way it can go wrong into `ImageProviderError` rather
than an unhandled exception or a silently-empty result.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import marketing.image_provider as image_provider
from marketing.image_provider import GeneratedImage, ImageProviderError, OpenAIImageProvider

_PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png but bytes are bytes"
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY", "sk-test-not-real")  # noqa: S105
    image_provider.reset_api_key_cache()
    yield
    image_provider.reset_api_key_cache()


def _provider(handler) -> OpenAIImageProvider:
    """An `OpenAIImageProvider` whose one outbound call is served by
    `handler` over an in-memory transport — no socket, ever."""
    return OpenAIImageProvider(transport=httpx.MockTransport(handler))


async def test_generate_still_parses_the_image_and_reports_unpriced() -> None:
    """OpenAI's Images API returns no price at all (the module docstring's
    whole point) — so a successful generation must be `cost_usd=None`, never
    a fabricated `0.0`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test-not-real"
        return httpx.Response(200, json={"data": [{"b64_json": _PNG_B64}]})

    image = await _provider(handler).generate_still(prompt="a friendly logo")

    assert isinstance(image, GeneratedImage)
    assert image.content == _PNG_BYTES
    assert image.content_type == "image/png"
    assert image.provider == "openai"
    assert image.model == "gpt-image-1"
    assert image.units == 1.0
    assert image.unit_kind == "image"
    assert image.cost_usd is None


async def test_generate_still_sends_the_prompt() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": _PNG_B64}]})

    await _provider(handler).generate_still(prompt="a friendly logo, flat style")

    assert seen["body"]["prompt"] == "a friendly logo, flat style"
    assert seen["body"]["model"] == "gpt-image-1"


async def test_generate_still_raises_on_a_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad prompt"}})

    with pytest.raises(ImageProviderError, match="400"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_data_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ImageProviderError, match="no image data"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_b64_json_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://example.com/img.png"}]})

    with pytest.raises(ImageProviderError, match="b64_json"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_without_a_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", raising=False)
    image_provider.reset_api_key_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never reach the transport with no key")

    with pytest.raises(ImageProviderError, match="No image provider API key"):
        await _provider(handler).generate_still(prompt="anything")


def test_a_transient_ssm_failure_does_not_poison_the_api_key_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #25. A `ThrottlingException` on a cold start's key lookup must
    not be remembered as "not configured" for the rest of that warm
    container's life — the fake `_provider(handler)` fixture above proves the
    request-time symptom; this proves the caching contract that causes it.

    Fails before the fix: the old `_api_key` caught every SSM failure the
    same way and always cached `""`, so the second call below never
    re-consulted SSM and stayed wrong for a key that was there all along.
    """
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", "/marketing/image-key")
    image_provider.reset_api_key_cache()

    responses: list[str | None] = [None, "sk-live-real-key"]
    calls: list[str] = []

    def _flaky_then_fine(parameter: str) -> str | None:
        calls.append(parameter)
        return responses.pop(0)

    monkeypatch.setattr(image_provider.ssm, "read_parameter", _flaky_then_fine)

    # First call: SSM could not be reached. Reports unconfigured for THIS
    # call only...
    assert image_provider._api_key() == ""
    # ...but that must not have been cached as the answer.
    assert image_provider._cached_api_key is None

    # A later call — same warm container — retries and gets the real key.
    assert image_provider._api_key() == "sk-live-real-key"
    assert len(calls) == 2
