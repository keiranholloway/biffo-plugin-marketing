"""Deterministic video assembly (video_assembly.py) — the core of M7.

The property the whole module rests on is stated in its own docstring:
**assembled, never generated**, and — extending `render.py`'s own headline
claim — the same inputs produce byte-identical mp4 output every time.
`test_assembling_twice_is_byte_identical` and its siblings below are the
whole point of this file; everything else checks that the composed video is
actually what was asked for (right placement ratio, right number of stills,
caption present/absent, failure on bad input).

Skipped, not failed, when no `ffmpeg` binary is on `PATH` — this module
shells out to a system binary this repo does not control (see
`video_assembly.py`'s "Deployment" docstring section), and a CI runner or a
contributor's machine without it should not read as this module being
broken.
"""

from __future__ import annotations

import io
import shutil
from typing import cast

import pytest
from PIL import Image

from marketing.definitions import PLACEMENTS
from marketing.render import UnknownPlacementError, _center_crop_box
from marketing.video_assembly import (
    FfmpegNotFoundError,
    InvalidCreativeError,
    VideoAssemblyError,
    _concat_file_text,
    _even,
    assemble,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not on PATH — see video_assembly.py's Deployment section.",
)


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _probe(mp4_bytes: bytes) -> dict[str, str]:
    """A handful of ffprobe fields, parsed from `-of default=nk=1`, keyed by
    the order requested. Used instead of a full JSON parse because every
    call site here wants exactly one or two scalar fields.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None, "ffprobe must be on PATH alongside ffmpeg for this test to run"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp4"
        path.write_bytes(mp4_bytes)
        result = subprocess.run(  # noqa: S603
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,nb_frames:format=duration",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
        )
    fields: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _first_frame_colors(mp4_bytes: bytes) -> set[tuple[int, int, int]]:
    """The distinct RGB colours present in the encoded video's first frame,
    extracted via ffmpeg back to a PNG and read with Pillow — for asserting
    what the encoder actually wrote, not just what was asked for."""
    import subprocess
    import tempfile
    from pathlib import Path

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None

    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "in.mp4"
        video_path.write_bytes(mp4_bytes)
        frame_path = Path(tmp) / "frame.png"
        subprocess.run(  # noqa: S603
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(frame_path),
            ],
            capture_output=True,
            check=True,
        )
        with Image.open(frame_path) as img:
            return {cast("tuple[int, int, int]", pixel) for pixel in img.convert("RGB").getdata()}


# --- Determinism: the property the whole module rests on -------------------


def test_assembling_twice_is_byte_identical() -> None:
    """The headline claim, extended from render.py's still-image case to a
    real encode: two stills, a caption, composed twice, must match exactly —
    not merely play back the same, but be the same bytes."""
    stills = [_png_bytes(320, 320, (200, 40, 40)), _png_bytes(320, 320, (40, 200, 40))]

    first = assemble(stills, "feed_1x1", text="Byte for byte.", seconds_per_frame=0.5)
    second = assemble(stills, "feed_1x1", text="Byte for byte.", seconds_per_frame=0.5)

    assert first == second


def test_assembling_is_deterministic_across_every_placement() -> None:
    stills = [_png_bytes(500, 300, (10, 10, 200))]
    for placement in PLACEMENTS:
        assert assemble(stills, placement, seconds_per_frame=0.5) == assemble(
            stills, placement, seconds_per_frame=0.5
        )


def test_a_single_still_with_no_caption_is_deterministic() -> None:
    """The simplest possible call — one frame, no text — is still pinned:
    proves determinism does not depend on the multi-frame or caption paths."""
    stills = [_png_bytes(400, 400, (5, 5, 5))]

    first = assemble(stills, "feed_1x1", seconds_per_frame=0.25)
    second = assemble(stills, "feed_1x1", seconds_per_frame=0.25)

    assert first == second


def test_odd_dimensioned_source_still_assembles_deterministically() -> None:
    """render.py's crop can legitimately hand back an odd width/height (it
    has no reason to avoid one) — this is the case the even-padding step
    exists for, and it must not reintroduce nondeterminism of its own."""
    stills = [_png_bytes(401, 401, (90, 90, 90))]

    first = assemble(stills, "feed_1x1", seconds_per_frame=0.25)
    second = assemble(stills, "feed_1x1", seconds_per_frame=0.25)

    assert first == second


# --- The composed video is actually what was asked for ---------------------


def test_output_is_cropped_to_the_placements_exact_ratio() -> None:
    """Reuses render.py for the crop — this proves the crop's exact pixel
    dimensions survive into the encoded stream, at each placement, up to the
    even-dimension padding `_even` applies afterward (H.264's yuv420p cannot
    represent an odd size at all — see the module docstring). Expected
    dimensions are computed the same way `render.py`'s own crop-box tests do
    (`_center_crop_box` directly), then padded, rather than asserting the
    ratio survives exactly — it provably cannot when the crop box itself is
    odd-sized, which `_center_crop_box` legitimately produces."""
    source_w, source_h = 900, 500
    stills = [_png_bytes(source_w, source_h, (0, 0, 0))]

    for placement, (ratio_w, ratio_h) in {
        "feed_1x1": (1, 1),
        "portrait_4x5": (4, 5),
        "story_9x16": (9, 16),
    }.items():
        left, top, right, bottom = _center_crop_box(source_w, source_h, ratio_w, ratio_h)
        expected_w = _even(right - left)
        expected_h = _even(bottom - top)

        out = assemble(stills, placement, seconds_per_frame=0.25)
        probe = _probe(out)
        width, height = int(probe["width"]), int(probe["height"])

        assert (width, height) == (expected_w, expected_h), (
            f"{placement}: got {width}x{height}, expected {expected_w}x{expected_h}"
        )


def test_multiple_stills_extend_the_total_duration() -> None:
    stills_one = [_png_bytes(300, 300, (1, 1, 1))]
    stills_three = [
        _png_bytes(300, 300, (1, 1, 1)),
        _png_bytes(300, 300, (2, 2, 2)),
        _png_bytes(300, 300, (3, 3, 3)),
    ]

    one = _probe(assemble(stills_one, "feed_1x1", seconds_per_frame=1.0))
    three = _probe(assemble(stills_three, "feed_1x1", seconds_per_frame=1.0))

    assert float(three["duration"]) == pytest.approx(3 * float(one["duration"]), rel=0.05)


def test_seconds_per_frame_is_honoured_in_the_encoded_duration() -> None:
    stills = [_png_bytes(300, 300, (7, 7, 7))]

    out = assemble(stills, "feed_1x1", seconds_per_frame=2.0)
    probe = _probe(out)

    assert float(probe["duration"]) == pytest.approx(2.0, abs=0.1)


# --- Bad input ---------------------------------------------------------


def test_an_unknown_placement_raises_the_same_error_render_does() -> None:
    """Delegated to render.py's own error, deliberately not re-implemented —
    see the module docstring."""
    stills = [_png_bytes(100, 100, (0, 0, 0))]
    with pytest.raises(UnknownPlacementError):
        assemble(stills, "billboard_21x9")


def test_an_empty_creative_list_raises_rather_than_producing_nothing() -> None:
    with pytest.raises(ValueError, match="at least one still"):
        assemble([], "feed_1x1")


def test_undecodable_creative_bytes_raise_invalid_creative_error() -> None:
    """Not a PIL.UnidentifiedImageError/OSError escaping unwrapped — wrapped
    into this module's own documented error type."""
    with pytest.raises(InvalidCreativeError):
        assemble([b"this is not an image"], "feed_1x1")


