"""Tests for the in-process proxy's connection bookkeeping.

The streaming itself needs a real camera and is exercised by hand; what is unit-tested
here is the logic that has edge cases: the per-connection id and the active-connection
counter that the logs report, which must stay correct under concurrent connect/close.
"""

from __future__ import annotations

import socket
import threading

import pytest

from ezviz_stream_bridge.proxy import PEER_POLL_INTERVAL, ProxyServer, watch_peer


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
