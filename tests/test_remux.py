"""Tests for the remux argv on its own, now that it is a function rather than part of a session.

The session tests already check what FFmpeg is told through a running session; these pin the
argv directly, which is the thing `tools/verify_rtp_against_addon.sh` replays on the add-on's
FFmpeg (DEC-0001).
"""

from __future__ import annotations

from ezviz_stream_bridge.payload import _PayloadPlan
from ezviz_stream_bridge.remux import remux_argv


def _plan(demuxer: str, *, audio: int | None = None) -> _PayloadPlan:
    return _PayloadPlan("RTP", demuxer, demuxer, 96, (), audio_payload_type=audio)


def test_mpeg_ps_keeps_its_own_timestamps() -> None:
    argv = remux_argv("ffmpeg", _PayloadPlan("MPEG_PS", "mpeg", None, None, ()), log_level="error")

    assert argv == [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "mpeg", "-i", "pipe:0",
        "-c", "copy", "-f", "mpegts", "pipe:1",
    ]  # fmt: skip


def test_an_elementary_stream_is_stamped_with_the_wallclock() -> None:
    argv = remux_argv("ffmpeg", _plan("hevc"), log_level="info")

    assert argv[argv.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert argv[argv.index("-f") + 1] == "hevc"
    assert argv[argv.index("-loglevel") + 1] == "info"


def test_the_audio_is_a_second_input_on_its_own_descriptor_and_clock() -> None:
    argv = remux_argv(
        "ffmpeg", _plan("h264", audio=104), log_level="error", audio_fd=7, audio_offset=-0.0625
    )

    second = argv.index("pipe:7")
    assert argv[second - 1] == "-i"
    assert argv[second - 3 : second - 1] == ["-f", "aac"]
    assert argv[argv.index("-itsoffset") + 1] == "-0.062"
    # The wallclock is the video's only: the audio keeps the clock its demuxer gives it.
    assert argv.count("-use_wallclock_as_timestamps") == 1
    assert argv.index("-use_wallclock_as_timestamps") < argv.index("pipe:0")
