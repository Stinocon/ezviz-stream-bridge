#!/usr/bin/env python3
"""Probe every locally-documented CP4 service while the device is awake.

EZVIZ's "Network Open Port List and Usage Specification" lists these services for
Video Door Viewers: HIKSDK TCP 8000/8443, EZVIZ Signaling TCP 9010/9020 + UDP 9035,
SADP UDP 37020, Device Interconnection TCP 50100 + UDP 50160/50161/50162.

TCP is unambiguous: OPEN / clsd (RST, stack awake) / filt (silence, asleep).

UDP cannot be probed directly -- silence means "open or filtered". So we also probe
a control port that is certainly closed. If the control answers ICMP port-unreachable
("clsd") while a real port stays silent, that silence is evidence the port IS open.
If the control is silent too, the whole UDP column is inconclusive.

Usage:
    python3 probe_local_ports.py <camera-ip> [seconds]
"""

from __future__ import annotations

import errno
import socket
import sys
import time

if len(sys.argv) < 2:
    raise SystemExit(f"usage: {sys.argv[0]} <camera-ip> [seconds]")
HOST = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

TCP_PORTS = (9010, 9020, 8000, 8443, 50100, 554)
UDP_PORTS = (50160, 50161, 50162, 9035, 37020)
UDP_CONTROL = 50999  # no service is documented here: must come back closed

TCP_TIMEOUT = 1.2
UDP_TIMEOUT = 1.0


def probe_tcp(host: str, port: int) -> str:
    """Return OPEN (listening) / clsd (RST) / filt (no reply)."""
    sock = socket.socket()
    sock.settimeout(TCP_TIMEOUT)
    try:
        sock.connect((host, port))
    except socket.timeout:
        return "filt"
    except OSError as err:
        if err.errno == errno.ECONNREFUSED:
            return "clsd"
        return errno.errorcode.get(err.errno, str(err.errno))
    else:
        return "OPEN"
    finally:
        sock.close()


def probe_udp(host: str, port: int) -> tuple[str, bytes | None]:
    """Return clsd (ICMP unreachable) / RPLY (answered) / sil. (silent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(UDP_TIMEOUT)
    try:
        sock.sendto(b"\x00\x00\x00\x00\x00\x00\x00\x00", (host, port))
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        return "sil.", None
    except OSError as err:
        if err.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH):
            return "clsd", None
        return errno.errorcode.get(err.errno, str(err.errno)), None
    else:
        return "RPLY", data
    finally:
        sock.close()


def main() -> int:
    all_udp = (*UDP_PORTS, UDP_CONTROL)
    print(f"Probing {HOST} for {DURATION:.0f}s")
    print("TCP:  OPEN=listening  clsd=RST(awake)  filt=silent(asleep)")
    print("UDP:  RPLY=answered   clsd=ICMP unreachable  sil.=silent")
    print(f"UDP {UDP_CONTROL} is the control: it must read 'clsd' for the UDP column to mean anything.")
    print("\n--> Open the CP4 live view in the EZVIZ app NOW, and keep it open <--\n")

    header = (
        "time      | "
        + " ".join(f"{p:>5}" for p in TCP_PORTS)
        + " | "
        + " ".join(f"{p:>5}" for p in all_udp)
    )
    print(header)
    print("-" * len(header))

    tcp_open: set[int] = set()
    udp_replied: dict[int, bytes] = {}
    udp_states: dict[int, set[str]] = {p: set() for p in all_udp}

    deadline = time.monotonic() + DURATION
    while time.monotonic() < deadline:
        tcp_states = [probe_tcp(HOST, p) for p in TCP_PORTS]
        for port, state in zip(TCP_PORTS, tcp_states, strict=True):
            if state == "OPEN" and port not in tcp_open:
                tcp_open.add(port)
                print(f"\n*** TCP {port} OPEN ***\n")

        udp_out: list[str] = []
        for port in all_udp:
            state, data = probe_udp(HOST, port)
            udp_states[port].add(state)
            if state == "RPLY" and port not in udp_replied and data is not None:
                udp_replied[port] = data
                print(f"\n*** UDP {port} ANSWERED ({len(data)}B): {data[:64].hex()} ***\n")
            udp_out.append(state)

        print(
            time.strftime("%H:%M:%S")
            + " | "
            + " ".join(f"{s:>5}" for s in tcp_states)
            + " | "
            + " ".join(f"{s:>5}" for s in udp_out)
        )

    print("\n================ RISULTATO ================")
    print(f"TCP in ascolto: {sorted(tcp_open) if tcp_open else 'NESSUNA'}")
    print(f"UDP che hanno risposto: {sorted(udp_replied) if udp_replied else 'NESSUNA'}")

    control_saw_closed = "clsd" in udp_states[UDP_CONTROL]
    if not control_saw_closed:
        print(
            f"UDP: INCONCLUSIVO — la porta di controllo {UDP_CONTROL} non ha mai\n"
            "     risposto ICMP, quindi il silenzio sulle altre non significa nulla."
        )
    else:
        silent_only = [
            p
            for p in UDP_PORTS
            if udp_states[p] and udp_states[p] <= {"sil."}
        ]
        print(
            f"UDP: la porta di controllo {UDP_CONTROL} ha dato ICMP unreachable,\n"
            "     quindi lo stack UDP segnala le porte chiuse."
        )
        if silent_only:
            print(f"     -> SEMPRE silenziose (probabilmente APERTE): {silent_only}")
        else:
            print("     -> nessuna porta silenziosa: sembrano tutte chiuse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
