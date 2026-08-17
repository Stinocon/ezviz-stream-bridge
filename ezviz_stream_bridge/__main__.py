"""Entry point: `python -m ezviz_stream_bridge` / the `ezviz-stream-bridge` command."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import BridgeConfig, ConfigError, load_options
from .supervisor import Supervisor, configure_logging
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
        config = BridgeConfig.from_options(load_options(args.options))
    except ConfigError as err:
        _LOGGER.error("Configuration problem: %s", err)
        return 1

    configure_logging(config.log_level)

    tokens = TokenStore(
        args.token_file,
        username=config.username,
        password=config.password,
        region=config.region,
    )

    return Supervisor(config, tokens).run()


if __name__ == "__main__":
    raise SystemExit(main())
