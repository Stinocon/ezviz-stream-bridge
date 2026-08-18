"""Entry point: `python -m ezviz_stream_bridge` / the `ezviz-stream-bridge` command."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from pyezvizapi.client import EzvizClient

from .config import DEFAULT_REGION, BridgeConfig, ConfigError, load_options
from .devices import list_account_cameras
from .log import configure_logging
from .supervisor import Supervisor
from .token import TokenStore

_LOGGER = logging.getLogger(__name__)

# Home Assistant writes the add-on options here, and /data is the add-on's persistent
# volume, so the token survives restarts and updates.
DEFAULT_OPTIONS = Path("/data/options.json")
DEFAULT_TOKEN = Path("/data/ezviz_token.json")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ezviz-stream-bridge",
        description=(
            "Serve EZVIZ camera video as local MPEG-TS over HTTP, for go2rtc, "
            "Frigate and anything else that speaks FFmpeg."
        ),
    )
    parser.add_argument(
        "--options",
        type=Path,
        default=DEFAULT_OPTIONS,
        help=f"Home Assistant add-on options file (default: {DEFAULT_OPTIONS})",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN,
        help=f"where the EZVIZ session token is kept (default: {DEFAULT_TOKEN})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Load configuration, then supervise one proxy per camera."""
    args = _parse_args(argv)

    # Logging is set up twice on purpose: once at a safe default so a configuration
    # error is reported at all, then again at the configured level. Without the first
    # call, a bad options file would fail silently before the level was ever known.
    configure_logging("info")

    try:
        options = load_options(args.options)
    except ConfigError as err:
        _LOGGER.error("Configuration problem: %s", err)
        return 1

    try:
        config = BridgeConfig.from_options(options)
    except ConfigError as err:
        _LOGGER.error("Configuration problem: %s", err)
        # The serial is the one field a user cannot make up, and it is the usual reason
        # to land here. If the credentials are usable, listing the account's cameras
        # answers "which serial?" without sending anyone to hunt a label.
        _log_available_cameras(options, args.token_file)
        return 1

    configure_logging(config.log_level)

    tokens = TokenStore(
        args.token_file,
        username=config.username,
        password=config.password,
        region=config.region,
    )

    return Supervisor(config, tokens).run()


def _log_available_cameras(options: dict[str, Any], token_file: Path) -> None:
    """Best-effort: log the account's cameras to help fill in a missing serial.

    Never raises. This runs on an error path that is already returning failure, so a
    problem here must not replace the real configuration error with a stack trace --
    it just means the extra help is unavailable this time.
    """
    try:
        client = _client_for_listing(options, token_file)
        if client is None:
            return
        cameras = list_account_cameras(client)
    except Exception as err:  # noqa: BLE001 - a hint must never crash the error path
        _LOGGER.debug("Could not list account cameras for the hint: %s", err)
        return

    if not cameras:
        return

    _LOGGER.error("Cameras on this EZVIZ account -- copy a serial into the configuration:")
    for camera in cameras:
        _LOGGER.error("  %s", camera.describe())


def _client_for_listing(options: dict[str, Any], token_file: Path) -> EzvizClient | None:
    """Build a client for the camera-listing hint, or None if it cannot be built.

    A stored token is preferred over a fresh login: it needs no password and works even
    for accounts whose two-factor prompt blocks a new login. Only if there is no usable
    token does it fall back to the configured credentials.
    """
    region = str(options.get("region") or "").strip() or DEFAULT_REGION

    token = _load_token(token_file)
    if token is not None:
        return EzvizClient(token=token, url=region)

    username = str(options.get("username") or "").strip()
    password = str(options.get("password") or "")
    if not username or not password:
        return None

    client = EzvizClient(username, password, region)
    client.login()
    return client


def _load_token(token_file: Path) -> dict[str, Any] | None:
    """Return a stored token that at least carries a session, or None."""
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("session_id"):
        return data
    return None


if __name__ == "__main__":
    raise SystemExit(main())
