"""Small MPEG-PS reader for caching and replaying HEVC parameter sets.

The bridge otherwise leaves program streams to FFmpeg. This reader only walks pack and PES
boundaries, then inspects video elementary-stream NAL units; it does not rewrite source packets.
"""

# Protocol field values below stay next to the bit tests that use them.
# ruff: noqa: PLR2004

from __future__ import annotations

from dataclasses import dataclass

_START = b"\x00\x00\x01"
_PACK = 0xBA
_VIDEO_MIN = 0xE0
_VIDEO_MAX = 0xEF
_MAX_PENDING = 2 * 1024 * 1024
_MAX_PARAMETER_SET = 1024 * 1024
_MAX_PARAMETER_SETS_PER_TYPE = 64
_MAX_PARAMETER_SETS_BYTES = 1024 * 1024
_PES_PACKET_LENGTH_MAX = 0xFFFF
_HEVC_TYPES = {32: "vps", 33: "sps", 34: "pps"}


@dataclass(frozen=True)
class ParameterSets:
    stream_id: int
    pack_header: bytes
    vps: tuple[bytes, ...]
    sps: tuple[bytes, ...]
    pps: tuple[bytes, ...]

    @property
    def complete(self) -> bool:
        return bool(self.vps and self.sps and self.pps)

    def as_annex_b(self) -> bytes:
        return b"".join(
            b"\x00\x00\x00\x01" + unit
            for group in (self.vps, self.sps, self.pps)
            for unit in group
        )


def make_prelude(sets: ParameterSets) -> bytes:
    """Wrap cached Annex-B parameter sets in a timestamped video PES and valid PS pack."""
    if not sets.complete or not (_VIDEO_MIN <= sets.stream_id <= _VIDEO_MAX):
        return b""
    elementary = sets.as_annex_b()
    # PTS=0 gives FFmpeg's MPEG-TS muxer a timestamp for this parameter-only packet; without
    # one, `-c copy` fails before it reaches the first timestamped camera frame.
    packet_length = len(elementary) + 8
    # Video PES permits packet_length=0 when the elementary payload does not fit the 16-bit size.
    encoded_length = (
        packet_length.to_bytes(2, "big")
        if packet_length <= _PES_PACKET_LENGTH_MAX
        else b"\x00\x00"
    )
    pes = (
        _START
        + bytes((sets.stream_id,))
        + encoded_length
        + b"\x80\x80\x05\x21\x00\x01\x00\x01"
        + elementary
    )
    return sets.pack_header + pes


