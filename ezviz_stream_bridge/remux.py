"""The FFmpeg command line a session remuxes with: MPEG-TS out of whatever the camera sent.

Kept as one pure function so the argv is in one place: the tests read it, and
`tools/verify_rtp_against_addon.sh` feeds what it produces to the FFmpeg the add-on image
installs (DEC-0001). Split out of `CloudSession._start_ffmpeg` in 0.1.18, unchanged; starting
the process, its pipes and its descriptors stay with the session.
"""

from __future__ import annotations

from .payload import _AAC_DEMUXER, _MPEG_PS_DEMUXER, _PayloadPlan


def remux_argv(
    ffmpeg_path: str,
    plan: _PayloadPlan,
    *,
    log_level: str,
    audio_fd: int | None = None,
    audio_offset: float = 0.0,
) -> list[str]:
    """The argv that remuxes `plan`'s payload, with no re-encoding.

    Two input formats are possible. MPEG-PS is the case this was built for and FFmpeg
    demuxes it directly with `mpeg`. RTP is not, and no demuxer reads it out of a pipe --
    `rtp` wants a UDP URL and an SDP -- so by the time FFmpeg is started the session has
    depacketized to Annex-B and the demuxer names the elementary stream it is receiving,
    `h264` or `hevc`.

    `-use_wallclock_as_timestamps` is what makes that second case work at all: a raw
    elementary stream has no container to carry a timestamp, and without one the MPEG-TS
    muxer refuses the packet outright -- `first pts and dts value must be set`, zero bytes
    written, for H.264 and HEVC alike. Wallclock is the packet's arrival time, which for a
    live stream is the right reading anyway.

    `audio_fd` is the read end of the audio pipe when the plan has audio, named as
    `pipe:<fd>`: the only spelling that reads a pipe FFmpeg did not open itself.
    """
    argv = [ffmpeg_path, "-hide_banner", "-loglevel", log_level]
    if plan.demuxer != _MPEG_PS_DEMUXER:
        argv += ["-use_wallclock_as_timestamps", "1"]
    argv += ["-f", plan.demuxer, "-i", "pipe:0"]
    if audio_fd is not None:
        # The audio is the one input that keeps the clock it is given, so it is the
        # one input whose clock has to be its own: the demuxer paces an ADTS stream
        # at a frame per 1024 samples whatever the reads do, and the reads here are
        # bursts -- the pipe is read in one go after the video's probe. The
        # wallclock it was given is the read time, so a burst would stamp whole
        # seconds of audio into one instant and the muxer would play them folded;
        # the demuxer's own timeline is burst-proof. `-itsoffset` is the arrival gap
        # the session measured between its first audio packet and its first video
        # one, which is what makes the two timelines share a zero: the demuxer's
        # clock starts at the audio's first AU, the muxer's at the video's first
        # frame.
        argv += [
            "-itsoffset",
            f"{audio_offset:.3f}",
            "-f",
            _AAC_DEMUXER,
            "-i",
            f"pipe:{audio_fd}",
        ]
    argv += ["-c", "copy", "-f", "mpegts", "pipe:1"]
    return argv
