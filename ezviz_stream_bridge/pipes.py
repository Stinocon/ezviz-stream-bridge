"""FFmpeg's two inputs, and the routing that decides which of them a packet goes to.

Both write ends are non-blocking queues, and that is the whole point of them: the session has
one pump thread to keep both inputs moving, and a pump parked in a blocking `write` on either
is the deadlock DEC-0004 and DEC-0005 record. Split out of `session.py` in 0.1.18, unchanged;
the session still constructs and tears down every one of them.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO

from .payload import _payload_type_of
from .rtp import AacHbrDepacketizer, RtpDepacketizer

# How much audio the pipe has not taken yet. A pipe holds 64 KiB, and FFmpeg can leave the audio
# unread for as long as it is waiting for video -- so without a bound of its own this buffer is the
# only thing standing between a slow consumer and an unbounded one. A minute of this family's
# audio is about 200 KiB.
_AUDIO_PENDING_BYTES = 256 * 1024

# How much video the stdin queue holds for FFmpeg. Its probe of the video input runs before it
# opens the audio one, and the interleaver will not drain the video past the audio's timestamps:
# the whole probe's worth of video has to wait, and a 1080p camera sends 150-500 KiB/s -- so the
# bound that absorbs one probe plus one slow start is a few MiB, not the 64 KiB of the pipe. A
# consumer further behind than this is not slow but stalled: the oldest chunks are dropped and
# counted, because a queue this deep is already most of a minute of video.
_VIDEO_PENDING_BYTES = 8 * 1024 * 1024


class _AudioPipe:
    """The write end of the pipe FFmpeg reads the audio from -- and never a place to park.

    A blocking write here is a deadlock with no way out of it. FFmpeg opens its inputs in order
    and probes the video one before it opens the audio input at all, so a thread parked in
    `write` on a full audio pipe is parked while FFmpeg waits for video that the same thread has
    not written yet -- and no watchdog can help, because closing a file does not wake a thread
    that is already inside `write`. The descriptor is therefore non-blocking, and what it will not
    take is kept here and offered again on every later packet, video included: FFmpeg always
    reaches the audio pipe once it has the video its probe wants, and then it drains this.

    Whole chunks rather than bytes, and bounded. A chunk is one packet's Access Units, so dropping
    the oldest when the bound is reached loses frames instead of corrupting the framing. `dropped`
    counts every audio byte that never reached FFmpeg -- the chunks the bound threw away, and
    whatever was still queued when the session ended or FFmpeg died -- and the session reports it:
    a camera that outruns FFmpeg for longer than the bound gets gaps it can be told about, which is
    a number rather than a session that quietly loses sound.
    """

    def __init__(self, fd: int) -> None:
        os.set_blocking(fd, False)
        self._fd = fd
        # Held only around non-blocking operations, so it cannot park anybody: it is here so that
        # a close from the stall watchdog cannot land between a write's check and its syscall,
        # where the descriptor could already have been recycled by another thread.
        self._lock = threading.Lock()
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self.dropped = 0
        self._closed = False

    def write(self, frames: bytes) -> None:
        """Offer audio bytes to the pipe, keeping what it will not take."""
        with self._lock:
            if self._closed:
                return
            self._pending.append(frames)
            self._pending_bytes += len(frames)
            while self._pending_bytes > _AUDIO_PENDING_BYTES and len(self._pending) > 1:
                self.dropped += len(self._pending.popleft())
                self._pending_bytes = sum(len(chunk) for chunk in self._pending)
            self._drain()

    def flush(self) -> None:
        """Try the pipe again with whatever is waiting for room."""
        with self._lock:
            self._drain()

    def close(self) -> None:
        """Release the descriptor, once. Later writes find it closed and do nothing."""
        with self._lock:
            if self._closed:
                return
            # Set before the syscall: the flag is what keeps a recycled descriptor number from
            # ever being written to by a write that lost the race.
            self._closed = True
            with suppress(OSError):
                os.close(self._fd)
            # Counted as well, not just discarded: these bytes never reached FFmpeg either, and a
            # number that means "the bound threw some away" while silently dropping the tail would
            # undercount exactly the case it exists for.
            self.dropped += self._pending_bytes
            self._pending.clear()
            self._pending_bytes = 0

    def _drain(self) -> None:
        """Write what fits, oldest first, and keep the rest. Called with the lock held."""
        if self._closed:
            return
        while self._pending:
            try:
                written = os.write(self._fd, self._pending[0])
            except BlockingIOError:
                return
            except OSError:
                # The reader is gone: FFmpeg is dead and the teardown is on its way. What is left
                # never made it out, and it belongs in the same count as the rest.
                self.dropped += self._pending_bytes
                self._pending.clear()
                self._pending_bytes = 0
                return
            self._pending_bytes -= written
            if written < len(self._pending[0]):
                self._pending[0] = self._pending[0][written:]
                return
            self._pending.popleft()


class _VideoPipe:
    """The video's write end of FFmpeg's stdin: a bounded queue that cannot park the pump.

    FFmpeg probes the video input before it opens the audio one, and its interleaver will not
    drain the video past the audio's timestamps: from the first audio packet to the end of the
    session, the video is always read with a delay. A blocking stdin holds less than a second
    of a 1080p stream, so the pump parks in `write` -- and a parked pump is a pump that can no
    longer feed the audio pipe the interleaver is waiting on. That cycle mutes sessions (only
    the stall watchdog can break it, by ending the audio), and it is why the video waits here
    instead: chunks stay whole in a queue big enough to absorb a whole probe, the descriptor
    is non-blocking, and a stdin that stays jammed degrades to the oldest chunks dropped and
    counted -- a bounded gap, not a mute. Unlike the audio's pipe, an error here raises: a
    closed stdin means FFmpeg is gone and the session has to end, not keep draining the
    camera into a queue nobody reads.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        os.set_blocking(fd, False)
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self.dropped = 0
        self.dropped_chunks = 0
        self._closed = False

    def write(self, chunk: bytes) -> None:
        """Queue one chunk, whole, and try the descriptor: what fits goes now, the rest waits."""
        if self._closed:
            raise BrokenPipeError("the video pipe is closed")
        self._pending.append(chunk)
        self._pending_bytes += len(chunk)
        while self._pending_bytes > _VIDEO_PENDING_BYTES and len(self._pending) > 1:
            self.dropped_chunks += 1
            self.dropped += len(self._pending[0])
            self._pending_bytes -= len(self._pending[0])
            self._pending.popleft()
        self._drain()

    def flush(self) -> None:
        """Try the descriptor again. The pump calls this on every packet, like the audio's."""
        self._drain()

    def close(self) -> None:
        """End the input: what the descriptor has is all FFmpeg gets, and it reads EOF after.

        The queue is dropped rather than flushed because the session is over and the reader
        behind it -- FFmpeg -- is about to be asked to exit; the bytes still queued belong in
        the same count as the dropped ones.
        """
        self._closed = True
        self.dropped += self._pending_bytes
        self.dropped_chunks += len(self._pending)
        self._pending.clear()
        self._pending_bytes = 0
        if self._fd >= 0:
            fd, self._fd = self._fd, -1
            with suppress(OSError):
                os.close(fd)

    def _drain(self) -> None:
        """Write what fits, oldest first, and keep the rest. A reader that is gone raises,
        so the session ends the way a blocking stdin's BrokenPipeError ended it."""
        if self._closed:
            return
        while self._pending:
            try:
                written = os.write(self._fd, self._pending[0])
            except BlockingIOError:
                return
            self._pending_bytes -= written
            if written < len(self._pending[0]):
                self._pending[0] = self._pending[0][written:]
                return
            self._pending.popleft()


