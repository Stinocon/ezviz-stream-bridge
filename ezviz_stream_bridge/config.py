"""Configuration for the bridge, read from the Home Assistant add-on options.

The add-on writes its options to `/data/options.json`. Parsing them here, in one
typed place, keeps the supervisor free of dictionary lookups and gives the user a
single error message per mistake instead of a `KeyError` traceback several
seconds after start-up.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The add-on declares these ports in `config.yaml`, and Home Assistant only allows
# ports that are declared there. Validating the choice against the same range means
# a typo is reported at start-up, instead of turning into a stream that nothing can
# reach because the port was never mapped.
FIRST_PORT = 8558
LAST_PORT = 8562

DEFAULT_REGION = "apiieu.ezvizlife.com"


class ConfigError(Exception):
    """The add-on options cannot be turned into a usable configuration."""


@dataclass(frozen=True)
class CameraConfig:
    """One camera to serve, and the port its MPEG-TS stream is served on."""

    serial: str
    port: int

    @property
    def path(self) -> str:
        """HTTP path `pyezvizapi stream proxy` serves for this camera."""
        return f"/{self.serial}.ts"


@dataclass(frozen=True)
class BridgeConfig:
    """Everything the supervisor needs for a run."""

    username: str
    password: str
    region: str
    cameras: tuple[CameraConfig, ...]
    log_level: str
    log_ffmpeg_stderr: bool = False
    # Seconds the bridge reads ahead for the camera's audio before it starts FFmpeg. None means
    # the option was not set at all, and the proxy's own default applies: the add-on always
    # writes it because `config.yaml` declares it, but a hand-written options.json need not.
    audio_window: float | None = None

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> BridgeConfig:
        """Build a configuration from parsed add-on options."""
        username = str(options.get("username") or "").strip()
        password = str(options.get("password") or "")
        if not username or not password:
            raise ConfigError(
                "username and password are required: the EZVIZ cloud is what hands "
                "out the stream tokens, so the bridge cannot start without an account."
            )

        region = str(options.get("region") or "").strip() or DEFAULT_REGION

        raw_cameras = options.get("cameras")
        if not isinstance(raw_cameras, list) or not raw_cameras:
            raise ConfigError(
                "at least one camera is required. Add one under `cameras` with its "
                "serial, which is printed on the device label and shown in the EZVIZ app."
            )

        cameras = tuple(cls._camera(entry, index) for index, entry in enumerate(raw_cameras))

        serials = [camera.serial for camera in cameras]
        if len(set(serials)) != len(serials):
            raise ConfigError(f"the same serial is listed more than once: {serials}")

        ports = [camera.port for camera in cameras]
        if len(set(ports)) != len(ports):
            raise ConfigError(
                f"two cameras are configured on the same port: {ports}. Each camera "
                "needs its own, because each one is served by its own process."
            )

        return cls(
            username=username,
            password=password,
            region=region,
            cameras=cameras,
            log_level=str(options.get("log_level") or "info").lower(),
            log_ffmpeg_stderr=cls._as_bool(options.get("log_ffmpeg_stderr")),
            audio_window=cls._as_seconds(options.get("audio_window"), "audio_window"),
        )

    @staticmethod
    def _as_seconds(value: Any, option: str) -> float | None:
        """Read a duration option, which may arrive as a number or as its text form.

        None and the empty string both mean "not set", and the caller's own default then
        applies. A negative value is refused here rather than silently clamped, because the
        only honest reading of a negative window is that the option was misunderstood.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"{option} is not a number of seconds: {value!r}") from err
        if not math.isfinite(seconds) or seconds < 0:
            # `nan` and `inf` parse as floats and then disarm every comparison they are used in,
            # which here would turn a bounded wait into an unbounded one -- silently, and from
            # a value no operator meant to write.
            raise ConfigError(f"{option} is not a finite number of seconds: {value!r}")
        return seconds

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """Read a boolean option that may arrive as a bool or as its text form.

        Home Assistant's JSON gives a real boolean, but the option is also documented
        for hand-written `options.json`, where `"false"` is the mistake that would
        otherwise read as true.
        """
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _camera(entry: Any, index: int) -> CameraConfig:
        """Validate one entry of the `cameras` list."""
        if not isinstance(entry, dict):
            raise ConfigError(f"camera #{index + 1} is not a mapping: {entry!r}")

        serial = str(entry.get("serial") or "").strip().upper()
        if not serial:
            raise ConfigError(f"camera #{index + 1} has no serial.")

        # Serials are uppercase letters and digits. Rejecting the rest here catches the
        # common mistake of pasting the verification code (or a whole URL) into the
        # field, which would otherwise surface as an opaque cloud error per camera.
        if not serial.isalnum():
            raise ConfigError(
                f"{serial!r} does not look like a serial: expected letters and digits "
                "only. The serial is on the device label, not the 6-letter verification code."
            )

        raw_port = entry.get("port", FIRST_PORT + index)
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as err:
            raise ConfigError(f"camera {serial}: port {raw_port!r} is not a number.") from err

        if not FIRST_PORT <= port <= LAST_PORT:
            raise ConfigError(
                f"camera {serial}: port {port} is outside {FIRST_PORT}-{LAST_PORT}. "
                "Home Assistant only routes the ports the add-on declares, so a port "
                "outside that range would serve a stream nothing could reach."
            )

        return CameraConfig(serial=serial, port=port)


def load_options(path: Path) -> dict[str, Any]:
    """Read and parse the add-on options file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise ConfigError(f"options file {path} does not exist.") from err
    except OSError as err:
        raise ConfigError(f"cannot read options file {path}: {err}") from err

    try:
        options = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigError(f"options file {path} is not valid JSON: {err}") from err

    if not isinstance(options, dict):
        raise ConfigError(f"options file {path} does not contain an object.")
    return options
