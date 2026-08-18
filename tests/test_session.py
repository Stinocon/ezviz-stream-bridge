"""Tests for the session lifecycle -- above all, that it can always be torn down.

The bug these exist for: while the camera sends nothing, both ends of the session sit
in blocking reads. Nothing is written to the consumer, so a consumer that goes away is
invisible, and the cloud session outlives it indefinitely (the keepalives holding it
open are the bridge's own). Every test here therefore runs the real machinery -- real
threads, a real socket pair, a real child process -- because the failure mode is
entirely about what blocks and what unblocks. A mock would prove nothing.

The stand-in for FFmpeg is `cat`: same pipes, same EOF semantics, and it copies stdin
to stdout so the forwarding path can be checked without a camera.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.stream import VtmChannel

from ezviz_stream_bridge import session as session_module
from ezviz_stream_bridge.session import CloudSession

# Generous: the teardown paths under test are meant to take milliseconds. These bounds
# only need to fail loudly if something blocks, which before 0.1.3 it did forever.
TEARDOWN_LIMIT = 5.0
SETTLE = 2.0


@dataclass
class FakePacket:
    """The parts of a VtmPacket this code depends on."""

    channel: int
    body: bytes = b""

    @property
    def encrypted(self) -> bool:
        return self.channel in (VtmChannel.ENCRYPTED_MESSAGE, VtmChannel.ENCRYPTED_STREAM)


def video(body: bytes) -> FakePacket:
    return FakePacket(channel=VtmChannel.STREAM, body=body)


def control(body: bytes = b"keepalive") -> FakePacket:
    return FakePacket(channel=VtmChannel.MESSAGE, body=body)


class FakeVtmStream:
    """A VTM client whose silence is a real blocking read on a real socket.

    After the scripted packets are exhausted it does what the real client does when the
    camera has nothing to send: block in `recv`. That is the state the session has to be
    able to escape, and it can only be escaped by shutting the socket down.
    """

    def __init__(self, sock: socket.socket, packets: list[FakePacket], *, silent_after: bool):
        self._sock = sock
        self._packets = packets
        self._silent_after = silent_after
        self.started = False
        self.closed = False
        self.iterating = threading.Event()

    def start(self) -> None:
        self.started = True

    def iter_packets(self, *, include_control: bool = False, **_: Any):
        self.iterating.set()
        for packet in self._packets:
            if include_control or packet.channel in (
                VtmChannel.STREAM,
                VtmChannel.ENCRYPTED_STREAM,
            ):
                yield packet
        if not self._silent_after:
            return
        while True:
            data = self._sock.recv(4096)
            if not data:  # shutdown or peer close
                return
            if include_control:
                yield control(data)

    def close(self) -> None:
        self.closed = True
        self._sock.close()


class ChattyVtmStream(FakeVtmStream):
    """A VTM that never stops talking, and never sends video.

    The real client's `iter_packets` swallows control traffic, so a session whose camera
    is asleep but whose connection is busy would never hand control back. This is what
    `include_control=True` is for: `stopped` only gets set if the reader is given the
    chance to see the cancel flag.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, [], silent_after=False)
        self.stopped = threading.Event()

    def iter_packets(self, *, include_control: bool = False, **_: Any):
        self.iterating.set()
        try:
            while True:
                time.sleep(0.01)
                if include_control:
                    yield control()
        finally:
            self.stopped.set()


class WedgedVtmStream(FakeVtmStream):
    """A VTM reader that no socket shutdown can wake.

    Stands in for anything that leaves the reader stuck where the session cannot reach
    it -- and, in production, for an FFmpeg that will not act on SIGTERM while it is
    probing an input that never delivers. The session still has to end.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, [], silent_after=False)
        self.release = threading.Event()

    def iter_packets(self, *, include_control: bool = False, **_: Any):
        self.iterating.set()
        self.release.wait(30)
        return
        yield  # pragma: no cover - makes this a generator


class Sink:
    """Stands in for the HTTP response body."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        return None


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> str:
    """A stand-in for the remux: copies stdin to stdout and ignores the arguments."""
    script = tmp_path / "fake-ffmpeg"
    script.write_text("#!/bin/sh\nexec cat\n")
    script.chmod(0o755)
    return str(script)


