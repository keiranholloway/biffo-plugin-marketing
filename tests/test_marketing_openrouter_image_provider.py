"""`OpenRouterImageProvider` (issue #63) — over `httpx.MockTransport`, never
the network.

Mirrors `test_marketing_image_provider.py`'s shape for `OpenAIImageProvider`,
with the one asymmetry the module docstring calls out: OpenRouter's Image API
*does* return a price, so the interesting cases here are "a real cost gets
carried through" and, just as load-bearing, "a response with no `usage.cost`
still renders as unpriced (`None`), never a fabricated `0.0`" — the same
null-cost contract `OpenAIImageProvider`'s tests exercise, proven here on a
provider where it is the exceptional case rather than the constant one.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import marketing.image_provider as image_provider
from marketing.image_provider import GeneratedImage, ImageProviderError, OpenRouterImageProvider

_PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png but bytes are bytes"
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY", "sk-or-test-not-real")  # noqa: S105
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_MODEL", raising=False)
    image_provider.reset_api_key_cache()
    yield
    image_provider.reset_api_key_cache()


def _provider(handler) -> OpenRouterImageProvider:
    """An `OpenRouterImageProvider` whose one outbound call is served by
    `handler` over an in-memory transport — no socket, ever."""
    return OpenRouterImageProvider(transport=httpx.MockTransport(handler))


async def test_generate_still_parses_the_image_and_the_real_cost() -> None:
    """OpenRouter's Image API returns a priced `usage` object — the ledger
    row this issue exists to make non-null."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-or-test-not-real"
        return httpx.Response(
            200,
            json={
                "created": 1748372400,
                "data": [{"b64_json": _PNG_B64, "media_type": "image/png"}],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 4175,
                    "total_tokens": 4175,
                    "cost": 0.04,
                },
            },
        )

    image = await _provider(handler).generate_still(prompt="a friendly logo")

    assert isinstance(image, GeneratedImage)
    assert image.content == _PNG_BYTES
    assert image.content_type == "image/png"
    assert image.filename.endswith(".png")
    assert image.provider == "openrouter"
    assert image.model == "google/gemini-3.1-flash-image"
    assert image.unit_kind == "token"
    assert image.units == 4175.0
    assert image.cost_usd == 0.04


async def test_generate_still_reports_unpriced_when_usage_has_no_cost() -> None:
    """A response with no `usage.cost` (a model/route OpenRouter has not
    priced) must render as `cost_usd=None`, never `0.0` — the same contract
    `OpenAIImageProvider` upholds in the constant case, exercised here in the
    exceptional one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": _PNG_B64, "media_type": "image/png"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10},
            },
        )

    image = await _provider(handler).generate_still(prompt="anything")

    assert image.cost_usd is None
    assert image.units == 10.0
    assert image.unit_kind == "token"


async def test_generate_still_reports_unpriced_when_usage_is_entirely_absent() -> None:
    """No `usage` key at all is the same "no price returned" fact as a
    `usage` with no `cost` field — still `None`, still zero-valued units
    rather than a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _PNG_B64}]})

    image = await _provider(handler).generate_still(prompt="anything")

    assert image.cost_usd is None
    assert image.units == 0.0


async def test_generate_still_sends_the_prompt_and_default_model() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": _PNG_B64}], "usage": {}})

    await _provider(handler).generate_still(prompt="a friendly logo, flat style")

    assert seen["body"]["prompt"] == "a friendly logo, flat style"
    assert seen["body"]["model"] == "google/gemini-3.1-flash-image"


async def test_generate_still_honours_a_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model is configuration (issue #63), not a hardcoded constant like
    `OpenAIImageProvider`'s `_OPENAI_MODEL` — `MARKETING_IMAGE_PROVIDER_MODEL`
    overrides the default with no code change."""
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_MODEL", "openai/gpt-5-image-mini")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": _PNG_B64}], "usage": {}})

    image = await _provider(handler).generate_still(prompt="anything")

    assert seen["body"]["model"] == "openai/gpt-5-image-mini"
    assert image.model == "openai/gpt-5-image-mini"


async def test_generate_still_uses_the_returned_media_type_and_extension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": _PNG_B64, "media_type": "image/webp"}],
                "usage": {"total_tokens": 5, "cost": 0.001},
            },
        )

    image = await _provider(handler).generate_still(prompt="anything")

    assert image.content_type == "image/webp"
    assert image.filename.endswith(".webp")


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
        return httpx.Response(200, json={"data": [{"media_type": "image/png"}]})

    with pytest.raises(ImageProviderError, match="b64_json"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_wraps_a_transport_level_failure() -> None:
    """`except httpx.HTTPError` — a connection failure/timeout reaching
    OpenRouter at all, distinct from `test_generate_still_raises_on_a_non_200`
    (a completed exchange with an error status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ImageProviderError, match="OpenRouter request failed"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_the_response_body_is_not_json() -> None:
    """`except ValueError` around `response.json()` — a 200 with a body that
    is not parseable JSON at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(ImageProviderError, match="not valid JSON"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_b64_json_does_not_decode() -> None:
    """`except (ValueError, TypeError)` around `base64.b64decode(...,
    validate=True)` — a `b64_json` present but not valid base64."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": "not-base64!!!"}]})

    with pytest.raises(ImageProviderError, match="did not decode"):
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
