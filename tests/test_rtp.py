"""Tests for the RTP depacketizer, against the bytes a real camera sent.

The fixtures are the leading packets of the CS-C8c session reported in issue #1, copied out of
its log byte by byte: two payload-type-112 packets whose header extension consumes the whole
packet, then the H.264 stream proper -- an SPS, a PPS, and the start of a fragmented IDR. The
log only carries the first 64 bytes of each packet, so the fragment bodies below are those
bytes: enough to prove the header layouts, and the continuations are built to the same layout
because the log does not contain them.
"""

from __future__ import annotations

import pytest

from ezviz_stream_bridge import rtp as rtp_module
from ezviz_stream_bridge.rtp import (
    H264,
    HEVC,
    AacHbrDepacketizer,
    RtpDepacketizer,
    adts_frame,
    carries_aac_hbr,
    describe_config,
    detect_codec,
    parse_au_section,
)

START_CODE = b"\x00\x00\x00\x01"

# The value the camera puts in its RTP SSRC field -- and, byte for byte, the IDMX sentinel
# pyezvizapi's local frame parser looks for in the same place (see 0.1.6's note).
RTP_SENTINEL = b"\x55\x66\x77\x88"

# packet[0], 64 bytes: V2 P0 X1 CC0 M1 PT112, extension profile 0x0001 and 12 words long, which
# is the whole packet. No media at all.
PT112_WITH_EXTENSION = bytes.fromhex(
    "90 f0 3f b7 7c 74 80 64 55 66 77 88 00 01 00 0c 40 0e 48 4b 00 02 1a 9b ab 23 31 c8"
    " 00 ff ff ff 45 0a 1b 0f 00 be fd b4 ff ff ff ff 41 12 48 4b 00 01 02 03 04 05 06 07"
    " 08 09 0a 0b 0c 0d 0e 0f"
)

# packet[1], 44 bytes: the same shape with a 7-word extension (0x0002).
PT112_WITH_PROFILE_2 = bytes.fromhex(
    "90 f0 3f b8 7c 74 80 64 55 66 77 88 00 02 00 07 42 0e 07 10 10 ea 0a 00 05 a0 11 1f"
    " 00 00 2e e0 43 0a 00 90 fe 00 fa 03 01 f4 03 ff"
)

# packet[2], 36 bytes: PT96, and the payload opens with 0x67 -- an H.264 SPS, H.264 Main
# (profile_idc 0x4d) at level 5.0.
H264_SPS = bytes.fromhex(
    "80 60 32 03 7c 74 80 64 55 66 77 88 67 4d 00 32 8d 8d 40 14 00 5a d3 70 10 10 14 00"
    " 00 2e e0 00 05 7e 40 10"
)

# packet[3], 16 bytes: the matching PPS.
H264_PPS = bytes.fromhex(
    "80 60 32 04 7c 74 80 64 55 66 77 88 68 ee 38 80"
)

# packet[4], the first 64 of its 1152 bytes: 0x7c is FU-A, and 0x85 is a FU header with the
# start bit set and nal_unit_type 5 -- the beginning of a fragmented IDR.
H264_IDR_START = bytes.fromhex(
    "80 60 32 05 7c 74 80 64 55 66 77 88 7c 85 88 80 00 00 03 00 ea 03 00 00 03 02 1f 72"
    " 7b 00 b3 da 1d 70 14 f6 c3 ac 07 09 f2 36 1a 89 16 80 88 c4 f8 8d 22 a4 47 9b b6 b3"
    " 18 f7 af c0 38 e5 15 ce"
)

# The fragbody the start packet above actually carries: payload minus the FU indicator and the
# FU header.
IDR_START_BODY = H264_IDR_START[14:]


def rtp_packet(payload: bytes, *, payload_type: int = 96, padding: int = 0) -> bytes:
    """An RTP packet in the shape this camera sends: 12-byte header, then the media."""
    first = 0xA0 if padding else 0x80
    body = (
        bytes([first, payload_type])
        + (0x3205).to_bytes(2, "big")
        + (0x7C748064).to_bytes(4, "big")
        + RTP_SENTINEL
        + payload
    )
    if padding:
        body += b"\x00" * (padding - 1) + bytes([padding])
    return body


# -- codec detection -------------------------------------------------------------------


def test_the_reported_sps_identifies_h264() -> None:
    assert detect_codec(H264_SPS[12:]) == H264


