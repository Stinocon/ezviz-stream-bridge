#!/usr/bin/env python3
"""Transparent TCP relay for the EZVIZ device-side control plane.

This sits in the path between the camera and the real EZVIZ servers. It does
NOT terminate, parse, impersonate or modify the protocol: it accepts the
redirected connection, recovers the original destination the camera intended
(via SO_ORIGINAL_DST, populated by an iptables REDIRECT on the same host),
opens a fresh connection to that real EZVIZ endpoint, and relays bytes in both
directions unchanged. Its only job is to prove we can sit in the path and to
record what crosses it.

Because the EZVIZ control plane is plaintext custom TCP (measured: no TLS on
:8666/:8820), a plain relay observes the full handshake, timing, segment sizes
and direction in clear, with no crypto to break.

Logging is layered so secrets need never touch disk:
  - metadata (always): timestamp, direction, segment size, first bytes hex
  - payload dumps (opt-in): raw per-direction .bin, replayable
  - pcap (opt-in): synthetic but Wireshark-openable framing of the relay

Read-only relay. It changes nothing on the camera or the servers.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

# getsockopt level/opt for the original destination behind an iptables REDIRECT.
SO_ORIGINAL_DST = 80
SOL_IP = 0


def original_destination(sock: socket.socket) -> tuple[str, int] | None:
    """Recover the pre-REDIRECT destination address, or None if unavailable."""
    try:
        raw = sock.getsockopt(SOL_IP, SO_ORIGINAL_DST, 16)
    except OSError:
        return None
    port = struct.unpack(">H", raw[2:4])[0]
    host = socket.inet_ntoa(raw[4:8])
    return host, port


class PcapWriter:
    """Minimal classic-pcap writer.

    Frames each relayed chunk as a synthetic Ethernet/IP/TCP packet so the
    result opens in Wireshark. Addresses and sequence numbers are consistent
    within a connection but otherwise synthetic: this is a readable transcript,
    not a wire-faithful capture (the real capture path is the router mirror).
    """

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._lock = threading.Lock()
        self._seq: dict[tuple[str, bool], int] = {}
        handle.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        handle.flush()

    def write(self, conn_id: str, to_server: bool, payload: bytes,
              client_ip: str, server_ip: str, server_port: int) -> None:
        if not payload:
            return
        with self._lock:
            key = (conn_id, to_server)
            seq = self._seq.get(key, 1)
            self._seq[key] = seq + len(payload)
            if to_server:
                src_ip, dst_ip, src_port, dst_port = client_ip, server_ip, 40000, server_port
            else:
                src_ip, dst_ip, src_port, dst_port = server_ip, client_ip, server_port, 40000
            packet = self._frame(src_ip, dst_ip, src_port, dst_port, seq, payload)
            now = time.time()
            self._handle.write(struct.pack("<IIII", int(now), int((now % 1) * 1e6),
                                           len(packet), len(packet)))
            self._handle.write(packet)
            self._handle.flush()

    @staticmethod
    def _frame(src_ip: str, dst_ip: str, src_port: int, dst_port: int,
               seq: int, payload: bytes) -> bytes:
        eth = b"\x02\x00\x00\x00\x00\x02\x02\x00\x00\x00\x00\x01\x08\x00"
        tcp = struct.pack(">HHIIBBHHH", src_port, dst_port, seq, 0,
                          (5 << 4), 0x18, 65535, 0, 0) + payload
        total = 20 + len(tcp)
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 6, 0,
                         socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
        return eth + ip + tcp


class Relay:
    def __init__(self, outdir: Path, log_payload: bool, pcap: PcapWriter | None,
                 upstream: tuple[str, int] | None) -> None:
        self.outdir = outdir
        self.log_payload = log_payload
        self.pcap = pcap
        # When set, the destination is this named upstream, resolved fresh on
        # each connection (the camera's :8666 always targets litedev, so we do
        # not need SO_ORIGINAL_DST when the router dst-nats to us). When None,
        # fall back to the original destination behind an iptables REDIRECT.
        self.upstream = upstream
        self.lock = threading.Lock()
        self.count = 0
        self.meta_path = outdir / "connections.jsonl"

    def emit(self, record: dict[str, Any]) -> None:
        with self.lock, self.meta_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        tag = record.get("event", "")
        print(f"[{record['t']}] {tag} {record.get('detail', '')}", flush=True)

    def handle(self, client: socket.socket, peer: tuple[str, int]) -> None:
        if self.upstream is not None:
            host, port = self.upstream
            try:
                resolved = socket.getaddrinfo(host, port, socket.AF_INET,
                                              socket.SOCK_STREAM)[0][4]
                original: tuple[str, int] | None = (resolved[0], resolved[1])
            except OSError as err:
                self.emit({"t": dt.datetime.now(dt.UTC).isoformat(),
                           "event": "resolve-fail", "conn": "-",
                           "detail": f"{host}:{port}: {err}"})
                client.close()
                return
        else:
            original = original_destination(client)
        with self.lock:
            self.count += 1
            conn_id = f"c{self.count:04d}"
        started_wall = dt.datetime.now(dt.UTC).isoformat()
        started = time.monotonic()

        if original is None:
            self.emit({"t": started_wall, "event": "no-original-dst", "conn": conn_id,
                       "detail": f"from {peer[0]}:{peer[1]} — is the REDIRECT in place?"})
            client.close()
            return

        server_ip, server_port = original
        self.emit({"t": started_wall, "event": "open", "conn": conn_id,
                   "detail": f"{peer[0]}:{peer[1]} -> {server_ip}:{server_port}"})

        try:
            upstream = socket.create_connection((server_ip, server_port), timeout=10.0)
        except OSError as err:
            self.emit({"t": dt.datetime.now(dt.UTC).isoformat(), "event": "upstream-fail",
                       "conn": conn_id, "detail": f"{server_ip}:{server_port}: {err}"})
            client.close()
            return

        dumps: dict[str, BinaryIO] = {}
        if self.log_payload:
            dumps["c2s"] = (self.outdir / f"{conn_id}_cam2srv.bin").open("wb")
            dumps["s2c"] = (self.outdir / f"{conn_id}_srv2cam.bin").open("wb")

        stats = {"c2s_bytes": 0, "s2c_bytes": 0, "c2s_segs": 0, "s2c_segs": 0}
        done = threading.Event()

        def pump(src: socket.socket, dst: socket.socket, to_server: bool) -> None:
            key = "c2s" if to_server else "s2c"
            first_logged = False
            try:
                while not done.is_set():
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
                    stats[f"{key}_bytes"] += len(chunk)
                    stats[f"{key}_segs"] += 1
                    if not first_logged:
                        self.emit({
                            "t": round(time.monotonic() - started, 4),
                            "event": "first-bytes", "conn": conn_id,
                            "dir": "cam->srv" if to_server else "srv->cam",
                            "detail": chunk[:32].hex(),
                        })
                        first_logged = True
                    if self.log_payload:
                        dumps[key].write(chunk)
                        dumps[key].flush()
                    if self.pcap:
                        self.pcap.write(conn_id, to_server, chunk,
                                        peer[0], server_ip, server_port)
            except OSError:
                pass
            finally:
                done.set()
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)

        threads = [
            threading.Thread(target=pump, args=(client, upstream, True), daemon=True),
            threading.Thread(target=pump, args=(upstream, client, False), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        client.close()
        upstream.close()
        for handle in dumps.values():
            handle.close()
        self.emit({"t": dt.datetime.now(dt.UTC).isoformat(), "event": "close", "conn": conn_id,
                   "detail": f"{server_ip}:{server_port}  open={round(time.monotonic()-started,2)}s "
                             f"cam->srv={stats['c2s_bytes']}B/{stats['c2s_segs']}seg "
                             f"srv->cam={stats['s2c_bytes']}B/{stats['s2c_segs']}seg"})



class ReplayServer:
    """Mock EZVIZ :8666 server that replays a recorded server-side script.

    Instead of forwarding to the real cloud, it answers the camera with a fixed
    sequence of recorded server->camera messages, one per camera "turn" (a burst
    of camera bytes followed by an idle gap). This tests whether the camera will
    accept a replayed challenge/final pair and proceed — the emulation POC.

    It interprets nothing: the recorded messages are opaque bytes. Feasibility is
    decided by the camera's behaviour, not by us understanding the crypto.
    """

    def __init__(self, outdir: Path, messages: list[bytes], pcap: PcapWriter | None,
                 log_payload: bool, idle_gap: float = 0.25, tail_seconds: float = 6.0) -> None:
        self.outdir = outdir
        self.messages = messages
        self.pcap = pcap
        self.log_payload = log_payload
        self.idle_gap = idle_gap
        self.tail_seconds = tail_seconds
        self.lock = threading.Lock()
        self.count = 0
        self.meta_path = outdir / "connections.jsonl"

    def emit(self, record: dict[str, Any]) -> None:
        with self.lock, self.meta_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(f"[{record['t']}] {record.get('event','')} {record.get('detail','')}", flush=True)

    def _read_turn(self, sock: socket.socket) -> bytes:
        """Read one camera turn: bytes until an idle gap, or until timeout."""
        chunks = bytearray()
        sock.settimeout(self.idle_gap)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except TimeoutError:
                if chunks:            # a gap after some data ends the turn
                    break
                continue              # nothing yet, keep waiting
            if not data:
                break                 # peer closed
            chunks.extend(data)
        return bytes(chunks)

    def handle(self, client: socket.socket, peer: tuple[str, int]) -> None:
        with self.lock:
            self.count += 1
            conn_id = f"r{self.count:04d}"
        started = time.monotonic()
        self.emit({"t": dt.datetime.now(dt.UTC).isoformat(), "event": "replay-open",
                   "conn": conn_id, "detail": f"{peer[0]}:{peer[1]} (mock :8666)"})

        cam_dump = (self.outdir / f"{conn_id}_cam.bin").open("wb") if self.log_payload else None
        try:
            for index, message in enumerate(self.messages):
                turn = self._read_turn(client)
                self.emit({"t": round(time.monotonic() - started, 4), "event": "cam-turn",
                           "conn": conn_id, "detail": f"turn {index} in={len(turn)}B "
                           f"head={turn[:16].hex()}"})
                if cam_dump and turn:
                    cam_dump.write(turn); cam_dump.flush()
                if self.pcap and turn:
                    self.pcap.write(conn_id, True, turn, peer[0], "10.0.0.1", 8666)
                if not turn:
                    self.emit({"t": round(time.monotonic()-started,4), "event": "cam-silent",
                               "conn": conn_id, "detail": f"no data at turn {index}; camera gone"})
                    break
                client.sendall(message)
                self.emit({"t": round(time.monotonic() - started, 4), "event": "replay-send",
                           "conn": conn_id, "detail": f"msg {index} out={len(message)}B "
                           f"head={message[:16].hex()}"})
                if self.pcap:
                    self.pcap.write(conn_id, False, message, peer[0], "10.0.0.1", 8666)

            # After the scripted messages, watch what the camera does: accept and
            # stay/continue, or reject and close. This is the POC verdict signal.
            tail = self._read_tail(client)
            self.emit({"t": round(time.monotonic() - started, 4), "event": "replay-tail",
                       "conn": conn_id, "detail": f"after script: {len(tail)}B "
                       f"head={tail[:24].hex() if tail else '(camera sent nothing / closed)'}"})
            if cam_dump and tail:
                cam_dump.write(tail)
        except OSError as err:
            self.emit({"t": round(time.monotonic()-started,4), "event": "replay-error",
                       "conn": conn_id, "detail": str(err)})
        finally:
            client.close()
            if cam_dump:
                cam_dump.close()
            self.emit({"t": dt.datetime.now(dt.UTC).isoformat(), "event": "replay-close",
                       "conn": conn_id, "detail": f"open={round(time.monotonic()-started,2)}s"})

    def _read_tail(self, sock: socket.socket) -> bytes:
        chunks = bytearray()
        sock.settimeout(self.tail_seconds)
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.extend(data)
        except (TimeoutError, OSError):
            pass
        return bytes(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, required=True,
                        help="local port the iptables REDIRECT points at")
    parser.add_argument("--outdir", default="/share/ezviz-cp-proxy")
    parser.add_argument("--log-payload", action="store_true",
                        help="also write raw per-direction payload dumps (may contain secrets)")
    parser.add_argument("--pcap", action="store_true",
                        help="also write a Wireshark-openable transcript pcap")
    parser.add_argument("--upstream", default=None,
                        help="host:port to forward every connection to, resolved "
                             "fresh per connection (e.g. litedev.eu.ezvizlife.com:8666). "
                             "When set, SO_ORIGINAL_DST is not used — this is the "
                             "router-dst-nat design. Omit to use SO_ORIGINAL_DST.")
    parser.add_argument("--replay", default=None,
                        help="MOCK MODE: path to a JSON file with a 'server_messages' list of hex "
                             "strings. Instead of forwarding, reply with those recorded server "
                             "messages, one per camera turn. Tests emulation by replay.")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    upstream = None
    if args.upstream:
        host, _, port = args.upstream.rpartition(":")
        upstream = (host, int(port))

    pcap = None
    if args.pcap:
        pcap = PcapWriter((outdir / f"transcript_{int(time.time())}.pcap").open("wb"))

    handler: Any
    if args.replay:
        script = json.loads(Path(args.replay).read_text())
        messages = [bytes.fromhex(h) for h in script["server_messages"]]
        handler = ReplayServer(outdir, messages, pcap, args.log_payload)
        mode = f"REPLAY MOCK ({len(messages)} recorded msgs from {args.replay})"
    else:
        handler = Relay(outdir, args.log_payload, pcap, upstream)
        mode = "transparent relay"

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.listen_port))  # REDIRECT target; must not be loopback-only
    server.listen(32)
    print(f"{mode} on :{args.listen_port}  outdir={outdir}  "
          f"payload={'on' if args.log_payload else 'off'}  pcap={'on' if args.pcap else 'off'}",
          flush=True)

    try:
        while True:
            conn, peer = server.accept()
            threading.Thread(target=handler.handle, args=(conn, peer), daemon=True).start()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