@pytest.mark.parametrize("seconds_per_frame", [0.0, -1.0, -0.001])
def test_non_positive_seconds_per_frame_raises(seconds_per_frame: float) -> None:
    stills = [_png_bytes(100, 100, (0, 0, 0))]
    with pytest.raises(ValueError, match="seconds_per_frame must be positive"):
        assemble(stills, "feed_1x1", seconds_per_frame=seconds_per_frame)


def test_transparent_source_is_composited_over_black_not_channel_dropped() -> None:
    """A raw `.convert('RGB')` on an RGBA image keeps whatever RGB values sit
    under the transparent pixels — for a freshly-created `Image.new('RGBA',
    ..., (0, 0, 0, 0))` that happens to be black anyway, so this test uses a
    transparent pixel whose RGB channels are deliberately something else
    (white) to prove the encoded frame shows the composited background
    (black, matching the even-padding colour) and not the stored-but-hidden
    white."""
    transparent_white = Image.new("RGBA", (300, 300), (255, 255, 255, 0))
    buffer = io.BytesIO()
    transparent_white.save(buffer, format="PNG")

    out = assemble([buffer.getvalue()], "feed_1x1", seconds_per_frame=0.25)
    probe_colors = _first_frame_colors(out)

    assert (255, 255, 255) not in probe_colors, "transparent white must not leak through"
    assert (0, 0, 0) in probe_colors, "transparent pixels must composite onto the black background"


