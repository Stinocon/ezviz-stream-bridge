"""Runs one `pyezvizapi stream proxy` per camera and keeps them up.

Each proxy is a long-lived HTTP server, so under normal operation it never exits.
Every exit is therefore treated as a failure and retried with a growing delay --
capped, because the two common causes (the cloud refusing us, or a serial that does
not exist on the account) are not fixed by trying harder, and a tight restart loop
would fill the add-on log and keep waking the camera.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from types import FrameType

from .config import BridgeConfig, CameraConfig
from .token import MfaRequired, TokenStore

_LOGGER = logging.getLogger(__name__)

# Backoff between restarts of the same camera, in seconds.
FIRST_BACKOFF = 5.0
MAX_BACKOFF = 300.0

# A proxy that stayed up this long is considered healthy, so its next failure starts
# from the shortest delay again. Without this, one bad night would leave a camera on
# the five-minute delay for as long as the add-on runs.
HEALTHY_AFTER = 120.0

POLL_INTERVAL = 1.0
# How long a proxy gets to exit on its own after SIGTERM before it is killed. The
# proxy has an FFmpeg child to tear down, so this is not instant.
SHUTDOWN_GRACE = 10.0


@dataclass
class _Proxy:
    """State of one camera's proxy process."""

    camera: CameraConfig
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    failures: int = 0
    next_attempt_at: float = 0.0
    logged_wait: bool = field(default=False, repr=False)

    @property
    def running(self) -> bool:
        """True while the proxy process is alive."""
        return self.process is not None and self.process.poll() is None

    @property
    def backoff(self) -> float:
        """Delay before the next start attempt."""
        if self.failures <= 1:
            return FIRST_BACKOFF
        return min(FIRST_BACKOFF * 2 ** (self.failures - 1), MAX_BACKOFF)


