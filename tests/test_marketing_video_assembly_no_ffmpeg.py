"""The `OSError`/`subprocess.TimeoutExpired` branches of `video_assembly.py`,
exercised WITHOUT a real `ffmpeg` binary on `PATH`.

`tests/test_marketing_video_assembly.py` skips its ENTIRE module when ffmpeg
is absent (deliberately — most of it renders and inspects real mp4 output,
which genuinely needs the binary). But that module-wide skip also hides two
tests that do not actually need ffmpeg at all, and on a CI runner with no
ffmpeg installed, that made both of these named failure modes completely
unexercised — not merely skipped for a good reason, but never observed by
anything.

Both are decoupled from ffmpeg here, so they run everywhere:

- `_prepare_frame`'s `except OSError` (an undecodable creative) is called
  directly. This is purely a Pillow decode failure — `_prepare_frame` never
  shells out — so routing it through `assemble()` (which calls
  `_ffmpeg_path()` before it ever reaches `_prepare_frame`) was the only
  reason it needed ffmpeg in the first place.
- `assemble()`'s `except subprocess.TimeoutExpired` is reached with
  `_ffmpeg_path` monkeypatched to a fake path and `subprocess.run` replaced
  outright, exactly as `test_marketing_video_assembly.py`'s own
  `test_ffmpeg_timeout_surfaces_as_video_assembly_error` does for
  `subprocess.run` — except that test leaves `_ffmpeg_path` untouched, so it
  still needs a real `ffmpeg` on `PATH` purely to get past that call, even
  though the mocked `subprocess.run` means the binary is never actually
  invoked. Faking `_ffmpeg_path` too removes that unnecessary dependency.
"""

from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image

import marketing.video_assembly as video_assembly_module
from marketing.video_assembly import InvalidCreativeError, VideoAssemblyError, _prepare_frame


def test_prepare_frame_wraps_an_undecodable_creative_without_ffmpeg() -> None:
    """The `OSError` branch (`_prepare_frame`, video_assembly.py ~line 292):
    Pillow's `UnidentifiedImageError`/`OSError` on unreadable bytes must not
    escape unwrapped — it becomes this module's own `InvalidCreativeError`,
    naming what actually happened rather than surfacing a bare Pillow error
    to a caller that has never imported PIL."""
    with pytest.raises(InvalidCreativeError, match="Could not decode a creative as an image"):
        _prepare_frame(b"this is not an image", "feed_1x1", text=None)


def test_assemble_wraps_a_timed_out_ffmpeg_without_a_real_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `subprocess.TimeoutExpired` branch (`assemble()`, video_assembly.py
    ~line 430): a wedged ffmpeg must surface as this module's own named
    `VideoAssemblyError`, not block the caller until some outer timeout (a
    Lambda's own) fires opaquely instead."""

    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["ffmpeg"], timeout=video_assembly_module._FFMPEG_TIMEOUT_S
        )

    monkeypatch.setattr(video_assembly_module, "_ffmpeg_path", lambda: "ffmpeg-stub-never-run")
    monkeypatch.setattr(video_assembly_module.subprocess, "run", _raise_timeout)

    still = Image.new("RGB", (100, 100), (0, 0, 0))
    buffer = io.BytesIO()
    still.save(buffer, format="PNG")

    with pytest.raises(VideoAssemblyError, match="did not finish"):
        video_assembly_module.assemble([buffer.getvalue()], "feed_1x1")