# --- Failure modes this module names explicitly -----------------------


def test_missing_ffmpeg_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # `video_assembly` does `import shutil` and calls `shutil.which(...)`, so
    # patching the `shutil` module's own attribute (the same module object
    # both files import) is enough — no need to reach into the other module.
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    stills = [_png_bytes(100, 100, (0, 0, 0))]
    with pytest.raises(FfmpegNotFoundError):
        assemble(stills, "feed_1x1")


def test_ffmpeg_failure_surfaces_as_video_assembly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ffmpeg exit must not be swallowed — simulated by pointing
    `_ffmpeg_path` at a binary that always fails (`false`), proving the
    return-code check actually fires rather than only being reachable in
    theory."""
    import marketing.video_assembly as video_assembly_module

    false_binary = shutil.which("false")
    assert false_binary is not None, "the `false` binary must exist for this test to mean anything"
    monkeypatch.setattr(video_assembly_module, "_ffmpeg_path", lambda: false_binary)

    stills = [_png_bytes(100, 100, (0, 0, 0))]
    with pytest.raises(VideoAssemblyError):
        assemble(stills, "feed_1x1")


def test_ffmpeg_timeout_surfaces_as_video_assembly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged ffmpeg must not block assemble() forever — simulated by
    making `subprocess.run` itself raise `TimeoutExpired`, proving the
    `except` around it actually fires rather than only being reachable in
    theory. Not exercised by actually waiting `_FFMPEG_TIMEOUT_S` seconds:
    that would make this the slowest test in the suite for no extra
    coverage."""
    import subprocess

    import marketing.video_assembly as video_assembly_module

    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["ffmpeg"], timeout=video_assembly_module._FFMPEG_TIMEOUT_S
        )

    monkeypatch.setattr(video_assembly_module.subprocess, "run", _raise_timeout)

    stills = [_png_bytes(100, 100, (0, 0, 0))]
    with pytest.raises(VideoAssemblyError, match="did not finish"):
        assemble(stills, "feed_1x1")


# --- Frame-count arithmetic directly: exact, no drift from timing edge cases


def test_even_rounds_up_only_when_needed() -> None:
    assert _even(400) == 400
    assert _even(401) == 402
    assert _even(0) == 0


def test_concat_file_text_repeats_each_frame_the_exact_computed_count() -> None:
    """No `duration` directive — see the module docstring's "No per-run
    frame-count ambiguity". Each name must appear exactly
    `round(seconds_per_frame * _FPS)` times, in order."""
    text = _concat_file_text(["a.png", "b.png"], seconds_per_frame=0.5)
    lines = text.strip().splitlines()

    # _FPS is 24; 0.5s * 24fps = 12 repeats per frame.
    assert lines.count("file 'a.png'") == 12
    assert lines.count("file 'b.png'") == 12
    assert lines[:12] == ["file 'a.png'"] * 12
    assert lines[12:] == ["file 'b.png'"] * 12
