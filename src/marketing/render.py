"""Deterministic form-factor rendering — the self-contained core of M5.

``definitions.PLACEMENTS`` names three shapes a channel wants from **one**
approved creative: ``feed_1x1``, ``portrait_4x5``, ``story_9x16``. This module
is the only thing allowed to turn the creative into those three images.

## Rendered, never regenerated

An agent could instead be asked to *generate* a new image per placement. That
is deliberately not what happens here, for three reasons stated in
``definitions.py`` and repeated here because they are this module's whole
reason to exist:

1. **Drift.** Three separate generations of "the same" creative will not agree
   on the subject, the crop, or even the palette. The variants stop looking
   like one campaign.
2. **Cost.** N generations instead of one.
3. **Hallucination surface.** Every regeneration is another chance for the
   model to invent something that was never approved. A human approved *one*
   image; that is the only image that should ever ship.

So this module never calls a model. It takes the bytes of the single approved
creative and a placement name, and produces that placement's crop by pure,
local, Pillow-only image manipulation — an agent decides what to make; this
renders it.

## The cropping rule: centre-crop to the placement's exact ratio

Each placement has a fixed target aspect ratio (``feed_1x1`` 1:1,
``portrait_4x5`` 4:5, ``story_9x16`` 9:16). Given a source image whose ratio
does not already match:

- If the source is proportionally **wider** than the target (e.g. a landscape
  photo asked for ``story_9x16``), the **left and right edges are discarded**
  and the full height is kept — the crop narrows toward the centre column.
- If the source is proportionally **taller** than the target (e.g. a portrait
  photo asked for ``feed_1x1``), the **top and bottom edges are discarded**
  and the full width is kept — the crop narrows toward the centre row.

Centre-crop is the default because it is the only rule that needs no opinion
about the subject: it has no face detector, no saliency model, nothing that
can be wrong. It is not always the *best* crop — a subject standing at the
left edge of a landscape shot will be cut in a 9:16 crop of it — but "correct
without looking at the pixels" is the property this module trades for that,
and the human approval gate upstream (``PIPELINE_STAGES``) is exactly the
point where a bad crop gets caught before anything publishes.

**A source already at the target ratio is untouched, exactly.** No crop is
computed and the original bytes are returned unchanged — not decoded and
re-encoded. This matters for two reasons: it is what makes a pre-cropped
asset round-trip bit-for-bit, and it is what keeps "already correct" from
silently costing a generation-quality loss on every placement whose ratio the
creative happened to already match. The equality test is exact integer
cross-multiplication (``width * target_h == height * target_w``), never a
float comparison against some epsilon — there is no tolerance band in which
"close enough" is treated as "already there".

## Determinism

The whole design in ``definitions.py`` rests on one property: the same input
bytes and the same placement produce the **same output bytes**, every time.
That is what makes "rendered, never regenerated" true in practice rather than
in name only — a renderer that drifted between calls would have reintroduced
the exact problem it exists to remove, just one layer down. Concretely:

- No randomness anywhere in this module.
- No timestamps, software tags, or other run-varying metadata written to the
  output. Pillow's PNG encoder does not add a modification-time chunk unless
  one is explicitly supplied via ``pnginfo``, and this module never supplies
  one.
- The crop box is computed with exact integer arithmetic (see
  ``_center_crop_box``) rather than floating-point division, so it cannot
  vary with platform-specific float rounding.
- Output is always re-encoded as PNG with fixed encoder parameters
  (``compress_level=6``, no ``optimize``), regardless of the source format.
  PNG is lossless, so re-cropped output never compounds recompression
  artefacts onto an already-approved creative the way re-saving as JPEG on
  every render would; fixing the encoder parameters removes the one place a
  format like JPEG could otherwise vary output size across otherwise-identical
  runs. The one exception is the no-op path above, which preserves the
  source's original format because it does not re-encode at all.

Not handled, deliberately out of scope for this milestone: EXIF orientation
(the raw pixel grid is treated as canonical; a JPEG whose EXIF says "rotate
90°" is cropped as stored, not as displayed) and any minimum-resolution floor
(a tiny source is cropped down to whatever integer pixel size the ratio
implies, even if that is a sliver a few pixels wide). Both are product
decisions for a later milestone, not determinism concerns.
"""

