#!/usr/bin/env python3
"""Decide whether an EZVIZ live view is a LAN-local P2P flow or a cloud relay.

The physical constraint: on Wi-Fi a laptop cannot see the unicast traffic between two
other stations -- the access point does not reflect it. So the video flow itself is
invisible from a third host. But ARP is broadcast, and two hosts that are about to
exchange unicast IP MUST first resolve each other's MAC. That resolution IS visible.

So the tell is: during a live view opened on a phone, does the phone ARP-resolve the
camera? If yes, they are about to talk directly -> the stream is on the LAN. If instead
the camera only ever resolves the gateway, its packets leave through the router -> the
stream is going to the cloud, and no local interception can reach video that never
crosses the LAN.

Run the capture on a laptop on the same Wi-Fi (needs sudo); open the live view on a
PHONE, not the laptop, because the phone's ARP cache is cold toward the camera and it
is therefore forced to resolve it if it talks to it directly:

    sudo tcpdump -ni <iface> -e -s0 -w p2p.pcap \
        'arp or ether proto 0x8d8d or multicast or broadcast'

Then:

    python3 p2p_arp_analyze.py p2p.pcap <camera-mac> <camera-ip>
"""

from __future__ import annotations

import struct
import sys
from collections import defaultdict

ETHERTYPE_ARP = 0x0806
ETHERTYPE_EZVIZ_L2 = 0x8D8D
ARP_REQUEST = 1


def read_pcap(path: str) -> list[tuple[float, bytes]]:
    """Return (timestamp, frame) pairs from a classic pcap file."""
    with open(path, "rb") as handle:
        blob = handle.read()

    magic = blob[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian, nano = "<", magic == b"\x4d\x3c\xb2\xa1"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian, nano = ">", magic == b"\xa1\xb2\x3c\x4d"
    else:
        raise SystemExit(f"not a classic pcap (magic {magic.hex()})")

    frames: list[tuple[float, bytes]] = []
    offset = 24
    while offset + 16 <= len(blob):
        ts_sec, ts_frac, caplen, _ = struct.unpack_from(endian + "IIII", blob, offset)
        offset += 16
        ts = ts_sec + ts_frac / (1_000_000_000 if nano else 1_000_000)
        frames.append((ts, blob[offset : offset + caplen]))
        offset += caplen
    return frames


def parse_arp(frame: bytes) -> dict[str, object] | None:
    """Return the fields of an ARP packet, or None if this is not one."""
    if len(frame) < 14 + 28:
        return None
    if struct.unpack_from("!H", frame, 12)[0] != ETHERTYPE_ARP:
        return None
    return {
        "oper": struct.unpack_from("!H", frame, 20)[0],
        "sender_mac": ":".join(f"{b:02x}" for b in frame[22:28]),
        "sender_ip": ".".join(str(b) for b in frame[28:32]),
        "target_ip": ".".join(str(b) for b in frame[38:42]),
    }


def _detect_gateway(requests: list[dict[str, object]]) -> str | None:
    """Guess the gateway: the address the most distinct stations resolve.

    Everything on the subnet ARPs the gateway to reach the internet, so it is the IP
    with the widest set of distinct requesters. Detected rather than configured so the
    tool carries no site-specific addresses.
    """
    resolvers: dict[str, set[str]] = defaultdict(set)
    for arp in requests:
        resolvers[str(arp["target_ip"])].add(str(arp["sender_ip"]))
    if not resolvers:
        return None
    gateway, who = max(resolvers.items(), key=lambda kv: len(kv[1]))
    return gateway if len(who) >= 2 else None


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit(f"usage: {sys.argv[0]} <p2p.pcap> <camera-mac> <camera-ip>")
    path, camera_mac, camera_ip = sys.argv[1], sys.argv[2].lower(), sys.argv[3]

    frames = read_pcap(path)
    if not frames:
        raise SystemExit("empty capture")
    t0 = frames[0][0]
    span = frames[-1][0] - t0

    requests = [a for _, f in frames if (a := parse_arp(f)) and a["oper"] == ARP_REQUEST]
    ez_frames = sum(
        1
        for _, f in frames
        if len(f) >= 14 and struct.unpack_from("!H", f, 12)[0] == ETHERTYPE_EZVIZ_L2
    )
    gateway = _detect_gateway(requests)

    print(f"Capture: {len(frames)} frames over {span:.0f}s")
    print(f"Camera: {camera_mac} / {camera_ip}")
    print(f"Gateway (detected): {gateway or 'unknown'}")
    print(f"EZVIZ 0x8d8d discovery frames: {ez_frames}\n")

    # Single pass over the frames, recording the first time each thing was seen. A
    # station's own gratuitous ARP (sender == target) is self-announcement, not a
    # resolution of the camera, so it is excluded from "who resolved the camera".
    resolved_camera: dict[str, float] = {}
    camera_targets: dict[str, float] = {}
    for ts, frame in frames:
        arp = parse_arp(frame)
        if arp is None or arp["oper"] != ARP_REQUEST:
            continue
        rel = ts - t0
        sender_ip = str(arp["sender_ip"])
        target_ip = str(arp["target_ip"])

        if target_ip == camera_ip and sender_ip not in (camera_ip, "0.0.0.0"):
            resolved_camera.setdefault(sender_ip, rel)

        if arp["sender_mac"] == camera_mac and target_ip not in (camera_ip, gateway):
            camera_targets.setdefault(target_ip, rel)

    if resolved_camera:
        print("Stations that resolved the CAMERA (about to talk to it directly):")
        for ip, rel in sorted(resolved_camera.items(), key=lambda kv: kv[1]):
            print(f"  {ip:<16} first at t+{rel:.1f}s")
    else:
        print("No station resolved the camera (only its own gratuitous ARP, if any).")

    print()
    if camera_targets:
        print("The CAMERA resolved these non-gateway hosts (wanted to reach them directly):")
        for ip, rel in sorted(camera_targets.items(), key=lambda kv: kv[1]):
            print(f"  {ip:<16} first at t+{rel:.1f}s")
    else:
        camera_asked_gateway = any(
            a["sender_mac"] == camera_mac and a["target_ip"] == gateway for a in requests
        )
        where = "only the gateway" if camera_asked_gateway else "nothing"
        print(f"The camera resolved {where}: its traffic is heading off the subnet.")

    print("\n================ VERDICT ================")
    if set(resolved_camera) & set(camera_targets):
        print("LOCAL P2P: camera and a station resolved EACH OTHER. The video path is on")
        print("the LAN, and capturing that flow is worth pursuing.")
    elif resolved_camera or camera_targets:
        print("PARTIAL: one side resolved the other but not mutually within the window.")
        print("Re-run keeping the live view open the whole time; ARP entries may be cached.")
    else:
        print("NO LOCAL P2P: no station resolved the camera, and the camera resolved only")
        print("the gateway. The stream leaves through the router -> cloud path. Local")
        print("interception cannot reach video that never traverses the LAN.")
        print("\nNote: run this with the live view opened on a PHONE, not the capturing")
        print("laptop. The laptop's ARP cache may be warm toward the camera from earlier")
        print("probing, which would hide a resolution that did happen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
