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

from . import ssm

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

#: OpenRouter's dedicated Image API. **Not** chat completions — issue #63
#: went in assuming image models were only reachable through
#: `/api/v1/chat/completions`, with the image folded into message content.
#: That assumption did not survive contact with OpenRouter's current docs:
#: as of 2026-08-11 there is a first-class `POST /api/v1/images` endpoint
#: (openrouter.ai/docs/guides/overview/multimodal/image-generation),
#: confirmed live by posting `{}` to it and reading back a validation error
#: naming exactly `model` and `prompt` as the required fields. Its response
#: shape is close cousin to OpenAI's own Images API below — a `data` array
#: of `{"b64_json", "media_type"}` — which is what keeps this adapter this
#: short.
_OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"

#: The model is configuration, not a constant like `_OPENAI_MODEL` above —
#: direct env only, no SSM parameter, because a model slug is not a secret.
_MODEL_ENV = "MARKETING_IMAGE_PROVIDER_MODEL"

#: ``google/gemini-3.1-flash-image`` ("Nano Banana 2"): the cheapest of the
#: 11 image-output models in OpenRouter's live catalogue at issue-filing time
#: (2026-08-11), and one confirmed (via its own `/endpoints` discovery
#: response) to price per output *token* rather than per flat image — which
#: is exactly the shape this adapter needs to report a real, non-fabricated
#: cost. Override with `_MODEL_ENV` for a different model without a code
#: change.
_DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-flash-image"

#: Filename extension for each `media_type` OpenRouter's Image API can return
#: (`output_format`: png/jpeg/webp, or svg from vectorization models). Falls
#: back to `png` for anything unrecognised or absent, matching
#: `OpenAIImageProvider`'s own fixed choice.
_EXTENSION_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


def _openrouter_model() -> str:
    """The configured OpenRouter image model, or the default."""
    return os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_OPENROUTER_MODEL