from __future__ import annotations

import io

from PIL import Image

from marketing.definitions import PLACEMENTS

#: Each placement's target aspect ratio, as an exact ``(width, height)``
#: integer pair — never a float. Keeping it an integer ratio is what lets the
#: "already at this ratio" check in `render` be exact cross-multiplication
#: rather than an approximate comparison.
_PLACEMENT_RATIOS: dict[str, tuple[int, int]] = {
    "feed_1x1": (1, 1),
    "portrait_4x5": (4, 5),
    "story_9x16": (9, 16),
}

# Guard the guard: if `definitions.PLACEMENTS` ever gains or loses a name,
# this table drifting out of step with it would silently mis-render or
# silently stop covering a placement. Fail at import time, not at first call.
assert set(_PLACEMENT_RATIOS) == set(PLACEMENTS), (
    f"_PLACEMENT_RATIOS {sorted(_PLACEMENT_RATIOS)} has drifted from "
    f"definitions.PLACEMENTS {sorted(PLACEMENTS)}"
)

#: Modes Pillow's PNG encoder writes directly. Anything else (most commonly
#: CMYK, from a print-oriented source asset) is converted to RGB first — PNG
#: has no CMYK representation at all, so saving would otherwise raise.
_PNG_NATIVE_MODES = ("RGB", "RGBA", "L", "LA", "P")


class UnknownPlacementError(ValueError):
    """`placement` is not one of `definitions.PLACEMENTS`."""


def render(image_bytes: bytes, placement: str) -> bytes:
    """The `placement` crop of the single approved creative in `image_bytes`.

    Deterministic: the same `image_bytes` and `placement` always produce
    byte-identical output (see the module docstring's Determinism section).
    Raises `UnknownPlacementError` for any `placement` not in
    `definitions.PLACEMENTS`.
    """
    try:
        target_w, target_h = _PLACEMENT_RATIOS[placement]
    except KeyError:
        raise UnknownPlacementError(
            f"{placement!r} is not a known placement: {sorted(_PLACEMENT_RATIOS)}"
        ) from None

    with Image.open(io.BytesIO(image_bytes)) as source:
        width, height = source.size

        if width * target_h == height * target_w:
            # Exact match already — a genuine no-op. Returning the original
            # bytes (rather than decoding and re-encoding an unchanged crop)
            # is what makes a pre-cropped asset round-trip bit-for-bit.
            return image_bytes

        box = _center_crop_box(width, height, target_w, target_h)
        cropped = source.crop(box)
        if cropped.mode not in _PNG_NATIVE_MODES:
            cropped = cropped.convert("RGB")

        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG", optimize=False, compress_level=6)
        return buffer.getvalue()


def _center_crop_box(
    width: int, height: int, target_w: int, target_h: int
) -> tuple[int, int, int, int]:
    """The largest centred `target_w:target_h` box that fits in `width`x`height`.

    Pure integer arithmetic throughout — see the module docstring's
    Determinism section for why. Rounds the cropped dimension to the nearest
    whole pixel (half rounds up), via ``(numerator + denominator // 2) //
    denominator``, which is exact integer division and never touches a float.
    """
    if width * target_h > height * target_w:
        # Source is proportionally wider than the target: crop the width,
        # keep the full height. The left and right edges are discarded.
        new_width = (height * target_w + target_h // 2) // target_h
        new_width = max(1, min(new_width, width))
        left = (width - new_width) // 2
        return (left, 0, left + new_width, height)
    else:
        # Source is proportionally taller than the target: crop the height,
        # keep the full width. The top and bottom edges are discarded.
        new_height = (width * target_h + target_w // 2) // target_w
        new_height = max(1, min(new_height, height))
        top = (height - new_height) // 2
        return (0, top, width, top + new_height)