def test_a_pps_alone_does_not_identify_a_codec() -> None:
    """0x48 is both an H.264 PPS and an HEVC filler-data unit: waiting for the SPS is what
    removes the ambiguity, and one packet of waiting is the price."""
    assert detect_codec(H264_PPS[12:]) is None
    assert detect_codec(bytes([0x48, 0x01])) is None
    assert detect_codec(bytes([0x68, 0xee])) is None


def test_hevc_parameter_sets_identify_hevc() -> None:
    assert detect_codec(bytes([0x40, 0x01, 0x00, 0x00])) == HEVC  # VPS
    assert detect_codec(bytes([0x42, 0x01, 0x01, 0x60])) == HEVC  # SPS
    assert detect_codec(bytes([0x44, 0x01, 0xc0])) == HEVC  # PPS


def test_a_parameter_set_inside_an_aggregate_still_identifies_the_codec() -> None:
    """RFC 6184 lets an SPS and a PPS travel in one STAP-A and RFC 7798 sends a VPS/SPS/PPS set
    in an AP, so refusing the stream over the packaging would refuse a conforming one. The
    marker alone is ambiguous -- 0x78 reads as HEVC type 60, 0x60 as H.264 type 0 -- and it is
    the unit inside that makes the reading trustworthy."""
    h264 = bytes([0x67, 0x4D, 0x00, 0x32])
    pps = bytes([0x68, 0xEE, 0x38, 0x80])
    stap_a = b"\x78" + b"".join(len(unit).to_bytes(2, "big") + unit for unit in (h264, pps))

    vps = bytes([0x40, 0x01, 0x0C, 0x01])
    hevc_sps = bytes([0x42, 0x01, 0x01, 0x60])
    ap = bytes([0x60, 0x01]) + b"".join(
        len(unit).to_bytes(2, "big") + unit for unit in (vps, hevc_sps)
    )

    assert detect_codec(stap_a) == H264
    assert detect_codec(ap) == HEVC


def test_an_aggregate_does_not_hand_the_codec_to_the_other_one() -> None:
    """The marker commits to a codec and the unit inside only has to agree with it: a STAP-A
    carrying an HEVC-looking unit is not evidence of HEVC, and the other way round."""
    hevc_sps = bytes([0x42, 0x01, 0x01, 0x60])
    h264_sps = bytes([0x67, 0x4D, 0x00, 0x32])

    assert detect_codec(b"\x78" + len(hevc_sps).to_bytes(2, "big") + hevc_sps) is None
    assert detect_codec(bytes([0x60, 0x01]) + len(h264_sps).to_bytes(2, "big") + h264_sps) is None
    assert detect_codec(b"\x78" + (3).to_bytes(2, "big") + bytes([0x41, 0x9A, 0x00])) is None


def test_the_two_headers_do_not_claim_each_other_s_units() -> None:
    """What makes each reading plausible is an ordinary unit of the other codec, and a wrong
    codec is worse than no codec at all: the demuxer, not the stream, is then what produces
    nothing.

    The guarantee holds for single-layer streams, which is every stream a camera sends. A
    layered HEVC stream could still put a layer id of 32 or more in an access-unit delimiter
    and a profile number in its second byte, and nothing short of parsing the slice data would
    tell that apart from an H.264 SPS; nothing here tries to.
    """
    # 0x41 is an H.264 P-slice (nal_ref_idc 2, nal_unit_type 1). Read as HEVC it is a VPS whose
    # layer id is 32 or more -- a layer no camera sends, and the low bit says so.
    assert detect_codec(bytes([0x41, 0x01, 0x00, 0x00])) is None
    # 0x47 is HEVC's access-unit delimiter with that same impossible layer id, and reads as an
    # H.264 SPS; 0x27 is HEVC's IDR_W_RADL and reads the same way. The profile byte is what
    # settles both: 0x01 is no profile.
    assert detect_codec(bytes([0x47, 0x01, 0x00, 0x00])) is None
    assert detect_codec(bytes([0x27, 0x01, 0x00, 0x00])) is None
    # The same header with a profile the specification has assigned is an SPS.
    assert detect_codec(bytes([0x27, 0x4D, 0x00, 0x32])) == H264
    # An aggregate marker with that low bit set is an H.264 slice, not an HEVC AP.
    assert detect_codec(bytes([0x61, 0x01, 0x00, 0x04, 0x42, 0x01, 0x01, 0x60])) is None


