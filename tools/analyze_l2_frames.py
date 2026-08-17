#!/usr/bin/env python3
"""Analyze the proprietary EtherType 0x8d8d frames the CP4 broadcasts.

These frames carry no IP header, which is why every TCP/UDP scan and every
IP-filtered tcpdump missed them. This parses the raw pcap without scapy.
"""

from __future__ import annotations

import collections
import struct
import sys

if len(sys.argv) < 2:
    raise SystemExit(f"usage: {sys.argv[0]} <capture.pcap>")
PATH = sys.argv[1]
EZVIZ_ETHERTYPE = 0x8D8D


def read_pcap(path: str) -> list[bytes]:
    """Return raw link-layer frames from a classic pcap file."""
    with open(path, "rb") as handle:
        blob = handle.read()

    magic = blob[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        raise SystemExit(f"not a classic pcap (magic {magic.hex()})")

    frames: list[bytes] = []
    offset = 24
    while offset + 16 <= len(blob):
        _, _, caplen, _ = struct.unpack_from(endian + "IIII", blob, offset)
        offset += 16
        frames.append(blob[offset : offset + caplen])
        offset += caplen
    return frames


def main() -> int:
    frames = read_pcap(PATH)
    kinds: collections.Counter[str] = collections.Counter()
    ez: list[bytes] = []

    for frame in frames:
        if len(frame) < 14:
            kinds["runt"] += 1
            continue
        ethertype = struct.unpack_from("!H", frame, 12)[0]
        if ethertype == EZVIZ_ETHERTYPE:
            kinds["0x8d8d EZVIZ-L2"] += 1
            ez.append(frame)
        elif ethertype == 0x0806:
            kinds["ARP"] += 1
        elif ethertype == 0x0800:
            kinds["IPv4"] += 1
        elif ethertype == 0x86DD:
            kinds["IPv6"] += 1
        else:
            kinds[f"other 0x{ethertype:04x}"] += 1

    print(f"frames totali: {len(frames)}")
    for kind, count in kinds.most_common():
        print(f"  {kind:20} {count}")

    if not ez:
        print("\nnessun frame 0x8d8d.")
        return 0

    print(f"\n=== {len(ez)} frame 0x8d8d ===")
    dst = {f[0:6].hex(":") for f in ez}
    src = {f[6:12].hex(":") for f in ez}
    print(f"dst MAC: {dst}")
    print(f"src MAC: {src}")
    print(f"lunghezze: {sorted({len(f) for f in ez})}")

    payloads = [f[14:] for f in ez]

    print("\n--- struttura del payload (primo frame) ---")
    body = payloads[0]
    ip_field = body[0:16].split(b"\x00", 1)[0].decode("ascii", "replace")
    print(f"[0x00:0x10] IP ASCII null-padded : {ip_field!r}")
    print(f"[0x10:0x14] u32 LE               : {struct.unpack_from('<I', body, 16)[0]}")
    print(f"[0x14:0x16] u16 LE / BE          : "
          f"{struct.unpack_from('<H', body, 20)[0]} / {struct.unpack_from('>H', body, 20)[0]}")

    # the ASCII path is self-delimiting: printable run after the header
    start = 22
    end = start
    while end < len(body) and 0x20 <= body[end] < 0x7F:
        end += 1
    print(f"[0x16:0x{end:02x}] path ASCII          : {body[start:end].decode()!r}")
    print(f"resto                            : {len(body) - end} byte ad alta entropia")

    print("\n--- tutti i path ASCII osservati ---")
    paths: collections.Counter[bytes] = collections.Counter()
    for body in payloads:
        run = bytearray()
        for byte in body[22:]:
            if 0x20 <= byte < 0x7F:
                run.append(byte)
            else:
                break
        paths[bytes(run)] += 1
    for path, count in paths.most_common():
        print(f"  {count:3}x {path.decode(errors='replace')!r}")

    print("\n--- confronto byte-a-byte tra frame ---")
    shortest = min(len(p) for p in payloads)
    first = payloads[0]
    for index, body in enumerate(payloads[1:], start=1):
        differing = [i for i in range(shortest) if body[i] != first[i]]
        if not differing:
            print(f"  frame {index}: IDENTICO al frame 0")
        else:
            print(
                f"  frame {index}: differisce in {len(differing)} byte, "
                f"primo diverso a offset 0x{differing[0]:03x}"
            )

    stable = [i for i in range(shortest) if len({p[i] for p in payloads}) == 1]
    print(
        f"\nbyte costanti su tutti i {len(payloads)} frame: {len(stable)}/{shortest}"
        f"  -> prefisso stabile fino a offset 0x{max(stable) if stable else 0:03x}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
