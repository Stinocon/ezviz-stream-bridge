"""RTP depacketization: RFC 6184 (H.264) and RFC 7798 (HEVC) into an Annex-B stream.

The VTM payload is not always MPEG-PS. A CS-C8c sends its video as RTP with payload type 96
carrying RFC 6184 H.264, and FFmpeg's `mpeg` demuxer cannot read a byte of it: fed that
payload it answers `could not find codec parameters` and writes nothing, which is exactly the
`bytes=0` a reporter saw on a stream that was arriving intact. A different FFmpeg demuxer does
not fix it either -- `-f rtp` wants a UDP URL and an SDP, and the packets are already here --
so the packaging is undone in this process, where the reassembly can also be tested against
the bytes a real camera sent.

Only the packaging is handled here. Where the payload starts inside a packet (CSRC list,
header extension, padding) comes from `pyezvizapi.stream.rtp_payload` -- the same unwrap the
diagnostic logs, so there is one implementation of the offset rule and not two.

Deliberately not handled, because nothing this bridge has seen emits them: H.264 FU-B and
HEVC PACI. A packet carrying one is dropped and counted, as is any packet whose NAL header is
not valid for the codec the session settled on. A payload the depacketizer cannot make sense
of has to be visible as a number, not as silence.
"""

from __future__ import annotations

from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.stream import rtp_payload

H264 = "h264"
HEVC = "hevc"

# Annex-B start code, four bytes rather than three. An aggregate packet (STAP-A, HEVC AP) is a
# length field followed by NAL units, and after a unit that ends in 0x00 a three-byte code
# would let that byte merge into the next unit's prefix.
_START_CODE = b"\x00\x00\x00\x01"

# H.264 NAL unit types (RFC 6184 table 1).
_H264_SPS = 7
_H264_STAP_A = 24
_H264_FU_A = 28

# HEVC NAL unit types (RFC 7798 table 1).
_HEVC_VPS = 32
_HEVC_SPS = 33
_HEVC_PPS = 34
_HEVC_AP = 48
_HEVC_FU = 49

# The largest single-NAL-unit type in RFC 7798 1.1.3. Above it every payload carries its own
# aggregation, fragmentation or padding header instead of a bare NAL unit.
_HEVC_SINGLE_NAL_MAX = 47

# The last single-NAL-unit type in RFC 6184: 1-5 are slices, 6-9 SEI and access-unit
# delimiters, and 10-23 the remaining single units. 24 onwards is STAP-A and the FU forms.
_H264_SINGLE_NAL_MAX = 23

# Bytes each header takes before its payload: an H.264 NAL header is one byte where HEVC's is
# two, and a fragmented packet carries one more byte of FU header on top of that.
_H264_FU_HEADER = 2
_HEVC_NAL_HEADER = 2
_HEVC_FU_HEADER = 3

# What `detect_codec` needs before it can tell the two headers apart: the second byte is the
# H.264 profile in one reading and HEVC's layer id and temporal id in the other.
_DETECTION_BYTES = 2

# The `profile_idc` values the H.264 specification has ever assigned, read as an SPS's second
# byte. Requiring one is what stops an HEVC IDR from claiming an H.264 SPS: 0x27 is
# nal_unit_type 7 to one codec and IDR_W_RADL to the other, and there is no profile number that
# is also a plausible layer-and-temporal-id byte for a single-layer stream.
_H264_PROFILES = frozenset(
    (44, 66, 77, 83, 86, 88, 100, 110, 118, 122, 128, 134, 135, 138, 139, 244)
)

# The fixed part of an RTP header, before any CSRC list or extension. Only the payload type and
# the version are read from it here, and the version is 2 in every RTP packet this bridge sees.
_RTP_HEADER_BYTES = 12
_RTP_VERSION = 2

# A NAL this large is not one these cameras send, and the bound is what keeps a stream that
# never signals the end of a fragment from growing without limit.
_MAX_NAL_BYTES = 4 * 1024 * 1024


def _detect_parameter_set(unit: bytes) -> str | None:
    """The codec a NAL unit's own header names, from parameter sets only.

    The two headers are told apart by construction, and three checks are what make that hold.
    Without them the two readings claim each other's units -- and a wrong codec is worse than
    no codec at all, because the demuxer, not the stream, is then what produces nothing:

    - HEVC (RFC 7798 1.1.4) is `type = (b0 >> 1) & 0x3F`, and VPS/SPS/PPS are 32/33/34. The low
      bit of that byte is the top bit of `nuh_layer_id`, so requiring it clear confines the
      reading to single-layer streams -- where a camera lives -- and rules out 0x41/0x43/0x45,
      which are H.264 slices.
    - H.264 (RFC 6184 1.3) is `nal_unit_type = b0 & 0x1F`, and an SPS is 7. Its `b0` is one of
      0x07/0x27/0x47/0x67, whose HEVC readings are 3/19/35/51 -- never a parameter set -- and
      all four are odd, where a single-layer HEVC unit's `b0` is always even: its low bit is
      the top bit of a layer id that a single-layer stream leaves at zero. Only a *layered*
      HEVC stream can collide there, so the second byte must be a profile the specification
      has assigned as well. Layers are the boundary of this guarantee, and no camera in this
      family sends them.
    - HEVC's temporal id (`second & 0x07`) is forbidden from being zero.

    A PPS is not enough on its own and is not accepted: H.264's is 0x48/0x68, and 0x48 is also
    what an HEVC filler-data unit (type 36) looks like. Waiting for the SPS costs one packet
    and removes that ambiguity entirely.
    """
    if len(unit) < _DETECTION_BYTES or unit[0] & 0x80:
        return None
    first, second = unit[0], unit[1]
    if (
        (first >> 1) & 0x3F in (_HEVC_VPS, _HEVC_SPS, _HEVC_PPS)
        and (first & 0x01) == 0
        and second & 0x07
    ):
        return HEVC
    if first & 0x1F == _H264_SPS and second in _H264_PROFILES:
        return H264
    return None


