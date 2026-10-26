from __future__ import annotations

import shutil
import subprocess

import pytest

from ezviz_stream_bridge.mpeg_ps import ProgramStreamInspector, make_prelude

PACK = bytes.fromhex("000001ba2100010001c33367")
VPS = bytes.fromhex("40010c01")
SPS = bytes.fromhex("420101")
PPS = bytes.fromhex("4401c0")
START = b"\x00\x00\x01"


def _pes(payload: bytes, *, stream_id: int = 0xE0, unbounded: bool = False) -> bytes:
    length = len(payload) + 3
    encoded_length = b"\x00\x00" if unbounded else length.to_bytes(2, "big")
    return START + bytes((stream_id,)) + encoded_length + b"\x80\x00\x00" + payload


def _source() -> bytes:
    elementary = b"".join(b"\x00\x00\x01" + unit for unit in (VPS, SPS, PPS))
    return PACK + _pes(elementary)


def test_inspector_finds_hevc_parameter_sets_across_arbitrary_input_chunks() -> None:
    inspector = ProgramStreamInspector()
    source = _source()
    result = None
    for offset in range(0, len(source), 3):
        result = inspector.feed(source[offset : offset + 3]) or result

    sets = inspector.flush() or result
    assert sets is not None
    assert sets.complete
    assert sets.stream_id == 0xE0
    assert (sets.vps, sets.sps, sets.pps) == ((VPS,), (SPS,), (PPS,))
    assert sets.pack_header == PACK


def test_inspector_handles_zero_length_video_pes_ending_at_next_pack() -> None:
    inspector = ProgramStreamInspector()
    elementary = b"".join(b"\x00\x00\x01" + unit for unit in (VPS, SPS, PPS))
    next_pack = PACK
    stream = PACK + _pes(elementary, unbounded=True) + next_pack

    for offset in range(0, len(stream), 7):
        inspector.feed(stream[offset : offset + 7])
    sets = inspector.flush()

    assert sets is not None and sets.complete
    assert sets.pack_header == next_pack


def test_inspector_reads_mpeg2_pack_and_pts_bearing_pes_headers() -> None:
    pack = bytes.fromhex("000001ba4400040004018666cff8")
    elementary = b"".join(b"\x00\x00\x01" + unit for unit in (VPS, SPS, PPS, b"\x26\x01\x80"))
    pes = b"\x00\x00\x01\xe0" + (len(elementary) + 8).to_bytes(2, "big")
    pes += b"\x80\x80\x05\x21\x00\x01\x00\x01" + elementary
    inspector = ProgramStreamInspector()

    inspector.feed(pack + pes)
    sets = inspector.snapshot()

    assert sets is not None and sets.complete
    assert sets.pack_header == pack


def test_inspector_keeps_distinct_parameter_sets_of_the_same_type() -> None:
    second_sps, second_pps = b"\x42\x01\x02", b"\x44\x01\xc1"
    elementary = b"".join(
        b"\x00\x00\x01" + unit
        for unit in (VPS, SPS, second_sps, PPS, second_pps, b"\x26\x01\x80")
    )
    inspector = ProgramStreamInspector()
    inspector.feed(PACK + _pes(elementary))

    sets = inspector.snapshot()

    assert sets is not None and sets.complete
    assert sets.vps == (VPS,)
    assert sets.sps == (SPS, second_sps)
    assert sets.pps == (PPS, second_pps)


def test_prelude_is_a_timestamped_video_pes_inside_a_pack() -> None:
    inspector = ProgramStreamInspector()
    inspector.feed(_source())
    sets = inspector.flush()
    assert sets is not None

    prelude = make_prelude(sets)
    assert prelude.startswith(PACK + START + b"\xe0")
    assert prelude[18:26] == b"\x80\x80\x05\x21\x00\x01\x00\x01"
    assert prelude[26:] == sets.as_annex_b()


def test_ffmpeg_decodes_a_real_hevc_program_stream_after_prelude_injection() -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are needed for the program-stream integration check")
    generated = subprocess.run(  # noqa: S603 - executable is resolved from PATH; argv is fixed
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-frames:v",
            "5",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx265",
            "-x265-params",
            "log-level=error:keyint=5:repeat-headers=1",
            "-c:a",
            "mp2",
            "-ar",
            "48000",
            "-f",
            "mpeg",
            "pipe:1",
        ],
        capture_output=True,
        check=False,  # noqa: S603 - executable is resolved from PATH; argv is fixed
    )
    if generated.returncode:
        pytest.skip("this ffmpeg build cannot generate a synthetic HEVC program stream")

    inspector = ProgramStreamInspector()
    for offset in range(0, len(generated.stdout), 4096):
        inspector.feed(generated.stdout[offset : offset + 4096])
    sets = inspector.flush()
    assert sets is not None and sets.complete
    joined = subprocess.run(  # noqa: S603 - executable is resolved from PATH; argv is fixed
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-map",
            "0",
            "-c:v",
            "copy",
            "-bsf:v",
            "filter_units=remove_types=32|33|34",
            "-f",
            "mpeg",
            "pipe:1",
        ],
        input=generated.stdout,
        capture_output=True,
        check=False,  # noqa: S603 - executable is resolved from PATH; argv is fixed
    )
    if joined.returncode:
        pytest.skip("this ffmpeg build cannot strip HEVC parameter sets from MPEG-PS")
    join_inspector = ProgramStreamInspector()
    join_inspector.feed(joined.stdout)
    assert join_inspector.flush() is None, "the mid-GOP fixture still carries parameter sets"
    payload = make_prelude(sets) + joined.stdout

    decoded = subprocess.run(  # noqa: S603 - executable is resolved from PATH; argv is fixed
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "null", "-"],
        input=payload,
        capture_output=True,
        check=False,  # noqa: S603 - executable is resolved from PATH; argv is fixed
    )
    probed = subprocess.run(  # noqa: S603 - executable is resolved from PATH; argv is fixed
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "default=nw=1",
            "pipe:0",
        ],
        input=payload,
        capture_output=True,
        check=False,  # noqa: S603 - executable is resolved from PATH; argv is fixed
    )
    assert decoded.returncode == 0, decoded.stderr.decode(errors="replace")
    assert probed.returncode == 0, probed.stderr.decode(errors="replace")
    assert b"codec_name=hevc" in probed.stdout
    assert b"codec_name=mp2" in probed.stdout
    assert b"width=160" in probed.stdout and b"height=90" in probed.stdout
