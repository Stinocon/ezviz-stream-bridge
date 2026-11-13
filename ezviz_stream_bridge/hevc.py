"""Reading just enough of an HEVC parameter set to tell one a decoder can use from noise.

Why this exists: a camera with *Video Encryption* on keeps the NAL headers of its stream and
encrypts what follows them. Its VPS, SPS and PPS still announce themselves as NAL types 32, 33
and 34 -- which was all the program-stream inspector looked at -- so 0.1.17 cached them, handed
them to every join, and FFmpeg refused them on each one: `vps_reserved_three_2bits is not
three`, `VPS 6 does not exist`, `PPS id out of range` (issue #2). Nothing the bridge does to the
container can repair an encrypted stream. What it can do is not cache noise as if it were a
parameter set, and say in the log what that noise usually means.

The checks are the fields a decoder refuses a parameter set over, and nothing more: the bits
the standard fixes, the ids and their ranges, the picture size, and the references between the
three (an SPS names its VPS, a PPS its SPS). Each is cheap and none needs the whole syntax, and
together they are far beyond what encrypted bytes pass by chance: the VPS alone carries eighteen
fixed bits.
"""

# Protocol field values below stay next to the bit reads that give them meaning.
# ruff: noqa: PLR2004

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_NAL_HEADER_BYTES = 2
# The largest picture dimension any HEVC level allows: sqrt(8 * MaxLumaPs) at level 6.2.
_MAX_DIMENSION = 16888
# A ue(v) longer than this cannot be a field of a parameter set; in noise it is common.
_MAX_UE_LEADING_ZEROS = 31


class UnreadableParameterSet(ValueError):
    """A parameter set a decoder would refuse. The message names the field."""


@dataclass(frozen=True)
class Vps:
    vps_id: int


@dataclass(frozen=True)
class Sps:
    sps_id: int
    vps_id: int
    width: int
    height: int


@dataclass(frozen=True)
class Pps:
    pps_id: int
    sps_id: int


def rbsp(unit: bytes) -> bytes:
    """The payload of a NAL unit after its header, with the emulation-prevention bytes removed.

    Every `00 00 03` in a NAL unit is an escape the encoder added so the payload never looks
    like a start code; the fields are read from what it escaped. Real parameter sets carry
    several: the profile flags are mostly zero.
    """
    out = bytearray()
    zeros = 0
    for byte in unit[_NAL_HEADER_BYTES:]:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    """MSB-first reader over an RBSP, raising `UnreadableParameterSet` past its end."""

    def __init__(self, data: bytes, what: str) -> None:
        self._data = data
        self._position = 0
        self._what = what

    def u(self, count: int) -> int:
        if self._position + count > len(self._data) * 8:
            raise UnreadableParameterSet(f"{self._what} ends before its fields do")
        value = 0
        for _ in range(count):
            byte = self._data[self._position >> 3]
            value = (value << 1) | ((byte >> (7 - (self._position & 7))) & 1)
            self._position += 1
        return value

    def skip(self, count: int) -> None:
        if self._position + count > len(self._data) * 8:
            raise UnreadableParameterSet(f"{self._what} ends before its fields do")
        self._position += count

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > _MAX_UE_LEADING_ZEROS:
                raise UnreadableParameterSet(f"{self._what} holds a number no field can have")
        return (1 << zeros) - 1 + self.u(zeros)


def read_vps(unit: bytes) -> Vps:
    """The VPS id, once the bits the standard fixes have been found where it fixes them."""
    bits = _Bits(rbsp(unit), "VPS")
    vps_id = bits.u(4)
    if bits.u(2) != 3:
        raise UnreadableParameterSet("VPS reserved bits are not 11 (vps_reserved_three_2bits)")
    bits.skip(6)  # vps_max_layers_minus1
    if bits.u(3) > 6:
        raise UnreadableParameterSet("VPS declares more than seven temporal sub-layers")
    bits.skip(1)  # vps_temporal_id_nesting_flag
    if bits.u(16) != 0xFFFF:
        raise UnreadableParameterSet("VPS reserved bits are not ffff (vps_reserved_0xffff_16bits)")
    return Vps(vps_id)


