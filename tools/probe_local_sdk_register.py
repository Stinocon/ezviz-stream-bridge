#!/usr/bin/env python3
"""Test whether the EZVIZ local-SDK registration sequence opens a LAN listener.

pyezvizapi's `open_local_sdk_stream_from_client(..., register_p2p_session=True)`
performs an app-style P2P session registration before querying CAS. The open
question this tool answers is whether that registration is what makes a camera
start listening on its advertised 9010/9020 ports.

The measurement is only meaningful inside a window where the camera is provably
answering us. Battery devices alternate between windows of RST replies and
windows of complete silence, so a bare TIMEOUT means "unreachable right now",
not "no listener". Every cycle therefore waits for an observed RST on the
reference port before firing any cloud call, and re-checks it afterwards.

Probe rate matters too: these devices tolerate roughly 1-2 connections per
second and go silent under bursts, so this tool deliberately probes slowly.

Per cycle:
    1. poll the command port until it answers RST  -> the stack is talking to us
    2. probe both ports                            -> pre-register reference
    3. register_p2p_session, then CAS getDevOperationCode
    4. probe both ports at 1 Hz for --watch seconds
    5. attempt the real local-SDK connect while still inside the window

Read-only: no device configuration is changed.

Usage:
    python3 probe_local_sdk_register.py --serial ABC123456 --host 192.0.2.34 \
        --token-file ezviz_token.json
"""

from __future__ import annotations

import argparse
import errno
import json
import socket
import time
from typing import Any

from pyezvizapi.cas import CasDeviceSession, EzvizCAS
from pyezvizapi.client import EzvizClient
from pyezvizapi.local_stream import open_local_sdk_stream_from_client

COMMAND_PORT = 9010
STREAM_PORT = 9020


def probe(host: str, port: int, timeout: float = 1.5) -> str:
    """Return OPEN (listening) / REFUSED (RST, stack awake) / TIMEOUT (silent)."""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return "OPEN"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as err:
        if err.errno == errno.ECONNREFUSED:
            return "REFUSED"
        return errno.errorcode.get(err.errno or 0, str(err.errno))
    finally:
        sock.close()


def wait_for_window(host: str, deadline: float, interval: float) -> bool:
    """Poll gently until the device answers RST, proving it is reachable now."""
    while time.monotonic() < deadline:
        if probe(host, COMMAND_PORT) == "REFUSED":
            return True
        time.sleep(interval)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", required=True, help="camera LAN IP")
    parser.add_argument("--token-file", default="ezviz_token.json")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--watch", type=float, default=20.0)
    parser.add_argument("--window-timeout", type=float, default=150.0)
    parser.add_argument("--poll-interval", type=float, default=4.0)
    args = parser.parse_args()

    with open(args.token_file, encoding="utf-8") as handle:
        token = json.load(handle)
    client = EzvizClient(token=token)
    client.login()

    started = time.monotonic()
    any_open = False
    inconclusive = 0

    for index in range(1, args.cycles + 1):
        print(f"\n--- cycle {index}: waiting for a responsive window ---", flush=True)
        if not wait_for_window(
            args.host, time.monotonic() + args.window_timeout, args.poll_interval
        ):
            print(f"no responsive window within {args.window_timeout:.0f}s "
                  "-> this cycle proves nothing", flush=True)
            inconclusive += 1
            continue

        def sample(tag: str) -> dict[str, Any]:
            row = {
                "t": time.monotonic() - started,
                str(COMMAND_PORT): probe(args.host, COMMAND_PORT),
                str(STREAM_PORT): probe(args.host, STREAM_PORT),
            }
            print(f"t={row['t']:7.2f} {tag:<16} "
                  f"{COMMAND_PORT}={row[str(COMMAND_PORT)]:<8} "
                  f"{STREAM_PORT}={row[str(STREAM_PORT)]:<8}", flush=True)
            return row

        rows = [sample("pre-register")]

        try:
            meta = client.register_p2p_session().get("meta", {})
            print(f"t={time.monotonic()-started:7.2f} register_p2p_session -> "
                  f"{meta.get('code')}", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"register failed: {type(err).__name__}: {err}", flush=True)

        rows.append(sample("post-register"))

        try:
            session = CasDeviceSession.from_response(
                EzvizCAS(client.export_token()).cas_get_encryption(args.serial)
            )
            print(f"t={time.monotonic()-started:7.2f} CAS ok "
                  f"(encrypt_type={session.encrypt_type})", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"CAS failed: {type(err).__name__}: {err}", flush=True)

        rows.append(sample("post-cas"))

        watch_end = time.monotonic() + args.watch
        while time.monotonic() < watch_end:
            rows.append(sample("watch"))
            time.sleep(1.0)

        print(f"t={time.monotonic()-started:7.2f} attempting the real local-SDK connect",
              flush=True)
        try:
            with open_local_sdk_stream_from_client(
                client, args.serial, register_p2p_session=False, timeout=8.0
            ) as stream:
                first = next(iter(stream.iter_packets(max_packets=1)), None)
                print(f"*** LOCAL SDK MEDIA: {len(first.body) if first else 0} bytes ***",
                      flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"local-SDK attempt: {type(err).__name__}: {err}", flush=True)

        rows.append(sample("post-sdk-attempt"))
        any_open |= any("OPEN" in (row[str(COMMAND_PORT)], row[str(STREAM_PORT)])
                        for row in rows)

    print("\n=== verdict ===")
    if inconclusive == args.cycles:
        print("  every cycle was inconclusive: the device never answered. "
              "Wake it (open the live view) and run again.")
    elif any_open:
        print("  A PORT ACCEPTED A CONNECTION. Stop and analyse before concluding anything.")
    else:
        print("  no port ever accepted, inside windows where the device was provably "
              "answering: the registration does not open a LAN listener.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