class StubbornFfmpeg:
    """A remux that ignores SIGTERM, which is what the real one does.

    Found by running the real thing: an FFmpeg still probing an input that never
    delivers a byte sits inside the read, not in its event loop, so it does not act on
    the terminate. A session that waited for its EOF would hang in exactly the case this
    release exists to fix.

    `wait_until_stubborn` exists because the trap is not installed until the shell gets
    to it: without it a test can terminate the process during that first millisecond and
    quietly prove nothing.
    """

    def __init__(self, path: Path, ready: Path) -> None:
        self.path = str(path)
        self._ready = ready

    def wait_until_stubborn(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.exists():
                return True
            time.sleep(0.01)
        return False


@pytest.fixture
def stubborn_ffmpeg(tmp_path: Path) -> StubbornFfmpeg:
    script = tmp_path / "stubborn-ffmpeg"
    ready = tmp_path / "trap-installed"
    script.write_text(f"#!/bin/sh\ntrap '' TERM\n: > '{ready}'\nwhile :; do sleep 1; done\n")
    script.chmod(0o755)
    return StubbornFfmpeg(script, ready)


@pytest.fixture
def vtm(monkeypatch: pytest.MonkeyPatch):
    """Wire `open_cloud_stream` to a FakeVtmStream backed by a real socket pair.

    The returned helper builds the stream the session will use; the far end of the pair
    stays with the test, so it can feed traffic or hang up like a VTM server would.
    """
    made: dict[str, Any] = {}

    def build(
        packets: list[FakePacket],
        *,
        silent_after: bool = True,
        stream_class: type[FakeVtmStream] | None = None,
    ) -> dict[str, Any]:
        near, far = socket.socketpair()
        made["far"] = far

        def fake_create_connection(address: Any, timeout: Any = None) -> socket.socket:
            return near

        def fake_open_cloud_stream(client, serial, *, timeout, socket_factory):
            sock = socket_factory(("vtm.invalid", 8666), timeout)
            if stream_class is not None:
                stream: FakeVtmStream = stream_class(sock)
            else:
                stream = FakeVtmStream(sock, packets, silent_after=silent_after)
            made["stream"] = stream
            return stream

        monkeypatch.setattr(session_module.socket, "create_connection", fake_create_connection)
        monkeypatch.setattr(session_module, "open_cloud_stream", fake_open_cloud_stream)
        return made

    yield build

    far = made.get("far")
    if far is not None:
        far.close()


def run_in_thread(
    session: CloudSession, sink: Sink
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            session.run(sink)
        except BaseException as err:  # noqa: BLE001 - re-raised by the assertions
            errors.append(err)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def test_abort_tears_down_while_the_camera_is_silent(vtm, fake_ffmpeg) -> None:
    """The 0.1.2 bug: a consumer disappearing with no video flowing left the VTM open.

    Nothing has been written to the consumer, so the only thing that can end the session
    is the abort path itself.
    """
    made = vtm([])
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)

    assert made["stream"].iterating.wait(SETTLE), "session never reached the read loop"

    started = time.monotonic()
    session.abort("client gone")
    thread.join(timeout=TEARDOWN_LIMIT)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "session did not tear down after abort()"
    assert elapsed < TEARDOWN_LIMIT
    assert errors == []
    assert session.abort_reason == "client gone"
    assert made["stream"].closed, "the VTM session was left open"
    assert bytes(sink.data) == b""


def test_no_video_within_the_timeout_ends_the_session(vtm, fake_ffmpeg) -> None:
    """The safety net: a camera that never wakes must not hold a cloud session."""
    made = vtm([])
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0.3)
    sink = Sink()
    started = time.monotonic()
    thread, errors = run_in_thread(session, sink)

    thread.join(timeout=TEARDOWN_LIMIT)
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert errors == []
    assert session.abort_reason == "no video"
    assert elapsed >= 0.3, "closed before the timeout it was given"
    assert made["stream"].closed


def test_keepalives_do_not_hold_a_silent_session_open(vtm, fake_ffmpeg) -> None:
    """Control traffic must not look like video to the deadline.

    This is the shape of the original failure: the session stayed alive because the
    keepalives kept the socket busy. They are wake-ups, not video.
    """
    made = vtm([])
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0.5)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    assert made["stream"].iterating.wait(SETTLE)

    stop = threading.Event()

    def keepalives() -> None:
        while not stop.wait(0.05):
            try:
                made["far"].send(b"keepalive")
            except OSError:
                return

    chatter = threading.Thread(target=keepalives, daemon=True)
    chatter.start()
    try:
        thread.join(timeout=TEARDOWN_LIMIT)
    finally:
        stop.set()
        chatter.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    assert session.abort_reason == "no video"
    assert session.metrics.first_video_at is None
    assert session.metrics.video_packets == 0


