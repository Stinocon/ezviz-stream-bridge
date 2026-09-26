"""EZVIZ Stream Bridge: EZVIZ camera video and audio as local MPEG-TS for go2rtc."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth for the version; reading it back from
    # the installed metadata is what keeps the two from drifting. The add-on Dockerfile
    # prints this to confirm the image carries the version it claims.
    __version__ = version("ezviz-stream-bridge")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