def test_a_parameter_set_is_found_anywhere_inside_an_aggregate() -> None:
    """The parameter sets sit at the front of an aggregate by convention, not by rule, so
    stopping at the first unit would refuse a conforming stream over where its SPS happened
    to be bundled."""
    pps = bytes([0x68, 0xEE, 0x38, 0x80])
    sps = bytes([0x67, 0x4D, 0x00, 0x32])
    payload = b"\x78" + b"".join(len(unit).to_bytes(2, "big") + unit for unit in (pps, sps))

    assert detect_codec(payload) == H264


def test_a_fragment_open_when_the_stream_ends_is_counted() -> None:
    """A NAL whose end fragment never arrived is media that did not make it out, and this
    module's rule is that such a loss is a number rather than silence. Counting it twice would
    be its own kind of lie."""
    depacketizer = RtpDepacketizer(H264)

    assert depacketizer.feed(rtp_packet(b"\x7c\x85" + b"\xaa" * 4)) == b""
    assert depacketizer.dropped == 0, "nothing is lost while the fragment is still open"

    depacketizer.flush()
    assert depacketizer.dropped == 1
    depacketizer.flush()
    assert depacketizer.dropped == 1


def test_another_payload_type_is_not_this_elementary_stream() -> None:
    """The CS-C8c sends its metadata under payload type 112 on the same RTP session as the
    video. Once the codec has been identified from payload type 96, nothing else is this
    stream, whatever its bytes happen to look like."""
    depacketizer = RtpDepacketizer(H264, payload_type=96)

    assert depacketizer.feed(H264_SPS) == START_CODE + H264_SPS[12:]
    assert depacketizer.feed(rtp_packet(b"\x67\x4d\x00\x32", payload_type=112)) == b""
    assert depacketizer.skipped == 1
    assert depacketizer.dropped == 0, "another payload type is not an unreadable packet"


def test_the_breakdown_counts_every_type_the_unwrap_accepted() -> None:
    """The skipped count says how much of another payload type was dropped and nothing about
    what it was. These are the bytes that say: a packet whose extension consumes it is a packet
    of its type and not of the elementary stream, and the first packet that carried media is the
    one worth printing, because the ones before it are the ones that say nothing."""
    depacketizer = RtpDepacketizer(H264, payload_type=96)

    depacketizer.feed(PT112_WITH_EXTENSION)
    depacketizer.feed(rtp_packet(b"\xff\xf1\x50\x80\x00", payload_type=112))
    depacketizer.feed(rtp_packet(b"\x67\x4d\x00\x32", payload_type=96))

    stats = {stat.payload_type: stat for stat in depacketizer.payload_types}
    metadata = stats[112]
    assert (metadata.packets, metadata.media) == (2, 1), "the empty packet is counted there"
    assert (metadata.smallest, metadata.largest) == (5, 5)
    assert metadata.payload == b"\xff\xf1\x50\x80\x00"
    assert metadata.header[:2] == bytes([0x80, 112])
    video = stats[96]
    assert (video.packets, video.media) == (1, 1)
    assert video.payload == bytes.fromhex("67 4d 00 32")


def test_the_breakdown_is_ordered_by_volume() -> None:
    """The type worth looking at is the one that carried the most packets, whatever its
    number; and one type is still one entry, because "this session carried only the elementary
    stream" is the answer that rules a second stream out."""
    depacketizer = RtpDepacketizer(H264, payload_type=96)
    depacketizer.feed(H264_SPS)
    depacketizer.feed(PT112_WITH_EXTENSION)
    depacketizer.feed(PT112_WITH_PROFILE_2)

    assert [stat.payload_type for stat in depacketizer.payload_types] == [112, 96]

    single = RtpDepacketizer(H264, payload_type=96)
    single.feed(H264_SPS)
    assert [stat.payload_type for stat in single.payload_types] == [96]
    assert single.payload_types[0].header == H264_SPS[:12]


def test_without_a_payload_type_nothing_is_filtered() -> None:
    """The type is only enforced when the caller knows it; a depacketizer built without one
    must not start inventing a filter of its own."""
    depacketizer = RtpDepacketizer(H264)

    assert depacketizer.feed(rtp_packet(b"\x67\x4d\x00\x32", payload_type=112)) != b""
    assert depacketizer.skipped == 0


