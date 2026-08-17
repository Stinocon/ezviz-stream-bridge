"""Keeping a usable EZVIZ session token on disk.

Why this exists at all, rather than handing the credentials to
`pyezvizapi stream proxy` and letting it log in: the CLI takes the password as an
argument, which puts it in the process table for anything running in the container,
and in any crash dump that captures a command line. Doing the login here means the
password stays in this process's memory, and the proxies only ever receive
`--token-file`.

The cost of that choice is that nothing else refreshes the token, so this module
owns expiry: `ensure()` is called before every proxy start, verifies the stored
token against the cloud, and logs in again when it has stopped working.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    PyEzvizError,
)

_LOGGER = logging.getLogger(__name__)


class MfaRequired(Exception):
    """The account asks for a verification code, which an add-on cannot answer.

    Raised as its own type because it is the one failure the user has to act on
    rather than wait out, and the remedy is different from every other login error.
    """


class TokenStore:
    """Loads, validates and refreshes the EZVIZ session token."""

    def __init__(
        self,
        path: Path,
        *,
        username: str,
        password: str,
        region: str,
    ) -> None:
        self._path = path
        self._username = username
        self._password = password
        self._region = region

    @property
    def path(self) -> Path:
        """Where the token is stored."""
        return self._path

    def ensure(self) -> None:
        """Guarantee that `path` holds a token the cloud currently accepts."""
        stored = self._load()
        if stored is not None and self._works(stored):
            _LOGGER.debug("Stored EZVIZ token still works.")
            return

        if stored is None:
            _LOGGER.info("No stored EZVIZ token: logging in.")
        else:
            _LOGGER.info("Stored EZVIZ token no longer works: logging in again.")
        self._login()

    def _load(self) -> dict[str, Any] | None:
        """Return the stored token, or None when there is nothing usable."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as err:
            # A truncated token file is not worth a hard failure: a fresh login
            # recovers, and refusing to start would need a manual delete to fix.
            _LOGGER.warning("Ignoring unreadable token file %s: %s", self._path, err)
            return None

        if not isinstance(data, dict) or not data.get("session_id"):
            _LOGGER.warning("Token file %s has no session: ignoring it.", self._path)
            return None
        return data

    def _works(self, token: dict[str, Any]) -> bool:
        """Check the token against the cloud with one cheap authenticated call.

        The client is built without credentials on purpose. With them, `pyezvizapi`
        answers a 401 by logging in again by itself and the call would succeed, so
        this would report a dead token as alive and we would go on writing a stale
        file back to disk.
        """
        client = EzvizClient(token=dict(token), url=self._region)
        try:
            client.get_device_infos()
        except (EzvizAuthTokenExpired, HTTPError):
            return False
        except PyEzvizError as err:
            # Anything else -- the cloud being briefly unhappy, a network blip -- is
            # not evidence that the token is bad. Logging in again on every such
            # error would burn logins during an outage, and EZVIZ rate-limits those.
            _LOGGER.warning("Could not verify the stored token (%s): keeping it.", err)
            return True
        else:
            return True

    def _login(self) -> None:
        """Log in with the configured credentials and store the new token."""
        client = EzvizClient(self._username, self._password, self._region)
        try:
            client.login()
        except EzvizAuthVerificationCode as err:
            raise MfaRequired(
                "the EZVIZ account asked for a verification code. An add-on cannot "
                "type one in, so either turn off two-factor authentication for this "
                "account, or log in once with `pyezvizapi -u ... -p ... --save-token "
                f"--token-file {self._path}` and copy the resulting file into place."
            ) from err
        except PyEzvizError as err:
            raise RuntimeError(f"EZVIZ login failed: {err}") from err

        self._write(client.export_token())
        _LOGGER.info("Logged in to EZVIZ as %s.", self._username)

    def _write(self, token: dict[str, Any]) -> None:
        """Write the token so a crash mid-write cannot leave a corrupt file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(token), encoding="utf-8")
        # The token is a bearer credential for the whole EZVIZ account, not just this
        # camera: it stays readable by its owner only.
        os.chmod(temporary, 0o600)
        temporary.replace(self._path)
