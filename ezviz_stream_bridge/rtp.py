"""RTP depacketization: RFC 6184/7798 video into Annex-B, RFC 3640 audio into ADTS.

The VTM payload is not always MPEG-PS. A CS-C8c sends its video as RTP with payload type 96
carrying RFC 6184 H.264, and FFmpeg's `mpeg` demuxer cannot read a byte of it: fed that
payload it answers `could not find codec parameters` and writes nothing, which is exactly the
`bytes=0` a reporter saw on a stream that was arriving intact. A different FFmpeg demuxer does
not fix it either -- `-f rtp` wants a UDP URL and an SDP, and the packets are already here --
so the packaging is undone in this process, where the reassembly can also be tested against
the bytes a real camera sent.

The same session also carries the camera's audio, under a second payload type. A CS-C8c sends
it as RFC 3640 MPEG4-GENERIC in `AAC-hbr` mode, which is the other half of the same problem:
FFmpeg has a demuxer for it (`aac`) and that demuxer reads ADTS, which the payload does not
carry -- the SDP does, as an AudioSpecificConfig. So the Access Units are unwrapped here and
reframed, exactly as the video NAL units are.

Only the packaging is handled here. Where the payload starts inside a packet (CSRC list,
header extension, padding) comes from `pyezvizapi.stream.rtp_payload` -- the same unwrap the
diagnostic logs, so there is one implementation of the offset rule and not two.

Deliberately not handled, because nothing this bridge has seen emits them: H.264 FU-B,
HEVC PACI and RFC 3640 fragmentation. A packet carrying one is dropped and counted, as is any
packet whose NAL header is not valid for the codec the session settled on. A payload the
depacketizer cannot make sense of has to be visible as a number, not as silence.
"""

from __future__ import annotations

from dataclasses import dataclass

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

# Payload bytes kept per type for the breakdown. Enough to name a codec from its own header --
# an ADTS frame, a G.711 sample, a NAL unit -- and not enough to keep a packet.
_SAMPLE_BYTES = 32

# RFC 3640 MPEG4-GENERIC in `AAC-hbr` mode, which is what an EZVIZ/Hikvision camera signals
# for its audio: `mode=AAC-hbr; sizelength=13; indexlength=3; indexdeltalength=3; config=1408`.
# The two lengths are what make an AU header exactly 16 bits -- 13 of size and 3 of index --
# which is why a two-byte AU-header section always holds a whole number of them, and why the
# session's first audio payload (`00 10 | 07 20`) reads as one 228-byte Access Unit.
AAC_HBR = "aac-hbr"
_AAC_SIZE_LENGTH = 13
_AAC_INDEX_LENGTH = 3
_AAC_AU_HEADER_BITS = _AAC_SIZE_LENGTH + _AAC_INDEX_LENGTH

# The AU-headers-length field that opens the AU header section (RFC 3640 3.2.1).
_AU_HEADERS_LENGTH_BYTES = 2

# AudioSpecificConfig 0x1408, the value those SDPs carry: AAC-LC (audio object type 2),
# sampling frequency index 8 (16000 Hz), channel configuration 1 (mono). The rate is confirmed
# twice -- by the family's SDP convention and by the cadence of the reporter's own session
# (1081 packets in 69.76 s = 64.5 ms, which is 1024 samples at 16000 Hz). Mono is the
# convention, and the only reading the payload sizes allow: 228 bytes per 64 ms is ~28 kbit/s,
# where the same audio in stereo would be about twice that. Every session that handles audio
# logs this config, so a camera that differs is visible instead of silent.
AAC_LC_16K_MONO = 0x1408

# Audio object types whose name is worth printing in a log line. ADTS cannot describe any
# other, so a config naming one never reaches a stream.
_AAC_OBJECT_TYPES = {1: "AAC-Main", 2: "AAC-LC", 3: "AAC-SSR", 4: "AAC-LTP"}

# Sampling frequency by index, in the order ISO/IEC 14496-3 assigns them. The last three are
# the reserved values, which nothing legal writes.
_AAC_FREQUENCIES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
    16000, 12000, 11025, 8000, 7350,
)

# The three fields of an AudioSpecificConfig this reads or writes: an object type, a sampling
# frequency index and a channel configuration, each in its documented place beside the three
# GASpecificConfig bits that follow and that nothing here changes.
_AAC_OBJECT_TYPE_SHIFT = 11
_AAC_FREQUENCY_SHIFT = 7
_AAC_CHANNELS_SHIFT = 3
_AAC_CHANNELS_MASK = 0x07 << _AAC_CHANNELS_SHIFT

