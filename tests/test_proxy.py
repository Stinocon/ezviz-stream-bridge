"""Tests for the in-process proxy's connection bookkeeping.

The streaming itself needs a real camera and is exercised by hand; what is unit-tested
here is the logic that has edge cases: the per-connection id and the active-connection
counter that the logs report, which must stay correct under concurrent connect/close.
"""

from __future__ import annotations

import threading

import pytest

from ezviz_stream_bridge.proxy import ProxyServer


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
