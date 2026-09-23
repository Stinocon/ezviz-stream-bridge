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

import logging
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
from ezviz_stream_bridge.session import CloudSession, classify_payload

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


# The value that sits in an EZVIZ camera's SSRC field, and the one pyezvizapi's local frame
# parser uses as its IDMX sentinel -- four bytes, in the same place either way, which is the
# point when the whole header is only twelve.
RTP_SENTINEL = b"\x55\x66\x77\x88"


def rtp_packet(  # noqa: PLR0913 - one per header field the tests vary, all keyword-only
    payload: bytes,
    *,
    sequence: int = 0xB3A3,
    timestamp: int = 0xFB3D75B8,
    payload_type: int = 112,
    csrc_count: int = 0,
    extension: bytes | None = None,
    padding: int = 0,
) -> bytes:
    """A video packet as an RTP-transport camera sends it: header fields, then the media.

    With `extension` set the X bit is set and the four extension bytes carry the profile and
    the length in 32-bit words, which pushes the media 4 + 4*words bytes further in -- the
    case no 24-byte head can show, and the reason the packet dump exists. `csrc_count` moves
    it forward four bytes per source, and `padding` appends its own count, which a demux has
    to read from the last byte of the packet rather than from its header.
    """
    first = 0x80
    if extension is not None:
        first |= 0x10
    if padding:
        first |= 0x20
    fixed = (
        bytes([first | csrc_count, 0x80 | payload_type])
        + sequence.to_bytes(2, "big")
        + timestamp.to_bytes(4, "big")
        + RTP_SENTINEL
    )
    body = fixed + b"".join(
        (0x0A000000 + index).to_bytes(4, "big") for index in range(csrc_count)
    )
    if extension is not None:
        body += b"\x00\x01" + (len(extension) // 4).to_bytes(2, "big") + extension
    body += payload
    if padding:
        body += b"\x00" * (padding - 1) + bytes([padding])
    return body


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


def _arg_recording_ffmpeg(
    tmp_path: Path, *, stderr_lines: int, line_padding: int = 0
) -> tuple[str, Path]:
    """A fake remux that records its argv and emits a known number of stderr lines.

    `printf '%s\n' "$@"` is the only way to observe the arguments FFmpeg was started
    with, which is how the test checks that the level really was raised to `info`.
    `line_padding` grows each line so a test can exceed the OS pipe buffer.
    """
    script = tmp_path / "noisy-ffmpeg"
    args_file = tmp_path / "noisy-ffmpeg.args"
    padding = "x" * line_padding
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{args_file}"\n'
        "i=0\n"
        f'while [ "$i" -lt {stderr_lines} ]; do\n'
        f'  printf \'demux complaint %s {padding}\\n\' "$i" >&2\n'
        "  i=$((i+1))\n"
        "done\n"
        "exec cat\n"
    )
    script.chmod(0o755)
    return str(script), args_file


def _loglevel_arg(args_file: Path) -> str:
    args = args_file.read_text(encoding="utf-8").splitlines()
    return args[args.index("-loglevel") + 1]


