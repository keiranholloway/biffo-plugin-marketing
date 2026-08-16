"""`OpenAIImageProvider` (M6) — over `httpx.MockTransport`, never the network.

Exercises the adapter's own contract: parse a still out of OpenAI's response
shape, and turn every way it can go wrong into `ImageProviderError` rather
than an unhandled exception or a silently-empty result.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError

import marketing.image_provider as image_provider
from marketing.image_provider import (
    GeneratedImage,
    ImageProviderError,
    OpenAIImageProvider,
    asset_filename,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png but bytes are bytes"
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


class _FakeSSMClient:
    """Stands in for `boto3.client("ssm")` at the lowest layer this module
    reaches — used only by the "real wiring" test below, which exercises
    `_api_key` -> `ssm.read_parameter` through the actual call rather than a
    `image_provider.ssm.read_parameter` monkeypatch that would keep passing
    even if the two modules' contract drifted apart."""

    def __init__(self, *, value: str | None = None, error_code: str | None = None) -> None:
        self._value = value
        self._error_code = error_code

    def get_parameter(self, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
        if self._error_code is not None:
            raise ClientError(
                {"Error": {"Code": self._error_code, "Message": "synthetic"}}, "GetParameter"
            )
        return {"Parameter": {"Value": self._value}}


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


async def test_generate_still_wraps_a_transport_level_failure() -> None:
    """`except httpx.HTTPError` — a connection failure/timeout reaching
    OpenAI at all, distinct from `test_generate_still_raises_on_a_non_200`
    (which is a completed HTTP exchange, just an error status). Must not
    escape as a raw `httpx` exception a caller has never imported."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ImageProviderError, match="OpenAI request failed"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_the_response_body_is_not_json() -> None:
    """`except ValueError` around `response.json()` — a 200 whose body is not
    parseable JSON at all (an upstream proxy error page, say), distinct from
    a well-formed JSON body missing the fields this adapter expects."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(ImageProviderError, match="not valid JSON"):
        await _provider(handler).generate_still(prompt="anything")


async def test_generate_still_raises_when_b64_json_does_not_decode() -> None:
    """`except (ValueError, TypeError)` around `base64.b64decode(...,
    validate=True)` — a `b64_json` field that is present but not valid
    base64, distinct from it being entirely absent."""

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


def test_the_no_key_message_does_not_claim_the_parameter_is_unset_when_it_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_api_key()` returning `""` is not always "nobody configured
    anything" — a parameter CAN be configured and still fail to read (denied,
    throttled, genuinely absent). The old fixed message ("are both unset")
    was false in every one of those cases; this checks both branches of the
    corrected one."""
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", raising=False)
    assert "are both unset" in image_provider._no_api_key_message()

    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", "/marketing/image-key")
    message = image_provider._no_api_key_message()
    assert "are both unset" not in message
    assert "/marketing/image-key" in message


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

    def _flaky_then_fine(parameter: str, *, purpose: str) -> str | None:
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


def test_the_real_ssm_wiring_does_not_cache_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test, through the real call chain: `_api_key` ->
    `ssm.read_parameter` -> `boto3`, with only `boto3.client` stubbed —
    everything above it is the actual production code, unlike the test
    above, which monkeypatches `image_provider.ssm.read_parameter` directly
    and would not notice if the two modules' contract drifted apart."""
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", "/marketing/image-key")
    image_provider.reset_api_key_cache()

    clients = iter(
        [
            _FakeSSMClient(error_code="ThrottlingException"),
            _FakeSSMClient(value="sk-live-real-key"),
        ]
    )
    monkeypatch.setattr("boto3.client", lambda service: next(clients))

    assert image_provider._api_key() == ""
    assert image_provider._cached_api_key is None
    assert image_provider._api_key() == "sk-live-real-key"


def test_the_real_ssm_wiring_reports_a_confirmed_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """As above, for the other half of the contract: a genuinely-absent
    parameter through the real chain, not a mocked `ssm.read_parameter`."""
    monkeypatch.delenv("MARKETING_IMAGE_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER", "/marketing/image-key")
    image_provider.reset_api_key_cache()
    monkeypatch.setattr(
        "boto3.client", lambda service: _FakeSSMClient(error_code="ParameterNotFound")
    )

    assert image_provider._api_key() == ""
    assert image_provider._cached_api_key == ""


# ── issue #122: `slugify`/`asset_filename` mirror
# `web-admin/src/lib/assetFilename.ts`'s own naming convention ──────────────
#
# The fixtures that were here are gone, and that is the point. They were
# "ported 1:1" from `assetFilename.test.ts` — a SECOND COPY of the client's
# cases, which is the same mistake in the test suite that the two
# implementations make in the source, and it left both suites green while the
# two halves disagreed (measured 2026-08-16: `MAX_SLUG` 60 -> 40 in the client
# broke nothing, here or there).
#
# The shared behaviour now lives once, in `shared/cross-language-ports.json`,
# and BOTH suites execute it: `tests/test_marketing_cross_language_ports.py`
# and `web-admin/src/lib/crossLanguagePorts.test.ts`. What stays below is only
# what is deliberately NOT shared — this side's own fallbacks, which the
# client's differ from on purpose (issue #119).


def test_asset_filename_falls_back_to_asset_when_the_part_slugs_to_nothing() -> None:
    """This side's own fallback, and deliberately not the client's: the
    browser picks `source`/`creative` from the asset's `is_source` flag, which
    this side is not given. No call site here passes an empty `part` — every
    one passes `"source"` or a `definitions.PLACEMENTS` entry — so this pins
    the behaviour rather than describing a live case, and it is stated in the
    shared spec's `deliberate_differences` so nobody later "fixes" the two
    into agreement."""
    assert asset_filename(campaign_name="Spring Launch", part="  ", extension="png") == (
        "spring-launch-asset.png"
    )


def test_asset_filename_never_contains_a_uuid() -> None:
    """The regression this whole issue is about, stated directly: nothing
    about the composed filename is random — same campaign name and part
    always produce the same filename, unlike the old
    `f"{uuid.uuid4()}.png"`."""
    first = asset_filename(campaign_name="Spring Launch", part="source", extension="png")
    second = asset_filename(campaign_name="Spring Launch", part="source", extension="png")
    assert first == second == "spring-launch-source.png"
