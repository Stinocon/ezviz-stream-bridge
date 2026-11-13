"""In-process instrumented MPEG-TS proxy for a single camera.

This replaces the `pyezvizapi stream proxy` subprocess. The reason is not to
reimplement the protocol -- the VTM session and the remux still come from
`pyezvizapi.cloud_stream` -- but to own the HTTP layer, so every connection can be
logged with an id, its source address, its User-Agent, and the exact moment it
opens and closes. Without that, "who keeps requesting the stream?" is unanswerable,
and that question is the whole point of the on-demand design.

The lifecycle is on-demand by construction: concurrent GETs for one camera share a VTM
session and remux, and the session closes as soon as the last client goes away. No client,
no VTM, no camera battery drain. Every GET in the log is therefore an inbound request
from a real consumer, never something the bridge generated itself.

The one deliberate exception is the post-timeout cooldown: when a session ends because
the camera went offline mid-stream, the next wake-up is delayed so a reconnect loop in
the consumer cannot keep a battery camera permanently awake. That is a pause between
requests, never a request the bridge originates.

"the instant the client goes away" is the part that needed work, and is why this module
now watches the request socket for a peer close instead of finding out on the next
write. A consumer that disconnects while the camera is asleep produces no write at all,
so before 0.1.3 the session simply stayed open, holding the camera's cloud session with
it. See `session.py`.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import select
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import DeviceException, PyEzvizError

from .hub import StreamHub, Subscription
from .log import configure_logging, level_for
from .session import DEFAULT_AUDIO_WINDOW, DEFAULT_FIRST_VIDEO_TIMEOUT, CloudSession
from .token import SESSION_REJECTED_EXIT, read_token_file, session_rejected

_LOGGER = logging.getLogger(__name__)

# How often the peer watchdog looks at the request socket. Half a second is far below
# any downstream timeout that matters and costs two syscalls per second per connection.
PEER_POLL_INTERVAL = 0.5

# How long a new VTM session is withheld after a "camera timeout". A timeout means the
# camera went offline mid-stream and is back to sleep; opening a session the instant the
# consumer reconnects would wake it again seconds after it just fell asleep -- the loop
# that flattens a battery camera. 0 disables the cooldown.
TIMEOUT_COOLDOWN = 30.0
CLIENT_WRITE_TIMEOUT = 5.0

# How long a proxy that is stepping aside for a new login waits for its other streams to tear
# down cleanly before it exits anyway. Their FFmpeg and VTM socket die with the process either
# way; waiting is only so that each one logs its own close line instead of vanishing.
RESTART_DRAIN = 5.0


class ProxyServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the shared client and per-camera settings.

    Threaded so each HTTP consumer can read independently from the per-camera hub; daemon
    threads keep shutdown from being held up by a consumer that will not leave on its own.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(  # noqa: PLR0913 - one per camera setting, all named at the call site
        self,
        address: tuple[str, int],
        *,
        client: EzvizClient,
        serial: str,
        path: str,
        ffmpeg_path: str,
        first_video_timeout: float = DEFAULT_FIRST_VIDEO_TIMEOUT,
        timeout_cooldown: float = TIMEOUT_COOLDOWN,
        max_stream_duration: float = 0,
        ffmpeg_stderr: bool = False,
        audio_window: float = DEFAULT_AUDIO_WINDOW,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.serial = serial
        self.path = path
        self.ffmpeg_path = ffmpeg_path
        self.first_video_timeout = first_video_timeout
        self.timeout_cooldown = timeout_cooldown
        self.ffmpeg_stderr = ffmpeg_stderr
        self.audio_window = audio_window
        self._now = monotonic
        self._sleep = sleep
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._active = 0
        self._active_lock = threading.Lock()
        self._sessions: set[CloudSession] = set()
        self._restart_requested = False
        super().__init__(address, _ProxyHandler)
        self.hub = StreamHub(
            make_session=self._make_session,
            before_start=self.wait_for_cooldown,
            on_track=self.track,
            on_untrack=self.untrack,
            on_camera_timeout=lambda conn_id, err: _on_camera_timeout(self, conn_id, err),
            on_cloud_error=lambda conn_id, err: _on_cloud_error(self, conn_id, err),
            max_duration=max_stream_duration,
        )

    def _make_session(self, conn_id: int) -> CloudSession:
        return CloudSession(
            self.client,
            self.serial,
            ffmpeg_path=self.ffmpeg_path,
            first_video_timeout=self.first_video_timeout,
            audio_window=self.audio_window,
            ffmpeg_stderr=self.ffmpeg_stderr,
            connection_id=conn_id,
            on_event=lambda event, elapsed: _log_session_event(conn_id, event, elapsed),
        )

    def next_id(self) -> int:
        """A monotonically increasing connection id for correlating log lines."""
        return next(self._ids)

    def opened(self) -> int:
        """Record a new active connection and return the new active count."""
        with self._active_lock:
            self._active += 1
            return self._active

    def closed(self) -> int:
        """Record a finished connection and return the remaining active count."""
        with self._active_lock:
            self._active -= 1
            return self._active

    def track(self, session: CloudSession) -> None:
        """Remember a session in flight, so a restart can end it cleanly."""
        with self._active_lock:
            self._sessions.add(session)

    def untrack(self, session: CloudSession) -> None:
        """Forget a session that has finished."""
        with self._active_lock:
            self._sessions.discard(session)

    @property
    def restart_requested(self) -> bool:
        """True once a handler has found that the cloud no longer accepts the session."""
        with self._active_lock:
            return self._restart_requested

    def request_restart(self) -> None:
        """Stop serving, so the supervisor can renew the session and start this proxy again.

        Called from a handler thread. `shutdown()` blocks until `serve_forever()` has
        returned, which happens on the main thread, so it is handed to a thread of its own:
        the handler that asked must still be free to finish and log its close line.
        Idempotent, because every stream that fails after the session expired asks again.
        """
        with self._active_lock:
            if self._restart_requested:
                return
            self._restart_requested = True
        threading.Thread(target=self.shutdown, name="proxy-restart", daemon=True).start()

    def end_sessions(self, reason: str) -> None:
        """Abort every session still in flight. Non-blocking: each one tears itself down."""
        with self._active_lock:
            sessions = list(self._sessions)
        for session in sessions:
            session.abort(reason)

    def wait_idle(self, timeout: float) -> bool:
        """Wait, at most `timeout` seconds, for every connection to close. True if they did."""
        deadline = self._now() + timeout
        while True:
            with self._active_lock:
                if self._active <= 0:
                    return True
            if self._now() >= deadline:
                return False
            self._sleep(min(PEER_POLL_INTERVAL, max(0.0, deadline - self._now())))

    def start_cooldown(self) -> None:
        """Begin the post-timeout cooldown, during which no VTM session may open.

        A "camera timeout" means the camera went offline mid-stream and is back to
        sleep. Serving the very next GET immediately would wake it again seconds after
        it just fell asleep, which is the loop that flattens a battery camera. The
        cooldown forces a pause between that failure and the next wake-up. No-op when
        `timeout_cooldown` is 0.
        """
        if self.timeout_cooldown <= 0:
            return
        with self._cooldown_lock:
            self._cooldown_until = self._now() + self.timeout_cooldown

    def wait_for_cooldown(self, session: CloudSession) -> None:
        """Block until the cooldown has elapsed, or the consumer has gone away.

        Called on the handler thread before a VTM session is opened. If the consumer
        disconnects while we wait, the peer watchdog aborts `session` and this returns
        immediately, so a dead connection never turns into a wake-up.
        """
        while session.abort_reason is None:
            with self._cooldown_lock:
                remaining = self._cooldown_until - self._now()
            if remaining <= 0:
                return
            self._sleep(min(remaining, PEER_POLL_INTERVAL))


def watch_peer(sock: socket.socket, stop: threading.Event, on_gone: Callable[[], None]) -> None:
    """Call `on_gone` when the consumer closes its end, without writing to it.

    This is the detector the bridge was missing: an HTTP client that goes away while no
    video is flowing produces no error anywhere, because nothing is ever written to it.
    A readable socket with nothing to read is EOF, which is exactly what a closed peer
    looks like -- and MSG_PEEK leaves the byte in place for a client that did send
    something, which no stream consumer does.
    """
    while not stop.wait(PEER_POLL_INTERVAL):
        try:
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            on_gone()
            return
        if not readable:
            continue
        try:
            if sock.recv(1, socket.MSG_PEEK) == b"":
                on_gone()
                return
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            on_gone()
            return
        # The client sent something unexpected. Not our business, and the poll interval
        # keeps this from becoming a spin.


# How a session's abort reason reads in the connection log.
_REASONS = {
    "client gone": "client disconnected",
    "slow consumer": "slow consumer",
    "no video": "no video",
    "finished": "stream ended",
    "session expired": "session expired",
    "max duration": "maximum stream duration reached",
}


def _log_session_event(conn_id: int, event: str, elapsed: float) -> None:
    """Log a session milestone as it happens.

    As it happens, not at close: the value of these lines is their own timestamp, which
    is what lines the bridge up against Frigate's, go2rtc's and Home Assistant's logs.
    `first-video` is the camera starting to send; `first-byte` is the consumer starting
    to receive, and the gap between them is FFmpeg's probe, not the camera's fault.
    """
    if event == "opened":
        _LOGGER.info("[VTM]  conn=%d session opened after=%.3fs", conn_id, elapsed)
    elif event == "first-video":
        _LOGGER.info("[VTM]  conn=%d first-video after=%.3fs", conn_id, elapsed)
    elif event == "first-byte":
        _LOGGER.info("[VTM]  conn=%d first-remux-byte after=%.3fs", conn_id, elapsed)


def _on_camera_timeout(server: ProxyServer, conn_id: int, err: DeviceException) -> str:
    """Log a camera that did not deliver in time, arm the cooldown, and name the close reason.

    Asleep, or waking slowly: expected for a battery doorbell, and the consumer will retry with
    a fresh GET -- which is what the cooldown holds back, so the camera is not woken again the
    instant it has fallen asleep.
    """
    server.start_cooldown()
    _LOGGER.warning("[VTM]  conn=%d %s", conn_id, err)
    if server.timeout_cooldown > 0:
        _LOGGER.info(
            "[VTM]  conn=%d camera went offline mid-stream; next wake delayed %.0fs",
            conn_id,
            server.timeout_cooldown,
        )
    return "camera timeout"


def _on_cloud_error(server: ProxyServer, conn_id: int, err: PyEzvizError) -> str:
    """Log a session that failed in the cloud, and return the reason its close line gives.

    One failure is different from the rest: the cloud no longer accepting the session. Every
    stream after it would fail the same way, and the fix is a login this process cannot do --
    it was deliberately given the token and not the password. So it steps aside, and the
    supervisor renews the session and starts it again.
    """
    if not session_rejected(err):
        _LOGGER.error("[VTM]  conn=%d %s", conn_id, err)
        return "error"
    _LOGGER.error(
        "[VTM]  conn=%d the EZVIZ cloud no longer accepts the session (%s); "
        "restarting this camera's proxy so the supervisor can log in again",
        conn_id,
        err,
    )
    server.request_restart()
    return "session expired"


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "ezviz-stream-bridge/1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802, PLR0912, PLR0915 - one complete HTTP lifecycle
        server = cast(ProxyServer, self.server)
        if urlparse(self.path).path != server.path:
            self.send_error(404, "Stream not found")
            return

        conn_id = server.next_id()
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        user_agent = self.headers.get("User-Agent", "-")
        active = server.opened()
        started = time.monotonic()
        _LOGGER.info(
            "[HTTP] conn=%d connected from=%s ua=%s active=%d",
            conn_id,
            peer,
            user_agent,
            active,
        )

        if self.headers and _LOGGER.isEnabledFor(logging.DEBUG):
            for name, value in self.headers.items():
                _LOGGER.debug("[HTTP] conn=%d header %s: %s", conn_id, name, value)

        subscription: Subscription | None = None
        stop_watching = threading.Event()
        watcher: threading.Thread | None = None

        reason = "stream ended"
        first_byte_logged = False
        try:
            self.connection.settimeout(CLIENT_WRITE_TIMEOUT)
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            subscription = server.hub.subscribe(conn_id)

            # One upstream serves every concurrent consumer. A disconnect removes only its
            # subscription; the last consumer leaving aborts the upstream session.
            watcher = threading.Thread(
                target=watch_peer,
                args=(
                    self.connection,
                    stop_watching,
                    lambda: (
                        server.hub.unsubscribe(subscription) if subscription is not None else None
                    ),
                ),
                name=f"peer-{conn_id}",
                daemon=True,
            )
            watcher.start()
            _LOGGER.info("[HTTP] conn=%d subscribed to shared stream", conn_id)
            while True:
                chunk = subscription.read()
                if chunk is None:
                    terminal = subscription.reason or "stream ended"
                    reason = _REASONS.get(terminal, terminal)
                    break
                if not chunk:
                    continue
                if not first_byte_logged:
                    _LOGGER.info(
                        "[HTTP] conn=%d first-byte after=%.3fs",
                        conn_id,
                        time.monotonic() - started,
                    )
                    first_byte_logged = True
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            reason = "client disconnected"
        except DeviceException as err:
            reason = _on_camera_timeout(server, conn_id, err)
        except PyEzvizError as err:
            reason = _on_cloud_error(server, conn_id, err)
        except Exception:
            reason = "error"
            _LOGGER.exception("[HTTP] conn=%d request handler failed", conn_id)
        finally:
            if subscription is not None:
                server.hub.unsubscribe(subscription)
            stop_watching.set()
            if watcher is not None:
                watcher.join(timeout=PEER_POLL_INTERVAL * 4)
            remaining = server.closed()
            _LOGGER.info("[HTTP] conn=%d subscription closed", conn_id)
            _LOGGER.info(
                "[HTTP] conn=%d closed after=%.3fs reason=%r bytes=%d video-packets=%d active=%d",
                conn_id,
                time.monotonic() - started,
                reason,
                subscription.bytes_out if subscription is not None else 0,
                subscription.session.metrics.video_packets if subscription is not None else 0,
                remaining,
            )

    def log_message(self, format: str, *args: Any) -> None:
        # Silence BaseHTTPRequestHandler's default per-request line: this handler emits
        # its own richer connect/close lines above, and the default one would duplicate
        # them without the id, the source address or the User-Agent.
        return


def _load_client(token_file: Path, region: str) -> EzvizClient:
    """Build an EZVIZ client from the token the supervisor already validated."""
    token = read_token_file(token_file)
    if token is None:
        raise PyEzvizError(
            f"No usable EZVIZ token in {token_file}. The supervisor is expected to "
            "establish one before starting the proxy."
        )
    return EzvizClient(token=token, url=region)


def _finite_seconds(text: str) -> float:
    """A duration argument that is a finite, non-negative number of seconds.

    `float()` accepts `nan` and `inf`, and every comparison against them is then False -- so an
    `--audio-window nan` would disarm the deadline it was meant to set, which is the one thing
    that number exists to bound. The add-on options go through `BridgeConfig._as_seconds`, which
    refuses both; a value typed on the command line has to meet the same rule or the two doors
    disagree about what a duration is.
    """
    try:
        value = float(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from err
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number of seconds")
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ezviz-stream-bridge-proxy",
        description="Instrumented MPEG-TS proxy for one EZVIZ camera.",
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - see supervisor note
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--path", default=None, help="default: /<serial>.ts")
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--first-video-timeout",
        type=_finite_seconds,
        default=DEFAULT_FIRST_VIDEO_TIMEOUT,
        help=(
            "seconds to wait for the camera's first video packet before closing the "
            f"session (default: {DEFAULT_FIRST_VIDEO_TIMEOUT:g}, 0 disables it)"
        ),
    )
    parser.add_argument(
        "--timeout-cooldown",
        type=_finite_seconds,
        default=TIMEOUT_COOLDOWN,
        help=(
            "seconds to wait after a camera timeout before opening a new VTM session "
            f"(default: {TIMEOUT_COOLDOWN:g}, 0 disables it)"
        ),
    )
    parser.add_argument(
        "--max-stream-duration",
        type=_finite_seconds,
        default=0,
        help="maximum seconds one shared camera stream stays open (default: 0, disabled)",
    )
    parser.add_argument(
        "--audio-window",
        type=_finite_seconds,
        default=DEFAULT_AUDIO_WINDOW,
        help=(
            "seconds to keep reading the leading packets for the camera's audio before "
            "starting FFmpeg, which is what lets a stream carry its sound; 0 serves video "
            f"only (default: {DEFAULT_AUDIO_WINDOW:g})"
        ),
    )
    parser.add_argument(
        "--log-ffmpeg-stderr",
        action="store_true",
        help=(
            "capture FFmpeg's stderr at `info` level and log it, bounded, instead of "
            "discarding it; for diagnosing a remux that produces no output"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Serve one camera's stream until terminated. Returns a process exit code."""
    args = _parse_args(argv)
    configure_logging(level_for(args.log_level))
    path = args.path or f"/{args.serial}.ts"

    try:
        client = _load_client(args.token_file, args.region)
    except PyEzvizError as err:
        _LOGGER.error("%s", err)
        return 1

    try:
        server = ProxyServer(
            (args.host, args.port),
            client=client,
            serial=args.serial,
            path=path,
            ffmpeg_path=args.ffmpeg_path,
            first_video_timeout=args.first_video_timeout,
            timeout_cooldown=args.timeout_cooldown,
            max_stream_duration=args.max_stream_duration,
            audio_window=args.audio_window,
            ffmpeg_stderr=args.log_ffmpeg_stderr,
        )
    except OSError as err:
        _LOGGER.error("Could not bind proxy to %s:%d: %s", args.host, args.port, err)
        return 1

    _LOGGER.info(
        "Proxy for %s listening on %s:%d%s (on-demand: no client, no stream)",
        args.serial,
        args.host,
        args.port,
        path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()

    if not server.restart_requested:
        return 0

    # The listening socket is closed, so no new stream can start on the dead session. The ones
    # already running are ended here rather than by the exit, so each logs how it closed.
    server.end_sessions("session expired")
    if not server.wait_idle(RESTART_DRAIN):
        _LOGGER.warning(
            "Proxy for %s: streams still open after %.0fs; exiting anyway.",
            args.serial,
            RESTART_DRAIN,
        )
    return SESSION_REJECTED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
