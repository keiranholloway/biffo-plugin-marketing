"""The still-image generation port (M6, issue #5).

`ImageProvider` is the single choke point issue #5 asks for: nothing outside
this module names a specific image-generation vendor, model, or SDK.
`image_routes.py` depends only on the `ImageProvider` Protocol below, and the
cost-ledger write it makes reads its fields straight off the `GeneratedImage`
a provider returns — so replacing `OpenAIImageProvider` with a second vendor
is a new class implementing this Protocol, never a change to a caller or to
how cost gets recorded.

Modelled on `idea_scout.ports.CoreGateway` (this estate's other Protocol-based
port) and on this repo's own `pipeline.AgentGateway` — same shape, narrower
surface: one method, because "turn a prompt into a still" is the only
operation this milestone needs behind the seam.
"""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from aws_lambda_powertools import Logger

logger = Logger(child=True)


@dataclass(frozen=True)
class GeneratedImage:
    """One generated still, and everything the media-generation ledger
    (biffo-template#1439) needs about it.

    ``cost_usd`` is ``None``, never ``0.0``, whenever the provider's response
    carries no price — see `OpenAIImageProvider` below for a provider where
    that is the normal case, not an error. Core's own ledger schema treats
    "no price returned" and "genuinely free" as structurally different facts
    (nullable ``cost_usd``, with the null case load-bearing); collapsing the
    two here would make that distinction impossible for anything downstream
    to recover.

    ``units``/``unit_kind`` are stored **verbatim in the provider's own
    unit** — never normalised here. One image is ``units=1.0,
    unit_kind="image"`` for this milestone's only provider, but a video or
    per-second provider would report different units, and choosing one
    canonical unit at this layer would bake in a conversion Core's ledger is
    deliberately built not to need (see `media_generation.py` in Core).
    """

    content: bytes
    content_type: str
    filename: str
    provider: str
    model: str
    units: float
    unit_kind: str
    cost_usd: float | None


class ImageProviderError(RuntimeError):
    """A provider could not produce a still — a missing/invalid credential, a
    non-2xx response, or a response this adapter cannot parse.

    Never raised for "the provider returned no price" — that is a successful
    generation with ``GeneratedImage.cost_usd = None``, not a failure.
    """


class ImageProvider(Protocol):
    """Everything a route needs to turn a prompt into a still.

    Exactly one method. Requirement 3 of issue #5 — "swapping the provider is
    an implementation change, not a rewrite" — is proven in this plugin's
    tests by a fake implementing this same Protocol with no network calls; a
    second real provider would be exactly the same kind of addition.
    """

    async def generate_still(self, *, prompt: str) -> GeneratedImage: ...


#: Direct env, checked first — set in local development and in tests, so
#: neither has to reach AWS to run. Named for this plugin specifically
#: (`MARKETING_`, not a shared `BIFFO_` prefix) because the credential belongs
#: to this provider integration, not to the platform.
_DIRECT_ENV = "MARKETING_IMAGE_PROVIDER_API_KEY"

#: The SSM parameter NAME (never the value — see `config.public_base_url`'s
#: docstring in this same repo for why: an installed plugin has no Terraform
#: channel for instance-specific secrets until biffo-template#1456 closes, so
#: the value arrives out of band via `aws ssm put-parameter`, and this module
#: only ever reads a name Terraform can safely put in state).
_PARAMETER_ENV = "MARKETING_IMAGE_PROVIDER_API_KEY_PARAMETER"

_OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_OPENAI_MODEL = "gpt-image-1"

#: Resolved once per Lambda container, mirroring `config.public_base_url`'s
#: cache: `None` means "not yet looked up", distinct from `""` meaning
#: "looked up, and there is nothing there" — without that distinction an
#: unconfigured deployment would re-query SSM on every request.
_cached_api_key: str | None = None


def _api_key() -> str:
    """This deployment's image-provider API key, or ``""`` if unconfigured."""
    global _cached_api_key

    if _cached_api_key is not None:
        return _cached_api_key

    direct = os.environ.get(_DIRECT_ENV, "").strip()
    if direct:
        _cached_api_key = direct
        return _cached_api_key

    parameter = os.environ.get(_PARAMETER_ENV, "").strip()
    if not parameter:
        _cached_api_key = ""
        return _cached_api_key

    try:
        import boto3

        client = boto3.client("ssm")
        value = client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
        _cached_api_key = str(value).strip()
    except Exception:
        logger.warning(
            "Could not read the image provider API key from %s", parameter, exc_info=True
        )
        _cached_api_key = ""
    return _cached_api_key


def reset_api_key_cache() -> None:
    """Forget the resolved key. For tests only."""
    global _cached_api_key
    _cached_api_key = None


class OpenAIImageProvider:
    """`ImageProvider` backed by OpenAI's Images API (`gpt-image-1`).

    **This provider's responses carry no price.** Unlike OpenRouter's chat
    completions — what `agent_runs.cost_usd` is sourced from, a real snapshot
    of `usage.cost` — OpenAI's Images API returns no cost field at all. Every
    generation through this adapter is therefore, correctly, `unpriced` in
    the ledger. That is not a gap here; it is the real shape of the upstream
    API, and it is exactly the case `media_generations.cost_usd` being
    nullable exists to represent honestly, rather than defaulting to a wrong
    zero or asserting a rate-card price this adapter has no authority to
    invent.
    """

    def __init__(
        self, *, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        #: `None` in production — real network. Tests pass an
        #: `httpx.MockTransport` so this adapter's parsing/error-mapping is
        #: exercised with no socket ever opened, per this repo's "no network
        #: calls in tests" rule.
        self._transport = transport

    async def generate_still(self, *, prompt: str) -> GeneratedImage:
        api_key = _api_key()
        if not api_key:
            raise ImageProviderError(
                f"No image provider API key configured ({_DIRECT_ENV} / "
                f"{_PARAMETER_ENV} are both unset)."
            )

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    _OPENAI_IMAGES_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": _OPENAI_MODEL, "prompt": prompt, "n": 1, "size": "1024x1024"},
                )
            except httpx.HTTPError as exc:
                raise ImageProviderError(f"OpenAI request failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise ImageProviderError(
                f"OpenAI returned {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ImageProviderError("OpenAI response was not valid JSON.") from exc

        items = body.get("data") or []
        if not items:
            raise ImageProviderError("OpenAI returned no image data.")

        b64 = items[0].get("b64_json")
        if not b64:
            raise ImageProviderError("OpenAI response had no b64_json payload.")

        try:
            content = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageProviderError("OpenAI's b64_json payload did not decode.") from exc

        return GeneratedImage(
            content=content,
            content_type="image/png",
            filename=f"{uuid.uuid4()}.png",
            provider="openai",
            model=_OPENAI_MODEL,
            units=1.0,
            unit_kind="image",
            cost_usd=None,
        )
