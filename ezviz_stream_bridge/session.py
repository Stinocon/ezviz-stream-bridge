"""One VTM session per HTTP request, with a teardown that does not depend on data.

`pyezvizapi.cloud_stream.copy_cloud_stream_to_mpegts` does this job in a single call,
and 0.1.2 used it. The problem is that it owns both the FFmpeg process and the VTM
socket and exposes neither, so while the camera is asleep there is nothing to cancel:
both threads sit in blocking reads, the consumer that asked for the stream goes away
unnoticed -- the bridge only finds out it is gone when it next tries to write, and with
no video there is nothing to write -- and the cloud session outlives it. The keepalives
that hold that orphan session open are ours, not the cloud's: `iter_packets` sends one
every 5 seconds, so it never times out on its own.

The protocol still belongs to the library: handshake, framing, redirects and keepalives
all come from `open_cloud_stream` / `iter_packets`. What lives here is only the plumbing
that makes a session interruptible -- the socket comes from our own factory so it can be
shut down, and the FFmpeg process is ours so it can be terminated.

The rule that keeps the teardown deterministic: watchdogs only ever UNBLOCK (raise the
cancel flag, shut the socket down, terminate FFmpeg). The thread running `run()` is the
only one that joins, closes and reaps.
"""

from __future__ import annotations

import logging
import select
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, BinaryIO

from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.stream import VtmChannel, rtp_payload

from .rtp import RtpDepacketizer, detect_codec

_LOGGER = logging.getLogger(__name__)

# Channels that carry video. `iter_packets(include_control=True)` also yields control
# packets, and the caller has to filter them out exactly as the library does internally.
_STREAM_CHANNELS = (VtmChannel.STREAM, VtmChannel.ENCRYPTED_STREAM)

# Per-read timeout on the VTM socket, matching the library's own default.
VTM_SOCKET_TIMEOUT = 10.0

# Seconds to wait for the camera's first video packet before giving the session up.
# go2rtc's `exec` producer abandons a source that has not produced anything after 30s
# (hardcoded in v1.9.10), so staying under that guarantees the cloud session is released
# while our consumer is still there to notice, rather than being left behind.
DEFAULT_FIRST_VIDEO_TIMEOUT = 25.0

# FFmpeg -> consumer chunk size, matching the library's copy loop.
_READ_SIZE = 65536

# How long the consumer pump waits on FFmpeg's output before re-checking the cancel
# flag. Idle cost is two syscalls a second; while video flows, `select` returns at once.
_OUTPUT_POLL_INTERVAL = 0.5

# After the socket is shut down the writer returns in milliseconds; these are the
# "something is badly wrong" limits, not the expected wait.
_WRITER_JOIN_TIMEOUT = 5.0
_FFMPEG_REAP_TIMEOUT = 2.0

# FFmpeg exits with 0 when the input ends cleanly and with -15 when we terminate it;
# anything else is a real remux failure worth reporting.
_EXPECTED_FFMPEG_CODES = (0, -15)

# When the diagnostic capture is on, FFmpeg is run at `info` rather than `error`. The
# failure this exists to explain -- FFmpeg sitting in its input probe producing nothing --
# logs nothing at `error`, so capturing stderr without raising the level would change
# nothing. The bound is what keeps a remux that repeats the same complaint once per frame
# from turning into a second failure: an unbounded log.
_FFMPEG_LOG_LEVEL = "info"
_FFMPEG_STDERR_MAX_LINES = 20
_FFMPEG_STDERR_MAX_LINE = 500
_FFMPEG_STDERR_JOIN_TIMEOUT = 2.0

# Every session reads the leading video packets before it starts FFmpeg, because the demuxer
# depends on what they are: the same camera family sends either MPEG-PS or RTP, and FFmpeg has
# no single input format that reads both. The prefix is what the transport is classified from,
# and one packet's first byte cannot say -- a VTM packet may begin mid-pack, and a mid-stream
# byte that reads as RTP is exactly what 0.1.5's per-packet check kept reporting as a fault.
# It is bounded by the VTM framing itself: a 16-bit length, so under 64 KiB per packet.
_PREFIX_PACKETS = 8

