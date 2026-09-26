"""Tests for the in-process proxy's connection bookkeeping.

The streaming itself needs a real camera and is exercised by hand; what is unit-tested
here is the logic that has edge cases: the per-connection id and the active-connection
counter that the logs report, which must stay correct under concurrent connect/close.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from ezviz_stream_bridge.proxy import PEER_POLL_INTERVAL, ProxyServer, _parse_args, watch_peer
from ezviz_stream_bridge.session import CloudSession


@pytest.fixture
def server() -> ProxyServer:
    # Bind to an ephemeral port; the server is never asked to serve, only to account
    # for connections. The client is a placeholder: no request is dispatched here.
    srv = ProxyServer(
        ("127.0.0.1", 0),
        client=object(),
        serial="BB1234567",
        path="/BB1234567.ts",
        ffmpeg_path="ffmpeg",
    )
    yield srv
    srv.server_close()


def test_connection_ids_are_monotonic(server: ProxyServer) -> None:
    assert [server.next_id() for _ in range(4)] == [1, 2, 3, 4]


def test_active_count_tracks_open_and_close(server: ProxyServer) -> None:
    assert server.opened() == 1
    assert server.opened() == 2
    assert server.closed() == 1
    assert server.opened() == 2
    assert server.closed() == 1
    assert server.closed() == 0


def test_counters_are_consistent_under_concurrency(server: ProxyServer) -> None:
    # daemon_threads means many handlers touch these counters at once; the lock has to
    # hold or the active count drifts and the "active=N" log lines become fiction.
    def churn() -> None:
        for _ in range(1000):
            server.opened()
            server.next_id()
            server.closed()

    threads = [threading.Thread(target=churn) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert server.opened() == 1
    assert server.closed() == 0
    # 8 threads * 1000 ids consumed 1..8000; the next id is 8001. opened()/closed()
    # do not touch the id counter.
    assert server.next_id() == 8 * 1000 + 1


def test_watch_peer_fires_when_the_consumer_hangs_up() -> None:
    """The detector that replaces "find out on the next write".

    A stream consumer never sends anything after its GET, so a readable socket with
    nothing on it is the consumer having closed -- which is the only signal available
    while the camera is asleep and there is nothing to write to it.
    """
    near, far = socket.socketpair()
    stop = threading.Event()
    gone = threading.Event()
    watcher = threading.Thread(target=watch_peer, args=(near, stop, gone.set), daemon=True)
    watcher.start()
    try:
        assert not gone.wait(PEER_POLL_INTERVAL * 2), "fired while the peer was still there"
        far.close()
        assert gone.wait(PEER_POLL_INTERVAL * 6), "did not notice the peer closing"
    finally:
        stop.set()
        watcher.join(timeout=2.0)
        near.close()


def test_watch_peer_ignores_a_client_that_sends_something() -> None:
    """Unexpected inbound bytes are not a disconnect, and must not become a spin."""
    near, far = socket.socketpair()
    stop = threading.Event()
    gone = threading.Event()
    watcher = threading.Thread(target=watch_peer, args=(near, stop, gone.set), daemon=True)
    watcher.start()
    try:
        far.send(b"unexpected")
        assert not gone.wait(PEER_POLL_INTERVAL * 3)
    finally:
        stop.set()
        watcher.join(timeout=2.0)
        near.close()
        far.close()


class FakeClock:
    """A monotonic clock and sleep that advance together, so cooldown waits stay fast."""

    def __init__(self) -> None:
        self.value = 1000.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _cooldown_server(timeout_cooldown: float) -> tuple[ProxyServer, FakeClock]:
    clock = FakeClock()
    server = ProxyServer(
        ("127.0.0.1", 0),
        client=object(),
        serial="BB1234567",
        path="/BB1234567.ts",
        ffmpeg_path="ffmpeg",
        timeout_cooldown=timeout_cooldown,
        monotonic=clock.now,
        sleep=clock.sleep,
    )
    return server, clock


def test_start_cooldown_sets_a_deadline() -> None:
    """A camera timeout arms the cooldown; a new wake-up is delayed by exactly it."""
    server, clock = _cooldown_server(30.0)
    server.start_cooldown()
    assert server._cooldown_until == clock.now() + 30.0
    server.server_close()


def test_start_cooldown_disabled_when_configured_to_zero() -> None:
    """`timeout_cooldown=0` must keep today's behaviour: no delay at all."""
    server, clock = _cooldown_server(0.0)
    server.start_cooldown()
    assert server._cooldown_until == 0.0
    server.server_close()


def test_wait_for_cooldown_returns_when_there_is_nothing_to_wait() -> None:
    """With no cooldown armed, the handler proceeds straight to opening the VTM."""
    server, clock = _cooldown_server(30.0)
    session = CloudSession(object(), "BB1234567")
    before = clock.now()
    server.wait_for_cooldown(session)
    assert clock.now() == before
    server.server_close()


def test_wait_for_cooldown_blocks_until_the_cooldown_elapses() -> None:
    """After a timeout the next session is withheld for the whole cooldown."""
    server, clock = _cooldown_server(30.0)
    server.start_cooldown()
    session = CloudSession(object(), "BB1234567")
    before = clock.now()
    server.wait_for_cooldown(session)
    assert clock.now() >= before + 30.0
    server.server_close()


def test_wait_for_cooldown_returns_when_the_consumer_has_gone() -> None:
    """A consumer that leaves during the cooldown must not be made to wait for it."""
    server, clock = _cooldown_server(30.0)
    server.start_cooldown()
    session = CloudSession(object(), "BB1234567")
    session.abort("client gone")
    before = clock.now()
    server.wait_for_cooldown(session)
    assert clock.now() == before  # returned immediately, no sleep
    server.server_close()


DURATION_FLAGS = ("--first-video-timeout", "--timeout-cooldown", "--audio-window")


def _duration_args(tmp_path: Path, flag: str, value: str) -> list[str]:
    return [
        "--serial",
        "BB1234567",
        "--port",
        "8558",
        "--token-file",
        str(tmp_path / "ezviz_token.json"),
        "--region",
        "apiieu.ezvizlife.com",
        flag,
        value,
    ]


@pytest.mark.parametrize("flag", DURATION_FLAGS)
@pytest.mark.parametrize("value", ["nan", "inf", "-1", "2s"])
def test_a_duration_the_bridge_cannot_use_is_refused_at_the_command_line(
    tmp_path: Path, flag: str, value: str
) -> None:
    """All three durations, not only the one the audio feature added.

    They are the same defect at the same three doors: `float()` accepts `nan` and `inf`, and every
    comparison against them is false afterwards, so a no-video budget set to `nan` never fires and
    a cooldown set to `inf` never ends. A fix with no test is a claim, which is how the two older
    flags kept the defect for a release after the third one lost it.
    """
    with pytest.raises(SystemExit):
        _parse_args(_duration_args(tmp_path, flag, value))


@pytest.mark.parametrize("flag", DURATION_FLAGS)
@pytest.mark.parametrize("value", ["0", "1.5", "0.25"])
def test_a_usable_duration_survives_the_command_line(
    tmp_path: Path, flag: str, value: str
) -> None:
    args = _parse_args(_duration_args(tmp_path, flag, value))

    # `0` is not a mistake at any of the three: it is how each one is disabled.
    assert getattr(args, flag.lstrip("-").replace("-", "_")) == float(value)