def _aggregated_codec(payload: bytes, *, offset: int, codec: str) -> str | None:
    """The codec an aggregate names, from any of its length-prefixed units.

    Every unit, not only the first one: RFC 6184 and RFC 7798 put the parameter sets at the
    front by convention, not by requirement, and stopping at the first unit would refuse a
    conforming stream over where inside the bundle its SPS happened to sit.
    """
    while offset + 2 <= len(payload):
        size = int.from_bytes(payload[offset : offset + 2], "big")
        unit = payload[offset + 2 : offset + 2 + size]
        if size == 0 or len(unit) != size:
            return None
        if _detect_parameter_set(unit) == codec:
            return codec
        offset += 2 + size
    return None


def _payload_type(packet: bytes) -> int | None:
    """The RTP payload type of a packet, or None when the bytes are not an RTP header."""
    if len(packet) < _RTP_HEADER_BYTES or packet[0] >> 6 != _RTP_VERSION:
        return None
    return packet[1] & 0x7F


def detect_codec(payload: bytes) -> str | None:
    """Name the codec from a payload carrying a parameter set, or None if it cannot.

    Parameter sets only, and deliberately so. An SPS or VPS is the first thing either codec
    sends, and it is the one payload whose interpretation does not already require knowing
    the codec: for a payload of any other type the two readings collide (H.264 nal_unit_type 7
    and HEVC nal_unit_type 7 are different units), and guessing there is how a session ends up
    handing FFmpeg the wrong demuxer.

    A parameter set that arrives bundled in an aggregate still counts. RFC 6184 lets an SPS and
    a PPS travel together in one STAP-A, and RFC 7798 sends a VPS/SPS/PPS set in an AP, so
    refusing the stream over the packaging would refuse a conforming one. The aggregate marker
    commits to a codec and the unit inside only has to agree with it: the markers collide with
    the other codec's slice types (0x78 reads as HEVC type 60, 0x60 as H.264 type 0), so it is
    the inner unit, not the marker, that makes the reading trustworthy.
    """
    codec = _detect_parameter_set(payload)
    if codec is not None:
        return codec
    if len(payload) < _DETECTION_BYTES or payload[0] & 0x80:
        return None
    first = payload[0]
    if first & 0x1F == _H264_STAP_A:
        return _aggregated_codec(payload, offset=1, codec=H264)
    # The low bit of an HEVC payload header is the top bit of the layer id, so an AP from a
    # single-layer stream is 0x60 exactly -- and 0x61, which the same bits would allow, is an
    # H.264 slice.
    if first == (_HEVC_AP << 1):
        return _aggregated_codec(payload, offset=2, codec=HEVC)
    return None


