"""Deterministic form-factor rendering (render.py) — the core of M5.

The property the whole design rests on is stated in `definitions.PLACEMENTS`
and `render.py`'s module docstring: **rendered, never regenerated**. Two
things make that true rather than aspirational, and both are tested directly
here rather than inferred from the crop looking right:

1. **Determinism** — the same input bytes and the same placement produce
   byte-identical output, every time. `test_rendering_twice_is_byte_identical`
   is the whole point of this file; everything else is checking the crop rule
   is the one documented.
2. **The no-op path is a real no-op** — an asset already at a placement's
   ratio is returned untouched, not decoded and re-encoded losslessly-but-
   differently. Proven with a JPEG input: if the output were a re-encode it
   could not possibly equal the JPEG bytes exactly.

Crop-position tests use an image with a distinct marker colour painted on each
edge and on the centre line, rather than checking pixel dimensions alone —
dimensions can be right while the crop is still off-centre or discarding the
wrong side.
"""

from __future__ import annotations

import io
from typing import cast

import pytest
from PIL import Image

from marketing.definitions import PLACEMENTS
from marketing.render import UnknownPlacementError, _center_crop_box, render

_LEFT = (255, 0, 0)
_RIGHT = (0, 255, 0)
_TOP = (255, 0, 0)
_BOTTOM = (0, 255, 0)
_CENTRE = (0, 0, 255)
_BACKGROUND = (255, 255, 255)


