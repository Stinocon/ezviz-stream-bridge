from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from ezviz_stream_bridge.hub import StreamHub, Subscription


class _FakeSession:
    def __init__(self, *_args: object) -> None:
        self.abort_reason: str | None = None
        self.metrics = SimpleNamespace(video_packets=0)
        self.stopped = threading.Event()
        self.finished = threading.Event()

    def abort(self, reason: str) -> None:
        self.abort_reason = self.abort_reason or reason
        self.stopped.set()

    def run(self, output: object) -> None:
        while not self.stopped.is_set():
            output.write(b"transport-stream-chunk")  # type: ignore[attr-defined]
            self.metrics.video_packets += 1
            self.stopped.wait(0.005)
        self.finished.set()


def _hub(*, max_duration: float = 0) -> tuple[StreamHub, list[_FakeSession]]:
    sessions: list[_FakeSession] = []

    def make_session(_connection_id: int) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    return (
        StreamHub(
            make_session=make_session,  # type: ignore[arg-type]
            before_start=lambda _session: None,
            on_track=lambda _session: None,
            on_untrack=lambda _session: None,
            on_camera_timeout=lambda _conn_id, _err: "camera timeout",
            on_cloud_error=lambda _conn_id, _err: "error",
            max_duration=max_duration,
        ),
        sessions,
    )


def _wait_for_data(subscription: Subscription) -> bytes:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        chunk = subscription.read(timeout=0.05)
        if chunk:
            return chunk
    raise AssertionError("the shared producer did not publish data")


def test_concurrent_consumers_share_one_upstream_and_late_join_gets_replay() -> None:
    hub, sessions = _hub()
    first = hub.subscribe(1)
    first_chunk = _wait_for_data(first)
    second = hub.subscribe(2)

    try:
        assert sessions == [first.session]
        replayed = second.read(timeout=0.1)
        assert replayed is not None
        assert first_chunk in replayed

        hub.unsubscribe(first)
        assert first.session.abort_reason is None
        assert _wait_for_data(second)
    finally:
        hub.unsubscribe(second)

    assert first.session.finished.wait(2)
    assert first.session.abort_reason == "client gone"


def test_last_consumer_leaving_aborts_upstream_and_next_join_starts_a_new_one() -> None:
    hub, sessions = _hub()
    first = hub.subscribe(1)
    _wait_for_data(first)
    hub.unsubscribe(first)
    assert first.session.finished.wait(2)

    second = hub.subscribe(2)
    try:
        assert len(sessions) == 2
        assert second.session is not first.session
    finally:
        hub.unsubscribe(second)
    assert second.session.finished.wait(2)


def test_max_stream_duration_closes_every_consumer_and_is_measured_from_start() -> None:
    hub, _sessions = _hub(max_duration=0.05)
    subscription = hub.subscribe(1)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        chunk = subscription.read(timeout=0.05)
        if chunk is None:
            break
    assert subscription.reason == "max duration"


def test_slow_consumer_is_closed_when_its_bounded_buffer_fills() -> None:
    session = _FakeSession()
    subscription = Subscription(session)  # type: ignore[arg-type]

    assert not subscription._push(b"x" * (1024 * 1024 + 1))
    assert subscription.read(timeout=0) is None
    assert subscription.reason == "slow consumer"


def test_a_temporary_gap_in_output_is_not_treated_as_end_of_stream() -> None:
    subscription = Subscription(_FakeSession())  # type: ignore[arg-type]

    assert subscription.read(timeout=0) == b""
    assert subscription.reason is None
    subscription._finish("finished")
    assert subscription.read(timeout=0) is None
