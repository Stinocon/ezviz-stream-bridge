"""Listing the cameras on an EZVIZ account, to help fill in the ``serial`` field.

The serial is the one value a user cannot invent, and the add-on already holds the
credentials that can look it up. So rather than send someone to hunt for a label,
the missing-serial error lists what the account actually has. That turns the one
dead-end in configuration into a self-service step: enter the credentials, leave the
serial blank, read the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CameraListing:
    """One device on the account, as much as is useful for picking a serial."""

    serial: str
    name: str
    model: str

    def describe(self) -> str:
        """One human line: serial, then the name and model that identify it."""
        label = self.name or "(unnamed)"
        model = f" [{self.model}]" if self.model else ""
        return f"{self.serial}  {label}{model}"


def parse_camera_listings(device_infos: Any) -> list[CameraListing]:
    """Turn a pagelist ``device_infos`` mapping into a sorted list of devices.

    Kept pure and separate from the network call so it can be tested against a
    captured response. The shape is ``{serial: {"deviceInfos": {...}, ...}}``; a
    device without a usable ``deviceInfos`` block is skipped rather than guessed at.
    """
    if not isinstance(device_infos, dict):
        return []

    listings: list[CameraListing] = []
    for serial, device in device_infos.items():
        if not isinstance(serial, str) or not isinstance(device, dict):
            continue
        info = device.get("deviceInfos")
        if not isinstance(info, dict):
            continue
        listings.append(
            CameraListing(
                serial=serial,
                name=str(info.get("name") or "").strip(),
                model=str(info.get("deviceType") or "").strip(),
            )
        )

    listings.sort(key=lambda entry: entry.serial)
    return listings


def list_account_cameras(client: Any) -> list[CameraListing]:
    """Fetch and parse the account's devices through an authenticated client."""
    return parse_camera_listings(client.get_device_infos())