# The element ids a raw_data_block can open with, as far as the channel count goes: an SCE
# carries one channel and a CPE a pair. Anything else opening the block -- a fill element, a
# program config element, a coupling channel -- says nothing about the layout, and an encoder's
# primer frame is exactly that: FFmpeg's AAC encoder opens with a fill element alone (measured),
# which is why one reading is not enough and the scan below keeps looking.
_AAC_ELEMENT_CHANNELS = {0: 1, 1: 2}
_AAC_ELEMENT_BITS = 3

# ADTS, the framing FFmpeg's `aac` demuxer reads and the only one it reads. Its frame_length
# field is 13 bits and counts the header, so this is the largest Access Unit it can describe.
_ADTS_HEADER_BYTES = 7
_ADTS_MAX_AU_BYTES = (1 << 13) - 1 - _ADTS_HEADER_BYTES
# ADTS carries the profile in two bits, as `audio_object_type - 1`, over a range that starts at
# 1 for the object types the header can describe. AOT 5 (SBR) and above cannot be expressed.
_ADTS_MAX_OBJECT_TYPE = 4


@dataclass
class PayloadTypeStat:
    """What one RTP payload type carried over a session, as far as the depacketizer saw it.

    One RTP session can carry more than one payload type, and the count of the one that is not
    the elementary stream says only how much was discarded, not what it was. These are the
    fields that tell a second media stream apart from more of the first one's metadata: how
    many of its packets carried anything at all, how large those payloads were, which sequence
    numbers they arrived under -- a stream of its own numbers them separately even when it
    shares the SSRC, as the CS-C8c's metadata does not -- and what their first bytes are.

    `RtpDepacketizer` owns the instances and updates them as packets arrive; `payload_types`
    hands them out for reporting.
    """

    payload_type: int | None
    packets: int = 0
    media: int = 0  # of `packets`, the ones that carried payload bytes
    smallest: int = 0  # of those payloads, the shortest ...
    largest: int = 0  # ... and the longest
    header: bytes = b""  # the twelve fixed bytes of the first packet that carried media
    payload: bytes = b""  # the first media bytes of this type, capped at `_SAMPLE_BYTES`


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
        # Per payload type, not per packet: at most 128 keys, one per type the seven-bit field
        # can hold, so the memory this takes is bounded by the protocol rather than by the
        # stream.
        self._types: dict[int | None, PayloadTypeStat] = {}
        self._fragment: bytearray | None = None

    @property
    def payload_types(self) -> tuple[PayloadTypeStat, ...]:
        """One entry per payload type fed to this depacketizer, the busiest first.

        By volume rather than by type number: whether a second stream is here is a question
        about the type that carried most of the packets, not about the smallest number on the
        wire. Ties keep the order the types were first seen in.
        """
        return tuple(sorted(self._types.values(), key=lambda stat: -stat.packets))

    def feed(self, packet: bytes) -> bytes:
        """The Annex-B bytes one RTP packet contributes, empty when it contributes none."""
        try:
            payload = rtp_payload(packet)
        except PyEzvizError:
            # Not an RTP packet, or a header that overruns it: nothing to unwrap.
            self.dropped += 1
            return b""
        # Before the branch below, so the breakdown sees every packet the unwrap accepted: an
        # empty payload is still a packet of its type, and "this type arrives with a header
        # extension and nothing else" is half of what the breakdown exists to say. The type is
        # read here rather than in the unwrap because a header the unwrap rejects is a drop
        # above, not a payload type this session has to report on.
        self._account(_payload_type(packet), packet, payload)
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

    def _account(self, payload_type: int | None, packet: bytes, payload: bytes) -> None:
        """Count one packet under its payload type, keeping the first bytes of each kind."""
        stat = self._types.get(payload_type)
        if stat is None:
            stat = PayloadTypeStat(payload_type)
            self._types[payload_type] = stat
        stat.packets += 1
        if not payload:
            return
        # The first packet that carried media, not the first packet of the type: an entry is
        # read to ask what the type is, and a type whose opening packets are metadata has to be
        # described by the first bytes that were meant for a decoder.
        if not stat.header:
            stat.header = packet[:_RTP_HEADER_BYTES]
        size = len(payload)
        stat.smallest = size if stat.media == 0 else min(stat.smallest, size)
        stat.largest = max(stat.largest, size)
        stat.media += 1
        if not stat.payload:
            stat.payload = payload[:_SAMPLE_BYTES]

    def _drop(self) -> None:
        self.dropped += 1


