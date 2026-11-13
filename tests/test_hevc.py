"""Tests for telling a usable HEVC parameter set from noise.

The fields checked are the ones FFmpeg refused the BC1C's sets over in issue #2, so each
refusal here is pinned to the message a decoder would have given for it.
"""

from __future__ import annotations

import pytest

from ezviz_stream_bridge.hevc import (
    UnreadableParameterSet,
    rbsp,
    read_pps,
    read_sps,
    read_vps,
    sets_problem,
    unit_problem,
)

# libx265 at 160x90 (coded 160x96) and at 1920x1080. Both VPSs and SPSs carry emulation-prevention
# bytes, which is the case a reader that skips the unescaping would get wrong.
VPS = bytes.fromhex("40010c01ffff01600000030090000003000003001e959809")
SPS = bytes.fromhex(
    "42010101600000030090000003000003001ea0142061f265959a4932bc05a020000003002000000300a1"
)
SPS_1080 = bytes.fromhex(
    "420101016000000300900000030000030078a003c08010e596566924caf0168080000003008000000c84"
)
PPS = bytes.fromhex("4401c172b46240")
PPS_1 = bytes.fromhex("4401505cad1890")


def test_real_parameter_sets_read() -> None:
    assert read_vps(VPS).vps_id == 0
    sps = read_sps(SPS)
    assert (sps.sps_id, sps.vps_id, sps.width, sps.height) == (0, 0, 160, 96)
    assert (read_sps(SPS_1080).width, read_sps(SPS_1080).height) == (1920, 1080)
    assert (read_pps(PPS).pps_id, read_pps(PPS).sps_id) == (0, 0)
    assert (read_pps(PPS_1).pps_id, read_pps(PPS_1).sps_id) == (1, 0)


def test_emulation_prevention_bytes_are_removed() -> None:
    escaped = b"\x40\x01" + b"\x00\x00\x03\x01" + b"\x00\x00\x03\x00\x00"

    assert rbsp(escaped) == b"\x00\x00\x01" + b"\x00\x00\x00\x00"


def test_real_sets_open_a_stream_together() -> None:
    assert sets_problem([VPS], [SPS, SPS_1080], [PPS, PPS_1]) is None


def _with_body(unit: bytes, body: bytes) -> bytes:
    return unit[:2] + body


def test_the_vps_reserved_bits_are_checked_where_ffmpeg_checks_them() -> None:
    """`vps_reserved_three_2bits is not three`, the first line of the issue's log."""
    body = bytearray(rbsp(VPS))
    body[0] &= 0b11110011
    with pytest.raises(UnreadableParameterSet, match="vps_reserved_three_2bits"):
        read_vps(_with_body(VPS, bytes(body)))

    body = bytearray(rbsp(VPS))
    body[2] = 0x7F
    with pytest.raises(UnreadableParameterSet, match="ffff"):
        read_vps(_with_body(VPS, bytes(body)))


def test_an_out_of_range_pps_id_is_refused() -> None:
    """`PPS id out of range`: ue(v) 64 is six zeros, a one and 000001; then SPS 0 and the stop
    bit."""
    with pytest.raises(UnreadableParameterSet, match="PPS id 64"):
        read_pps(b"\x44\x01" + bytes((0b00000010, 0b00001110)))


def test_an_sps_naming_a_vps_that_is_not_there_is_refused() -> None:
    """`VPS 6 does not exist`: the SPS reads, and names a VPS the stream never sent."""
    body = bytearray(rbsp(SPS))
    body[0] = (6 << 4) | (body[0] & 0x0F)
    problem = sets_problem([VPS], [_with_body(SPS, bytes(body))], [PPS])
    assert problem is not None and "VPS 6" in problem


def test_a_pps_naming_an_sps_that_is_not_there_is_refused() -> None:
    """`SPS 0 does not exist`, from the PPS side: the PPS reads, and names an SPS that is not
    among the sets. ue(0) ue(1) and the stop bit: 1 010 1 000."""
    names_sps_1 = b"\x44\x01" + bytes((0b10101000,))
    assert unit_problem("pps", names_sps_1) is None

    problem = sets_problem([VPS], [SPS], [names_sps_1])

    assert problem is not None and "SPS 1" in problem
    assert sets_problem([VPS], [], [PPS]) is not None


def test_a_truncated_set_is_refused_rather_than_read_past_its_end() -> None:
    # Five bytes leave the VPS one short of its reserved ffff, and the SPS far short of its
    # twelve-byte profile.
    for kind, unit in (("vps", VPS), ("sps", SPS)):
        problem = unit_problem(kind, unit[:5])
        assert problem is not None and "ends before" in problem


@pytest.mark.parametrize("kind", ["vps", "sps", "pps"])
def test_a_body_of_zeros_is_refused(kind: str) -> None:
    """Zeros are what a reader without a bound would loop on: ue(v) of all-zero bits never ends."""
    header = {"vps": VPS, "sps": SPS, "pps": PPS}[kind][:2]
    assert unit_problem(kind, header + bytes(64)) is not None


def test_scrambled_sets_never_pass_as_a_whole() -> None:
    """Encrypted bodies behind real headers, many times over: the VPS's eighteen fixed bits alone
    make a false pass vanishingly rare, and the references between the three make it rarer."""
    seed = 12345
    passes = 0
    for _ in range(2000):
        units = []
        for unit in (VPS, SPS, PPS):
            body = bytearray()
            for _ in range(len(unit) - 2):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                body.append(seed >> 16 & 0xFF)
            units.append(unit[:2] + bytes(body))
        if sets_problem([units[0]], [units[1]], [units[2]]) is None:
            passes += 1
    assert passes == 0
