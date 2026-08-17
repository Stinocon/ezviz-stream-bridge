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
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlparse

from pyezvizapi.client import EzvizClient
from pyezvizapi.cloud_stream import copy_cloud_stream_to_mpegts
from pyezvizapi.exceptions import DeviceException, PyEzvizError

from .token import read_token_file

_LOGGER = logging.getLogger(__name__)


class ProxyServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the shared client and per-camera settings.

    Threaded because each consumer gets its own VTM session; daemon threads so a
    shutdown is not held up by a stream that will not end on its own.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        client: EzvizClient,
        serial: str,
        path: str,
        ffmpeg_path: str,
    ) -> None:
        self.client = client
        self.serial = serial
        self.path = path
        self.ffmpeg_path = ffmpeg_path
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

        reason = "stream ended"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            _LOGGER.info("[VTM]  conn=%d session opening", conn_id)
            # One VTM session for the life of this request. copy_cloud_stream_to_mpegts
            # opens it, remuxes VTM -> ffmpeg -> this socket, and closes it on return
            # or on any exception -- including the client going away, which surfaces as
            # a BrokenPipeError while writing below.
            copy_cloud_stream_to_mpegts(
                server.client,
                server.serial,
                cast(BinaryIO, self.wfile),
                ffmpeg_path=server.ffmpeg_path,
            )
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
            remaining = server.closed()
            _LOGGER.info(
                "[VTM]  conn=%d session closed",
                conn_id,
            )
            _LOGGER.info(
                "[HTTP] conn=%d closed after %.1fs reason=%r active=%d",
                conn_id,
                time.monotonic() - started,
                reason,
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Serve one camera's stream until terminated. Returns a process exit code."""
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="[%(levelname)s] %(message)s"
    )
    args = _parse_args(argv)
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