class RtpDepacketizer:
    """One codec's RTP payloads, turned into the Annex-B stream FFmpeg reads.

    Stateful by necessity: a fragmented NAL exists only once its last fragment has arrived.
    One instance per session, bound to one codec, which is what lets the fragment header be
    read the one way that codec defines instead of being inferred per packet.
    """

    def __init__(self, codec: str, *, payload_type: int | None = None) -> None:
        if codec not in (H264, HEVC):
            raise ValueError(f"Unsupported codec: {codec!r}")
        self.codec = codec
        # The RTP payload type the codec was identified from, when the caller knows it. One RTP
        # session can carry more than one payload type -- the CS-C8c sends its metadata under
        # payload type 112 -- and a packet whose type is not this one is not this elementary
        # stream, whatever its bytes happen to look like.
        self.payload_type = payload_type
        # Packets that carried media the depacketizer could not interpret, and packets that
        # were not this elementary stream at all. Counted rather than logged here so a session
        # can report the numbers once, at its end.
        self.dropped = 0
        self.skipped = 0
        self._fragment: bytearray | None = None

    def feed(self, packet: bytes) -> bytes:
        """The Annex-B bytes one RTP packet contributes, empty when it contributes none."""
        try:
            payload = rtp_payload(packet)
        except PyEzvizError:
            # Not an RTP packet, or a header that overruns it: nothing to unwrap.
            self.dropped += 1
            return b""
        if not payload:
            # A packet with metadata in its header extension and no media at all. The CS-C8c
            # sends two of these before its first NAL; they are not an error, not video, and
            # not a payload type this session has to be warned about.
            return b""
        if self.payload_type is not None and _payload_type(packet) != self.payload_type:
            # Another stream sharing the RTP session, carrying something that is not this
            # elementary stream. Checking after the unwrap is what keeps the count honest:
            # a malformed packet is dropped, not "another payload type".
            self.skipped += 1
            return b""
        if self.codec == H264:
            return self._h264(payload)
        return self._hevc(payload)

    # -- H.264 (RFC 6184) --------------------------------------------------------

    def _h264(self, payload: bytes) -> bytes:
        if payload[0] & 0x80:
            self._drop()
            return b""
        nal_type = payload[0] & 0x1F
        if 0 < nal_type <= _H264_SINGLE_NAL_MAX:
            return _START_CODE + payload
        if nal_type == _H264_STAP_A:
            return self._aggregate(payload, offset=1)
        if nal_type == _H264_FU_A:
            return self._h264_fragment(payload)
        # 0 is unspecified and 25-27, 29-31 are reserved or the FU-B form this does not read.
        self._drop()
        return b""

    def _h264_fragment(self, payload: bytes) -> bytes:
        if len(payload) < _H264_FU_HEADER:
            self._drop()
            return b""
        fu_header = payload[1]
        if fu_header & 0x80:
            if self._fragment is not None:
                # A NAL started before the previous one ended: those bytes are gone, and the
                # counter is what makes the loss visible instead of silent.
                self._drop()
            # Start: reconstruct the NAL header the fragments were split from, which is the
            # FU indicator's F and NRI plus the FU header's type (RFC 6184 5.8).
            self._fragment = bytearray(((payload[0] & 0xE0) | (fu_header & 0x1F),))
        elif self._fragment is None:
            # Mid-fragment with no start seen: the beginning was lost, and RFC 6184 requires
            # the fragments of a NAL to be consecutive, so there is nothing to attach it to.
            self._drop()
            return b""
        if not self._append(payload[2:]):
            return b""
        return self._emit_fragment() if fu_header & 0x40 else b""

    # -- HEVC (RFC 7798) ---------------------------------------------------------

    def _hevc(self, payload: bytes) -> bytes:
        if len(payload) < _HEVC_NAL_HEADER or payload[0] & 0x80:
            self._drop()
            return b""
        nal_type = (payload[0] >> 1) & 0x3F
        if nal_type <= _HEVC_SINGLE_NAL_MAX:
            return _START_CODE + payload
        if nal_type == _HEVC_AP:
            return self._aggregate(payload, offset=2)
        if nal_type == _HEVC_FU:
            return self._hevc_fragment(payload)
        self._drop()
        return b""

    def _hevc_fragment(self, payload: bytes) -> bytes:
        if len(payload) < _HEVC_FU_HEADER:
            self._drop()
            return b""
        fu_header = payload[2]
        if fu_header & 0x80:
            if self._fragment is not None:
                self._drop()
            # Start: F and the top bit of the layer id survive from the payload header, the
            # type comes from the FU header, and the rest of the layer id and the temporal id
            # are payload[1] (RFC 7798 4.4.3).
            self._fragment = bytearray(
                ((payload[0] & 0x81) | ((fu_header & 0x3F) << 1), payload[1])
            )
        elif self._fragment is None:
            self._drop()
            return b""
        if not self._append(payload[3:]):
            return b""
        return self._emit_fragment() if fu_header & 0x40 else b""

    # -- shared ------------------------------------------------------------------

    def _aggregate(self, payload: bytes, *, offset: int) -> bytes:
        """Split a length-prefixed aggregate (STAP-A, HEVC AP) into its NAL units."""
        out = bytearray()
        while offset + 2 <= len(payload):
            size = int.from_bytes(payload[offset : offset + 2], "big")
            unit = payload[offset + 2 : offset + 2 + size]
            if size == 0 or len(unit) != size:
                # A zero length is not a NAL unit, and a short one means the packet was cut:
                # either way the rest of the aggregate cannot be trusted.
                self._drop()
                break
            out += _START_CODE + unit
            offset += 2 + size
        return bytes(out)

    def _append(self, chunk: bytes) -> bool:
        """Grow the fragment being reassembled, refusing one that never ends."""
        if self._fragment is None:  # pragma: no cover - callers only append after a start
            return False
        self._fragment += chunk
        if len(self._fragment) > _MAX_NAL_BYTES:
            self._fragment = None
            self._drop()
            return False
        return True

    def _emit_fragment(self) -> bytes:
        fragment = bytes(self._fragment or b"")
        self._fragment = None
        return _START_CODE + fragment

    def flush(self) -> None:
        """Count a fragment the stream ended in the middle of.

        A NAL whose end fragment never arrived is media that did not make it out, and this
        module's rule is that such a loss is a number rather than silence. Called once, when
        the session stops feeding the depacketizer.
        """
        if self._fragment is not None:
            self._fragment = None
            self._drop()

    def _drop(self) -> None:
        self.dropped += 1