def test_video_is_forwarded_and_timings_recorded(vtm, fake_ffmpeg) -> None:
    """Control packets are wake-ups only; video reaches the consumer and is timed."""
    packets = [control(b"not video"), video(b"first"), video(b"second")]
    events: list[str] = []
    vtm(packets, silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        on_event=lambda event, _elapsed: events.append(event),
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    assert bytes(sink.data) == b"firstsecond", "control packet body must not be remuxed"
    assert session.metrics.video_packets == 2
    assert session.metrics.bytes_out == len(b"firstsecond")
    assert session.metrics.first_video_at is not None
    assert session.metrics.first_byte_at is not None
    assert session.metrics.first_byte_at >= session.metrics.first_video_at
    assert events == ["opened", "first-video", "first-byte"]


def test_encrypted_stream_packet_is_reported(vtm, fake_ffmpeg) -> None:
    """A failure in the reader thread has to surface to the caller, not vanish."""
    vtm([FakePacket(channel=VtmChannel.ENCRYPTED_STREAM, body=b"x")], silent_after=False)
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PyEzvizError)


def test_control_traffic_lets_an_idle_session_notice_the_cancel(vtm, fake_ffmpeg) -> None:
    """The reader must get the chance to see the cancel flag, not only the socket.

    Without `include_control=True` the library handles control packets internally and
    this reader would spin inside the iterator, out of reach of `abort()`.
    """
    made = vtm([], stream_class=ChattyVtmStream)
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    assert made["stream"].iterating.wait(SETTLE)

    session.abort("client gone")
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    assert made["stream"].stopped.wait(SETTLE), "the reader never noticed the cancel"


def test_session_ends_even_if_the_reader_cannot_be_woken(
    vtm, fake_ffmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminating FFmpeg is the second lever, and it must be enough on its own.

    The reader here ignores the socket entirely, so the shutdown does nothing, and a
    real FFmpeg stuck probing a mute input would not act on the terminate either. The
    consumer pump has to end the session on the cancel flag alone.
    """
    monkeypatch.setattr(session_module, "_WRITER_JOIN_TIMEOUT", 0.5)
    made = vtm([], stream_class=WedgedVtmStream)
    session = CloudSession(object(), "BB1234567", ffmpeg_path=fake_ffmpeg, first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    assert made["stream"].iterating.wait(SETTLE)

    try:
        session.abort("client gone")
        thread.join(timeout=TEARDOWN_LIMIT)

        assert not thread.is_alive(), "a wedged reader must not hold the session open"
        assert errors == []
        assert made["stream"].closed
    finally:
        made["stream"].release.set()


def test_session_ends_even_if_ffmpeg_ignores_the_terminate(
    vtm, stubborn_ffmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither end cooperates: the reader is wedged and FFmpeg ignores SIGTERM.

    This is the real combination -- a mute camera keeps FFmpeg inside its input probe --
    and it is why the consumer pump stops on the cancel flag instead of waiting for an
    EOF that would never come.
    """
    monkeypatch.setattr(session_module, "_WRITER_JOIN_TIMEOUT", 0.5)
    made = vtm([], stream_class=WedgedVtmStream)
    session = CloudSession(
        object(), "BB1234567", ffmpeg_path=stubborn_ffmpeg.path, first_video_timeout=0
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    assert made["stream"].iterating.wait(SETTLE)
    assert stubborn_ffmpeg.wait_until_stubborn(SETTLE), "the stand-in never became stubborn"

    try:
        session.abort("client gone")
        thread.join(timeout=TEARDOWN_LIMIT)

        assert not thread.is_alive(), "a stubborn FFmpeg must not hold the session open"
        assert errors == []
        assert made["stream"].closed
    finally:
        made["stream"].release.set()


def test_a_failing_remux_is_reported(vtm, tmp_path: Path) -> None:
    """A session that ends by itself still has to check how FFmpeg exited.

    Easy to lose: the teardown aborts unconditionally, so anything conditioned on the
    cancel flag after it would never run.
    """
    broken = tmp_path / "broken-ffmpeg"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    vtm([video(b"data")], silent_after=False)
    session = CloudSession(object(), "BB1234567", ffmpeg_path=str(broken), first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PyEzvizError)
    assert "status 3" in str(errors[0])


def test_abort_is_idempotent_and_keeps_the_first_reason() -> None:
    """Two watchdogs can fire at once; the first reason is the true one."""
    session = CloudSession(object(), "BB1234567")
    session.abort("client gone")
    session.abort("no video")
    assert session.abort_reason == "client gone"


def test_abort_before_run_is_harmless() -> None:
    """There may be no socket and no FFmpeg yet. abort() must not care."""
    session = CloudSession(object(), "BB1234567")
    session.abort("client gone")
    assert session.abort_reason == "client gone"
