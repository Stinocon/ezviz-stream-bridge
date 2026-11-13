from __future__ import annotations

import shutil
import subprocess

import pytest

from ezviz_stream_bridge.mpeg_ps import ProgramStreamInspector, make_prelude

PACK = bytes.fromhex("000001ba2100010001c33367")
# Real parameter sets, out of libx265 for a 160x90 picture: the inspector keeps only units that
# read as what they claim to be, so the fixtures have to be ones a decoder would accept.
VPS = bytes.fromhex("40010c01ffff01600000030090000003000003001e959809")
SPS = bytes.fromhex(
    "42010101600000030090000003000003001ea0142061f265959a4932bc05a020000003002000000300a1"
)
PPS = bytes.fromhex("4401c172b46240")
# The same encoder at 1920x1080, and the PPS above renumbered as PPS 1: distinct units of the
# same types, each still valid.
SPS_1080 = bytes.fromhex(
    "420101016000000300900000030000030078a003c08010e596566924caf0168080000003008000000c84"
)
PPS_1 = bytes.fromhex("4401505cad1890")
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
    second_sps, second_pps = SPS_1080, PPS_1
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


# -- parameter sets that do not read -----------------------------------------------------------
#
# Issue #2: a camera with video encryption on keeps each NAL header and encrypts what follows.
# The inspector used to keep such units on their header alone, the session cached them, and
# every join was handed sets FFmpeg refuses. Now they are counted and refused.


def _scrambled(unit: bytes) -> bytes:
    """`unit` with its header kept and its body replaced, the way encryption leaves it."""
    return unit[:2] + bytes((index * 151 + 89) & 0xFF for index in range(len(unit) - 2))


def test_parameter_sets_that_do_not_read_are_refused_and_counted() -> None:
    inspector = ProgramStreamInspector()
    elementary = b"".join(START + _scrambled(unit) for unit in (VPS, SPS, PPS, b"\x26\x01\x80"))

    inspector.feed(PACK + _pes(elementary))

    assert inspector.snapshot() is None
    assert inspector.flush() is None
    assert inspector.rejected >= 1
    assert inspector.rejection is not None and "VPS" in inspector.rejection


def test_the_old_placeholder_sets_are_exactly_what_a_decoder_refuses() -> None:
    """The fixtures 0.1.17 was tested with announce the right NAL types and carry none of the
    fields, which is how a test passed against sets FFmpeg would never open."""
    placeholders = (b"\x40\x01\x0c\x01", b"\x42\x01\x01", b"\x44\x01\xc0")
    inspector = ProgramStreamInspector()

    inspector.feed(PACK + _pes(b"".join(START + unit for unit in placeholders)))

    assert inspector.flush() is None
    assert inspector.rejected == 2  # the VPS and the SPS; that PPS happens to read


def test_sets_whose_references_do_not_resolve_are_not_complete() -> None:
    """A PPS naming an SPS the stream has not sent is the `SPS 0 does not exist` of the issue:
    the set is not handed over until the SPS it names has arrived."""
    inspector = ProgramStreamInspector()
    inspector.feed(PACK + _pes(b"".join(START + unit for unit in (VPS, PPS))))
    assert inspector.snapshot() is None

    inspector.feed(PACK + _pes(START + SPS + START + b"\x26\x01\x80"))

    sets = inspector.snapshot()
    assert sets is not None and sets.complete
    assert inspector.rejected == 0
