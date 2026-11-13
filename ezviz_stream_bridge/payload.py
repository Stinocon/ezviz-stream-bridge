"""What the camera is sending, read from the leading packets of a session.

The transport verdict, the codec and audio readings taken from the prefix, and the plan that
says how FFmpeg has to be started to read the result. All of it is pure -- packet bytes in, a
reading out -- which is why it lives apart from the session, whose own business is threads,
sockets and a child process. Split out of `session.py` in 0.1.18, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.stream import rtp_payload

from .rtp import (
    AAC_LC_16K_MONO,
    carries_aac_hbr,
    detect_codec,
    parse_au_section,
    set_channels,
    stream_channels,
)

# The demuxer FFmpeg reads MPEG-PS with. Anything else here is an elementary stream the
# session has already depacketized, and the name is the codec.
_MPEG_PS_DEMUXER = "mpeg"

# The demuxer FFmpeg reads ADTS with, which is the framing the audio path rebuilds out of an
# RFC 3640 payload: the camera's audio arrives bare, and this is the only spelling of it that
# FFmpeg has a demuxer for.
_AAC_DEMUXER = "aac"

# MPEG-PS pack_start_code. The byte after `00 00 01` is 0xBA, whose top bit is set -- the
# forbidden_zero_bit of an H.264/H.265 NAL header -- so it cannot be a valid NAL, and a
# conformant elementary stream cannot contain this sequence (emulation prevention blocks
# `00 00 01` inside a NAL). Finding it is strong evidence of MPEG-PS.
_PS_PACK_START = b"\x00\x00\x01\xba"
_TS_SYNC_BYTE = 0x47
_TS_PACKET_SIZE = 188
# Sync bytes required on the 188-byte grid. Two is a coincidence about one time in nine on
# random data; three is about one time in two thousand, low enough to trust as a diagnostic.
_TS_SYNC_RUN = 3
# The two top bits of an RTP header byte carry the version, which is 2 here.
_RTP_VERSION_MASK = 0xC0
_RTP_VERSION_2 = 0x80
# The fixed part of an RTP header, before any CSRC list or extension.
_RTP_HEADER_BYTES = 12


def _looks_like_mpeg_ts(data: bytes, start: int) -> bool:
    """True when 0x47 repeats on the 188-byte grid from `start` for several packets."""
    for index in range(1, _TS_SYNC_RUN):
        position = start + index * _TS_PACKET_SIZE
        if position >= len(data) or data[position] != _TS_SYNC_BYTE:
            return False
    return True


def classify_payload(data: bytes) -> tuple[str, int]:
    """Best-effort transport for a buffered prefix, and where its signature sits.

    Returns a `pyezvizapi` `StreamTransport` name and the byte offset of the signature, or
    -1 when nothing recognisable is present. MPEG-PS is checked first and recognised by its
    pack start code, which may sit at any offset when the prefix starts mid-pack -- the
    reason a single-packet check could not see it. RTP has no in-band sync pattern, so it is
    only recognisable at the start of the buffer, and it is checked last: a mid-packet TS
    start whose first byte happens to carry RTP's version bits would otherwise be reported
    as RTP, far more often than a genuine RTP payload would produce a chance 0x47 grid.
    """
    pack = data.find(_PS_PACK_START)
    if pack >= 0:
        return "MPEG_PS", pack

    sync = data.find(bytes((_TS_SYNC_BYTE,)))
    while sync >= 0:
        if _looks_like_mpeg_ts(data, sync):
            return "MPEG_TS", sync
        sync = data.find(bytes((_TS_SYNC_BYTE,)), sync + 1)

    if data and (data[0] & _RTP_VERSION_MASK) == _RTP_VERSION_2:
        return "RTP", 0

    return "UNKNOWN", -1


def _codec_of(body: bytes) -> str | None:
    """The codec a video packet's payload names, if it names one."""
    try:
        return detect_codec(rtp_payload(body))
    except PyEzvizError:
        return None


def _audio_payload(body: bytes) -> bool:
    """True when a video packet's payload is audio this bridge can turn into ADTS.

    The reading is self-validating rather than a payload type: the AU sizes have to account
    for every byte of the payload exactly, which no other kind of packet does by accident. A
    camera that stamps its audio with a different payload type, or renumbers it mid-session,
    is therefore still recognised -- and one that sends a second kind of video under a payload
    type of its own is not mistaken for a camera with audio.
    """
    try:
        return carries_aac_hbr(rtp_payload(body))
    except PyEzvizError:
        return False


def _prefix_audio(payload_type: int | None, packets: tuple[bytes, ...]) -> int | None:
    """The audio payload type, when the video prefix already carried some.

    Separate from the window because it is the common case and it costs nothing: a camera that
    starts its audio with its video puts it inside the eight packets the demuxer is chosen
    from, and there is then nothing to wait for. Only a camera that starts later makes the
    session read on.
    """
    for body in packets:
        candidate = _payload_type_of(body)
        if candidate != payload_type and _audio_payload(body):
            return candidate
    return None


def _audio_channels(payload_type: int | None, packets: tuple[bytes, ...]) -> int | None:
    """The channel count the camera's own bytes name, from the first Access Unit that names one.

    The first, and not the first Access Unit: an encoder's primer frame is a fill element with
    no channel of its own -- FFmpeg's own AAC encoder opens with one, measured -- so a reading
    taken from packet one would be "this says nothing" on a perfectly ordinary stereo stream.
    Scanning on, inside the packets the session already had to parse, is what makes the reading
    land instead of falling back to the family's default.

    A payload type of None is not the audio's, whatever the bytes look like: it is how a body
    that is not an RTP packet is counted, and matching it here would hand the channel count of
    an unrelated payload to the audio stream.
    """
    if payload_type is None:
        return None
    for body in packets:
        if _payload_type_of(body) != payload_type:
            continue
        try:
            units = parse_au_section(rtp_payload(body))
        except PyEzvizError:
            continue
        for unit in units or ():
            channels = stream_channels(unit)
            if channels is not None:
                return channels
    return None