def test_detection_refuses_to_guess() -> None:
    """A slice or a fragment says nothing on its own: the two codecs number them differently
    and the bytes collide, which is exactly how a session would pick the wrong demuxer."""
    assert detect_codec(b"") is None
    assert detect_codec(b"\x67") is None  # one byte is not enough to read a profile
    assert detect_codec(b"\x80\x01\x00\x00") is None  # forbidden_zero_bit set
    assert detect_codec(bytes([0x65, 0x88, 0x80, 0x00])) is None  # H.264 IDR, no parameter set
    assert detect_codec(bytes([0x42, 0x00, 0x01])) is None  # HEVC type with temporal id 0


# -- the reported packets ---------------------------------------------------------------


def test_the_metadata_packets_carry_no_media() -> None:
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(PT112_WITH_EXTENSION) == b""
    assert depacketizer.feed(PT112_WITH_PROFILE_2) == b""
    assert depacketizer.dropped == 0, "a packet the camera meant to send is not a drop"


def test_the_parameter_sets_become_annex_b() -> None:
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(H264_SPS) == START_CODE + H264_SPS[12:]
    assert depacketizer.feed(H264_PPS) == START_CODE + H264_PPS[12:]
    assert depacketizer.dropped == 0


def test_the_reported_fragment_reassembles_into_its_idr() -> None:
    """The start packet carries 0x7c 0x85: FU-A with the start bit set and nal_unit_type 5.
    The NAL header that comes back out is the FU indicator's F and NRI plus that type, which
    is 0x65 -- an IDR."""
    depacketizer = RtpDepacketizer(H264)
    middle = rtp_packet(b"\x7c\x05" + b"\x11" * 8)
    end = rtp_packet(b"\x7c\x45" + b"\x22" * 4)

    assert depacketizer.feed(H264_IDR_START) == b"", "a fragment is not a NAL until it ends"
    assert depacketizer.feed(middle) == b""
    assert depacketizer.feed(end) == (
        START_CODE + b"\x65" + IDR_START_BODY + b"\x11" * 8 + b"\x22" * 4
    )
    assert depacketizer.dropped == 0


# -- fragmentation edges -----------------------------------------------------------------


def test_a_continuation_without_a_start_is_dropped() -> None:
    """RFC 6184 requires a NAL's fragments to be consecutive, so a continuation with no start
    seen has nothing to attach to. Dropping it is the only honest reading; emitting it would
    hand FFmpeg a NAL header invented from a mid-stream byte."""
    depacketizer = RtpDepacketizer(H264)

    assert depacketizer.feed(rtp_packet(b"\x7c\x05" + b"\xaa" * 4)) == b""
    assert depacketizer.dropped == 1


def test_a_new_start_abandons_the_fragment_in_progress() -> None:
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(rtp_packet(b"\x7c\x85\xaa\xbb")) == b""
    assert depacketizer.feed(rtp_packet(b"\x7c\x85\xcc\xdd")) == b""
    assert depacketizer.dropped == 1, "the abandoned NAL was lost, and that is worth counting"

    assert depacketizer.feed(rtp_packet(b"\x7c\x45\xee")) == START_CODE + b"\x65\xcc\xdd\xee"


def test_a_fragment_that_never_ends_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that never sets the end bit must not grow the buffer without limit; the size
    bound is the only thing standing between a broken camera and the process's memory."""
    monkeypatch.setattr(rtp_module, "_MAX_NAL_BYTES", 8)
    depacketizer = RtpDepacketizer(H264)

    assert depacketizer.feed(rtp_packet(b"\x7c\x85" + b"\x00" * 4)) == b""
    assert depacketizer.feed(rtp_packet(b"\x7c\x05" + b"\x00" * 8)) == b""
    assert depacketizer.dropped == 1
    # The buffer is gone, so the end of that NAL cannot resurrect it.
    assert depacketizer.feed(rtp_packet(b"\x7c\x45" + b"\x00" * 2)) == b""


# -- aggregation and other packet forms --------------------------------------------------


def test_a_stap_a_is_split_into_its_nal_units() -> None:
    units = [bytes([0x67, 0x4D, 0x00, 0x32]), bytes([0x68, 0xEE, 0x38, 0x80])]
    payload = b"\x78" + b"".join(len(unit).to_bytes(2, "big") + unit for unit in units)

    assert RtpDepacketizer(H264).feed(rtp_packet(payload)) == b"".join(
        START_CODE + unit for unit in units
    )


def test_a_truncated_aggregate_keeps_the_complete_units() -> None:
    payload = b"\x78" + (3).to_bytes(2, "big") + b"\x67\x01\x02" + b"\x00\x09\x68"

    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(rtp_packet(payload)) == START_CODE + b"\x67\x01\x02"
    assert depacketizer.dropped == 1


