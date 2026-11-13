"""Share one on-demand cloud session among concurrent HTTP stream consumers."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from pyezvizapi.exceptions import DeviceException, PyEzvizError

from .session import CloudSession

_LOGGER = logging.getLogger(__name__)
_REPLAY_BYTES = 512 * 1024
_SUBSCRIBER_BYTES = 1024 * 1024


class Subscription:
    """One bounded reader of a shared remuxed stream."""

    def __init__(self, session: CloudSession, initial: bytes = b"") -> None:
        self.session = session
        self.bytes_out = 0
        self.reason: str | None = None
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._closed = False
        self._condition = threading.Condition()
        if initial:
            self._chunks.append(initial)
            self._buffered = len(initial)

    def _push(self, chunk: bytes) -> bool:
        with self._condition:
            if self._closed:
                return False
            if self._buffered + len(chunk) > _SUBSCRIBER_BYTES:
                self.reason = "slow consumer"
                self._chunks.clear()
                self._buffered = 0
                self._closed = True
                self._condition.notify_all()
                return False
            self._chunks.append(chunk)
            self._buffered += len(chunk)
            self._condition.notify()
            return True

    def _finish(self, reason: str) -> None:
        with self._condition:
            if not self._closed:
                self.reason = reason
                self._closed = True
                self._condition.notify_all()

    def read(self, timeout: float = 0.5) -> bytes | None:
        """Return the next buffered chunk, or None when the subscription is finished."""
        with self._condition:
            deadline = time.monotonic() + timeout
            while not self._chunks and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            if self._chunks:
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self.bytes_out += len(chunk)
                return chunk
            return None


class _HubOutput:
    """The output sink CloudSession writes to; each write is fanned out once."""

    def __init__(self, hub: StreamHub) -> None:
        self._hub = hub

    def write(self, chunk: bytes) -> int:
        self._hub.publish(chunk)
        return len(chunk)

    def flush(self) -> None:
        return


class StreamHub:
    """An upstream session that exists only while at least one consumer is subscribed.

    Late subscribers receive a small rolling MPEG-TS tail. This lets the demuxer find the next
    repeated tables and keyframe without buffering the camera's whole stream or making a slow
    client back-pressure every other consumer.
    """

    def __init__(  # noqa: PLR0913 - each callback marks one lifecycle boundary
        self,
        *,
        make_session: Callable[[int], CloudSession],
        before_start: Callable[[CloudSession], None],
        on_track: Callable[[CloudSession], None],
        on_untrack: Callable[[CloudSession], None],
        on_camera_timeout: Callable[[int, DeviceException], str],
        on_cloud_error: Callable[[int, PyEzvizError], str],
        max_duration: float = 0,
    ) -> None:
        self._make_session = make_session
        self._before_start = before_start
        self._on_track = on_track
        self._on_untrack = on_untrack
        self._on_camera_timeout = on_camera_timeout
        self._on_cloud_error = on_cloud_error
        self._max_duration = max_duration
        self._condition = threading.Condition()
        self._subscribers: set[Subscription] = set()
        self._replay = bytearray()
        self._session: CloudSession | None = None
        self._producer: threading.Thread | None = None

    def subscribe(self, connection_id: int) -> Subscription:
        """Join the active stream, or start one if the camera currently has no consumers."""
        with self._condition:
            while (
                self._producer is not None
                and not self._subscribers
                and self._session is not None
                and self._session.abort_reason is not None
            ):
                self._condition.wait()

            if self._producer is None:
                session = self._make_session(connection_id)
                self._session = session
                self._replay.clear()
                self._on_track(session)
                self._producer = threading.Thread(
                    target=self._run,
                    args=(connection_id, session),
                    name=f"stream-hub-{connection_id}",
                    daemon=True,
                )
                subscription = Subscription(session)
                self._subscribers.add(subscription)
                try:
                    self._producer.start()
                except RuntimeError:
                    self._subscribers.discard(subscription)
                    self._session = None
                    self._producer = None
                    self._on_untrack(session)
                    session.abort("error")
                    self._condition.notify_all()
                    raise
                return subscription

            session = self._session
            if session is None:
                raise RuntimeError("stream hub producer has no session")
            subscription = Subscription(session, bytes(self._replay))
            self._subscribers.add(subscription)
            return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove one consumer and abort the upstream only after the last one leaves."""
        with self._condition:
            self._subscribers.discard(subscription)
            if not self._subscribers and self._session is not None:
                self._session.abort("client gone")
            self._condition.notify_all()

    def publish(self, chunk: bytes) -> None:
        """Fan out bytes without allowing an individual slow consumer to stall the source."""
        if not chunk:
            return
        with self._condition:
            self._replay.extend(chunk)
            if len(self._replay) > _REPLAY_BYTES:
                del self._replay[: len(self._replay) - _REPLAY_BYTES]
            for subscription in tuple(self._subscribers):
                if not subscription._push(chunk):
                    self._subscribers.discard(subscription)
                    _LOGGER.warning(
                        "[HTTP] a stream consumer was disconnected after its buffer "
                        "exceeded %d bytes",
                        _SUBSCRIBER_BYTES,
                    )
            if not self._subscribers and self._session is not None:
                self._session.abort("client gone")

    def _run(self, connection_id: int, session: CloudSession) -> None:
        timer: threading.Timer | None = None
        reason = "stream ended"
        try:
            self._before_start(session)
            if session.abort_reason is None:
                if self._max_duration > 0:
                    timer = threading.Timer(
                        self._max_duration,
                        lambda: session.abort("max duration"),
                    )
                    timer.daemon = True
                    timer.start()
                _LOGGER.info("[VTM] conn=%d shared session opening", connection_id)
                session.run(_HubOutput(self))  # type: ignore[arg-type]
            reason = session.abort_reason or "finished"
        except DeviceException as err:
            reason = self._on_camera_timeout(connection_id, err)
        except PyEzvizError as err:
            reason = self._on_cloud_error(connection_id, err)
        except Exception:
            reason = "error"
            _LOGGER.exception("[VTM] conn=%d shared stream failed", connection_id)
        finally:
            if timer is not None:
                timer.cancel()
            self._on_untrack(session)
            with self._condition:
                consumer_count = len(self._subscribers)
                for subscription in tuple(self._subscribers):
                    subscription._finish(reason)
                self._subscribers.clear()
                self._session = None
                self._producer = None
                self._condition.notify_all()
            _LOGGER.info(
                "[VTM] conn=%d shared session closed reason=%r consumers=%d",
                connection_id,
                reason,
                consumer_count,
            )


__all__ = ["StreamHub", "Subscription"]
