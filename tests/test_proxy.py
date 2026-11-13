"""Tests for the in-process proxy's connection bookkeeping.

The streaming itself needs a real camera and is exercised by hand; what is unit-tested
here is the logic that has edge cases: the per-connection id and the active-connection
counter that the logs report, which must stay correct under concurrent connect/close.
"""

from __future__ import annotations

import http.client
import logging
import socket
import threading
import time
from pathlib import Path

import pytest
from pyezvizapi.exceptions import EzvizAuthTokenExpired, PyEzvizError

from ezviz_stream_bridge import proxy as proxy_module
from ezviz_stream_bridge.proxy import PEER_POLL_INTERVAL, ProxyServer, _parse_args, watch_peer
from ezviz_stream_bridge.session import CloudSession, SessionMetrics
from ezviz_stream_bridge.token import SESSION_REJECTED_EXIT


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


DURATION_FLAGS = (
    "--first-video-timeout",
    "--timeout-cooldown",
    "--max-stream-duration",
    "--audio-window",
)


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
    """Every duration option refuses non-finite and negative values.

    `float()` accepts `nan` and `inf`, and every
    comparison against them is false afterwards, so a no-video budget set to `nan` never fires and
    a cooldown set to `inf` never ends.
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




# -- stepping aside when the cloud stops accepting the session ---------------------------------
#
# The proxy holds the token and not the password, so once the cloud refuses to renew the
# session, every stream it opens fails the same way until somebody logs in again -- and only
# the supervisor can. Before 0.1.18 the proxy kept serving those failures until the add-on was
# restarted by hand; now it stops, and says why with its exit code.


class _RejectedSession:
    """A session whose stream fails the way an expired login does."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.metrics = SessionMetrics()
        self.abort_reason: str | None = None

    def abort(self, reason: str) -> None:
        self.abort_reason = self.abort_reason or reason

    def run(self, _output: object) -> None:
        raise EzvizAuthTokenExpired("the refresh session has expired")


def _get(port: int, path: str = "/BB1234567.ts") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
        chunks = []
        while chunk := conn.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict, threading.Thread]:
    """Run the proxy's `main` on an ephemeral port; returns its result box and thread."""
    created: list[ProxyServer] = []

    class Recording(ProxyServer):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(proxy_module, "ProxyServer", Recording)
    monkeypatch.setattr(proxy_module, "_load_client", lambda *_args: object())
    # `main` sets logging up with `force=True`, which would take the capture handler with it.
    monkeypatch.setattr(proxy_module, "configure_logging", lambda _level: None)
    result: dict = {}
    argv = _duration_args(tmp_path, "--timeout-cooldown", "0")
    argv[argv.index("--port") + 1] = "0"
    thread = threading.Thread(target=lambda: result.update(code=proxy_module.main(argv)))
    thread.start()
    for _ in range(200):
        if created:
            break
        time.sleep(0.01)
    result["server"] = created[0]
    return result, thread


def test_a_rejected_session_stops_the_proxy_with_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(proxy_module, "CloudSession", _RejectedSession)
    result, thread = _serve(monkeypatch, tmp_path)

    _get(result["server"].server_address[1])
    thread.join(timeout=10)

    assert not thread.is_alive(), "the proxy kept serving on a session the cloud refused"
    assert result["code"] == SESSION_REJECTED_EXIT
    messages = [record.getMessage() for record in caplog.records]
    assert any("no longer accepts the session" in line for line in messages)
    assert any("reason='session expired'" in line for line in messages)


def test_a_restart_ends_the_streams_still_running_and_logs_each_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    sessions: list[_RejectedSession] = []
    producer_started = threading.Event()
    reject_session = threading.Event()

    def next_session(*args: object, **kwargs: object) -> _RejectedSession:
        class DelayedRejected(_RejectedSession):
            def run(self, _output: object) -> None:
                producer_started.set()
                reject_session.wait(5)
                raise EzvizAuthTokenExpired("the refresh session has expired")

        sessions.append(DelayedRejected(*args, **kwargs))
        return sessions[-1]

    monkeypatch.setattr(proxy_module, "CloudSession", next_session)
    result, thread = _serve(monkeypatch, tmp_path)
    port = result["server"].server_address[1]

    first = threading.Thread(target=_get, args=(port,), daemon=True)
    second = threading.Thread(target=_get, args=(port,), daemon=True)
    first.start()
    assert producer_started.wait(5)
    second.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with result["server"].hub._condition:
            if len(result["server"].hub._subscribers) == 2:
                break
        time.sleep(0.01)
    else:
        reject_session.set()
        pytest.fail("the second HTTP consumer did not join the shared upstream")
    reject_session.set()
    thread.join(timeout=10)
    first.join(timeout=5)
    second.join(timeout=5)

    assert result["code"] == SESSION_REJECTED_EXIT
    assert len(sessions) == 1
    closes = [r.getMessage() for r in caplog.records if "subscription closed" in r.getMessage()]
    assert len(closes) == 2, closes


def test_concurrent_http_consumers_share_one_cloud_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions: list[object] = []

    class StreamingSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.metrics = SessionMetrics()
            self.abort_reason: str | None = None
            self.stopped = threading.Event()
            sessions.append(self)

        def abort(self, reason: str) -> None:
            self.abort_reason = self.abort_reason or reason
            self.stopped.set()

        def run(self, output: object) -> None:
            self.stopped.wait(PEER_POLL_INTERVAL * 1.5)
            while not self.stopped.is_set():
                output.write(b"x" * 188)  # type: ignore[attr-defined]
                self.metrics.video_packets += 1
                self.stopped.wait(0.01)

    monkeypatch.setattr(proxy_module, "CloudSession", StreamingSession)
    result, thread = _serve(monkeypatch, tmp_path)
    port = result["server"].server_address[1]
    first = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    second = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    first.request("GET", "/BB1234567.ts")
    first_response = first.getresponse()
    assert first_response.status == 200
    assert len(first_response.read(188)) == 188

    second.request("GET", "/BB1234567.ts")
    second_response = second.getresponse()
    assert second_response.status == 200
    assert len(second_response.read(188)) == 188
    assert len(sessions) == 1

    first.close()
    first_response.close()
    time.sleep(PEER_POLL_INTERVAL * 2)
    assert sessions[0].abort_reason is None  # type: ignore[attr-defined]
    assert len(second_response.read(188)) == 188

    second.close()
    second_response.close()
    assert sessions[0].stopped.wait(2)  # type: ignore[attr-defined]
    result["server"].shutdown()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_an_ordinary_cloud_error_does_not_stop_the_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Failing(_RejectedSession):
        def run(self, _output: object) -> None:
            raise PyEzvizError("Could not find VTM server for resource 1")

    monkeypatch.setattr(proxy_module, "CloudSession", Failing)
    result, thread = _serve(monkeypatch, tmp_path)
    server = result["server"]

    _get(server.server_address[1])
    thread.join(timeout=1)

    try:
        assert thread.is_alive(), "a cloud error that says nothing about the session stopped it"
        assert not server.restart_requested
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert result["code"] == 0


def test_a_restart_is_requested_once_however_many_streams_ask(server: ProxyServer) -> None:
    calls = []
    server.shutdown = lambda: calls.append(1)  # type: ignore[method-assign]

    for _ in range(5):
        server.request_restart()
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)

    assert server.restart_requested
    assert calls == [1]
