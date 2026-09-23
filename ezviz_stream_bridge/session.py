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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, BinaryIO

from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.stream import VtmChannel, rtp_payload

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

# When the diagnostic capture is on, the transport sniff buffers a bounded prefix of the
# video payload and looks for a real signature in it. It does not classify each packet's
# first byte: VTM packets are arbitrary chunks of the byte stream, so a packet can start
# mid-pack and hand the sniff a mid-stream byte that reads as RTP -- which is what the first
# version did, and it looked like a fault. The prefix is scanned once enough of it is in hand
# that a signature has to be present, and once more if the stream ends first.
_PAYLOAD_SNIFF_BYTES = 8192
_PAYLOAD_SNIFF_PACKETS = 8

# The transport line above answers "what is this payload?" with the first 24 bytes. For a
# payload the remux does not expect, that is not enough to answer the next question -- where
# does the video actually start, and what codec is it? An RTP header is 12 bytes plus up to 60
# bytes of CSRC list plus a variable-length extension, so the media can begin well past byte
# 24 and a 24-byte head says nothing about it. These bound a per-packet dump that does answer
# it: the first packets of the session, each with its header decoded and its payload sliced
# out at the offset pyezvizapi's own unwrap computes.
#
# The whole body is kept, not a leading slice of it: `rtp_payload` reads the padding count
# from the last byte of the packet, so a truncated copy would strip a byte count taken from
# the middle of the media and report an offset that is simply wrong. The bodies are bounded
# by the VTM framing itself -- a 16-bit length, so under 64 KiB each, eight of them at most.
_PACKET_DUMP_PACKETS = 8
_PACKET_DUMP_HEAD = 64
_PACKET_DUMP_PAYLOAD = 64

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
        self._sniff_buffer = bytearray()
        self._sniff_packets = 0
        self._sniffed = False
        self._dump_bodies: list[bytes] = []
        self._dumped = False
        self._transport: str | None = None
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

            ffmpeg = self._start_ffmpeg()
            with self._lock:
                self._ffmpeg = ffmpeg
                cancelled = self._cancel.is_set()
            if cancelled:
                # Aborted while FFmpeg was starting: abort() could not see this process,
                # so stop it here instead of leaving it running.
                with suppress(OSError):
                    ffmpeg.terminate()

            if self._ffmpeg_stderr and ffmpeg.stderr is not None:
                stderr_thread = threading.Thread(
                    target=self._drain_ffmpeg_stderr,
                    args=(ffmpeg.stderr,),
                    name=f"ffmpeg-stderr-{self._serial}",
                    daemon=True,
                )
                stderr_thread.start()

            if self._first_video_timeout > 0:
                deadline = threading.Timer(self._first_video_timeout, self._on_deadline)
                deadline.daemon = True
                deadline.start()

            writer = threading.Thread(
                target=self._pump_vtm_to_ffmpeg,
                args=(stream, ffmpeg),
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

    def _start_ffmpeg(self) -> subprocess.Popen[bytes]:
        """Same remux as the library: MPEG-PS in, MPEG-TS out, no re-encoding.

        Normally FFmpeg's stderr is discarded and its level kept at `error`. With the
        diagnostic on it is piped and the level raised to `info`, so the reason a remux
        produces nothing is visible instead of the process simply sitting there mute.
        """
        capturing = self._ffmpeg_stderr
        try:
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    self._ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    _FFMPEG_LOG_LEVEL if capturing else "error",
                    "-f",
                    "mpeg",
                    "-i",
                    "pipe:0",
                    "-c",
                    "copy",
                    "-f",
                    "mpegts",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if capturing else subprocess.DEVNULL,
            )
        except OSError as err:
            raise PyEzvizError(f"Could not launch FFmpeg at {self._ffmpeg_path!r}: {err}") from err

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

    def _sniff_payload(self, body: bytes) -> None:
        """Buffer the leading payload and classify its transport once.

        Deferred until enough of the prefix is in hand that a signature has to be present,
        which is what makes the answer independent of where the VTM packet boundaries fell.
        """
        if not self._ffmpeg_stderr or not body:
            return
        if (
            not self._dumped
            and self._transport != "MPEG_PS"
            and len(self._dump_bodies) < _PACKET_DUMP_PACKETS
        ):
            self._dump_bodies.append(body)
        if self._sniffed:
            # The verdict is already out; the dump is complete the moment the last of the
            # leading packets is in hand, and until then there is nothing more to say.
            self._flush_dump(complete_only=True)
            return
        self._sniff_buffer.extend(body)
        self._sniff_packets += 1
        if (
            len(self._sniff_buffer) < _PAYLOAD_SNIFF_BYTES
            and self._sniff_packets < _PAYLOAD_SNIFF_PACKETS
        ):
            return
        self._report_transport()

    def _flush_sniff(self) -> None:
        """Report whatever prefix was buffered when the stream ended mid-sniff.

        The dump here is not held back for a full set of packets: a session that ended after
        three of them is still explained by those three.
        """
        if not self._ffmpeg_stderr:
            return
        if not self._sniffed and self._sniff_buffer:
            self._report_transport()
        self._flush_dump()

    def _report_transport(self) -> None:
        """Classify the buffered prefix and log it once, with where its signature sits."""
        data = bytes(self._sniff_buffer)
        if not data:
            # Nothing to classify yet (only empty bodies so far); keep waiting rather than
            # logging an empty UNKNOWN and locking the sniff.
            return
        self._sniffed = True
        transport, offset = classify_payload(data)
        self._transport = transport
        _LOGGER.info(
            "[FFmpeg] %spayload transport=%s at offset=%d head=%s",
            self._label(),
            transport,
            offset,
            data[:24].hex(" "),
        )
        self._flush_dump(complete_only=True)

    def _flush_dump(self, *, complete_only: bool = False) -> None:
        """Log the leading packets individually, for a payload the remux cannot use.

        Only when the transport is not MPEG-PS: a PS stream needs none of this, and FFmpeg
        identifies its codec on its own, so the noise would buy nothing. For anything else
        the open question is exactly where the video starts and what codec it is, and both
        are answered by a few consecutive packets, each with its header fields, its leading
        bytes, and the payload sliced out at the offset pyezvizapi's `rtp_payload` computes.
        That the offset is computed by the library's function and not here is the point: it
        is the same unwrap a fix would use, so the log shows what it would produce.

        `complete_only` is what keeps "the first eight packets" from meaning "however many had
        arrived when the verdict did": the byte threshold can fire on the third packet, and a
        dump taken then would answer the question with the least evidence available. The
        verdict itself is never held back -- only the packet dump is.
        """
        if self._dumped or self._transport is None or not self._dump_bodies:
            return
        if self._transport == "MPEG_PS":
            # Nothing to show, and no reason to hold eight packet bodies for a session.
            self._dump_bodies.clear()
            return
        if complete_only and len(self._dump_bodies) < _PACKET_DUMP_PACKETS:
            return
        self._dumped = True
        for index, body in enumerate(self._dump_bodies):
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

    def _pump_vtm_to_ffmpeg(self, stream: Any, ffmpeg: subprocess.Popen[bytes]) -> None:
        """VTM packets -> FFmpeg stdin, until the stream ends or the session aborts."""
        stdin = ffmpeg.stdin
        if stdin is None:  # pragma: no cover - Popen(stdin=PIPE) always provides one
            raise PyEzvizError("FFmpeg was started without a stdin pipe")
        try:
            # include_control=True is what makes an idle session interruptible: while
            # the camera sleeps the only traffic is control packets, which the library
            # otherwise handles and swallows -- and then this loop would never come back
            # to check the cancel flag.
            for packet in stream.iter_packets(include_control=True):
                if self._cancel.is_set():
                    break
                if packet.channel not in _STREAM_CHANNELS:
                    continue
                if packet.encrypted:
                    raise PyEzvizError(
                        "Received an encrypted VTM stream packet; "
                        "media decryption is not implemented"
                    )
                if self.metrics.first_video_at is None:
                    self._record("first-video")
                self._sniff_payload(packet.body)
                self.metrics.video_packets += 1
                if packet.body:
                    stdin.write(packet.body)
                    stdin.flush()
        except (BrokenPipeError, ConnectionResetError):
            # FFmpeg is gone; the reader side reports why the session ended.
            return
        except Exception as err:  # noqa: BLE001 - handed to run() to raise in its thread
            # A shut-down socket surfaces here as an OSError or a DeviceException. That
            # is how abort() stops this loop, so it is an outcome, not a failure.
            if not self._cancel.is_set():
                self._writer_error = err
        finally:
            # Report the prefix even if the stream ended before the sniff threshold, so a
            # short or stalled session still says what its payload looked like.
            self._flush_sniff()
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