def test_ffmpeg_stderr_is_captured_and_bounded(vtm, tmp_path, caplog) -> None:
    """The diagnostic the issue asked for: FFmpeg's own words, not DEVNULL.

    Bounded because a broken demux repeats one complaint per frame, and at `info`
    because a remux stuck in its input probe says nothing at `error` -- the exact case
    that has to be explained. The stderr is far larger than the OS pipe buffer, so the
    test also proves the reader keeps draining after the logging bound: if it stopped,
    the fake FFmpeg would block on the write and never reach `cat`.
    """
    caplog.set_level(logging.INFO)
    packet_body = b"\x00\x00\x01\xba" + b"data"
    ffmpeg_path, args_file = _arg_recording_ffmpeg(
        tmp_path, stderr_lines=1000, line_padding=200
    )
    vtm([video(packet_body)], silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=ffmpeg_path,
        first_video_timeout=0,
        ffmpeg_stderr=True,
        connection_id=7,
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    assert _loglevel_arg(args_file) == "info"
    assert bytes(sink.data) == packet_body, "the fake FFmpeg blocked on stderr and never drained"

    lines = [
        record.getMessage() for record in caplog.records if "[FFmpeg]" in record.getMessage()
    ]
    complaints = [line for line in lines if "demux complaint" in line]
    assert len(complaints) == session_module._FFMPEG_STDERR_MAX_LINES
    assert any("further stderr suppressed" in line for line in lines)
    payload_lines = [line for line in lines if "payload transport=" in line]
    assert len(payload_lines) == 1, "the sniff must log once, not once per packet"
    assert "transport=MPEG_PS at offset=0" in payload_lines[0]
    assert all("conn=7" in line for line in lines)


def test_classify_payload_recognises_mpeg_ps_at_any_offset() -> None:
    # The prefix starts mid-pack; the pack start code is what makes it PS, and it sits 100
    # bytes in. A per-packet check of the first byte could never see it.
    data = b"\x11" * 100 + b"\x00\x00\x01\xba" + b"\x22" * 50

    assert classify_payload(data) == ("MPEG_PS", 100)


def test_classify_payload_recognises_mpeg_ts_on_the_sync_grid() -> None:
    data = (
        bytes([0x47])
        + b"\x00" * 187
        + bytes([0x47])
        + b"\x00" * 187
        + bytes([0x47])
        + b"\x00" * 10
    )

    assert classify_payload(data) == ("MPEG_TS", 0)


def test_classify_payload_needs_three_sync_bytes_for_mpeg_ts() -> None:
    # Two 0x47 bytes 188 apart occur by chance in random data about one time in nine; on a
    # real RTP stream or an unrecognised payload that was reported as MPEG-TS.
    data = bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 100

    assert classify_payload(data) == ("UNKNOWN", -1)


def test_classify_payload_prefers_the_ts_grid_over_an_rtp_byte() -> None:
    # A TS stream starting mid-packet whose first byte happens to carry RTP's version bits
    # (0x80-0xBF) must be MPEG-TS, not RTP: the grid is the stronger evidence.
    data = (
        bytes([0x90])
        + b"\x00" * 4
        + bytes([0x47])
        + b"\x00" * 187
        + bytes([0x47])
        + b"\x00" * 187
        + bytes([0x47])
        + b"\x00" * 10
    )

    assert classify_payload(data) == ("MPEG_TS", 5)


def test_classify_payload_does_not_call_a_lone_sync_byte_mpeg_ts() -> None:
    assert classify_payload(b"\x00" * 10 + bytes([0x47]) + b"\x00" * 10) == ("UNKNOWN", -1)


def test_classify_payload_distinguishes_rtp_from_unknown() -> None:
    assert classify_payload(bytes([0x80]) + b"\x00" * 8) == ("RTP", 0)
    assert classify_payload(b"\x00" * 8) == ("UNKNOWN", -1)


def test_classify_payload_on_a_bare_pack_header() -> None:
    assert classify_payload(b"\x00\x00\x01\xba") == ("MPEG_PS", 0)


def test_transport_sniff_buffers_across_packet_boundaries(vtm, fake_ffmpeg, caplog) -> None:
    """The whole point of the buffered sniff: the signature arrives in a later packet."""
    caplog.set_level(logging.INFO)
    vtm(
        [video(b"\x11" * 200), video(b"\x00\x00\x01\xba" + b"\x22" * 100)],
        silent_after=False,
    )
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    lines = [
        record.getMessage()
        for record in caplog.records
        if "payload transport=" in record.getMessage()
    ]
    assert len(lines) == 1
    assert "transport=MPEG_PS at offset=200" in lines[0]


def test_transport_sniff_reports_on_the_threshold_while_the_stream_runs(
    vtm, fake_ffmpeg, caplog
) -> None:
    """The threshold path, not only the end-of-stream flush: it must log once, while live."""
    caplog.set_level(logging.INFO)
    made = vtm(
        [video(b"\x00\x00\x01\xba" + b"\x22" * 40) for _ in range(12)], silent_after=True
    )
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    try:
        assert made["stream"].iterating.wait(SETTLE)
        deadline = time.monotonic() + SETTLE
        sniff_lines: list[str] = []
        while time.monotonic() < deadline:
            sniff_lines = [
                record.getMessage()
                for record in caplog.records
                if "payload transport=" in record.getMessage()
            ]
            if sniff_lines:
                break
            time.sleep(0.01)
        # The fake VTM blocks after its packets, so the stream is still running here: the
        # line came from the threshold path, not from the end-of-stream flush.
        assert thread.is_alive(), "the sniff must fire while the stream is still running"
        assert len(sniff_lines) == 1, "the sniff must log once, not once per packet"
        assert "transport=MPEG_PS" in sniff_lines[0]
    finally:
        session.abort("client gone")
        thread.join(timeout=TEARDOWN_LIMIT)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("capturing", [False, True])
def test_stderr_pipe_matches_the_diagnostic_flag(fake_ffmpeg, capturing: bool) -> None:
    """`DEVNULL` off, a pipe on -- the thing that distinguishes the two modes."""
    session = CloudSession(
        object(), "BB1234567", ffmpeg_path=fake_ffmpeg, ffmpeg_stderr=capturing
    )
    process = session._start_ffmpeg()
    try:
        assert (process.stderr is None) is not capturing
    finally:
        process.terminate()
        process.wait(timeout=TEARDOWN_LIMIT)
        if process.stderr is not None:
            process.stderr.close()


def test_stderr_capture_tears_down_with_a_live_ffmpeg(
    vtm, stubborn_ffmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path where the reap-join-close ordering matters: the reader is blocked on a
    live FFmpeg's stderr while the session aborts, and FFmpeg ignores the terminate."""
    monkeypatch.setattr(session_module, "_WRITER_JOIN_TIMEOUT", 0.5)
    monkeypatch.setattr(session_module, "_FFMPEG_REAP_TIMEOUT", 0.2)
    made = vtm([], stream_class=WedgedVtmStream)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=stubborn_ffmpeg.path,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    assert made["stream"].iterating.wait(SETTLE)
    assert stubborn_ffmpeg.wait_until_stubborn(SETTLE), "the stand-in never became stubborn"

    try:
        session.abort("client gone")
        thread.join(timeout=TEARDOWN_LIMIT)

        assert not thread.is_alive(), "a blocked stderr reader must not hold the session open"
        assert errors == []
        assert made["stream"].closed
    finally:
        made["stream"].release.set()


def test_ffmpeg_stderr_is_discarded_by_default(vtm, tmp_path, caplog) -> None:
    """Off by default: the level stays `error` and nothing is logged."""
    caplog.set_level(logging.INFO)
    ffmpeg_path, args_file = _arg_recording_ffmpeg(tmp_path, stderr_lines=5)
    vtm([video(b"data")], silent_after=False)
    session = CloudSession(object(), "BB1234567", ffmpeg_path=ffmpeg_path, first_video_timeout=0)
    sink = Sink()
    thread, errors = run_in_thread(session, sink)
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    assert _loglevel_arg(args_file) == "error"
    assert not [r for r in caplog.records if "[FFmpeg]" in r.getMessage()]


def _diagnostic_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "[FFmpeg]" in record.getMessage()]


def _run_one_video_packet(vtm, fake_ffmpeg, body: bytes, *, stderr: bool):
    """Run a session over a single video packet and return its log lines."""
    vtm([video(body)], silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=stderr,
    )
    thread, errors = run_in_thread(session, Sink())
    thread.join(timeout=TEARDOWN_LIMIT)
    assert not thread.is_alive()
    assert errors == []
    return session


def test_packet_dump_slices_the_payload_past_an_rtp_extension(vtm, fake_ffmpeg, caplog) -> None:
    """The question the 24-byte head cannot answer: X is set, so where does the media start?

    Twelve fixed bytes plus a four-byte extension header plus twelve words puts the payload
    at byte 64 -- twice as far in as the transport line prints. The dump decodes the header
    and slices the payload out at the offset pyezvizapi's own unwrap computes, so the line
    is what a demux would actually be handed.
    """
    caplog.set_level(logging.INFO)
    payload = b"\x40\x01\x0c\x01\xff\xff\x04\x08" + b"\xaa" * 56
    _run_one_video_packet(
        vtm,
        fake_ffmpeg,
        rtp_packet(payload, extension=bytes(range(48))) + b"\xbb" * 60,
        stderr=True,
    )
    lines = _diagnostic_lines(caplog)

    assert any("payload transport=RTP at offset=0" in line for line in lines)
    header = next(line for line in lines if "packet[0] len=" in line)
    assert "rtp=V2 P0 X1 CC0 M1 PT112 seq=0xb3a3 ts=0xfb3d75b8" in header
    assert "head=90 f0 b3 a3 fb 3d 75 b8 55 66 77 88 00 01 00 0c" in header
    unwrapped = next(line for line in lines if "rtp payload+64=" in line)
    assert unwrapped.endswith(payload[:64].hex(" "))


def test_packet_dump_explains_a_packet_whose_extension_overruns_it(
    vtm, fake_ffmpeg, caplog
) -> None:
    """The reporter's own 24-byte head: the extension declares 48 bytes, the head has none.

    No unwrap can succeed on a packet the log only shows the start of, so the dump must say
    which of the two it is -- an overrun, or a payload -- instead of printing an offset that
    was never reached.
    """
    caplog.set_level(logging.INFO)
    head = bytes.fromhex(
        "90 f0 b3 a3 fb 3d 75 b8 55 66 77 88 00 01 00 0c 40 0e 48 4b 00 02 1a 9b"
    )
    _run_one_video_packet(vtm, fake_ffmpeg, head, stderr=True)
    lines = _diagnostic_lines(caplog)

    assert any("payload transport=RTP at offset=0" in line for line in lines)
    assert any(
        "packet[0] rtp payload: RTP extension payload exceeds packet length" in line
        for line in lines
    )


def test_packet_dump_waits_for_the_leading_set_after_an_early_verdict(
    vtm, fake_ffmpeg, caplog
) -> None:
    """The full shape of the bug this guards: the byte bound fires on the third packet, the
    verdict goes out then -- it must -- and the dump still waits for the leading eight rather
    than answering with the three that happened to trip it."""
    caplog.set_level(logging.INFO)
    packets = [video(rtp_packet(b"\x40\x01" + bytes([index]) * 3000)) for index in range(12)]
    vtm(packets, silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    thread, errors = run_in_thread(session, Sink())
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    lines = _diagnostic_lines(caplog)
    verdict = next(index for index, line in enumerate(lines) if "payload transport=" in line)
    numbered = [
        (index, line) for index, line in enumerate(lines) if " packet[" in line and " len=" in line
    ]
    assert [line.split("packet[")[1].split("]")[0] for _, line in numbered] == [
        str(index) for index in range(8)
    ]
    assert verdict < numbered[0][0], "the dump must come after the verdict, never before it"


def test_packet_dump_is_silent_for_an_mpeg_ps_stream(vtm, fake_ffmpeg, caplog) -> None:
    """A PS stream needs none of it: FFmpeg identifies its codec, and eight packets of noise
    per session is what an opt-in diagnostic must not turn into by default."""
    caplog.set_level(logging.INFO)
    session = _run_one_video_packet(
        vtm, fake_ffmpeg, b"\x00\x00\x01\xba" + b"\x22" * 100, stderr=True
    )
    lines = _diagnostic_lines(caplog)

    assert any("payload transport=MPEG_PS at offset=0" in line for line in lines)
    assert not [line for line in lines if "packet[" in line]
    assert session._dump_bodies == [], "a PS session has no reason to hold packet bodies"  # noqa: SLF001


def test_packet_dump_stops_after_the_leading_packets(vtm, fake_ffmpeg, caplog) -> None:
    """Bounded, and in order: the leading packets only, once each."""
    caplog.set_level(logging.INFO)
    packets = [video(rtp_packet(b"\x40\x01" + bytes([index]) * 30)) for index in range(12)]
    vtm(packets, silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    thread, errors = run_in_thread(session, Sink())
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    numbered = [
        line for line in _diagnostic_lines(caplog) if " packet[" in line and " len=" in line
    ]
    assert [line.split("packet[")[1].split("]")[0] for line in numbered] == [
        str(index) for index in range(8)
    ]


def test_packet_dump_reads_padding_from_the_real_end_of_the_packet(
    vtm, fake_ffmpeg, caplog
) -> None:
    """P is a count in the last byte of the packet, so the dump has to keep the whole packet:
    a truncated copy strips a byte count taken from the middle of the media and reports an
    offset that is simply wrong. No cap, however generous, would be safe here."""
    caplog.set_level(logging.INFO)
    payload = b"\x40\x01" + b"\xaa" * 6000
    _run_one_video_packet(vtm, fake_ffmpeg, rtp_packet(payload, padding=8), stderr=True)
    lines = _diagnostic_lines(caplog)

    assert any(f"packet[0] len={6000 + 2 + 12 + 8} rtp=V2 P1" in line for line in lines)
    unwrapped = next(line for line in lines if "packet[0] rtp payload+" in line)
    assert "packet[0] rtp payload+20=" in unwrapped
    assert unwrapped.endswith(payload[:64].hex(" "))


def test_packet_dump_skips_the_csrc_list(vtm, fake_ffmpeg, caplog) -> None:
    """CC moves the media forward four bytes per source: the header line must report it and
    the payload line must land past it, not on the first source identifier."""
    caplog.set_level(logging.INFO)
    payload = b"\x67\xf4\x00\x0b" + b"\xcc" * 60
    _run_one_video_packet(vtm, fake_ffmpeg, rtp_packet(payload, csrc_count=2), stderr=True)
    lines = _diagnostic_lines(caplog)

    assert any("rtp=V2 P0 X0 CC2 M1 PT112" in line for line in lines)
    unwrapped = next(line for line in lines if "packet[0] rtp payload+" in line)
    assert "packet[0] rtp payload+20=" in unwrapped
    assert unwrapped.endswith(payload[:64].hex(" "))


def test_packet_dump_is_not_cut_short_by_an_early_transport_verdict(
    vtm, fake_ffmpeg, caplog
) -> None:
    """Eight kilobytes can arrive in three packets, and the verdict must come then -- but the
    packet dump answers a question that wants the leading packets, not the three that happened
    to trip the byte bound. Here the stream ends before a full set, so all of them are shown."""
    caplog.set_level(logging.INFO)
    packets = [video(rtp_packet(b"\x40\x01" + bytes([index]) * 4000)) for index in range(5)]
    vtm(packets, silent_after=False)
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    thread, errors = run_in_thread(session, Sink())
    thread.join(timeout=TEARDOWN_LIMIT)

    assert not thread.is_alive()
    assert errors == []
    lines = _diagnostic_lines(caplog)
    assert any("payload transport=RTP" in line for line in lines)
    numbered = [line for line in lines if " packet[" in line and " len=" in line]
    assert [line.split("packet[")[1].split("]")[0] for line in numbered] == [
        str(index) for index in range(5)
    ]


def test_packet_dump_covers_a_payload_that_is_not_rtp_either(vtm, fake_ffmpeg, caplog) -> None:
    """UNKNOWN is a verdict too, and the dump is what says why: the header bytes are not an
    RTP header, and the unwrap says so in words rather than printing an offset that is not
    there."""
    caplog.set_level(logging.INFO)
    _run_one_video_packet(vtm, fake_ffmpeg, b"\x11" * 200, stderr=True)
    lines = _diagnostic_lines(caplog)

    assert any("payload transport=UNKNOWN at offset=-1" in line for line in lines)
    assert any("packet[0] len=200 rtp=none" in line for line in lines)
    assert any("packet[0] rtp payload: Unsupported RTP version" in line for line in lines)


def test_packet_dump_flushes_when_the_session_is_aborted_mid_sniff(
    vtm, fake_ffmpeg, caplog
) -> None:
    """Two packets and then silence: with less than a full set, the verdict and the dump both
    come from the teardown. Evidence that exists at the moment of an abort has to survive it."""
    caplog.set_level(logging.INFO)
    made = vtm(
        [video(rtp_packet(b"\x40\x01" + b"\xaa" * 30)) for _ in range(2)], silent_after=True
    )
    session = CloudSession(
        object(),
        "BB1234567",
        ffmpeg_path=fake_ffmpeg,
        first_video_timeout=0,
        ffmpeg_stderr=True,
    )
    thread, errors = run_in_thread(session, Sink())
    assert made["stream"].iterating.wait(SETTLE)
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline and len(session._dump_bodies) < 2:  # noqa: SLF001 - the only way to see the reader's progress
        time.sleep(0.01)

    try:
        session.abort("client gone")
        thread.join(timeout=TEARDOWN_LIMIT)
        assert not thread.is_alive()
        assert errors == []
    finally:
        # Nothing to release for this fake: the abort already shut its socket down, which is
        # exactly how the reader escapes the blocking recv.
        pass

    lines = _diagnostic_lines(caplog)
    assert any("payload transport=RTP" in line for line in lines)
    numbered = [line for line in lines if " packet[" in line and " len=" in line]
    assert [line.split("packet[")[1].split("]")[0] for line in numbered] == ["0", "1"]


def test_packet_dump_needs_the_diagnostic_flag(vtm, fake_ffmpeg, caplog) -> None:
    """Off by default, like the rest of the capture: an RTP payload must not change that."""
    caplog.set_level(logging.INFO)
    _run_one_video_packet(
        vtm, fake_ffmpeg, rtp_packet(b"\x40\x01" + b"\xaa" * 30), stderr=False
    )
    assert not _diagnostic_lines(caplog)