def _as_number(value: object) -> float | None:
    """``value`` as a ``float``, or ``None`` if it is missing or not a
    number. ``bool`` is deliberately excluded even though it is an ``int``
    subclass — a JSON ``true``/``false`` here would mean OpenRouter's
    response shape changed underneath this adapter, not a real cost/token
    count."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


#: Resolved once per Lambda container, mirroring `config.public_base_url`'s
#: cache: `None` means "not yet looked up", distinct from `""` meaning
#: "looked up, and there is nothing there" — without that distinction an
#: unconfigured deployment would re-query SSM on every request.
_cached_api_key: str | None = None


def _api_key() -> str:
    """This deployment's image-provider API key, or ``""`` if unconfigured.

    A call that could not even reach SSM (issue #25) also returns ``""`` for
    THIS call only — `ssm.read_parameter` distinguishes that from a
    confirmed-absent parameter, and only the latter gets cached. Without that
    distinction, a single `ThrottlingException` on a cold start would be
    remembered as "not configured" for the rest of that container's warm
    life, and `generate_still` would keep telling an operator to fix a
    deployment that was never broken.
    """
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

    value = ssm.read_parameter(parameter, purpose="image provider API key")
    if value is None:
        # Could not ask — fail only this call. `_cached_api_key` stays `None`
        # so the next call retries rather than repeating a non-answer.
        return ""
    _cached_api_key = value.strip()
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


class OpenRouterImageProvider:
    """`ImageProvider` backed by OpenRouter's dedicated Image API
    (`POST /api/v1/images`) — one key for both this plugin's text stages
    (via Core's agent runtime) and its stills, and the second, independent
    proof of issue #5's requirement 3: a provider swap that needed no change
    to `image_routes.py`, this time for real rather than only via the tests'
    fake.

    **This provider's responses carry a real price**, unlike
    `OpenAIImageProvider` above. OpenRouter's Image API returns a `usage`
    object with `prompt_tokens` / `completion_tokens` / `total_tokens` /
    `cost` on every successful generation (confirmed against the live docs,
    2026-08-11 — see `_OPENROUTER_IMAGES_URL`'s comment). `cost_usd` is
    populated from `usage.cost` whenever it comes back as a number; the
    `ImageProvider` contract's null case stays reachable rather than dead
    code by falling back to `None` if OpenRouter ever omits it for a given
    model or provider route, instead of fabricating `0.0`.

    **Units stay verbatim in OpenRouter's own accounting unit: tokens**, not
    a normalised "1 image". OpenRouter's own `/endpoints` discovery API shows
    some providers billing per image and others per megapixel or token
    internally, but the Image API always reports usage back to the caller in
    tokens (`total_tokens`) alongside the resulting `cost` — that is the
    provider's own unit for this response, in the same sense
    `agent_runs.cost_usd` elsewhere in this estate is sourced from a chat
    completion's own token-based `usage`. Reducing it to `units=1.0,
    unit_kind="image"` here would bake in the exact conversion the module
    docstring's contract says never to perform.
    """

    def __init__(
        self, *, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        #: `None` in production — real network. Tests pass an
        #: `httpx.MockTransport`, matching `OpenAIImageProvider`.
        self._transport = transport

    async def generate_still(self, *, prompt: str) -> GeneratedImage:
        api_key = _api_key()
        if not api_key:
            raise ImageProviderError(
                f"No image provider API key configured ({_DIRECT_ENV} / "
                f"{_PARAMETER_ENV} are both unset)."
            )

        model = _openrouter_model()

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    _OPENROUTER_IMAGES_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "prompt": prompt},
                )
            except httpx.HTTPError as exc:
                raise ImageProviderError(f"OpenRouter request failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise ImageProviderError(
                f"OpenRouter returned {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ImageProviderError("OpenRouter response was not valid JSON.") from exc

        items = body.get("data") or []
        if not items:
            raise ImageProviderError("OpenRouter returned no image data.")

        b64 = items[0].get("b64_json")
        if not b64:
            raise ImageProviderError("OpenRouter response had no b64_json payload.")

        try:
            content = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageProviderError("OpenRouter's b64_json payload did not decode.") from exc

        content_type = items[0].get("media_type") or "image/png"
        extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "png")

        usage = body.get("usage") or {}
        cost_usd = _as_number(usage.get("cost"))
        units = _as_number(usage.get("total_tokens")) or 0.0

        return GeneratedImage(
            content=content,
            content_type=content_type,
            filename=f"{uuid.uuid4()}.{extension}",
            provider="openrouter",
            model=model,
            units=units,
            unit_kind="token",
            cost_usd=cost_usd,
        )


#: Chooses which concrete `ImageProvider` a deployment uses. Values are
#: vendor names, never a platform or tenant name — this plugin installs on
#: every Biffo platform, so nothing configuration-facing here may assume one
#: of them (the class of defect biffo-template#1450's shared-set writeup
#: calls out: a group/vocabulary that exists on one product leaking into
#: code meant for all of them).
_PROVIDER_ENV = "MARKETING_IMAGE_PROVIDER"

#: OpenRouter is the default (issue #63): it is the only implementation here
#: whose response carries a real, non-fabricated cost, and getting the
#: ledger off permanent `unpriced` rows is the reason this provider exists.
_DEFAULT_PROVIDER = "openrouter"

#: One factory per accepted `_PROVIDER_ENV` value. A `dict` rather than an
#: `if`/`elif` chain so the accepted-values list in
#: :func:`create_image_provider`'s error message and this mapping cannot
#: drift apart.
_PROVIDER_FACTORIES: dict[str, type[ImageProvider]] = {
    "openrouter": OpenRouterImageProvider,
    "openai": OpenAIImageProvider,
}


def create_image_provider() -> ImageProvider:
    """This deployment's configured `ImageProvider`, chosen by
    `MARKETING_IMAGE_PROVIDER` (default: ``"openrouter"``).

    An **unset** value picks the default. An **unrecognised** one is a hard
    `ImageProviderError`, never a silent fallback to the default or to
    whichever entry happens to be first in `_PROVIDER_FACTORIES` — a typo'd
    value (``"openrouetr"``) must surface as a failure, not quietly bill an
    operator through a provider they did not choose and hand back a cost
    field shaped differently from the one they expected. Comparison is
    case-insensitive and trims whitespace only; it does not otherwise guess.
    """
    choice = os.environ.get(_PROVIDER_ENV, "").strip().lower() or _DEFAULT_PROVIDER
    try:
        factory = _PROVIDER_FACTORIES[choice]
    except KeyError:
        accepted = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise ImageProviderError(
            f"Unknown {_PROVIDER_ENV}={choice!r}. Expected one of: {accepted}."
        ) from None
    return factory()