def test_padding_is_stripped_before_the_nal_is_read() -> None:
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(rtp_packet(b"\x67\x4d\x00\x32", padding=8)) == (
        START_CODE + b"\x67\x4d\x00\x32"
    )


def test_a_packet_that_is_not_rtp_is_dropped() -> None:
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(b"\x11" * 40) == b""
    assert depacketizer.dropped == 1


def test_an_unsupported_packet_form_is_dropped() -> None:
    """FU-B and the reserved types are counted, not misread as a slice."""
    depacketizer = RtpDepacketizer(H264)
    assert depacketizer.feed(rtp_packet(b"\x7d\x85\xaa")) == b""  # type 29, FU-B
    assert depacketizer.feed(rtp_packet(b"\xfe\x00\x00")) == b""  # type 30, reserved
    assert depacketizer.dropped == 2


# -- HEVC --------------------------------------------------------------------------------


def test_an_hevc_fragment_reassembles_with_its_two_byte_header() -> None:
    """HEVC's header is F, type and the top of the layer id in one byte and the rest of the
    layer id plus the temporal id in the next; a fragment rebuilds the type and keeps the
    rest (RFC 7798 4.4.3)."""
    depacketizer = RtpDepacketizer(HEVC)

    assert depacketizer.feed(rtp_packet(b"\x62\x01\x85" + b"\xaa" * 4)) == b""
    assert depacketizer.feed(rtp_packet(b"\x62\x01\x45" + b"\xbb")) == (
        START_CODE + b"\x0a\x01" + b"\xaa" * 4 + b"\xbb"
    )
    assert depacketizer.dropped == 0


def test_an_hevc_aggregate_is_split_into_its_nal_units() -> None:
    units = [bytes([0x42, 0x01, 0x01, 0x60]), bytes([0x44, 0x01, 0xc0])]
    payload = b"\x60\x01" + b"".join(len(unit).to_bytes(2, "big") + unit for unit in units)

    assert RtpDepacketizer(HEVC).feed(rtp_packet(payload)) == b"".join(
        START_CODE + unit for unit in units
    )


def test_an_hevc_single_nal_unit_keeps_its_own_two_byte_header() -> None:
    nal = bytes([0x42, 0x01, 0x01, 0x60, 0x00])
    assert RtpDepacketizer(HEVC).feed(rtp_packet(nal)) == START_CODE + nal


# -- RFC 3640 audio (MPEG4-GENERIC, AAC-hbr) -------------------------------------------


def au_section(units: list[bytes]) -> bytes:
    """The AU header section an AAC-hbr sender puts in front of these Access Units.

    A 16-bit bit-length, then one 13-bit size beside a 3-bit index per unit, then the units.
    """
    headers = b"".join(
        ((len(unit) << 3) | index).to_bytes(2, "big") for index, unit in enumerate(units)
    )
    return (len(headers) * 8).to_bytes(2, "big") + headers + b"".join(units)


def test_the_reported_audio_payload_reads_as_one_access_unit() -> None:
    """The CS-C8c's audio, read the way the family's own SDP says to read it.

    `payload=00 10 07 20 ...`: an AU-headers-length of 16 bits, then one 13-bit size beside a
    3-bit index -- 0x0720 >> 3 is 228 bytes of Access Unit at index 0, and the media starts at
    byte 4, which is what a 232-byte payload has left after four bytes of header.
    """
    unit = bytes(range(228))
    payload = bytes.fromhex("00 10 07 20") + unit

    assert parse_au_section(payload) == [unit]
    assert len(payload) == 4 + 228


def test_an_au_section_whose_sizes_do_not_add_up_is_refused() -> None:
    """The sum is the whole reading. Anything short of an exact fit is a payload that merely
    starts with two plausible bytes, and framing it would hand FFmpeg an ADTS header describing
    a frame that is not there."""
    short = (16).to_bytes(2, "big") + (228 << 3).to_bytes(2, "big") + bytes(227)

    assert parse_au_section(short) is None
    assert parse_au_section(short + b"\x00") == [bytes(228)], "one byte more and it fits"


