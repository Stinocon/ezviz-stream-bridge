"""Tests for deciding whether a stored session is actually dead.

The distinction this pins down is the expensive one: every token wrongly declared dead
spends a real login, and EZVIZ rate-limits those. So a 500 from the cloud must not be
read as an expired session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pyezvizapi.exceptions import (
    DeviceException,
    EzvizAuthTokenExpired,
    HTTPError,
    PyEzvizError,
)

from ezviz_stream_bridge import token as token_module
from ezviz_stream_bridge.token import (
    _UNAUTHENTICATED_STATUS,
    TokenStore,
    _http_status,
    session_rejected,
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


# -- the session the cloud stopped accepting ---------------------------------------------------
#
# A proxy holds the token, never the password, and pyezvizapi renews it in memory with the
# refresh session. When that renewal is refused, every new stream fails the same way, and only
# the supervisor can fix it. These pin which failures mean that, and which do not: reading a 500
# as an expired session would restart a proxy and cut every stream that was working.


def _raised_while_handling(handled: BaseException, raised: BaseException) -> BaseException:
    """`raised`, with `handled` as its implicit context -- the shape a `raise` inside an
    `except` block gives it, which is how pyezvizapi reports a renewal it cannot attempt."""
    try:
        try:
            raise handled
        except type(handled):
            raise raised  # noqa: B904 - the implicit context is the point
    except type(raised) as err:
        return err
    raise AssertionError("unreachable")


def _http_error(status: int) -> HTTPError:
    err = HTTPError("HTTP error")
    err.__cause__ = _ResponseError(status)
    return err


def test_a_refused_renewal_means_the_session_is_rejected() -> None:
    assert session_rejected(EzvizAuthTokenExpired("refresh session expired"))


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_status_after_the_retries_means_the_session_is_rejected(status: int) -> None:
    assert session_rejected(_http_error(status))


def test_a_login_the_proxy_cannot_do_means_the_session_is_rejected() -> None:
    """No refresh session in the token: pyezvizapi asks for a password while handling the 401,
    so the status is on the context, not the cause."""
    err = _raised_while_handling(
        _http_error(401), PyEzvizError("Login with account and password required")
    )

    assert err.__cause__ is None
    assert session_rejected(err)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503])
def test_a_cloud_error_does_not_mean_the_session_is_rejected(status: int) -> None:
    assert not session_rejected(_http_error(status))


def test_a_camera_that_does_not_answer_does_not_mean_the_session_is_rejected() -> None:
    assert not session_rejected(DeviceException("camera timeout"))
    assert not session_rejected(PyEzvizError("Could not find VTM server for resource 1"))


def test_a_cloud_error_after_a_renewal_that_worked_is_not_read_as_a_rejection() -> None:
    """The retry after a successful renewal runs inside the 401 handler, so its own failure
    carries the old 401 two links down. Only the failure's own status counts."""
    inner_401 = _ResponseError(401)
    retry_500 = _raised_while_handling(inner_401, _ResponseError(500))
    wrapped = _raised_while_handling(retry_500, HTTPError("HTTP error"))
    wrapped.__cause__ = retry_500

    assert not session_rejected(wrapped)


# -- writing back a session the check itself renewed ------------------------------------------


class _Client:
    """Stands in for EzvizClient in TokenStore: one call, and the token it ends up holding."""

    renew_to: str | None = None
    fail_with: BaseException | None = None
    logins = 0

    def __init__(self, *args: object, token: dict | None = None, url: str = "") -> None:
        self._token = dict(token or {})

    def get_device_infos(self) -> dict:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        if type(self).renew_to is not None:
            self._token["session_id"] = type(self).renew_to
        return {}

    def login(self) -> None:
        type(self).logins += 1
        self._token = {"session_id": "from-login", "rf_session_id": "rf"}

    def export_token(self) -> dict:
        return dict(self._token)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    class Client(_Client):
        pass

    monkeypatch.setattr(token_module, "EzvizClient", Client)
    return Client


def _store(path: Path, session_id: str = "old") -> TokenStore:
    path.write_text(json.dumps({"session_id": session_id, "rf_session_id": "rf"}))
    return TokenStore(path, username="user@example.com", password="secret", region="apiieu")  # noqa: S106 - a test credential


def test_a_session_renewed_during_the_check_is_written_back(
    tmp_path: Path, client: type[_Client]
) -> None:
    client.renew_to = "renewed"
    store = _store(tmp_path / "ezviz_token.json")

    store.ensure()

    assert json.loads(store.path.read_text())["session_id"] == "renewed"
    assert client.logins == 0


def test_a_session_that_still_works_is_left_as_it_is(
    tmp_path: Path, client: type[_Client]
) -> None:
    store = _store(tmp_path / "ezviz_token.json")
    before = store.path.read_text()

    store.ensure()

    assert store.path.read_text() == before
    assert client.logins == 0


def test_a_refused_renewal_without_a_refresh_session_leads_to_a_login(
    tmp_path: Path, client: type[_Client]
) -> None:
    """Before this, the check read "Login with account and password required" as a network
    blip and kept the token: every proxy started on it would then fail, forever."""
    client.fail_with = _raised_while_handling(
        _http_error(401), PyEzvizError("Login with account and password required")
    )
    store = _store(tmp_path / "ezviz_token.json")

    store.ensure()

    assert client.logins == 1
    assert json.loads(store.path.read_text())["session_id"] == "from-login"


def test_a_cloud_error_during_the_check_keeps_the_token(
    tmp_path: Path, client: type[_Client]
) -> None:
    client.fail_with = _http_error(502)
    store = _store(tmp_path / "ezviz_token.json")

    store.ensure()

    assert client.logins == 0
    assert json.loads(store.path.read_text())["session_id"] == "old"