# The transport line answers "what is this payload?" with the first 24 bytes. For a payload the
# remux cannot use, that is not enough to answer the next question -- where does the video
# actually start, and what codec is it? An RTP header is 12 bytes plus up to 60 bytes of CSRC
# list plus a variable-length extension, so the media can begin well past byte 24 and a 24-byte
# head says nothing about it. These bound the per-packet dump that answers it.
_PACKET_DUMP_HEAD = 64
_PACKET_DUMP_PAYLOAD = 64

# The demuxer FFmpeg reads MPEG-PS with. Anything else here is an elementary stream the
# session has already depacketized, and the name is the codec.
_MPEG_PS_DEMUXER = "mpeg"

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


def _forward(
    stdin: BinaryIO, depacketizer: RtpDepacketizer | None, body: bytes
) -> None:
    """Write one video packet to FFmpeg, depacketizing it when the payload is RTP."""
    if not body:
        return
    chunk = depacketizer.feed(body) if depacketizer is not None else body
    if chunk:
        stdin.write(chunk)
        stdin.flush()


def _describe_rtp_header(body: bytes) -> str:
    """Decode the fixed RTP header fields, or say that the bytes are not one.

    The twelve fixed bytes only, so the reading is the one the specification gives and not
    an interpretation: X, CC and P decide where the payload starts and how much of its tail is
    padding, which is the whole point of printing them -- with X set and a length of 12 words,
    the media begins at byte 64 and no 24-byte head could have shown it.
    """
    if len(body) < _RTP_HEADER_BYTES or (body[0] & _RTP_VERSION_MASK) != _RTP_VERSION_2:
        return "none"
    return (
        f"V{body[0] >> 6} P{(body[0] >> 5) & 1} X{(body[0] >> 4) & 1} CC{body[0] & 0x0F} "
        f"M{body[1] >> 7} PT{body[1] & 0x7F} "
        f"seq=0x{int.from_bytes(body[2:4], 'big'):04x} "
        f"ts=0x{int.from_bytes(body[4:8], 'big'):08x}"
    )


@dataclass(frozen=True)
class _PayloadPlan:
    """What the leading video packets are, and how FFmpeg has to be started to read them.

    `demuxer` is the FFmpeg input format. For an RTP payload it is the codec itself: the
    session depacketizes to Annex-B, so there is no RTP left for FFmpeg to read. `codec` and
    `payload_type` are None unless that depacketization is needed; the payload type is the one
    the codec was identified from, so nothing else on the same RTP session is mistaken for it.
    """

    transport: str
    demuxer: str
    codec: str | None
    payload_type: int | None
    packets: tuple[bytes, ...]


@dataclass
class SessionMetrics:
    """Timings for one session, in seconds since the session object was created."""

    opened_at: float | None = None  # VTM handshake completed
    first_video_at: float | None = None  # first video packet from the camera
    first_byte_at: float | None = None  # first byte handed to the consumer
    video_packets: int = 0
    bytes_out: int = 0