class ProgramStreamInspector:
    """Incrementally extract complete HEVC VPS/SPS/PPS NAL units from video PES packets."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._pack_header = b""
        self._video_stream_id: int | None = None
        self._elementary = bytearray()
        self._sets: dict[str, list[bytes]] = {name: [] for name in _HEVC_TYPES.values()}

    def feed(self, data: bytes) -> ParameterSets | None:
        self._pending.extend(data)
        if len(self._pending) > _MAX_PENDING:
            # A malformed/incomplete PS packet must not turn inspection into unbounded buffering.
            del self._pending[:-4]
            self._elementary.clear()
        self._parse()
        return self.snapshot() if self.complete else None

    @property
    def complete(self) -> bool:
        return all(self._sets[name] for name in _HEVC_TYPES.values()) and bool(
            self._pack_header
        )

    def snapshot(self) -> ParameterSets | None:
        if not self.complete or self._video_stream_id is None:
            return None
        return ParameterSets(
            self._video_stream_id,
            self._pack_header,
            tuple(self._sets["vps"]),
            tuple(self._sets["sps"]),
            tuple(self._sets["pps"]),
        )

    def flush(self) -> ParameterSets | None:
        """Inspect the last NAL after the input stream has ended."""
        if self._elementary:
            start = self._elementary.find(_START)
            if start >= 0:
                self._inspect_nal(bytes(self._elementary[start + len(_START) :]))
            self._elementary.clear()
        return self.snapshot()

    def _parse(self) -> None:  # noqa: PLR0911, PLR0912, PLR2004 - PS syntax is a bounded wire-format decision tree
        while True:
            start = self._pending.find(_START)
            if start < 0:
                if len(self._pending) > 3:
                    del self._pending[:-3]
                return
            if start:
                del self._pending[:start]
            if len(self._pending) < 4:
                return
            code = self._pending[3]
            if code == _PACK:
                if len(self._pending) < 5:
                    return
                if self._pending[4] & 0xC0 == 0x40:  # MPEG-2 pack header
                    if len(self._pending) < 14:
                        return
                    size = 14 + (self._pending[13] & 0x07)
                else:  # MPEG-1 pack header
                    size = 12
                if len(self._pending) < size:
                    return
                self._pack_header = bytes(self._pending[:size])
                del self._pending[:size]
                continue
            if code == 0xB9:  # program_end_code
                del self._pending[:4]
                continue
            if code in (0xBB, 0xBC, 0xBE, 0xBF) or 0xC0 <= code <= 0xFF:
                if len(self._pending) < 6:
                    return
                length = int.from_bytes(self._pending[4:6], "big")
                if length == 0 and _VIDEO_MIN <= code <= _VIDEO_MAX:
                    boundary = _next_system_start(self._pending, 6)
                    if boundary < 0:
                        return
                    total = boundary
                else:
                    total = 6 + length
                if len(self._pending) < total:
                    return
                if _VIDEO_MIN <= code <= _VIDEO_MAX:
                    self._video_stream_id = code
                    payload = _pes_payload(bytes(self._pending[:total]))
                    if payload:
                        self._feed_elementary(payload)
                del self._pending[:total]
                continue
            # Other start codes have no system-layer length. Advance past the marker and keep
            # scanning; video NAL start codes are consumed only inside a PES payload above.
            del self._pending[:4]

    def _feed_elementary(self, payload: bytes) -> None:
        self._elementary.extend(payload)
        while True:
            first = self._elementary.find(_START)
            if first < 0:
                if len(self._elementary) > _MAX_PARAMETER_SET:
                    self._elementary.clear()
                return
            second = self._elementary.find(_START, first + 3)
            if second < 0:
                if first:
                    del self._elementary[:first]
                if len(self._elementary) > _MAX_PARAMETER_SET:
                    self._elementary.clear()
                return
            self._inspect_nal(bytes(self._elementary[first + 3 : second]))
            del self._elementary[:second]

    def _inspect_nal(self, unit: bytes) -> None:
        if (
            len(unit) < 2
            or unit[0] & 0x80
            or unit[0] & 0x01
            or unit[1] & 0x07 == 0
        ):
            return
        kind = (unit[0] >> 1) & 0x3F
        name = _HEVC_TYPES.get(kind)
        if name and len(unit) <= _MAX_PARAMETER_SET:
            group = self._sets[name]
            cached_size = sum(len(item) for values in self._sets.values() for item in values)
            if (
                unit not in group
                and len(group) < _MAX_PARAMETER_SETS_PER_TYPE
                and cached_size + len(unit) <= _MAX_PARAMETER_SETS_BYTES
            ):
                group.append(unit)


def _pes_payload(packet: bytes) -> bytes:  # noqa: PLR2004 - PES field values are protocol constants
    """Return elementary bytes from an MPEG-2 or MPEG-1 PES packet."""
    if len(packet) < 6:
        return b""
    position = 6
    if position >= len(packet):
        return b""
    if packet[position] & 0xC0 == 0x80:
        if len(packet) < 9:
            return b""
        position = 9 + packet[8]
    else:
        while position < len(packet) and packet[position] == 0xFF:
            position += 1
        if position < len(packet) and packet[position] & 0xC0 == 0x40:
            position += 2
        if position >= len(packet):
            return b""
        flag = packet[position] >> 4
        if flag == 0x02:
            position += 5
        elif flag == 0x03:
            position += 10
        elif packet[position] == 0x0F:
            position += 1
    return packet[position:] if position <= len(packet) else b""


def _next_system_start(data: bytes | bytearray, offset: int) -> int:  # noqa: PLR2004 - start-code IDs are protocol constants
    """Find the next PS-layer start code, excluding Annex-B HEVC NAL delimiters."""
    position = offset
    while True:
        position = data.find(_START, position)
        if position < 0 or position + 3 >= len(data):
            return -1
        code = data[position + 3]
        if code in (0xB9, 0xBA, 0xBB, 0xBC, 0xBE, 0xBF) or code >= 0xC0:
            return position
        position += 3
