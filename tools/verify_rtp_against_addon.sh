#!/usr/bin/env bash
# Verify the RTP path against the FFmpeg the add-on actually runs.
#
# The ffmpeg on a development machine proves nothing about the add-on: the image installs
# Debian's, and a remux that worked on a laptop has failed inside the container before. This
# builds real H.264, HEVC and AAC elementary streams, packetizes them the way RFC 6184, RFC 7798
# and RFC 3640 say a camera does, and hands them to FFmpeg through the session itself -- the same
# prefix read, the same plan, the same argv, the same two pipes. It reimplements nothing: if the
# module is wrong, the bytes out are wrong.
#
# The audio leg is checked twice over: the ADTS rebuilt from the Access Units has to be the ADTS
# FFmpeg wrote in the first place, byte for byte, and the MPEG-TS out of the session has to
# carry an audio stream that decodes.
#
# Needs docker and network access -- the image installs ffmpeg and pyezvizapi on the way.
# Override the base image with EZVIZ_VERIFY_IMAGE if the add-on's Dockerfile moves on.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${EZVIZ_VERIFY_IMAGE:-ghcr.io/home-assistant/aarch64-base-debian:trixie-2026.02.0}"
script="$(mktemp -t verify_rtp.XXXXXX.py)"
trap 'rm -f "${script}"' EXIT

cat > "${script}" <<'PYTHON'
"""End-to-end check of the RTP path with the FFmpeg the add-on really runs.

Runs inside the add-on's base image (Debian trixie, the same ffmpeg package the Dockerfile
installs). It builds real elementary streams, packetizes them the way RFC 6184 / RFC 7798 / RFC
3640 say a camera does, and pushes everything through this repository's own session and
depacketizers. Nothing here reimplements either: if the module is wrong, the bytes out are wrong.
"""

from __future__ import annotations

import logging
import subprocess
import sys

sys.path.insert(0, "/src")

from pyezvizapi.stream import VtmChannel  # noqa: E402

from ezviz_stream_bridge import session as session_module  # noqa: E402
from ezviz_stream_bridge.rtp import (  # noqa: E402
    H264,
    HEVC,
    AacHbrDepacketizer,
    RtpDepacketizer,
    detect_codec,
)

MAX_PAYLOAD = 1200
AUDIO_PAYLOAD_TYPE = 104
ADTS_HEADER = 7


# -- the synthetic camera --------------------------------------------------------------


def split_annexb(data: bytes) -> list[bytes]:
    units: list[bytes] = []
    starts: list[int] = []
    index = 0
    while True:
        found = data.find(b"\x00\x00\x01", index)
        if found < 0:
            break
        starts.append(found + 3)
        index = found + 3
    for position, start in enumerate(starts):
        if position + 1 < len(starts):
            end = starts[position + 1] - 3
            while end > start and data[end - 1] == 0:
                end -= 1
        else:
            end = len(data)
        if data[start:end]:
            units.append(data[start:end])
    return units


def split_adts(data: bytes) -> list[bytes]:
    """The Access Units of an ADTS stream: every frame with its seven-byte header removed.

    Which is what an MPEG4-GENERIC sender has to strip, and what the bridge has to put back.
    """
    units: list[bytes] = []
    index = 0
    while index + ADTS_HEADER <= len(data):
        length = (
            ((data[index + 3] & 0x03) << 11) | (data[index + 4] << 3) | (data[index + 5] >> 5)
        )
        assert length > ADTS_HEADER, f"ADTS frame at {index} has length {length}"
        units.append(data[index + ADTS_HEADER : index + length])
        index += length
    return units


def rtp(sequence: int, payload: bytes, payload_type: int = 96) -> bytes:
    return (
        bytes([0x80, payload_type])
        + sequence.to_bytes(2, "big")
        + (0).to_bytes(4, "big")
        + b"\x55\x66\x77\x88"
        + payload
    )


def fragments(nal: bytes, codec: str) -> list[bytes]:
    header = 1 if codec == H264 else 2
    if len(nal) <= MAX_PAYLOAD:
        return [nal]
    body = nal[header:]
    chunks = [body[i : i + MAX_PAYLOAD] for i in range(0, len(body), MAX_PAYLOAD)]
    out: list[bytes] = []
    for position, chunk in enumerate(chunks):
        start = position == 0
        end = position == len(chunks) - 1
        if codec == H264:
            fu = (nal[0] & 0x1F) | (0x80 if start else 0) | (0x40 if end else 0)
            out.append(bytes([(nal[0] & 0xE0) | 28, fu]) + chunk)
        else:
            fu = ((nal[0] >> 1) & 0x3F) | (0x80 if start else 0) | (0x40 if end else 0)
            out.append(bytes([(nal[0] & 0x81) | (49 << 1), nal[1], fu]) + chunk)
    return out


