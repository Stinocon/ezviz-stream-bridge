"""In-process instrumented MPEG-TS proxy for a single camera.

This replaces the `pyezvizapi stream proxy` subprocess. The reason is not to
reimplement the protocol -- the VTM session and the remux still come from
`pyezvizapi.cloud_stream` -- but to own the HTTP layer, so every connection can be
logged with an id, its source address, its User-Agent, and the exact moment it
opens and closes. Without that, "who keeps requesting the stream?" is unanswerable,
and that question is the whole point of the on-demand design.

The lifecycle is on-demand by construction, and this module does not add any retry,
reconnect or keepalive: one GET opens one VTM session, and the session is closed the
instant the client goes away. No client, no VTM, no camera battery drain. Every GET
in the log is therefore an inbound request from a real consumer, never something the
bridge generated itself.

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
import select
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlparse

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import DeviceException, PyEzvizError

from .log import configure_logging, level_for
from .session import DEFAULT_FIRST_VIDEO_TIMEOUT, CloudSession
from .token import read_token_file

_LOGGER = logging.getLogger(__name__)

# How often the peer watchdog looks at the request socket. Half a second is far below
# any downstream timeout that matters and costs two syscalls per second per connection.
PEER_POLL_INTERVAL = 0.5


class ProxyServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the shared client and per-camera settings.

    Threaded because each consumer gets its own VTM session; daemon threads so a
    shutdown is not held up by a stream that will not end on its own.
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
    ) -> None:
        self.client = client
        self.serial = serial
        self.path = path
        self.ffmpeg_path = ffmpeg_path
        self.first_video_timeout = first_video_timeout
        self._ids = itertools.count(1)
        self._active = 0
        self._active_lock = threading.Lock()
        super().__init__(address, _ProxyHandler)

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
    "no video": "no video",
    "finished": "stream ended",
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
        _LOGGER.info("[HTTP] conn=%d first-byte after=%.3fs", conn_id, elapsed)


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "ezviz-stream-bridge/1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
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

        session = CloudSession(
            server.client,
            server.serial,
            ffmpeg_path=server.ffmpeg_path,
            first_video_timeout=server.first_video_timeout,
            on_event=lambda event, elapsed: _log_session_event(conn_id, event, elapsed),
        )
        stop_watching = threading.Event()
        watcher: threading.Thread | None = None

        reason = "stream ended"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            _LOGGER.info("[VTM]  conn=%d session opening", conn_id)
            # One VTM session for the life of this request, and one watchdog that can
            # end it the moment the consumer disappears -- including while the camera is
            # asleep and nothing is being written, which is precisely when a disconnect
            # used to go unnoticed and leave the cloud session behind.
            watcher = threading.Thread(
                target=watch_peer,
                args=(self.connection, stop_watching, lambda: session.abort("client gone")),
                name=f"peer-{conn_id}",
                daemon=True,
            )
            watcher.start()

            session.run(cast(BinaryIO, self.wfile))
            reason = _REASONS.get(session.abort_reason or "", reason)
        except (BrokenPipeError, ConnectionResetError):
            reason = "client disconnected"
        except DeviceException as err:
            # The camera did not deliver in time -- asleep, or waking slowly. Expected
            # for a battery doorbell; the consumer will retry with a fresh GET.
            reason = "camera timeout"
            _LOGGER.warning("[VTM]  conn=%d %s", conn_id, err)
        except PyEzvizError as err:
            reason = "error"
            _LOGGER.error("[VTM]  conn=%d %s", conn_id, err)
        finally:
            stop_watching.set()
            if watcher is not None:
                watcher.join(timeout=PEER_POLL_INTERVAL * 4)
            remaining = server.closed()
            _LOGGER.info(
                "[VTM]  conn=%d session closed",
                conn_id,
            )
            _LOGGER.info(
                "[HTTP] conn=%d closed after=%.3fs reason=%r bytes=%d video-packets=%d active=%d",
                conn_id,
                time.monotonic() - started,
                reason,
                session.metrics.bytes_out,
                session.metrics.video_packets,
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
        type=float,
        default=DEFAULT_FIRST_VIDEO_TIMEOUT,
        help=(
            "seconds to wait for the camera's first video packet before closing the "
            f"session (default: {DEFAULT_FIRST_VIDEO_TIMEOUT:g}, 0 disables it)"
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