def _why_not_served(
    audio_window: float, window_closed_at: float | None, first_at: float
) -> str:
    """When a payload type that carried media was there, relative to the session's audio window.

    Two facts, and no cause. Comparing against the *configured* window instead of the one the
    session actually ran is what made the first version of this line blame the window for a payload
    that started inside it: the window opens after the VTM handshake and the prefix read, so on a
    slow handshake it is still open well past `audio_window` seconds into the session. The only
    thing that can be said without measuring anything else is whether the media arrived before or
    after the moment the session stopped looking -- and that is enough, because it is what decides
    whether raising the window is the fix or a number changed for nothing.
    """
    if audio_window <= 0 or window_closed_at is None:
        return "the audio path is off (audio_window 0)"
    if first_at > window_closed_at:
        return f"it started after the {audio_window:g}s audio window closed"
    return f"it started before the {audio_window:g}s audio window closed"


def _audio_config(plan: _PayloadPlan) -> int:
    """The AudioSpecificConfig for a session: the family's, with what the camera said about it.

    Only the channel count is ever replaced. The sample rate is not in the payload at all -- an
    Access Unit carries no such field, and the RTP timestamp counts samples rather than naming a
    rate -- so the rate stays the family's 16 kHz and is cross-checked against the measured
    cadence where that cadence is reported.
    """
    if plan.audio_channels is None:
        return AAC_LC_16K_MONO
    return set_channels(AAC_LC_16K_MONO, plan.audio_channels)


def _payload_type_of(body: bytes) -> int | None:
    """The RTP payload type of a packet body, or None when it is not an RTP header."""
    if len(body) < _RTP_HEADER_BYTES or (body[0] & _RTP_VERSION_MASK) != _RTP_VERSION_2:
        return None
    return body[1] & 0x7F


def _carries_media(body: bytes) -> bool:
    """True when a video packet holds anything besides headers.

    A non-empty body is not enough to call it video. The CS-C8c opens a session with two
    payload-type-112 packets that are 64 and 44 bytes long and carry no media at all -- their
    header extension consumes the whole packet -- and taking those for video would disarm the
    no-video budget for a camera that then sent nothing else, which is the one case that budget
    exists for.
    """
    if not body:
        return False
    if len(body) >= _RTP_HEADER_BYTES and (body[0] & _RTP_VERSION_MASK) == _RTP_VERSION_2:
        try:
            return bool(rtp_payload(body))
        except PyEzvizError:
            # An RTP-shaped header the unwrap rejects is still something the camera sent.
            return True
    return True


def _describe_rtp_header(body: bytes) -> str:
    """Decode the fixed RTP header fields, or say that the bytes are not one.

    The twelve fixed bytes only, so the reading is the one the specification gives and not
    an interpretation: X, CC and P decide where the payload starts and how much of its tail is
    padding, which is the whole point of printing them -- with X set and a length of 12 words,
    the media begins at byte 64 and no 24-byte head could have shown it. The SSRC is here for
    the same reason: two payload types can share one synchronisation source and still number
    their packets separately, and which of the two this is decides how to read the rest of a
    session that carries more than one.
    """
    if len(body) < _RTP_HEADER_BYTES or (body[0] & _RTP_VERSION_MASK) != _RTP_VERSION_2:
        return "none"
    return (
        f"V{body[0] >> 6} P{(body[0] >> 5) & 1} X{(body[0] >> 4) & 1} CC{body[0] & 0x0F} "
        f"M{body[1] >> 7} PT{body[1] & 0x7F} "
        f"seq=0x{int.from_bytes(body[2:4], 'big'):04x} "
        f"ts=0x{int.from_bytes(body[4:8], 'big'):08x} "
        f"ssrc=0x{int.from_bytes(body[8:12], 'big'):08x}"
    )


@dataclass(frozen=True)
class _PayloadPlan:
    """What the leading video packets are, and how FFmpeg has to be started to read them.

    `demuxer` is the FFmpeg input format. For an RTP payload it is the codec itself: the
    session depacketizes to Annex-B, so there is no RTP left for FFmpeg to read. `codec` and
    `payload_type` are None unless that depacketization is needed; the payload type is the one
    the codec was identified from, so nothing else on the same RTP session is mistaken for it.

    `audio_payload_type` is filled in afterwards, by the audio window, and it is the one field
    that cannot come from the video prefix: the camera's audio is a second payload type that
    may not have started yet when the video codec is already known. When it is set, FFmpeg is
    started with a second input, and `packets` includes what was read while waiting for it.
    """

    transport: str
    demuxer: str
    codec: str | None
    payload_type: int | None
    packets: tuple[bytes, ...]
    audio_payload_type: int | None = None
    # The channel count the camera's own Access Units named, or None when none of the ones in
    # the prefix named one. None is not "no channels": it means the family's default is what
    # this session will use, and the report says so rather than letting the log imply a reading
    # that was never taken. The sample rate has no such field at all -- see `_audio_config`.
    audio_channels: int | None = None
    # The parameter sets an earlier session of this camera opened with, ahead of a join that
    # has none of its own. Empty on every session whose prefix carries them (the stream is
    # already open) and on every join against a cold cache: there is nothing to hand over.
    parameter_prelude: bytes = b""
    # An MPEG-PS pack and timestamped video PES carrying cached HEVC parameter sets.
    container_prelude: bytes = b""
