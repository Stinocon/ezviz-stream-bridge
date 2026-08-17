"""Tests for deciding whether a stored session is actually dead.

The distinction this pins down is the expensive one: every token wrongly declared dead
spends a real login, and EZVIZ rate-limits those. So a 500 from the cloud must not be
read as an expired session.
"""

from __future__ import annotations

import pytest

from ezviz_stream_bridge.token import (
    _UNAUTHENTICATED_STATUS,
    _http_status,
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _ResponseError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _Response(status_code)


def test_status_is_found_on_the_exception_itself() -> None:
    assert _http_status(_ResponseError(401)) == 401


def test_status_is_found_on_the_cause() -> None:
    # This is the shape that matters in practice: pyezvizapi raises its own HTTPError
    # `from` the requests error, so the status is one link down the chain.
    wrapper = Exception("Could not get device infos")
    wrapper.__cause__ = _ResponseError(503)

    assert _http_status(wrapper) == 503


def test_status_is_found_several_links_down() -> None:
    inner = _ResponseError(403)
    middle = Exception("middle")
    middle.__cause__ = inner
    outer = Exception("outer")
    outer.__cause__ = middle

    assert _http_status(outer) == 403


def test_missing_status_is_reported_as_none() -> None:
    assert _http_status(Exception("no HTTP anywhere in here")) is None


def test_a_response_without_a_status_code_is_ignored() -> None:
    err = Exception("odd")
    err.response = object()  # type: ignore[attr-defined]

    assert _http_status(err) is None


def test_a_cycle_in_the_cause_chain_terminates() -> None:
    # Nothing in the library builds one, but __cause__ is writable and a loop here
    # would hang the supervisor's start path rather than fail it.
    first = Exception("first")
    second = Exception("second")
    first.__cause__ = second
    second.__cause__ = first

    assert _http_status(first) is None


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_mean_the_session_is_dead(status: int) -> None:
    assert status in _UNAUTHENTICATED_STATUS


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503])
def test_other_statuses_do_not_mean_the_session_is_dead(status: int) -> None:
    # 429 included deliberately: being rate-limited is the worst possible moment to
    # decide the answer is another login.
    assert status not in _UNAUTHENTICATED_STATUS