def video_packets(codec: str, source: bytes) -> list[bytes]:
    nals = split_annexb(source)
    assert nals, f"{codec}: the synthetic stream produced no NAL units"
    packets: list[bytes] = []
    for nal in nals:
        packets += [rtp(len(packets), payload) for payload in fragments(nal, codec)]
    return packets


def audio_packets(units: list[bytes]) -> list[bytes]:
    """One Access Unit per packet, in the AU header section `AAC-hbr` prescribes.

    A 16-bit bit-length, then a 13-bit size beside a 3-bit index, then the unit itself --
    which is the shape the reported camera's own payload has.
    """
    packets: list[bytes] = []
    for index, unit in enumerate(units):
        section = (16).to_bytes(2, "big") + ((len(unit) << 3) | (index & 0x07)).to_bytes(2, "big")
        packets.append(rtp(index, section + unit, AUDIO_PAYLOAD_TYPE))
    return packets


# -- the session, over fabricated packets ------------------------------------------------


class FakePacket:
    """The parts of a VtmPacket the session reads."""

    channel = VtmChannel.STREAM

    def __init__(self, body: bytes) -> None:
        self.body = body

    @property
    def encrypted(self) -> bool:
        return False


class FakeVtm:
    """Just enough of VtmStreamClient for `CloudSession.run`: the packets, then the end."""

    def __init__(self, packets: list[bytes]) -> None:
        self._packets = packets
        self.closed = False

    def start(self) -> None:
        return None

    def iter_packets(self, **_kwargs: object):
        for body in self._packets:
            yield FakePacket(body)

    def close(self) -> None:
        self.closed = True


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        return None


class Collected(logging.Handler):
    """The session's own log, so the counters it reports can be asserted on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def run_session(packets: list[bytes]) -> tuple[bytes, list[str]]:
    """Run a real CloudSession over fabricated packets and return its MPEG-TS and its log.

    `open_cloud_stream` is what gets replaced, not the session's read: the point is that the
    whole local path runs -- the prefix read, the audio window, the plan, the argv, both pipes
    and the teardown -- because that path is the thing being verified.
    """
    stream = FakeVtm(packets)
    session_module.open_cloud_stream = lambda *_args, **_kwargs: stream
    sink = Sink()
    collected = Collected()
    logging.getLogger("ezviz_stream_bridge.session").addHandler(collected)
    logging.getLogger("ezviz_stream_bridge.session").setLevel(logging.INFO)
    try:
        session_module.CloudSession(
            object(), "VERIFY", ffmpeg_path="ffmpeg", first_video_timeout=0
        ).run(sink)
    finally:
        logging.getLogger("ezviz_stream_bridge.session").removeHandler(collected)
    assert stream.closed, "the session left the VTM session open"
    return bytes(sink.data), collected.lines


# -- what FFmpeg makes of it -------------------------------------------------------------


def ffprobe(remuxed: bytes, entries: str) -> str:
    result = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-f", "mpegts",
         "-show_entries", entries, "-of", "csv=p=0", "-"],
        input=remuxed,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode()


def decodes(remuxed: bytes, codec: str) -> int:
    """The frames FFmpeg can actually get out of the muxed stream, decoded.

    Not "ffprobe named it": a stream FFmpeg names and cannot read is the failure this exists to
    catch, and naming it does not rule that out. The frames are counted by a real decode, which
    is the same thing a consumer would do with the stream.
    """
    result = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-count_frames",
         "-show_entries", "stream=codec_name,nb_read_frames", "-of", "csv=p=0", "-"],
        input=remuxed,
        capture_output=True,
        check=True,
    )
    counts = {
        name: int(frames)
        for name, frames in (line.split(",") for line in result.stdout.decode().split() if line)
        if frames.isdigit()
    }
    assert counts.get(codec, 0) > 0, f"{codec}: decoded {counts.get(codec, 0)} frames"
    return counts[codec]


def extracted(remuxed: bytes, stream: str, time_format: str) -> bytes:
    """One stream out of the MPEG-TS, copied without re-encoding. The bytes that went in, or
    they are not: a remux that carries the right number of bytes in the wrong frames would
    still be named correctly by ffprobe."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-map", stream, "-c", "copy", "-f", time_format, "pipe:1"],
        input=remuxed,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"extracting {stream} failed: {result.stderr.decode()}"
    return result.stdout


def probe_video(codec: str, demuxer: str, source_argv: list[str]) -> None:
    """The video path through the session: prefix, plan, argv, MPEG-TS out."""
    source = subprocess.run(source_argv, capture_output=True, check=True).stdout
    packets = video_packets(codec, source)
    assert detect_codec_for(codec, packets), f"{codec}: no parameter set in the first packets"

    remuxed, _ = run_session(packets)
    found = " ".join(sorted(set(ffprobe(remuxed, "stream=codec_name").split())))
    frames = decodes(remuxed, codec)
    print(
        f"{codec}: {len(packets)} RTP packet(s) -> {len(remuxed)} B MPEG-TS, "
        f"ffprobe {found!r}, {frames} frame(s) decoded"
    )
    assert remuxed, f"{codec}: zero bytes of MPEG-TS"
    assert found == codec, f"{codec}: ffprobe identified {found!r}"