@dataclass
class _Sinks:
    """Where one packet goes: FFmpeg's video pipe, and its audio pipe when there is one.

    Routing lives here rather than at each of the two call sites -- the prefix replay and the
    live loop -- because it is one decision taken from one fact, the packet's payload type,
    and the two have to take it the same way. A packet of the audio payload type belongs to
    the audio sink only when a depacketizer exists for it: with none, it falls through to the
    video one, which counts it as another payload type and skips it, exactly as it did before
    this path existed.

    Not frozen, unlike the rest of this module's value objects: the audio input can be given up
    mid-session (see `_end_stalled_audio`), and after that there is no audio sink to write to.
    """

    video: BinaryIO
    depacketizer: RtpDepacketizer | None
    audio: _AudioPipe | None = None
    audio_depacketizer: AacHbrDepacketizer | None = None
    now: Callable[[], float] = time.monotonic
    audio_at: float = 0.0  # when audio was last written, for the stall check

    def __post_init__(self) -> None:
        if self.audio is not None:
            # The clock starts with the input, not at the epoch: a zero here would read as a
            # stall that has not happened.
            self.audio_at = self.now()

    def owns_audio(self, body: bytes) -> bool:
        """True when this packet is the audio stream's, rather than one to skip."""
        return (
            self.audio is not None
            and self.audio_depacketizer is not None
            and _payload_type_of(body) == self.audio_depacketizer.payload_type
        )

    def feed(self, body: bytes) -> None:
        """Write one packet to the sink that owns it, and only when it produces bytes."""
        if not body:
            return
        audio = self.audio_depacketizer
        if audio is not None and self.owns_audio(body):
            frames = audio.feed(body)
            # Bound once, and after the parse rather than at the check above. The watchdog may
            # have given the input up while those bytes were being unpacked, and re-reading the
            # attribute here would be the one thing this path cannot afford: an AttributeError
            # on `None` is not an OSError, so it would escape the guard below, reach the pump's
            # `except Exception` and take the whole session down -- video included -- at exactly
            # the moment the watchdog was saving it. A bound reference to a pipe that is already
            # closed does nothing instead.
            pipe = self.audio
            if frames and pipe is not None:
                pipe.write(frames)
                self.audio_at = self.now()
            return
        # A video packet is also the moment to try the audio pipe again: one comparison when
        # nothing is waiting, and it is what lets the audio sink do without a thread of its own.
        if self.audio is not None:
            self.audio.flush()
        chunk = self.depacketizer.feed(body) if self.depacketizer is not None else body
        if chunk:
            self.video.write(chunk)
            self.video.flush()

    def give_up_audio(self) -> bool:
        """Release the audio input, once. False when there is nothing left to give up.

        Called from the watchdog thread while the pump may be writing, so the reference is
        dropped before the pipe is closed: the pump then loses frames rather than the session.
        Dropping it first is also what keeps `feed` from writing through a reference that is
        already closed, and the pipe closes the descriptor itself rather than the descriptor
        number being reused while a write still holds it.
        """
        target, self.audio = self.audio, None
        if target is None:
            return False
        target.close()
        return True
