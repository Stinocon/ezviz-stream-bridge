"""Tests for turning add-on options into a configuration."""

from __future__ import annotations

import json

import pytest

from ezviz_stream_bridge.config import (
    DEFAULT_REGION,
    FIRST_PORT,
    BridgeConfig,
    ConfigError,
    load_options,
)


def _options(**overrides: object) -> dict[str, object]:
    options: dict[str, object] = {
        "username": "user@example.com",
        "password": "secret",
        "cameras": [{"serial": "BB1234567"}],
    }
    options.update(overrides)
    return options


def test_defaults_are_filled_in() -> None:
    config = BridgeConfig.from_options(_options())

    assert config.region == DEFAULT_REGION
    assert config.log_level == "info"
    assert len(config.cameras) == 1
    assert config.cameras[0].serial == "BB1234567"
    assert config.cameras[0].port == FIRST_PORT
    assert config.cameras[0].path == "/BB1234567.ts"


def test_ports_are_assigned_in_order_when_omitted() -> None:
    config = BridgeConfig.from_options(
        _options(cameras=[{"serial": "AAA111"}, {"serial": "BBB222"}])
    )

    assert [camera.port for camera in config.cameras] == [FIRST_PORT, FIRST_PORT + 1]


def test_serial_is_normalised_to_uppercase() -> None:
    config = BridgeConfig.from_options(_options(cameras=[{"serial": " bb1234567 "}]))

    assert config.cameras[0].serial == "BB1234567"


@pytest.mark.parametrize("missing", ["username", "password"])
def test_credentials_are_required(missing: str) -> None:
    with pytest.raises(ConfigError, match="required"):
        BridgeConfig.from_options(_options(**{missing: ""}))


def test_at_least_one_camera_is_required() -> None:
    with pytest.raises(ConfigError, match="at least one camera"):
        BridgeConfig.from_options(_options(cameras=[]))


def test_verification_code_pasted_as_serial_is_rejected() -> None:
    # The 6-letter verification code is the thing users reach for first, and it is not
    # a serial. Caught here it is one clear message instead of an opaque cloud error.
    with pytest.raises(ConfigError, match="does not look like a serial"):
        BridgeConfig.from_options(_options(cameras=[{"serial": "WTT-NZU"}]))


def test_duplicate_serials_are_rejected() -> None:
    with pytest.raises(ConfigError, match="more than once"):
        BridgeConfig.from_options(
            _options(cameras=[{"serial": "AAA111"}, {"serial": "AAA111"}])
        )


def test_duplicate_ports_are_rejected() -> None:
    with pytest.raises(ConfigError, match="same port"):
        BridgeConfig.from_options(
            _options(
                cameras=[
                    {"serial": "AAA111", "port": FIRST_PORT},
                    {"serial": "BBB222", "port": FIRST_PORT},
                ]
            )
        )


def test_port_outside_the_declared_range_is_rejected() -> None:
    with pytest.raises(ConfigError, match="outside"):
        BridgeConfig.from_options(
            _options(cameras=[{"serial": "AAA111", "port": 9999}])
        )


def test_non_numeric_port_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not a number"):
        BridgeConfig.from_options(
            _options(cameras=[{"serial": "AAA111", "port": "eight"}])
        )


def test_missing_options_file_is_reported_clearly(tmp_path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_options(tmp_path / "absent.json")


def test_broken_options_file_is_reported_clearly(tmp_path) -> None:
    path = tmp_path / "options.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_options(path)


def test_round_trip_through_a_file(tmp_path) -> None:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(_options(region="apius.ezvizlife.com")), encoding="utf-8")

    config = BridgeConfig.from_options(load_options(path))

    assert config.region == "apius.ezvizlife.com"