@pytest.mark.parametrize(
    "payload",
    [
        b"",  # nothing at all
        b"\x00",  # half a length field
        b"\x00\x08" + bytes(32),  # 8 bits of AU header is not a whole one of these
        b"\x00\x10" + bytes(4),  # the section runs past the end of the payload
        b"\x00\x10\x00\x00",  # a zero-sized Access Unit
        b"\x00\xff\xff\xff\xff",  # a section far longer than any payload
    ],
)
def test_a_payload_that_is_not_an_au_section_is_refused(payload: bytes) -> None:
    assert parse_au_section(payload) is None


def test_a_multi_au_packet_yields_its_units_in_order() -> None:
    units = [bytes([1]) * 7, bytes([2]) * 9, bytes([3]) * 11]
    assert parse_au_section(au_section(units)) == units


def test_the_adts_header_is_the_one_ffmpeg_writes() -> None:
    """Observed, not built twice: for a 512-byte Access Unit at this config, FFmpeg's own ADTS
    writer emits `ff f1 60 40 40 ff fc`. Rebuilding it has to land on those seven bytes, or the
    rebuild is a second opinion about a format that already has one."""
    unit = bytes(512)

    frame = adts_frame(unit)

    assert frame[:7] == bytes.fromhex("ff f1 60 40 40 ff fc")
    assert frame[7:] == unit


def test_an_access_unit_too_large_for_adts_is_not_framable() -> None:
    """The header's length field is thirteen bits wide. Past that there is no frame to build,
    and the difference between "not audio" and "audio this cannot describe" has to be known
    before a second FFmpeg input is started on it: an input that never delivers a frame is one
    FFmpeg blocks on, so a session primed with nothing never starts."""
    assert carries_aac_hbr(au_section([bytes(8184)])) is True
    assert carries_aac_hbr(au_section([bytes(8185)])) is False


def test_the_audio_depacketizer_reframes_a_packet_the_way_the_camera_sent_it() -> None:
    """One packet end to end: RTP header, AU header section, Access Unit in, ADTS frame out --
    the seven bytes FFmpeg's own writer produces for the same config, in front of the same
    bytes, with the four bytes of section removed."""
    unit = bytes(range(256)) * 2
    depacketizer = AacHbrDepacketizer(payload_type=104)

    frames = depacketizer.feed(rtp_packet(au_section([unit]), payload_type=104))

    assert frames == adts_frame(unit)
    assert frames[7:] == unit
    assert (depacketizer.packets, depacketizer.aus, depacketizer.dropped) == (1, 1, 0)


def test_the_audio_depacketizer_counts_what_it_cannot_read() -> None:
    """Two different faults and two counters: a packet of another payload type is not this
    stream at all, and a payload that is not an AU header section is this bridge reading the
    format wrong. A session reporting zero of either is the evidence that it read it right,
    which is why both are numbers rather than silence."""
    depacketizer = AacHbrDepacketizer(payload_type=104)

    assert depacketizer.feed(rtp_packet(au_section([b"\x01" * 8]), payload_type=96)) == b""
    assert depacketizer.feed(rtp_packet(b"\xff\xf1\x50\x80\x00", payload_type=104)) == b""
    assert depacketizer.feed(rtp_packet(b"", payload_type=104)) == b"", "no payload, no fault"
    assert (depacketizer.skipped, depacketizer.dropped) == (1, 1)


def test_a_video_payload_is_not_read_as_audio() -> None:
    """The reading doubles as the detection, so it has to be a reading of audio rather than of
    whatever a video payload happens to look like. These are the three leading payloads of the
    reported session, and none of them is an AU header section."""
    for packet in (H264_SPS, H264_PPS, H264_IDR_START):
        assert parse_au_section(packet[12:]) is None, packet[12:20].hex(" ")


def test_a_config_adts_cannot_describe_is_refused() -> None:
    """AOT 5 is SBR and the two profile bits cannot hold it. Refusing is the point: framing it
    anyway would describe a stream a decoder would then read as something else entirely."""
    with pytest.raises(ValueError, match="not an ADTS profile"):
        AacHbrDepacketizer(config=0x2C08)


def test_describe_config_spells_out_the_family_default() -> None:
    """The one parameter of the audio path that is not in the payload, printed so it can be
    argued with: this is the value Hikvision and EZVIZ RTSP SDPs carry, and a camera that
    differs is meant to be visible in the log rather than audible on the speaker."""
    assert describe_config() == "AAC-LC, 16000 Hz, mono"
    assert describe_config(0x1210) == "AAC-LC, 44100 Hz, 2 channels"
    assert describe_config(0x1408) == describe_config()
    assert "reserved" in describe_config(0x1688)
