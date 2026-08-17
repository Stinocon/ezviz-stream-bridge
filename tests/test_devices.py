"""Tests for turning an EZVIZ device_infos response into a pickable camera list."""

from __future__ import annotations

from ezviz_stream_bridge.devices import parse_camera_listings


def test_parses_serial_name_and_model() -> None:
    infos = {
        "BB1234567": {
            "deviceInfos": {
                "name": "Front door",
                "deviceType": "CS-CP4-R100-6E2WPFBS",
                "deviceSubCategory": "CP4",
            }
        }
    }

    (camera,) = parse_camera_listings(infos)

    assert camera.serial == "BB1234567"
    assert camera.name == "Front door"
    assert camera.model == "CS-CP4-R100-6E2WPFBS"
    assert camera.describe() == "BB1234567  Front door [CS-CP4-R100-6E2WPFBS]"


def test_result_is_sorted_by_serial() -> None:
    infos = {
        "CCC333": {"deviceInfos": {"name": "c"}},
        "AAA111": {"deviceInfos": {"name": "a"}},
        "BBB222": {"deviceInfos": {"name": "b"}},
    }

    serials = [camera.serial for camera in parse_camera_listings(infos)]

    assert serials == ["AAA111", "BBB222", "CCC333"]


def test_a_device_without_deviceinfos_is_skipped() -> None:
    infos = {
        "AAA111": {"deviceInfos": {"name": "ok"}},
        "BROKEN": {"CONNECTION": {}},  # a real payload sometimes lacks deviceInfos
    }

    serials = [camera.serial for camera in parse_camera_listings(infos)]

    assert serials == ["AAA111"]


def test_missing_name_and_model_still_describe_cleanly() -> None:
    (camera,) = parse_camera_listings({"AAA111": {"deviceInfos": {}}})

    assert camera.name == ""
    assert camera.model == ""
    assert camera.describe() == "AAA111  (unnamed)"


def test_non_mapping_input_is_empty() -> None:
    assert parse_camera_listings(None) == []
    assert parse_camera_listings([]) == []