def detect_codec_for(codec: str, packets: list[bytes]) -> bool:
    """True when the codec is named by one of the first eight packets, as the session requires."""
    for packet in packets[:8]:
        if detect_codec(packet[12:]) == codec:
            return True
    return False


def probe_audio(video_source_argv: list[str], audio_source_argv: list[str]) -> None:
    """The audio path end to end: RFC 3640 in, the camera's own ADTS out, both streams muxed."""
    video = video_packets(H264, subprocess.run(video_source_argv, capture_output=True,
                                               check=True).stdout)
    adts = subprocess.run(audio_source_argv, capture_output=True, check=True).stdout
    units = split_adts(adts)
    assert units, "the synthetic audio produced no Access Units"
    audio = audio_packets(units)

    # Before the session, the reading itself: the framing rebuilt from the Access Units has to
    # be the framing FFmpeg wrote. If the sizes, the offset or the header are wrong, this is
    # where it shows -- and it shows as a byte comparison rather than as "the audio sounds odd".
    depacketizer = AacHbrDepacketizer(payload_type=AUDIO_PAYLOAD_TYPE)
    reframed = b"".join(depacketizer.feed(packet) for packet in audio)
    assert reframed == adts, "the ADTS rebuilt from the Access Units is not the one FFmpeg wrote"
    assert depacketizer.dropped == 0, f"{depacketizer.dropped} audio packet(s) unreadable"

    # The audio is deliberately after the video prefix's eight packets: that is the case the
    # window exists for, so the window is what this exercises.
    remuxed, lines = run_session([*video, *audio])

    streams = " ".join(sorted(set(ffprobe(remuxed, "stream=codec_name").split())))
    print(
        f"audio: {len(audio)} RTP packet(s) / {len(units)} AU(s) -> {len(reframed)} B ADTS, "
        f"ffprobe {streams!r}"
    )
    assert streams == "aac h264", f"the muxed stream carries {streams!r}"
    layout = {
        fields[0]: fields[1:]
        for fields in (
            line.split(",") for line in ffprobe(remuxed, "stream=codec_name,sample_rate,channels").splitlines()
        )
        if fields[0]
    }
    assert layout["aac"][:2] == ["16000", "1"], f"the audio is {layout['aac']}, not 16 kHz mono"
    assert extracted(remuxed, "0:a:0", "adts") == adts, (
        "the audio in the MPEG-TS is not the ADTS the camera's Access Units came from"
    )
    video_frames = decodes(remuxed, "h264")
    audio_frames = decodes(remuxed, "aac")
    print(f"  {video_frames} video frame(s) and {audio_frames} audio frame(s) decoded")

    reported = next(line for line in lines if " audio:" in line)
    print(f"  session reported: {reported.split('] ', 1)[1]}")
    assert "PT104 aac-hbr config=0x1408" in reported, reported
    assert f"{len(audio)} packet(s), {len(units)} AU(s), 0 unreadable" in reported, reported
    assert "was not served" not in " ".join(lines), "the audio was measured but not served"


def main() -> int:
    version = subprocess.run(
        ["ffmpeg", "-hide_banner", "-version"], capture_output=True, check=True
    ).stdout.decode().splitlines()[0]
    print(version)

    common = [
        "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15", "-t", "1",
        "-pix_fmt", "yuv420p",
    ]
    h264 = ["ffmpeg", *common, "-c:v", "libx264", "-f", "h264", "pipe:1"]
    probe_video(H264, "h264", h264)
    probe_video(
        HEVC,
        "hevc",
        ["ffmpeg", *common, "-c:v", "libx265", "-x265-params", "log-level=error",
         "-f", "hevc", "pipe:1"],
    )
    probe_audio(
        h264,
        # 16 kHz mono, which is what `config=1408` describes: the same config the bridge
        # rebuilds ADTS from, so a frame count mismatch here would be a real disagreement.
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "1",
         "-c:a", "aac", "-ar", "16000", "-ac", "1", "-f", "adts", "pipe:1"],
    )
    print("VERIFICATION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PYTHON

docker run --rm \
    -v "${repo}:/src:ro" \
    -v "${script}:/verify.py:ro" \
    -w /src \
    "${image}" \
    bash -lc 'apt-get update -qq >/dev/null 2>&1 \
        && apt-get install -y -qq ffmpeg python3-venv >/dev/null 2>&1 \
        && python3 -m venv /tmp/venv \
        && /tmp/venv/bin/pip install -q "pyezvizapi==1.0.5.0" \
        && /tmp/venv/bin/python /verify.py'
