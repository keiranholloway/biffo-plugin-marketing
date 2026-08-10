"""Deterministic video assembly (M7) — the decision this milestone made.

Issue #6 gated M7 on a spike: pick a generative-video provider, produce real
assets, agree a quality bar and a cost ceiling before writing any code. That
spike was not run. Instead the owner decided — and this module is the
record — to build **deterministic assembly** instead of generative video, for
reasons the issue itself anticipated and ``render.py``'s module docstring
already argues in miniature for still images:

1. **The renderer this needs already exists and is proven.** ``render.py``
   (M5) centre-crops one approved creative into every placement's exact
   ratio with integer arithmetic and byte-identical, no-op-when-possible
   output. This module reuses it rather than re-solving cropping.
2. **Cheaper, more controllable, and usually indistinguishable at social
   sizes** — the issue's own words for what assembly buys over generation.
3. **Generative video is the least predictable cost in the roadmap**, and
   would need a provider port, a queue, pre-generation cost estimation and a
   new failure class — all to produce output whose quality this milestone
   was never able to agree a bar for, because the spike that would have set
   one never ran.

So: **no model call anywhere in this module.** It takes the bytes of one or
more already-approved stills (the same creatives ``render.py`` crops for
placements) plus optional caption text, and produces a short H.264 mp4 for
one of ``definitions.PLACEMENTS`` — composed, not generated.

## Mechanism: ffmpeg via subprocess, not a pure-Python encoder

A pure-Python MP4/H.264 encoder is real work with no upside here: ffmpeg is a
single, well-understood dependency that already does exactly this, correctly,
and its determinism can be pinned with documented flags (below) rather than
built from scratch and re-proven. The cost is a **system binary this module
does not control**: see "Deployment" below.

## Determinism

The same property `render.py` rests on, extended to video: the same input
stills, the same placement, the same caption text and the same per-frame
duration must produce **byte-identical** mp4 output, every time — proven
directly in ``tests/test_marketing_video_assembly.py`` by assembling twice
and comparing bytes, the same pattern ``test_marketing_render.py`` uses for
the still-image case. ffmpeg is not deterministic by default; every flag
below exists to close one specific source of run-to-run drift, found by
assembling the same input twice and diffing the output until nothing
differed:

- **No wall-clock metadata.** mp4's ``mvhd``/``tkhd``/``mdhd`` atoms carry a
  creation/modification timestamp that defaults to "now" — ``-metadata
  creation_time=...`` pins it to a fixed value (not merely tagging metadata;
  the mov muxer reads this same value into those atoms). ``-map_metadata -1``
  strips anything else ffmpeg would otherwise copy from the input.
- **No run-varying encoder identification.** ``-fflags +bitexact -flags:v
  +bitexact`` stop libx264 and the mp4 muxer writing a version-carrying
  encoder tag; without it the output still differs byte-for-byte between
  ffmpeg builds (never observed to differ between two calls in the *same*
  process, but bitexact is what makes that a guarantee rather than an
  accident of this one ffmpeg build).
- **No thread-count-dependent encoding.** libx264's frame-parallel encoding
  can make its output depend on how many threads it ran with, which varies
  with the host's core count — a source of drift that would be invisible on
  one machine and real across two. ``-threads 1 -x264opts threads=1`` pins
  single-threaded encoding so the *host's* core count cannot leak into the
  output.
- **No per-run frame-count ambiguity.** Rather than lean on the concat
  demuxer's own ``duration`` directive (which has a well-known quirk: it is
  silently ignored for the last entry, and stacks unpredictably with output
  trimming), each still's concat-file entry is the same source frame
  **repeated** ``round(seconds_per_frame * fps)`` times, with ``-r`` set as
  an **input** rate. The total duration and frame count are then exact by
  construction — no directive whose edge behaviour has to be trusted.
  Confirmed empirically: two runs of an otherwise-identical two-still,
  six-second assembly produced exactly 144 frames and a 6.000000s duration,
  byte-identical, whether run from the same temp directory or two different
  ones a second apart.
- **No system-font dependency.** Caption text is drawn with Pillow's own
  embedded bitmap font (``ImageFont.load_default``), never a system font —
  system font availability and hinting vary by platform, which would make a
  captioned frame the one thing here that is not reproducible across
  machines. Confirmed: two independently-drawn captions on identical canvases
  produce byte-identical PNGs.

Not proven, and not claimed: byte-identical output **across different ffmpeg
builds/versions**. The bitexact flags close the known sources of
cross-version drift, but the only thing this module (and its tests) actually
demonstrates is repeat-call determinism on one ffmpeg binary — which is the
property "the same input always produces the same output" needs, and the one
a deployed Lambda actually holds fixed (one bundled binary, not a moving
target).

## Deployment: this needs a real ffmpeg binary — a Lambda layer, concretely

This module shells out to whatever ``ffmpeg`` resolves to on ``PATH``
(``shutil.which``) and raises ``FfmpegNotFoundError`` — loudly, at call time
— if there isn't one. **No Lambda layer for it exists yet in this repo's
``terraform/``.** A production deployment needs one bundling a static
``ffmpeg`` binary (several public layers exist, e.g. the
``ffmpeg-lambda-layer`` project) attached to the Lambda this admin app runs
in. That is infrastructure this milestone did not add — flagged here
deliberately rather than discovered at deploy time.

**Needs ffmpeg >= 5.1** — the ``-fps_mode`` option this module passes was
added in that release (it replaces the older, deprecated ``-vsync``). Several
widely-used community Lambda ffmpeg layers bundle older static builds that
predate it; whichever layer gets attached must be checked against this
before relying on it, or `assemble()` fails every call in production with a
`VideoAssemblyError` despite passing every test here against a newer local
binary. Not version-checked at runtime — a fast, loud failure on the very
first real call is an acceptable way to discover a wrong layer, and probing
`ffmpeg -version` output on every call would cost real latency for a
condition that, once the right layer is attached, never recurs.

## Frame preparation, not just cropping

``render.py`` is reused for the crop — this module never recomputes an
aspect ratio or a crop box. But a frame ffmpeg can encode needs two more
things `render.py` does not promise:

- **Even width and height.** H.264's ``yuv420p`` pixel format cannot
  represent an odd dimension at all (ffmpeg fails outright, confirmed:
  ``Invalid argument`` from libx264 on a 401x401 frame with no padding).
  ``render.py``'s crop can legitimately produce an odd dimension — it has no
  reason to avoid one, since nothing about a still image requires an even
  size. This module pads to the nearest even size (black, bottom/right)
  after the crop, before ffmpeg ever sees the frame.
- **A caption, optionally**, composited in Pillow rather than ffmpeg's
  ``drawtext`` filter — see "No system-font dependency" above.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from marketing.render import render as render_placement

#: Fixed so the concat file's frame-repeat count is exact and so the same
#: value is used everywhere duration math happens. Not configurable per call:
#: varying it would vary the encoded stream's frame count/timing, which is a
#: legitimate reason for two assemblies to differ — but nothing in this
#: milestone needs that yet, and a hardcoded value keeps the determinism
#: claim about *what this module does today*, not what it could be asked to.
_FPS = 24

#: How long a single still is shown, absent an explicit ``seconds_per_frame``.
_DEFAULT_SECONDS_PER_FRAME = 3.0

#: Pinned into the mp4's mvhd/tkhd/mdhd atoms (see module docstring). The
#: epoch is arbitrary — only fixedness matters.
_FIXED_CREATION_TIME = "1970-01-01T00:00:00.000000Z"

#: A stuck ffmpeg process (pathological input, a wedged bundled binary) must
#: not block `assemble()` forever — without this, the failure mode is the
#: Lambda's own function timeout killing the process, which surfaces as an
#: opaque infra-level timeout instead of the diagnosable `VideoAssemblyError`
#: this module otherwise guarantees. Generous relative to any clip this
#: milestone produces (a handful of stills, single-digit seconds each,
#: single-threaded medium-preset libx264) so a slow-but-healthy encode is
#: never mistaken for a hang.
_FFMPEG_TIMEOUT_S = 120

_CAPTION_FONT_SIZE = 28
_CAPTION_MARGIN = 16
_CAPTION_TEXT_COLOR = (255, 255, 255, 255)
_CAPTION_BAND_COLOR = (0, 0, 0, 170)


class FfmpegNotFoundError(RuntimeError):
    """No ``ffmpeg`` binary on ``PATH``. See the module docstring's
    "Deployment" section — this is a packaging gap to fix, not a bug in this
    module."""


class VideoAssemblyError(RuntimeError):
    """ffmpeg ran and either exited non-zero or ran past `_FFMPEG_TIMEOUT_S`
    and was killed. Carries its stderr tail (or "timed out") so the actual
    encoder failure is visible, not just "ffmpeg failed"."""


class InvalidCreativeError(ValueError):
    """One of `creative_bytes` is not decodable as an image at all (a
    truncated upload, a wrong content-type). Deliberately a `ValueError`
    subclass — `assemble`'s documented contract already includes `ValueError`
    for bad input (an empty list), and undecodable bytes is the same kind of
    caller mistake, not a new category. Raised instead of letting Pillow's
    own `UnidentifiedImageError`/`OSError` escape unwrapped, which would be
    an exception type nothing in this module's documented contract mentions.
    """


def _ffmpeg_path() -> str:
    resolved = shutil.which("ffmpeg")
    if resolved is None:
        raise FfmpegNotFoundError(
            "ffmpeg is not on PATH. This module shells out to a system ffmpeg "
            "binary for deterministic mp4 encoding (see video_assembly.py's "
            "module docstring) — the Lambda package deploying this plugin "
            "must bundle one (e.g. a Lambda layer); nothing in this repo "
            "does that yet."
        )
    return resolved


def _even(value: int) -> int:
    """`value`, rounded up to the nearest even number."""
    return value + (value % 2)


def _draw_caption(image: Image.Image, text: str) -> None:
    """Composite `text` onto `image` in place, bottom-anchored on a
    translucent band. Pillow's embedded bitmap font only — see the module
    docstring's "No system-font dependency" for why.
    """
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default(size=_CAPTION_FONT_SIZE)
    width, height = image.size
    _left, top, _right, bottom = draw.textbbox((0, 0), text, font=font)
    text_height = bottom - top
    band_top = max(0, height - text_height - 2 * _CAPTION_MARGIN)
    draw.rectangle([(0, band_top), (width, height)], fill=_CAPTION_BAND_COLOR)
    draw.text(
        (_CAPTION_MARGIN, band_top + _CAPTION_MARGIN),
        text,
        font=font,
        fill=_CAPTION_TEXT_COLOR,
    )


def _to_opaque_rgb(source: Image.Image) -> Image.Image:
    """`source` as opaque RGB, compositing any alpha channel over black
    rather than dropping it.

    mp4/H.264 has no alpha channel, so an RGB frame is required either way —
    but `Image.convert("RGB")` on an image with transparency (`RGBA`, `LA`,
    or a palette image with an alpha info entry) does a raw channel copy: it
    keeps whatever RGB values were stored under the transparent pixels, which
    for most encoders/tools is undefined content, not "the colour that should
    show through". A source with real transparency would then render as
    visibly wrong output — silently, since nothing about a raw channel copy
    raises. Compositing over a fixed black background (matching the black
    used for even-dimension padding just below) makes the "what shows where
    the source was transparent" choice explicit and deterministic instead of
    inheriting whatever bytes happened to be there.
    """
    if source.mode in ("RGBA", "LA") or (source.mode == "P" and "transparency" in source.info):
        rgba = source.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return source.convert("RGB")


def _prepare_frame(creative_bytes: bytes, placement: str, *, text: str | None) -> bytes:
    """One placement-cropped, even-dimensioned, optionally captioned PNG
    frame, ready for ffmpeg's ``image2``/concat input.

    Always decodes and re-saves through Pillow, even on `render_placement`'s
    byte-identical no-op path — ffmpeg's input needs even dimensions (see the
    module docstring), which `render.py` does not promise, so this cannot
    skip the round-trip the way `render.render` itself does.

    Raises `InvalidCreativeError` if `creative_bytes` (after `render_placement`
    has cropped or passed it through) is not decodable as an image at all —
    Pillow's own `UnidentifiedImageError`/`OSError` on a truncated or
    non-image input is deliberately not left to escape unwrapped (see that
    error's own docstring).

    Not handled, deliberately out of scope for this milestone, same posture
    `render.py` takes for EXIF orientation: Adobe-inverted CMYK JPEGs. A CMYK
    source that already matches `placement`'s ratio takes `render.render`'s
    no-op path unchanged, and this function's `.convert()` then uses
    Pillow's non-Adobe-aware CMYK interpretation — visibly wrong colours, not
    a crash, and not new: `render.py`'s own no-op path already hands back
    such bytes unmodified for any other consumer to interpret.
    """
    try:
        # `render_placement` (render.render) is where the FIRST decode
        # happens — it opens `creative_bytes` itself to read width/height —
        # so an undecodable creative fails there, not in the `Image.open`
        # below. Both calls are covered by the same try/except: Pillow
        # raises `UnidentifiedImageError` (an `OSError` subclass) for
        # unrecognised bytes, and a plain `OSError` for a truncated file it
        # partially recognised — both are "this isn't a usable image", not a
        # bug in this module or in `render.py`.
        cropped = render_placement(creative_bytes, placement)
        with Image.open(io.BytesIO(cropped)) as source:
            frame = _to_opaque_rgb(source)
    except OSError as exc:
        # Deliberately just `OSError`, not `ValueError` too:
        # `UnknownPlacementError` (raised by `render_placement` for a bad
        # `placement`) is a `ValueError` subclass with nothing to do with
        # decoding, and must propagate unwrapped for `assemble`'s documented
        # contract to hold. `PIL.UnidentifiedImageError` — the actual
        # bad-bytes case this except exists for — is an `OSError` subclass.
        raise InvalidCreativeError(f"Could not decode a creative as an image: {exc}") from exc

    width, height = frame.size
    even_width, even_height = _even(width), _even(height)
    if (even_width, even_height) != (width, height):
        padded = Image.new("RGB", (even_width, even_height), (0, 0, 0))
        padded.paste(frame, (0, 0))
        frame = padded

    if text:
        _draw_caption(frame, text)

    buffer = io.BytesIO()
    # Same fixed encoder parameters as render.py, for the same reason: no
    # optimize pass and a fixed compress_level keep the PNG encode itself
    # from being a source of drift, independent of ffmpeg entirely.
    frame.save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def _concat_file_text(frame_names: list[str], seconds_per_frame: float) -> str:
    """The ffmpeg concat demuxer script: each frame's filename repeated
    ``round(seconds_per_frame * _FPS)`` times, no ``duration`` directive.
    See the module docstring's "No per-run frame-count ambiguity" for why
    repetition is used instead of ``duration``.
    """
    repeats = max(1, round(seconds_per_frame * _FPS))
    lines = [f"file '{name}'" for name in frame_names for _ in range(repeats)]
    return "\n".join(lines) + "\n"


def assemble(
    creative_bytes: list[bytes],
    placement: str,
    *,
    text: str | None = None,
    seconds_per_frame: float = _DEFAULT_SECONDS_PER_FRAME,
) -> bytes:
    """An mp4 for `placement`, composed from one or more already-approved
    stills in `creative_bytes` (shown in order, each for `seconds_per_frame`
    seconds) with `text` optionally burned in as a caption on every frame.

    Deterministic: the same arguments produce byte-identical mp4 bytes, every
    time (see the module docstring's Determinism section).

    Raises `ValueError` for an empty `creative_bytes` or a non-positive
    `seconds_per_frame`, `InvalidCreativeError` (a `ValueError` subclass) for
    a creative that is not decodable as an image, `marketing.render.
    UnknownPlacementError` for a `placement` outside `definitions.PLACEMENTS`
    (raised by the first call to `render_placement` inside `_prepare_frame`
    below — not re-checked here, so there is exactly one place that decides
    what a valid placement is), `FfmpegNotFoundError` if no ffmpeg binary is
    available, and `VideoAssemblyError` if ffmpeg runs and fails or runs
    past `_FFMPEG_TIMEOUT_S`.
    """
    if not creative_bytes:
        raise ValueError("assemble() needs at least one still to compose.")
    if seconds_per_frame <= 0:
        # Silently clamped to one frame by `_concat_file_text`'s `max(1,
        # round(...))` otherwise — a near-zero or negative value is far more
        # likely a caller's sign error than an intentional flash-frame, and
        # `creative_bytes` already gets this same fail-fast treatment above.
        raise ValueError(f"seconds_per_frame must be positive, got {seconds_per_frame!r}.")

    ffmpeg = _ffmpeg_path()

    frames = [_prepare_frame(b, placement, text=text) for b in creative_bytes]

    with tempfile.TemporaryDirectory(prefix="marketing-video-assembly-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        frame_names: list[str] = []
        for index, frame in enumerate(frames):
            name = f"frame_{index:04d}.png"
            (tmp_path / name).write_bytes(frame)
            frame_names.append(name)

        concat_path = tmp_path / "concat.txt"
        concat_path.write_text(_concat_file_text(frame_names, seconds_per_frame))

        output_path = tmp_path / "output.mp4"
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-r",
            str(_FPS),
            "-i",
            str(concat_path),
            "-fps_mode",
            "cfr",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-threads",
            "1",
            "-x264opts",
            "threads=1",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-metadata",
            f"creation_time={_FIXED_CREATION_TIME}",
            str(output_path),
        ]
        # Every element of `command` is either a literal flag or a path this
        # function created itself under `tmp_path` — nothing here comes from
        # the caller unescaped (placement/text only ever reach Pillow, never
        # the shell), and shell=True is never used.
        try:
            result = subprocess.run(  # noqa: S603
                command,
                cwd=tmp_path,
                capture_output=True,
                check=False,
                timeout=_FFMPEG_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            # Without this, a wedged ffmpeg blocks assemble() until whatever
            # calls it times out first — a Lambda's own function timeout,
            # opaquely, instead of this module's own named error. See
            # `_FFMPEG_TIMEOUT_S`'s comment for why this bound is generous.
            raise VideoAssemblyError(
                f"ffmpeg did not finish within {_FFMPEG_TIMEOUT_S}s and was killed."
            ) from exc

        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-4000:]
            raise VideoAssemblyError(f"ffmpeg exited {result.returncode}: {stderr_tail}")

        return output_path.read_bytes()