class CloudSession:
    """A cancellable VTM -> MPEG-TS session for exactly one consumer."""

    def __init__(  # noqa: PLR0913 - every one of these is injected by a test
        self,
        client: Any,
        serial: str,
        *,
        ffmpeg_path: str = "ffmpeg",
        first_video_timeout: float = DEFAULT_FIRST_VIDEO_TIMEOUT,
        ffmpeg_stderr: bool = False,
        connection_id: int | None = None,
        on_event: Callable[[str, float], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._serial = serial
        self._ffmpeg_path = ffmpeg_path
        self._first_video_timeout = first_video_timeout
        self._ffmpeg_stderr = ffmpeg_stderr
        self._connection_id = connection_id
        self._on_event = on_event
        self._now = monotonic
        self._started = monotonic()

        self.metrics = SessionMetrics()

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._abort_reason: str | None = None
        self._socket: socket.socket | None = None
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._writer_error: Exception | None = None

    @property
    def abort_reason(self) -> str | None:
        """Why the session was asked to stop, or None if it ended on its own."""
        with self._lock:
            return self._abort_reason

    def abort(self, reason: str) -> None:
        """Ask the session to stop. Thread-safe, idempotent and non-blocking.

        This only unblocks, three ways, none of which depends on data arriving: the
        cancel flag ends the consumer pump, shutting the socket down wakes the reader out
        of `recv`, and terminating FFmpeg closes the pipes. Everything that can block --
        join, close, wait -- belongs to `run()`'s teardown, so a watchdog thread can
        never deadlock against it.
        """
        with self._lock:
            if self._cancel.is_set():
                return
            self._abort_reason = reason
            self._cancel.set()
            sock = self._socket
            ffmpeg = self._ffmpeg

        self._shutdown(sock)
        if ffmpeg is not None and ffmpeg.poll() is None:
            with suppress(OSError):
                ffmpeg.terminate()

    def run(self, output: BinaryIO) -> None:
        """Stream the camera to `output` until it ends, fails, or `abort()` is called."""
        stream = open_cloud_stream(
            self._client,
            self._serial,
            timeout=VTM_SOCKET_TIMEOUT,
            socket_factory=self._make_socket,
        )

        ffmpeg: subprocess.Popen[bytes] | None = None
        writer: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        deadline: threading.Timer | None = None
        try:
            stream.start()
            self._record("opened")

            # The no-video budget is armed before the prefix is read, not after FFmpeg starts:
            # a camera that sends nothing at all has to be given up on while that read is in
            # progress, or the budget would begin counting only once it had already elapsed.
            if self._first_video_timeout > 0:
                deadline = threading.Timer(self._first_video_timeout, self._on_deadline)
                deadline.daemon = True
                deadline.start()

            # Before FFmpeg, because the leading packets decide its demuxer and no input
            # format reads both MPEG-PS and a raw elementary stream.
            plan, packets = self._read_prefix(stream)
            if self._cancel.is_set():
                # Aborted while the prefix was being read -- a consumer that left, or the
                # no-video budget. There is nothing to remux and no process to start.
                return

            ffmpeg, stderr_thread = self._launch_remux(plan)

            writer = threading.Thread(
                target=self._pump_vtm_to_ffmpeg,
                args=(packets, ffmpeg, plan),
                name=f"vtm-{self._serial}",
                daemon=True,
            )
            writer.start()

            self._pump_ffmpeg_to_consumer(ffmpeg, output)
        finally:
            if deadline is not None:
                deadline.cancel()

            # Unblock both ends first, whatever brought us here. Idempotent, so an
            # abort that already happened keeps its original reason.
            self.abort("finished")

            if writer is not None:
                writer.join(timeout=_WRITER_JOIN_TIMEOUT)
                if writer.is_alive():
                    # Should be unreachable: a shut-down socket cannot block a read.
                    _LOGGER.warning(
                        "VTM reader for %s did not stop; closing the socket anyway",
                        self._serial,
                    )

            # Only now. Closing the descriptor while another thread could still be
            # reading it risks that thread landing on a recycled fd.
            with suppress(Exception):
                stream.close()

            # Reap first: once FFmpeg is dead its stderr write end closes, the drain
            # thread sees EOF and exits on its own, and only then is it safe to close
            # the read end underneath it.
            self._reap_ffmpeg(ffmpeg)
            if stderr_thread is not None:
                stderr_thread.join(timeout=_FFMPEG_STDERR_JOIN_TIMEOUT)
            if ffmpeg is not None and ffmpeg.stderr is not None:
                with suppress(OSError):
                    ffmpeg.stderr.close()

        if self._writer_error is not None:
            raise self._writer_error

        # Only a session that ran to its natural end can say anything about FFmpeg's exit
        # code -- the teardown above aborts unconditionally, so the cancel flag is always
        # set by now and testing it here would silently skip every check. `finished` is
        # the reason the teardown itself uses, so it means nobody aborted before the end.
        if ffmpeg is not None and self.abort_reason == "finished":
            code = ffmpeg.returncode
            if code is not None and code not in _EXPECTED_FFMPEG_CODES:
                raise PyEzvizError(f"FFmpeg exited with status {code}")

    # -- internals ---------------------------------------------------------------

    def _make_socket(self, address: tuple[str, int], timeout: float | None) -> Any:
        """Create the VTM socket and keep a reference so `abort()` can shut it down.

        `socket_factory` is a documented parameter of `open_cloud_stream`, which is what
        makes cancellation possible without reaching into the client's private state. It
        is called again when the VTM redirects, so the reference always tracks the live
        socket.
        """
        sock = socket.create_connection(address, timeout)
        with self._lock:
            self._socket = sock
            cancelled = self._cancel.is_set()
        if cancelled:
            # Aborted while connecting: this socket was created after abort() read the
            # reference, so shut it down here or the session would keep it open.
            self._shutdown(sock)
        return sock

    @staticmethod
    def _shutdown(sock: socket.socket | None) -> None:
        """Wake anything blocked on `sock` without releasing the descriptor.

        SHUT_RDWR rather than close(): close() is not guaranteed to wake a thread that
        is already inside recv(), and it frees the descriptor number for reuse while
        that thread may still be holding it.
        """
        if sock is None:
            return
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)

    def _start_ffmpeg(self, demuxer: str) -> subprocess.Popen[bytes]:
        """MPEG-TS out of whatever the camera sent, with no re-encoding.

        Two input formats are possible. MPEG-PS is the case this was built for and FFmpeg
        demuxes it directly with `mpeg`. RTP is not, and no demuxer reads it out of a pipe --
        `rtp` wants a UDP URL and an SDP -- so by the time FFmpeg is started the session has
        depacketized to Annex-B and the demuxer names the elementary stream it is receiving,
        `h264` or `hevc`.

        `-use_wallclock_as_timestamps` is what makes that second case work at all: a raw
        elementary stream has no container to carry a timestamp, and without one the MPEG-TS
        muxer refuses the packet outright -- `first pts and dts value must be set`, zero bytes
        written, for H.264 and HEVC alike. Wallclock is the packet's arrival time, which for a
        live stream is the right reading anyway.

        Normally FFmpeg's stderr is discarded and its level kept at `error`. With the
        diagnostic on it is piped and the level raised to `info`, so the reason a remux
        produces nothing is visible instead of the process simply sitting there mute.
        """
        capturing = self._ffmpeg_stderr
        argv = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            _FFMPEG_LOG_LEVEL if capturing else "error",
        ]
        if demuxer != _MPEG_PS_DEMUXER:
            argv += ["-use_wallclock_as_timestamps", "1"]
        argv += ["-f", demuxer, "-i", "pipe:0", "-c", "copy", "-f", "mpegts", "pipe:1"]
        try:
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if capturing else subprocess.DEVNULL,
            )
        except OSError as err:
            raise PyEzvizError(f"Could not launch FFmpeg at {self._ffmpeg_path!r}: {err}") from err

    def _launch_remux(
        self, plan: _PayloadPlan
    ) -> tuple[subprocess.Popen[bytes], threading.Thread | None]:
        """Start FFmpeg for this payload, plus the reader that drains its stderr.

        Two details that have to happen here and not at the call site: the process is
        registered under the lock so a concurrent `abort()` can terminate it, and if the
        cancel flag was already set while FFmpeg was starting then `abort()` could not see
        the process at all, so it is stopped here instead of being left running.
        """
        ffmpeg = self._start_ffmpeg(plan.demuxer)
        with self._lock:
            self._ffmpeg = ffmpeg
            cancelled = self._cancel.is_set()
        if cancelled:
            with suppress(OSError):
                ffmpeg.terminate()

        if not (self._ffmpeg_stderr and ffmpeg.stderr is not None):
            return ffmpeg, None
        stderr_thread = threading.Thread(
            target=self._drain_ffmpeg_stderr,
            args=(ffmpeg.stderr,),
            name=f"ffmpeg-stderr-{self._serial}",
            daemon=True,
        )
        stderr_thread.start()
        return ffmpeg, stderr_thread

    def _label(self) -> str:
        """Prefix that ties an FFmpeg line back to its camera and connection."""
        if self._connection_id is None:
            return f"serial={self._serial} "
        return f"serial={self._serial} conn={self._connection_id} "

    def _drain_ffmpeg_stderr(self, stderr: BinaryIO) -> None:
        """Log FFmpeg's own diagnostics, bounded, so a silent remux is explainable.

        Reading continues past the bound so FFmpeg never blocks on a full stderr pipe,
        but logging stops: a broken demux can repeat one complaint once per frame, and
        an unbounded log would be a second failure on top of the first.
        """
        shown = 0
        suppressed = False
        try:
            for raw in iter(stderr.readline, b""):
                if shown < _FFMPEG_STDERR_MAX_LINES:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if len(line) > _FFMPEG_STDERR_MAX_LINE:
                        line = line[:_FFMPEG_STDERR_MAX_LINE] + "..."
                    _LOGGER.info("[FFmpeg] %s%s", self._label(), line)
                    shown += 1
                elif not suppressed:
                    _LOGGER.info(
                        "[FFmpeg] %s... further stderr suppressed (%d lines shown)",
                        self._label(),
                        shown,
                    )
                    suppressed = True
        except (OSError, ValueError):
            # The pipe was closed during teardown. That is the expected way out.
            return

    def _read_prefix(self, stream: Any) -> tuple[_PayloadPlan, Iterator[Any]]:
        """Read the leading video packets and decide how FFmpeg has to read them.

        Returns the plan and the packet iterator it stopped in the middle of, which the pump
        resumes. One pass over the stream and one iterator: a second `iter_packets` call would
        work against the socket, but it would also restart the keepalive clock and leave the
        invariant "the stream is read once" to whichever library version is installed.

        `include_control=True` for the same reason the pump uses it: while the camera sleeps
        the only traffic is control packets, and without surfacing them this read would sit
        inside the iterator, out of reach of the cancel flag.

        It stops at `_PREFIX_PACKETS` bodies, or sooner if the stream ends, the session is
        aborted, or the camera falls silent long enough for the VTM socket to time out. A
        short prefix is still classified: a session that ended is explained by what it sent.
        """
        packets: list[bytes] = []
        packets_iter = stream.iter_packets(include_control=True)
        try:
            for packet in packets_iter:
                if self._cancel.is_set():
                    break
                body = self._accept(packet)
                if body is None:
                    continue
                if body:
                    packets.append(body)
                if len(packets) >= _PREFIX_PACKETS:
                    break
        except Exception:
            # A shut-down socket surfaces here as a DeviceException or an OSError, which is
            # how abort() stops this read: an outcome, not a failure. With nothing aborted,
            # it is a real error and belongs to the caller.
            if not self._cancel.is_set():
                raise

        prefix = tuple(packets)
        if self._ffmpeg_stderr:
            # Before the decision, not after: a payload whose codec cannot be named is
            # precisely the one whose bytes somebody needs to see.
            self._report_prefix(prefix)
        if self._cancel.is_set():
            # Aborted mid-read -- a consumer that left, or the no-video budget. What arrived
            # is reported above, but there is no demuxer to choose: nobody is waiting for it,
            # and "the payload names no codec" would be a misleading way to say so.
            return _PayloadPlan("UNKNOWN", _MPEG_PS_DEMUXER, None, None, prefix), packets_iter
        return self._decide(prefix), packets_iter

    def _decide(self, packets: tuple[bytes, ...]) -> _PayloadPlan:
        """Name the transport, and with it the demuxer FFmpeg will be started with.

        MPEG-PS keeps the demuxer it has always had. RTP is depacketized here, so FFmpeg is
        told which elementary stream it is getting -- and which one that is comes from the
        parameter set the stream opens with, never from a guess about the framing.

        RTP with no parameter set in the leading packets stays on the `mpeg` demuxer, which is
        what the bridge did before this path existed: FFmpeg will produce nothing from it, but
        the warning says so plainly, and a session is not refused over a payload the bridge
        merely failed to recognise. The transport can be read wrong in one direction -- a first
        byte carrying RTP's version bits is not rare -- and refusing to serve a stream that
        would have worked is worse than serving one that produces nothing.
        """
        data = b"".join(packets)
        transport = classify_payload(data)[0] if data else "UNKNOWN"
        if transport != "RTP":
            return _PayloadPlan(transport, _MPEG_PS_DEMUXER, None, None, packets)

        codec = None
        payload_type = None
        for body in packets:
            codec = _codec_of(body)
            if codec is not None:
                payload_type = _payload_type_of(body)
                break
        if codec is None:
            _LOGGER.warning(
                "[FFmpeg] %spayload transport=RTP but none of the first %d video packets "
                "carries an H.264 or HEVC parameter set; leaving it to FFmpeg's `%s` demuxer, "
                "which cannot read an RTP stream",
                self._label(),
                len(packets),
                _MPEG_PS_DEMUXER,
            )
            return _PayloadPlan(transport, _MPEG_PS_DEMUXER, None, None, packets)
        return _PayloadPlan(transport, codec, codec, payload_type, packets)

    def _accept(self, packet: Any) -> bytes | None:
        """Check one VTM packet, and return its body when it is video.

        None means the packet was not on a video channel; an empty body means it was a video
        packet with nothing in it. Two things happen here rather than at each of the two call
        sites -- the prefix read and the pump -- because they must happen the same way in both:
        a video packet is counted once, and `first-video` is recorded once, on the first packet
        that actually carries media.
        """
        if packet.channel not in _STREAM_CHANNELS:
            return None
        if packet.encrypted:
            raise PyEzvizError(
                "Received an encrypted VTM stream packet; media decryption is not implemented"
            )
        self.metrics.video_packets += 1
        body = packet.body
        if self.metrics.first_video_at is None and body and _carries_media(body):
            self._record("first-video")
        return body

    def _report_prefix(self, packets: tuple[bytes, ...]) -> None:
        """Log what the leading packets are, and where the video starts in them.

        Diagnostic only -- the decision is made either way -- and for a payload the remux
        cannot use the packets are printed one by one. An RTP header is 12 bytes plus up to 60
        bytes of CSRC list plus a variable-length extension, so the media can begin well past
        what a 24-byte head shows; a few consecutive packets are what answers "where does the
        video start, and what codec is it".
        """
        data = b"".join(packets)
        transport, offset = classify_payload(data) if data else ("UNKNOWN", -1)
        _LOGGER.info(
            "[FFmpeg] %spayload transport=%s at offset=%d head=%s",
            self._label(),
            transport,
            offset,
            data[:24].hex(" "),
        )
        if transport == "MPEG_PS":
            # FFmpeg identifies a PS codec on its own, so the dump would only be noise.
            return
        for index, body in enumerate(packets):
            _LOGGER.info(
                "[FFmpeg] %spacket[%d] len=%d rtp=%s head=%s",
                self._label(),
                index,
                len(body),
                _describe_rtp_header(body),
                body[:_PACKET_DUMP_HEAD].hex(" "),
            )
            try:
                payload = rtp_payload(body)
            except PyEzvizError as err:
                _LOGGER.info(
                    "[FFmpeg] %spacket[%d] rtp payload: %s",
                    self._label(),
                    index,
                    err,
                )
                continue
            _LOGGER.info(
                "[FFmpeg] %spacket[%d] rtp payload+%d=%s",
                self._label(),
                index,
                len(body) - len(payload),
                payload[:_PACKET_DUMP_PAYLOAD].hex(" "),
            )

    def _pump_vtm_to_ffmpeg(
        self, packets: Iterator[Any], ffmpeg: subprocess.Popen[bytes], plan: _PayloadPlan
    ) -> None:
        """VTM packets -> FFmpeg stdin, until the stream ends or the session aborts.

        The leading packets were read before FFmpeg was started, because they are what chose
        its demuxer, so they go in first and the iterator then continues from the packet that
        read stopped at. An RTP payload goes through the depacketizer one packet at a time:
        what FFmpeg is reading is an elementary stream, and RTP is not one.
        """
        stdin = ffmpeg.stdin
        if stdin is None:  # pragma: no cover - Popen(stdin=PIPE) always provides one
            raise PyEzvizError("FFmpeg was started without a stdin pipe")
        depacketizer = (
            RtpDepacketizer(plan.codec, payload_type=plan.payload_type)
            if plan.codec is not None
            else None
        )
        try:
            for body in plan.packets:
                _forward(stdin, depacketizer, body)

            # include_control=True is what makes an idle session interruptible: while
            # the camera sleeps the only traffic is control packets, which the library
            # otherwise handles and swallows -- and then this loop would never come back
            # to check the cancel flag.
            for packet in packets:
                if self._cancel.is_set():
                    break
                body = self._accept(packet)
                if body is not None:
                    _forward(stdin, depacketizer, body)
        except (BrokenPipeError, ConnectionResetError):
            # FFmpeg is gone; the reader side reports why the session ended.
            return
        except Exception as err:  # noqa: BLE001 - handed to run() to raise in its thread
            # A shut-down socket surfaces here as an OSError or a DeviceException. That
            # is how abort() stops this loop, so it is an outcome, not a failure.
            if not self._cancel.is_set():
                self._writer_error = err
        finally:
            if depacketizer is not None:
                # A fragment whose end never arrived is media that did not make it out, and it
                # belongs in the counts below rather than in silence.
                depacketizer.flush()
            if depacketizer is not None and (depacketizer.dropped or depacketizer.skipped):
                # The difference between "the camera sent nothing" and "the camera sent
                # something this bridge misread" is the whole reason these counts exist.
                _LOGGER.warning(
                    "[FFmpeg] %sthe %s depacketizer dropped %d unreadable packet(s) and "
                    "skipped %d carrying another RTP payload type",
                    self._label(),
                    depacketizer.codec,
                    depacketizer.dropped,
                    depacketizer.skipped,
                )
            # EOF for FFmpeg, which is what ends a session that stopped on its own.
            with suppress(OSError):
                stdin.close()

    def _pump_ffmpeg_to_consumer(self, ffmpeg: subprocess.Popen[bytes], output: BinaryIO) -> None:
        """FFmpeg stdout -> the consumer, until EOF or the session is cancelled.

        Deliberately not "block on read until FFmpeg dies". A real FFmpeg that is still
        probing an input which never delivers a byte does not act on SIGTERM -- it is
        inside the read, not in its event loop -- so a session whose camera stayed mute
        would hang here even though everything else had been told to stop. Waiting with
        a timeout and checking the cancel flag makes the exit ours rather than FFmpeg's.

        `read1` rather than `read`: `read` only returns once it has filled the whole
        buffer, which would hold up to 64 KiB of a live stream back and put that delay
        into the `first-byte` measurement. `read1` returns what has arrived, and since it
        never leaves anything buffered behind, `select` stays authoritative.
        """
        stdout = ffmpeg.stdout
        if stdout is None:  # pragma: no cover - Popen(stdout=PIPE) always provides one
            raise PyEzvizError("FFmpeg was started without a stdout pipe")
        while True:
            if self._cancel.is_set():
                return
            try:
                ready, _, _ = select.select([stdout], [], [], _OUTPUT_POLL_INTERVAL)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            chunk = stdout.read1(_READ_SIZE)
            if not chunk:
                return
            if self.metrics.first_byte_at is None:
                self._record("first-byte")
            output.write(chunk)
            output.flush()
            self.metrics.bytes_out += len(chunk)

    def _on_deadline(self) -> None:
        """No video within the budget: let the camera go back to sleep.

        Keyed on the first VTM video packet rather than the first byte out, because
        FFmpeg needs a few frames before it emits anything and that delay is ours, not
        the camera's.
        """
        if self.metrics.first_video_at is None:
            self.abort("no video")

    def _record(self, event: str) -> None:
        """Timestamp a lifecycle event and report it while it is happening."""
        elapsed = self._now() - self._started
        if event == "opened":
            self.metrics.opened_at = elapsed
        elif event == "first-video":
            self.metrics.first_video_at = elapsed
        elif event == "first-byte":
            self.metrics.first_byte_at = elapsed
        if self._on_event is not None:
            self._on_event(event, elapsed)

    def _reap_ffmpeg(self, ffmpeg: subprocess.Popen[bytes] | None) -> None:
        """Make sure the remux process is gone before the session is considered over."""
        if ffmpeg is None:
            return
        if ffmpeg.poll() is None:
            with suppress(OSError):
                ffmpeg.terminate()
        try:
            ffmpeg.wait(timeout=_FFMPEG_REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            ffmpeg.kill()
            ffmpeg.wait()
        for pipe in (ffmpeg.stdin, ffmpeg.stdout):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()
