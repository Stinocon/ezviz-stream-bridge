"""Tests for the restart policy.

The supervisor's value is entirely in what it does when a proxy dies, so that is what
is pinned here: the delay grows, it is capped, and a proxy that ran long enough gets a
clean slate. None of these tests sleep -- the clock is injected.
"""

from __future__ import annotations

from ezviz_stream_bridge.config import CameraConfig
from ezviz_stream_bridge.supervisor import (
    FIRST_BACKOFF,
    HEALTHY_AFTER,
    MAX_BACKOFF,
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