class Supervisor:
    """Starts the proxies and supervises them until asked to stop."""

    def __init__(
        self,
        config: BridgeConfig,
        token_store: TokenStore,
        *,
        executable: str = "pyezvizapi",
        monotonic: object = time.monotonic,
    ) -> None:
        self._config = config
        self._tokens = token_store
        self._executable = executable
        self._now = monotonic  # injected so tests do not have to sleep
        self._proxies = [_Proxy(camera=camera) for camera in config.cameras]
        self._stopping = False
        self._fatal = False

    def run(self) -> int:
        """Supervise until a signal asks for shutdown. Returns a process exit code."""
        if shutil.which(self._executable) is None:
            _LOGGER.error(
                "%s is not on PATH: the image is built wrong.", self._executable
            )
            return 1

        self._install_signal_handlers()

        _LOGGER.info(
            "Serving %d camera(s). Other add-ons reach them at "
            "http://local-ezviz_stream_bridge:<port>/<serial>.ts",
            len(self._proxies),
        )
        for proxy in self._proxies:
            _LOGGER.info(
                "  %s -> port %d, path %s",
                proxy.camera.serial,
                proxy.camera.port,
                proxy.camera.path,
            )

        # Said once, at start-up, because it is the mistake that costs the user a
        # battery rather than a stream: every HTTP client opens a new cloud session
        # and wakes the camera, so a continuously-connected consumer never lets it sleep.
        _LOGGER.info(
            "These streams are on-demand. Leave Frigate's detect and record off for "
            "battery cameras: a permanent consumer keeps the camera awake and flattens it."
        )

        while not self._stopping:
            self._reconcile()
            time.sleep(POLL_INTERVAL)

        self._shutdown()
        return 1 if self._fatal else 0

    def _install_signal_handlers(self) -> None:
        """Ask for a clean shutdown on the signals s6 uses to stop the add-on."""
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._on_signal)

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Record the stop request; the work happens on the main loop."""
        _LOGGER.info("Signal %s received: stopping.", signal.Signals(signum).name)
        self._stopping = True

    def _reconcile(self) -> None:
        """Bring every camera back to "a proxy is running", one pass."""
        for proxy in self._proxies:
            if proxy.running:
                continue

            if proxy.process is not None:
                self._note_exit(proxy)

            now = self._clock()
            if now < proxy.next_attempt_at:
                if not proxy.logged_wait:
                    _LOGGER.info(
                        "Camera %s: next attempt in %.0fs.",
                        proxy.camera.serial,
                        proxy.next_attempt_at - now,
                    )
                    proxy.logged_wait = True
                continue

            self._start(proxy)

    def _note_exit(self, proxy: _Proxy) -> None:
        """Account for a proxy that has stopped, and schedule the retry."""
        assert proxy.process is not None
        code = proxy.process.returncode
        uptime = self._clock() - proxy.started_at

        if uptime >= HEALTHY_AFTER:
            proxy.failures = 0

        proxy.failures += 1
        proxy.process = None
        proxy.logged_wait = False
        proxy.next_attempt_at = self._clock() + proxy.backoff

        _LOGGER.warning(
            "Camera %s: proxy exited with code %s after %.0fs (failure %d). "
            "Retrying in %.0fs.",
            proxy.camera.serial,
            code,
            uptime,
            proxy.failures,
            proxy.backoff,
        )

    def _start(self, proxy: _Proxy) -> None:
        """Refresh the token if needed, then start this camera's proxy."""
        try:
            self._tokens.ensure()
        except MfaRequired as err:
            # Retrying cannot help: nothing in here can supply a verification code.
            # Stopping makes the add-on show as failed, which is the honest state and
            # the only one that gets the user to read the log.
            _LOGGER.error("%s", err)
            self._fatal = True
            self._stopping = True
            return
        except Exception as err:  # noqa: BLE001 - the reason is logged and retried
            proxy.failures += 1
            proxy.logged_wait = False
            proxy.next_attempt_at = self._clock() + proxy.backoff
            _LOGGER.error(
                "Camera %s: cannot get a working EZVIZ session (%s). Retrying in %.0fs.",
                proxy.camera.serial,
                err,
                proxy.backoff,
            )
            return

        command = [
            self._executable,
            "--token-file",
            str(self._tokens.path),
            "stream",
            "proxy",
            "--serial",
            proxy.camera.serial,
            # 0.0.0.0 because the consumer -- go2rtc in another container -- connects
            # from outside this one. Nothing is published to the LAN unless the user
            # maps the port in the add-on configuration.
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            str(proxy.camera.port),
        ]

        try:
            # stdout/stderr are inherited so the proxy's own messages land in the
            # add-on log, where the user is already looking, instead of being buffered
            # here and reprinted with a second timestamp.
            process = subprocess.Popen(command)  # noqa: S603 - fixed argv, no shell
        except OSError as err:
            proxy.failures += 1
            proxy.logged_wait = False
            proxy.next_attempt_at = self._clock() + proxy.backoff
            _LOGGER.error(
                "Camera %s: could not start the proxy (%s). Retrying in %.0fs.",
                proxy.camera.serial,
                err,
                proxy.backoff,
            )
            return

        proxy.process = process
        proxy.started_at = self._clock()
        proxy.logged_wait = False
        _LOGGER.info(
            "Camera %s: proxy up on port %d (pid %d).",
            proxy.camera.serial,
            proxy.camera.port,
            process.pid,
        )

    def _shutdown(self) -> None:
        """Stop every proxy, escalating to SIGKILL only if one will not go."""
        running = [proxy for proxy in self._proxies if proxy.running]
        if not running:
            return

        for proxy in running:
            assert proxy.process is not None
            proxy.process.terminate()

        deadline = self._clock() + SHUTDOWN_GRACE
        for proxy in running:
            assert proxy.process is not None
            remaining = max(0.0, deadline - self._clock())
            try:
                proxy.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _LOGGER.warning(
                    "Camera %s: proxy did not stop in time; killing it.",
                    proxy.camera.serial,
                )
                proxy.process.kill()

        _LOGGER.info("All proxies stopped.")

    def _clock(self) -> float:
        """Monotonic seconds, injectable for tests."""
        return float(self._now())  # type: ignore[operator]


def configure_logging(level: str) -> None:
    """Send logs to stdout, which is what the add-on log shows."""
    numeric = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "notice": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "fatal": logging.CRITICAL,
    }.get(level, logging.INFO)

    logging.basicConfig(
        stream=sys.stdout,
        level=numeric,
        format="[%(levelname)s] %(message)s",
    )