# -- RFC 3640 (MPEG4-GENERIC, AAC-hbr) --------------------------------------------


def parse_au_section(payload: bytes) -> list[bytes] | None:
    """Split an RFC 3640 AU header section into its Access Units, or None if it is not one.

    The section is a 16-bit bit-length followed by that many bits of AU headers, then the
    Access Units concatenated. Because a header is a 13-bit size beside a 3-bit index -- the
    lengths the camera's own SDP names -- the section is always a whole number of two-byte
    headers, and it is byte aligned, which is where the media starts.

    The sum is what makes this a reading rather than a guess: the sizes have to account for
    every remaining byte of the payload, exactly. A hasty parse that only checked the first
    size would accept anything, and one that accepted a short sum would read the tail of an
    unrelated payload as audio. Nothing here is lenient for the same reason `detect_codec`
    is not: this doubles as the test that a second payload type is audio at all, and silence
    that sounds like success is the failure being designed against.

    Deliberately not handled: an Access Unit split across packets. `AAC-hbr` is the mode that
    requires one Access Unit per packet (RFC 3640 3.3.6), so a set of sizes that does not add
    up is not a fragment to reassemble, it is a payload this does not understand.
    """
    if len(payload) < _AU_HEADERS_LENGTH_BYTES:
        return None
    section_bits = int.from_bytes(payload[:_AU_HEADERS_LENGTH_BYTES], "big")
    if section_bits < _AAC_AU_HEADER_BITS or section_bits % _AAC_AU_HEADER_BITS:
        return None
    count = section_bits // _AAC_AU_HEADER_BITS
    section_bytes = _AU_HEADERS_LENGTH_BYTES + section_bits // 8
    if section_bytes > len(payload):
        return None

    sizes: list[int] = []
    offset = _AU_HEADERS_LENGTH_BYTES
    for _ in range(count):
        sizes.append(int.from_bytes(payload[offset : offset + 2], "big") >> _AAC_INDEX_LENGTH)
        offset += 2
    if any(size == 0 for size in sizes) or section_bytes + sum(sizes) != len(payload):
        return None

    units: list[bytes] = []
    for size in sizes:
        units.append(payload[offset : offset + size])
        offset += size
    return units


def adts_frame(unit: bytes, config: int = AAC_LC_16K_MONO) -> bytes:
    """Wrap one Access Unit in the 7-byte ADTS header FFmpeg's `aac` demuxer reads.

    RFC 3640 sends an AAC Access Unit bare, because the profile, sampling frequency and
    channel configuration an ADTS header would repeat are in the SDP instead. Rebuilding the
    header here is what turns the one back into the other, and it is why the config is a
    parameter: it is the one piece of the payload's meaning that is not in the payload.

    Buffer fullness is written as its 0x7FF "unknown" code and the raw data block count as
    zero. Neither carries anything for a stream being remuxed, and any other value would be an
    invented measurement in a header that FFmpeg has no reason to disbelieve.
    """
    profile = ((config >> _AAC_OBJECT_TYPE_SHIFT) & 0x1F) - 1
    frequency = (config >> _AAC_FREQUENCY_SHIFT) & 0x0F
    channels = (config >> _AAC_CHANNELS_SHIFT) & 0x07
    length = len(unit) + _ADTS_HEADER_BYTES
    return (
        bytes(
            (
                0xFF,
                0xF1,
                ((profile & 0x03) << 6) | (frequency << 2) | ((channels >> 2) & 0x01),
                ((channels & 0x03) << 6) | ((length >> 11) & 0x03),
                (length >> 3) & 0xFF,
                ((length & 0x07) << 5) | 0x1F,
                0xFC,
            )
        )
        + unit
    )


def carries_aac_hbr(payload: bytes) -> bool:
    """True when a payload is an AU header section whose Access Units this can frame.

    Asked ahead of the depacketizer, by a session deciding whether to give the stream a second
    FFmpeg input. That decision has to know a frame will come out of it: FFmpeg opens its
    inputs before it reads any of them, and blocks on one that has no frame yet, so an input
    primed with nothing is not "audio that has not started", it is a session that never
    starts. Saying so here, in terms of the same check the depacketizer makes, is what keeps
    the promise from being a second, weaker copy of it.
    """
    units = parse_au_section(payload)
    return units is not None and all(len(unit) <= _ADTS_MAX_AU_BYTES for unit in units)


