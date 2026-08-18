"""One logging setup, shared by the supervisor and the proxy.

Timestamps are the point of this module. A connection log is only useful when it can
be lined up against Frigate's, go2rtc's and Home Assistant's own logs, and that means
knowing when something happened to better than a second. Local time with an explicit
UTC offset, to the millisecond, makes the comparison a subtraction rather than a
conversion.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

# Home Assistant add-on log levels, mapped onto logging's.
_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def level_for(name: str) -> int:
    """Return the logging level for an add-on log level name, INFO if unknown."""
    return _LEVELS.get(name.strip().lower(), logging.INFO)


class IsoFormatter(logging.Formatter):
    """Formats times as `2026-08-18T09:03:12.481+02:00`."""

    def formatTime(  # noqa: N802 - name fixed by logging.Formatter
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        return (
            datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")
        )


def configure_logging(level: int | str) -> None:
    """Send logs to stdout, which is where the add-on log looks.

    `force` matters: this is called twice on purpose -- once at a safe default so a
    configuration error is reported at all, then again at the configured level. Without
    it the second call is a no-op, because `basicConfig` returns early once the root
    logger has a handler, and the configured level would never take effect.
    """
    numeric = level_for(level) if isinstance(level, str) else level
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(IsoFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.basicConfig(level=numeric, handlers=[handler], force=True)
