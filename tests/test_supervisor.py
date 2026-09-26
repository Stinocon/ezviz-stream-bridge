"""Tests for the restart policy.

The supervisor's value is entirely in what it does when a proxy dies, so that is what
is pinned here: the delay grows, it is capped, and a proxy that ran long enough gets a
clean slate. None of these tests sleep -- the clock is injected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ezviz_stream_bridge import supervisor as supervisor_module
from ezviz_stream_bridge.config import BridgeConfig, CameraConfig
from ezviz_stream_bridge.supervisor import (
    FIRST_BACKOFF,
    HEALTHY_AFTER,
    MAX_BACKOFF,
    Supervisor,
    _Proxy,
)


def _proxy() -> _Proxy:
    return _Proxy(camera=CameraConfig(serial="BB1234567", port=8558))


def test_first_failure_uses_the_short_delay() -> None:
    proxy = _proxy()
    proxy.failures = 1

    assert proxy.backoff == FIRST_BACKOFF


def test_delay_doubles_with_consecutive_failures() -> None:
    proxy = _proxy()

    delays = []
    for failures in range(1, 6):
        proxy.failures = failures
        delays.append(proxy.backoff)

    assert delays == [
        FIRST_BACKOFF,
        FIRST_BACKOFF * 2,
        FIRST_BACKOFF * 4,
        FIRST_BACKOFF * 8,
        FIRST_BACKOFF * 16,
    ]


def test_delay_is_capped() -> None:
    proxy = _proxy()
    # A serial that does not exist on the account fails forever. Without the cap this
    # would grow into days, and the add-on would look hung rather than broken.
    proxy.failures = 50

    assert proxy.backoff == MAX_BACKOFF


def test_a_proxy_with_no_process_is_not_running() -> None:
    assert _proxy().running is False


def test_healthy_threshold_is_longer_than_the_first_delay() -> None:
    # If it were not, a proxy that failed immediately could still be counted healthy
    # and the backoff would never grow.
    assert HEALTHY_AFTER > FIRST_BACKOFF


class _FakeTokens:
    """The supervisor only reads `.path` and calls `.ensure()`."""

    path = Path("ezviz_token.json")

    def ensure(self) -> None:
        return None


class _FakeProcess:
    pid = 4321

    def poll(self) -> int | None:
        return None


def _supervisor(
    *, log_ffmpeg_stderr: bool = False, audio_window: float | None = None
) -> Supervisor:
    options: dict[str, object] = {
        "username": "user@example.com",
        "password": "secret",
        "region": "apiieu.ezvizlife.com",
        "cameras": [{"serial": "BB1234567", "port": 8558}],
        "log_ffmpeg_stderr": log_ffmpeg_stderr,
    }
    if audio_window is not None:
        options["audio_window"] = audio_window
    config = BridgeConfig.from_options(options)
    return Supervisor(config, _FakeTokens())  # type: ignore[arg-type]


@pytest.mark.parametrize("enabled", [False, True])
def test_ffmpeg_diagnostic_flag_reaches_the_proxy_command(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_popen(command: list[str]) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    supervisor = _supervisor(log_ffmpeg_stderr=enabled)
    supervisor._start(supervisor._proxies[0])

    assert ("--log-ffmpeg-stderr" in captured["command"]) is enabled


def test_the_audio_window_reaches_the_proxy_only_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy holds the default, so an add-on that passes nothing must pass nothing: a flag
    on every command line would be a number in two places, and the one in the add-on would
    silently win."""
    captured: dict[str, list[str]] = {}

    def fake_popen(command: list[str]) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    supervisor = _supervisor()
    supervisor._start(supervisor._proxies[0])
    assert "--audio-window" not in captured["command"]

    passed = _supervisor(audio_window=1.5)
    passed._start(passed._proxies[0])
    command = captured["command"]
    assert command[command.index("--audio-window") + 1] == "1.5"