def stream_channels(unit: bytes) -> int | None:
    """The channels an Access Unit's own first element names, or None if it names none.

    The one part of the audio configuration that is readable from the payload. An Access Unit is
    a `raw_data_block`, and its first three bits are the element id: 0 is a single channel
    element and 1 a channel pair element, which is the difference between mono and stereo. The
    sample rate is not in there at all, which is why the config still comes from the family's
    SDP convention and only this part is refined from the bytes.
    """
    if not unit:
        return None
    return _AAC_ELEMENT_CHANNELS.get(unit[0] >> (8 - _AAC_ELEMENT_BITS))


def set_channels(config: int, channels: int) -> int:
    """The same AudioSpecificConfig with a different channel configuration."""
    return (config & ~_AAC_CHANNELS_MASK) | ((channels << _AAC_CHANNELS_SHIFT) & _AAC_CHANNELS_MASK)


def describe_config(config: int = AAC_LC_16K_MONO) -> str:
    """The AudioSpecificConfig spelled out, for a log line somebody has to be able to check.

    Four hex digits say nothing on their own, and this is the one parameter of the audio path
    that is not in the payload: it comes from the camera's SDP, which this bridge never sees.
    Printing the rate and the channel count is what turns "the audio is wrong on my camera"
    into a number somebody can compare against that same camera's RTSP SDP.
    """
    object_type = (config >> _AAC_OBJECT_TYPE_SHIFT) & 0x1F
    frequency = (config >> _AAC_FREQUENCY_SHIFT) & 0x0F
    channels = (config >> _AAC_CHANNELS_SHIFT) & 0x07
    name = _AAC_OBJECT_TYPES.get(object_type, f"object type {object_type}")
    if frequency < len(_AAC_FREQUENCIES):
        rate = f"{_AAC_FREQUENCIES[frequency]} Hz"
    else:
        rate = f"frequency index {frequency} (reserved)"
    # 0 is not "no channels": the specification uses it to say the layout is carried in-band.
    layout = {0: "layout in the stream", 1: "mono"}.get(channels, f"{channels} channels")
    return f"{name}, {rate}, {layout}"


class AacHbrDepacketizer:
    """RFC 3640 MPEG4-GENERIC AAC payloads, turned into the ADTS stream FFmpeg reads.

    Not an `RtpDepacketizer`: that one is bound to a codec whose bytes it validates as NAL
    units, and it holds the fragment being reassembled. Nothing here is fragmented and nothing
    is reassembled -- a packet carries whole Access Units and each one is framed on the spot --
    so the only state is the counters.

    `dropped` counts packets whose payload is not an AU header section and Access Units too
    large to describe in an ADTS frame. A session reporting zero is the evidence that the
    format was read right, which is why the number is printed even when it is reassuring.
    """

    def __init__(
        self, *, config: int = AAC_LC_16K_MONO, payload_type: int | None = None
    ) -> None:
        object_type = (config >> _AAC_OBJECT_TYPE_SHIFT) & 0x1F
        if not 1 <= object_type <= _ADTS_MAX_OBJECT_TYPE:
            # The header below builds `object_type - 1` into two bits, which is a lie for an
            # object type the field cannot hold. Refusing here keeps that from being a stream
            # that decodes to noise.
            raise ValueError(f"AudioSpecificConfig {config:#06x} is not an ADTS profile")
        self.codec = AAC_HBR
        self.config = config
        self.payload_type = payload_type
        self.packets = 0
        self.aus = 0
        self.dropped = 0
        self.skipped = 0

    def feed(self, packet: bytes) -> bytes:
        """The ADTS bytes one RTP packet contributes, empty when it contributes none."""
        try:
            payload = rtp_payload(packet)
        except PyEzvizError:
            self.dropped += 1
            return b""
        self.packets += 1
        if self.payload_type is not None and _payload_type(packet) != self.payload_type:
            self.skipped += 1
            return b""
        if not payload:
            # A packet carrying only a header extension, as the CS-C8c's first two are.
            return b""
        units = parse_au_section(payload)
        if units is None:
            self.dropped += 1
            return b""
        frames = bytearray()
        for unit in units:
            if len(unit) > _ADTS_MAX_AU_BYTES:
                # All or nothing for the packet: a partial one would frame an Access Unit
                # whose size the reader cannot know, and a wrong frame is worse than a
                # missing one.
                self.dropped += 1
                return b""
            frames += adts_frame(unit, self.config)
        self.aus += len(units)
        return bytes(frames)
