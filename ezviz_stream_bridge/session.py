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
from pyezvizapi.stream import VtmChannel

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
        on_event: Callable[[str, float], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._serial = serial
        self._ffmpeg_path = ffmpeg_path
        self._first_video_timeout = first_video_timeout
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

            self._reap_ffmpeg(ffmpeg)

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
        """Same remux as the library: MPEG-PS in, MPEG-TS out, no re-encoding."""
        try:
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    self._ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
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
                stderr=subprocess.DEVNULL,
            )
        except OSError as err:
            raise PyEzvizError(f"Could not launch FFmpeg at {self._ffmpeg_path!r}: {err}") from err

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