def _png_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _solid(width: int, height: int, color: tuple[int, int, int] = _BACKGROUND) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def _marked_horizontally(width: int, height: int) -> Image.Image:
    """A background image with distinct columns at the left edge, right edge
    and centre — for asserting a *width* crop kept the centre and discarded
    both edges."""
    img = _solid(width, height)
    for y in range(height):
        img.putpixel((0, y), _LEFT)
        img.putpixel((width - 1, y), _RIGHT)
        img.putpixel((width // 2, y), _CENTRE)
    return img


def _marked_vertically(width: int, height: int) -> Image.Image:
    """Same idea, rotated: distinct rows at the top edge, bottom edge and
    centre — for asserting a *height* crop kept the centre and discarded
    both edges."""
    img = _solid(width, height)
    for x in range(width):
        img.putpixel((x, 0), _TOP)
        img.putpixel((x, height - 1), _BOTTOM)
        img.putpixel((x, height // 2), _CENTRE)
    return img


def _colors(png_bytes: bytes) -> set[tuple[int, int, int]]:
    with Image.open(io.BytesIO(png_bytes)) as img:
        # getdata()'s stub covers every Pillow mode (L, F, RGBA, ...), so its
        # element type is a union pyright cannot narrow from `.convert("RGB")`
        # alone. The cast reflects a runtime fact (RGB pixels are always
        # 3-int tuples), not an assumption the test is making.
        rgb = img.convert("RGB")
        return {cast("tuple[int, int, int]", pixel) for pixel in rgb.getdata()}


# --- Determinism: the property the whole design rests on -------------------


def test_rendering_twice_is_byte_identical() -> None:
    """The headline claim. A crop is genuinely computed here (2:1 source into
    story_9x16), so this is not the no-op path — it is proof the encode path
    itself never varies between calls: no randomness, no timestamp, no
    run-varying metadata."""
    source = _png_bytes(_solid(400, 200))

    first = render(source, "story_9x16")
    second = render(source, "story_9x16")

    assert first == second


def test_rendering_is_deterministic_across_every_placement() -> None:
    """Not just one placement — the same source rendered into each of the
    three shapes must be stable on its own, every time."""
    source = _png_bytes(_solid(1000, 600))

    for placement in PLACEMENTS:
        assert render(source, placement) == render(source, placement)


# --- The no-op path is a real no-op, not a lossless re-encode --------------


def test_an_asset_already_at_the_placements_ratio_is_untouched() -> None:
    """PNG input, exact ratio match: output must be the *same object's*
    bytes, not merely visually identical."""
    source = _png_bytes(_solid(400, 500))  # exactly 4:5

    assert render(source, "portrait_4x5") == source


def test_the_noop_is_proven_with_jpeg_because_a_reencode_could_not_match() -> None:
    """A re-encode (even a careful, lossless-of-content one) cannot reproduce
    arbitrary JPEG bytes exactly — JPEG is lossy and Pillow's re-save is not
    guaranteed to reproduce the same compressed stream. So if this equality
    holds, the only explanation is that render() returned the input bytes
    without ever decoding them."""
    source = _jpeg_bytes(_solid(300, 300))  # exactly 1:1

    assert render(source, "feed_1x1") == source


def test_exactly_square_input_is_a_noop_for_feed_1x1() -> None:
    source = _png_bytes(_solid(50, 50))
    assert render(source, "feed_1x1") == source


# --- The cropping rule: centre-crop, correct axis, correct edges dropped ---


def test_landscape_source_for_story_9x16_crops_width_keeps_height() -> None:
    """A wide source asked for the tallest, narrowest placement: left and
    right are discarded, the full height survives."""
    width, height = 800, 200
    source = _png_bytes(_marked_horizontally(width, height))

    out = render(source, "story_9x16")

    with Image.open(io.BytesIO(out)) as img:
        assert img.height == height, "height must be preserved when cropping width"
        assert img.width < width, "width must actually have been cropped"

    colors = _colors(out)
    assert _CENTRE in colors, "the centre column must survive"
    assert _LEFT not in colors, "the left edge must be discarded"
    assert _RIGHT not in colors, "the right edge must be discarded"


def test_portrait_source_for_feed_1x1_crops_height_keeps_width() -> None:
    """A tall source asked for a square placement: top and bottom are
    discarded, the full width survives."""
    width, height = 200, 800
    source = _png_bytes(_marked_vertically(width, height))

    out = render(source, "feed_1x1")

    with Image.open(io.BytesIO(out)) as img:
        assert img.width == width, "width must be preserved when cropping height"
        assert img.height < height, "height must actually have been cropped"

    colors = _colors(out)
    assert _CENTRE in colors, "the centre row must survive"
    assert _TOP not in colors, "the top edge must be discarded"
    assert _BOTTOM not in colors, "the bottom edge must be discarded"


def test_a_very_wide_source_still_crops_to_the_exact_target_ratio() -> None:
    """An extreme case: a 10:1 banner asked for portrait_4x5 (0.8:1) — nearly
    everything is cut, but what remains must be exactly on-ratio."""
    source = _png_bytes(_solid(2000, 200))

    out = render(source, "portrait_4x5")

    with Image.open(io.BytesIO(out)) as img:
        w, h = img.size
        assert w * 5 == h * 4, f"{w}x{h} is not exactly 4:5"
        assert h == 200, "the shorter dimension is the one kept whole"


def test_a_very_tall_source_still_crops_to_the_exact_target_ratio() -> None:
    """The mirror case: a 1:10 source asked for feed_1x1."""
    source = _png_bytes(_solid(150, 1500))

    out = render(source, "feed_1x1")

    with Image.open(io.BytesIO(out)) as img:
        w, h = img.size
        assert w == h, f"feed_1x1 output {w}x{h} is not square"
        assert w == 150, "the shorter dimension is the one kept whole"


def test_a_tiny_source_still_renders_to_at_least_one_pixel() -> None:
    """No minimum-resolution floor is enforced (documented as out of scope),
    but the crop math must never collapse to zero or a negative extent."""
    source = _png_bytes(_solid(3, 3))

    out = render(source, "story_9x16")

    with Image.open(io.BytesIO(out)) as img:
        assert img.width >= 1
        assert img.height >= 1


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_every_declared_placement_renders_without_error(placement: str) -> None:
    """`_PLACEMENT_RATIOS` covering exactly `definitions.PLACEMENTS` is
    asserted at import time in render.py; this is the behavioural half —
    every name it declares must actually work end to end."""
    source = _png_bytes(_solid(1234, 777))
    out = render(source, placement)
    with Image.open(io.BytesIO(out)) as img:
        assert img.width > 0
        assert img.height > 0


# --- Bad input ---------------------------------------------------------


def test_an_unknown_placement_raises_rather_than_guessing() -> None:
    source = _png_bytes(_solid(100, 100))
    with pytest.raises(UnknownPlacementError):
        render(source, "billboard_21x9")


# --- The crop-box arithmetic directly: exact, integer, no float division --


def test_center_crop_box_rounds_with_exact_integer_arithmetic() -> None:
    """9:16 is not exactly representable in binary floating point, so this is
    the case most likely to expose float-division drift between runs or
    platforms. `_center_crop_box` never divides a float, so the result is
    pinned to one specific, hand-checked answer: 300 * 9 / 16 = 168.75, which
    rounds to 169 (half rounds up)."""
    left, top, right, bottom = _center_crop_box(1000, 300, 9, 16)
    assert (top, bottom) == (0, 300), "the full height must be kept"
    assert right - left == 169
    assert (left, right) == (415, 584)  # centred: (1000 - 169) // 2 == 415


def test_center_crop_box_is_centered_not_left_or_top_aligned() -> None:
    """A source proportionally *wider* than the target crops the width and
    keeps the full height — 100x100 asked for a 1:2 (width:height) box must
    narrow to a centred 50-pixel-wide column, not shrink its height."""
    left, top, right, bottom = _center_crop_box(100, 100, 1, 2)
    assert (top, bottom) == (0, 100), "height must be kept in full"
    assert (left, right) == (25, 75), "width must be cropped to a centred 50px column"
