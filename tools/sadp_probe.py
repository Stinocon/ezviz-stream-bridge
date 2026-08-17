#!/usr/bin/env python3
"""Probe the CP4 with Hikvision/EZVIZ SADP discovery and UDP signaling ports.

EZVIZ's own "Network Open Port List and Usage Specification" states that Video
Door Viewers open SADP Discovery on UDP 37020 (multicast 239.255.255.250) and
EZVIZ Signaling on UDP 9035. Both are read-only probes: SADP is a discovery
query, and the UDP signaling probe only sends a short opaque payload.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import sys
import time

if len(sys.argv) < 2:
    raise SystemExit(f"usage: {sys.argv[0]} <camera-ip>")
TARGET = sys.argv[1]
SADP_GROUP = "239.255.255.250"
SADP_PORT = 37020

SADP_INQUIRY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b"<Probe><Uuid>0FA1BE00-0000-0000-0000-000000000000</Uuid>"
    b"<Types>inquiry</Types></Probe>"
)


def _bind_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("0.0.0.0", 37020))
    mreq = struct.pack("4sl", socket.inet_aton(SADP_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)
    return sock


def sadp_discovery(rounds: int = 6) -> None:
    """Send SADP inquiries and print every responder on the LAN."""
    print(f"--- SADP discovery (UDP {SADP_PORT}, multicast {SADP_GROUP}) ---")
    try:
        sock = _bind_socket()
    except OSError as err:
        print(f"cannot bind UDP 37020: {err}")
        return

    seen: dict[str, bytes] = {}
    for _ in range(rounds):
        for dest in (SADP_GROUP, TARGET, "255.255.255.255"):
            # A destination this host has no route to is expected: all three are tried
            # precisely because it is not known which one the device answers on.
            with contextlib.suppress(OSError):
                sock.sendto(SADP_INQUIRY, (dest, SADP_PORT))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(8192)
            except TimeoutError:
                break
            except OSError:
                break
            if addr[0] not in seen:
                seen[addr[0]] = data
                tag = "  <== TARGET" if addr[0] == TARGET else ""
                print(f"\nreply from {addr[0]}:{addr[1]}  ({len(data)} bytes){tag}")
                try:
                    print(data.decode("utf-8", "replace")[:1500])
                except Exception:
                    print(data[:400].hex())
    sock.close()

    if TARGET in seen:
        print(f"\nRESULT: {TARGET} ANSWERED SADP -> local control plane is alive.")
    elif seen:
        print(f"\nRESULT: {TARGET} silent; other SADP devices: {sorted(seen)}")
    else:
        print("\nRESULT: no SADP replies at all on this LAN.")


def udp_signaling_probe() -> None:
    """Check whether UDP 9035 / 37020 answer a short opaque datagram."""
    print(f"\n--- UDP unicast probes to {TARGET} ---")
    for port in (9035, 37020, 554):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3.0)
        try:
            sock.sendto(b"\x00\x00\x00\x01", (TARGET, port))
            data, _ = sock.recvfrom(4096)
            print(f"UDP {port}: REPLY {len(data)} bytes: {data[:80].hex()}")
        except TimeoutError:
            print(f"UDP {port}: no reply (open|filtered — normal for UDP)")
        except OSError as err:
            print(f"UDP {port}: {err.strerror} (ICMP port-unreachable => closed)")
        finally:
            sock.close()


if __name__ == "__main__":
    sadp_discovery()
    udp_signaling_probe()
