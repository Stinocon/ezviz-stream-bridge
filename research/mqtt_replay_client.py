#!/usr/bin/env python3
"""Minimal raw-TCP MQTT CONNECT replayer.

Sends an exact captured MQTT CONNECT byte sequence to a broker over plain TCP
and records the response. It uses NO MQTT library: the bytes are transmitted
verbatim so the broker sees precisely what was captured, with nothing
reconstructed or re-encoded. This is a single controlled replay to one already
observed broker, for the specific test of whether an accepted CONNECT is
accepted again on replay.

Usage:
    python3 mqtt_replay_client.py --host <broker-ip> --port 8820 --connect-hex-file conn.hex
"""

from __future__ import annotations

import argparse
import socket
import time


def decode_connack(data: bytes) -> str:
    if len(data) >= 4 and data[0] == 0x20 and data[1] == 0x02:
        session_present = data[2] & 0x01
        rc = data[3]
        names = {0x00: "ACCEPTED", 0x01: "unacceptable protocol",
                 0x02: "id rejected", 0x03: "server unavailable",
                 0x04: "bad user/pass", 0x05: "not authorized"}
        return (f"CONNACK session_present={session_present} return_code=0x{rc:02x} "
                f"({names.get(rc, 'non-standard/vendor')})")
    return f"non-CONNACK response: {data[:16].hex()}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=8820)
    ap.add_argument("--connect-hex-file", required=True,
                    help="file with the CONNECT packet as one hex string")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    payload = bytes.fromhex(open(args.connect_hex_file).read().strip())
    assert payload[0] == 0x10, "not an MQTT CONNECT (first byte != 0x10)"
    print(f"replaying {len(payload)}B CONNECT to {args.host}:{args.port}", flush=True)

    t0 = time.monotonic()
    sock = socket.socket()
    sock.settimeout(args.timeout)
    events = []

    def mark(tag: str) -> None:
        events.append((round(time.monotonic() - t0, 4), tag))
        print(f"  t=+{events[-1][0]:.4f}s  {tag}", flush=True)

    try:
        sock.connect((args.host, args.port))
        mark("TCP connected (SYN/SYN-ACK/ACK ok)")
        sock.sendall(payload)
        mark(f"sent CONNECT {len(payload)}B")
        resp = sock.recv(256)
        mark(f"recv {len(resp)}B: {resp.hex()}")
        mark(decode_connack(resp))
        # watch what happens next: does the broker keep the connection or drop it?
        try:
            more = sock.recv(256)
            mark(f"after CONNACK, broker sent {len(more)}B: {more.hex()}"
                 if more else "after CONNACK, broker closed (empty recv = FIN)")
        except TimeoutError:
            mark("after CONNACK, no further data (connection held open)")
    except OSError as err:
        mark(f"error: {type(err).__name__}: {err}")
    finally:
        sock.close()
        mark("closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