def _skip_profile_tier_level(bits: _Bits, max_sub_layers_minus1: int) -> None:
    # general_profile_space, tier, profile_idc, 32 compatibility flags, 48 constraint bits,
    # general_level_idc: twelve bytes.
    bits.skip(96)
    profile_present = []
    level_present = []
    for _ in range(max_sub_layers_minus1):
        profile_present.append(bits.u(1))
        level_present.append(bits.u(1))
    if max_sub_layers_minus1 > 0:
        bits.skip(2 * (8 - max_sub_layers_minus1))  # reserved_zero_2bits
    for profile, level in zip(profile_present, level_present, strict=True):
        if profile:
            bits.skip(88)
        if level:
            bits.skip(8)


def read_sps(unit: bytes) -> Sps:
    """The SPS id, the VPS it names and the picture size, each within the range it allows."""
    bits = _Bits(rbsp(unit), "SPS")
    vps_id = bits.u(4)
    max_sub_layers_minus1 = bits.u(3)
    if max_sub_layers_minus1 > 6:
        raise UnreadableParameterSet("SPS declares more than seven temporal sub-layers")
    bits.skip(1)  # sps_temporal_id_nesting_flag
    _skip_profile_tier_level(bits, max_sub_layers_minus1)
    sps_id = bits.ue()
    if sps_id > 15:
        raise UnreadableParameterSet(f"SPS id {sps_id} is out of range")
    chroma_format_idc = bits.ue()
    if chroma_format_idc > 3:
        raise UnreadableParameterSet(f"SPS chroma format {chroma_format_idc} does not exist")
    if chroma_format_idc == 3:
        bits.skip(1)  # separate_colour_plane_flag
    width = bits.ue()
    height = bits.ue()
    if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
        raise UnreadableParameterSet(f"SPS picture size {width}x{height} is not a picture size")
    return Sps(sps_id, vps_id, width, height)


def read_pps(unit: bytes) -> Pps:
    """The PPS id and the SPS it names, each within the range it allows."""
    bits = _Bits(rbsp(unit), "PPS")
    pps_id = bits.ue()
    if pps_id > 63:
        raise UnreadableParameterSet(f"PPS id {pps_id} is out of range")
    sps_id = bits.ue()
    if sps_id > 15:
        raise UnreadableParameterSet(f"PPS names SPS {sps_id}, which is out of range")
    return Pps(pps_id, sps_id)


_READERS = {"vps": read_vps, "sps": read_sps, "pps": read_pps}


def unit_problem(kind: str, unit: bytes) -> str | None:
    """Why one parameter set of `kind` ("vps", "sps" or "pps") is unusable, or None if it reads."""
    try:
        _READERS[kind](unit)
    except UnreadableParameterSet as err:
        return str(err)
    return None


def sets_problem(
    vps: Sequence[bytes], sps: Sequence[bytes], pps: Sequence[bytes]
) -> str | None:
    """Why these sets cannot open a stream together, or None when they can.

    Each unit must read on its own, and the references must resolve: an SPS whose VPS is not
    among them is the `VPS 6 does not exist` of the issue, and a PPS whose SPS is not is its
    `SPS 0 does not exist`.
    """
    try:
        vps_ids = {read_vps(unit).vps_id for unit in vps}
        sps_read = [read_sps(unit) for unit in sps]
        pps_read = [read_pps(unit) for unit in pps]
    except UnreadableParameterSet as err:
        return str(err)
    if not (vps_ids and sps_read and pps_read):
        return "VPS, SPS and PPS are not all there"
    for item in sps_read:
        if item.vps_id not in vps_ids:
            return f"SPS {item.sps_id} names VPS {item.vps_id}, which is not among them"
    sps_ids = {item.sps_id for item in sps_read}
    for item in pps_read:
        if item.sps_id not in sps_ids:
            return f"PPS {item.pps_id} names SPS {item.sps_id}, which is not among them"
    return None
