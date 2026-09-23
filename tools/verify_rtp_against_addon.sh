#!/usr/bin/env bash
# Verify the RTP depacketizer against the FFmpeg the add-on actually runs.
#
# The ffmpeg on a development machine proves nothing about the add-on: the image installs
# Debian's, and a remux that worked on a laptop has failed inside the container before. This
# builds a real H.264 and HEVC elementary stream, packetizes them the way RFC 6184 and RFC 7798
# say a camera does, pushes every packet through this repository's depacketizer, and hands the
# result to FFmpeg using the exact argv `CloudSession._start_ffmpeg` produces. It reimplements
# nothing: if the module is wrong, the bytes out are wrong.
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
installs). It builds a real elementary stream, packetizes it the way RFC 6184 / RFC 7798 say a
camera does, pushes every packet through this repository's depacketizer, and hands the result to
FFmpeg using the exact argv `CloudSession._start_ffmpeg` produces. Nothing here reimplements the
depacketizer: if the module is wrong, the bytes out are wrong.
"""

from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, "/src")

from ezviz_stream_bridge import session as session_module  # noqa: E402
from ezviz_stream_bridge.rtp import H264, HEVC, RtpDepacketizer, detect_codec  # noqa: E402

MAX_PAYLOAD = 1200


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


def rtp(sequence: int, payload: bytes) -> bytes:
    return (
        bytes([0x80, 0x60])
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


def probe(codec: str, demuxer: str, source_argv: list[str]) -> None:
    source = subprocess.run(source_argv, capture_output=True, check=True).stdout
    nals = split_annexb(source)
    assert nals, f"{codec}: the synthetic stream produced no NAL units"

    depacketizer = RtpDepacketizer(codec, payload_type=96)
    detected = None
    served = bytearray()
    sequence = 0
    for nal in nals:
        for payload in fragments(nal, codec):
            if detected is None:
                detected = detect_codec(payload)
            served += depacketizer.feed(rtp(sequence, payload))
            sequence += 1

    assert detected == codec, f"{codec}: detect_codec said {detected!r}"
    assert depacketizer.dropped == 0, f"{codec}: {depacketizer.dropped} packets were dropped"
    assert served, f"{codec}: the depacketizer produced nothing"

    session = session_module.CloudSession(object(), "VERIFY", ffmpeg_path="ffmpeg")
    process = session._start_ffmpeg(demuxer)  # noqa: SLF001 - the argv is the subject
    process.stdin.write(bytes(served))
    process.stdin.close()
    remuxed = process.stdout.read()
    process.stdout.close()
    code = process.wait()
    assert code == 0, f"{codec}: ffmpeg exited with {code}"

    probe_result = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-f", "mpegts",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-"],
        input=remuxed,
        capture_output=True,
        check=False,
    )
    found = " ".join(sorted(set(probe_result.stdout.decode().split())))
    print(
        f"{codec}: {len(served)} B elementary -> {len(remuxed)} B MPEG-TS, "
        f"ffprobe stream={found!r}"
    )
    assert len(remuxed) > 0, f"{codec}: zero bytes of MPEG-TS"
    assert found == codec, f"{codec}: ffprobe identified {found!r}"


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
    probe(H264, "h264", ["ffmpeg", *common, "-c:v", "libx264", "-f", "h264", "pipe:1"])
    probe(
        HEVC,
        "hevc",
        ["ffmpeg", *common, "-c:v", "libx265", "-x265-params", "log-level=error",
         "-f", "hevc", "pipe:1"],
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
