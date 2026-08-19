# EZVIZ Control-Plane Proxy (diagnostic add-on)

A deliberately dumb transparent TCP relay. It sits in the path between one EZVIZ
camera and the real EZVIZ control-plane host, on a single port, forwards every
byte unchanged in both directions, and records what crosses it. It does **not**
terminate TLS (there is none on this channel), parse, impersonate or modify the
protocol. Its only purpose is to answer one experimental question:

> Can we place ourselves in the CP4 → EZVIZ path and capture a real session,
> without breaking the camera?

Research tool for your own device, not a normal camera integration.

## Design: the router does the redirect

All redirection lives on the router as a `dst-nat`, so the add-on is a pure
userspace relay — no iptables, no privileges. The camera's chosen control port
is dst-nat'd to this host; the relay forwards every connection to the real
EZVIZ control-plane host (`litedev.eu.ezvizlife.com`), resolved fresh per
connection. This works because the camera's `:8666` handshake always targets
that host, and its identity is carried in-band (a token), not bound to the
source IP.

```
  CP4 ──► router (dst-nat camera:8666 -> HAOS:19666, + masquerade) ──► HAOS add-on
                                                                          │ relay
                                                                          ▼
                                              litedev.eu.ezvizlife.com:8666
```

An earlier design tried policy-routing the camera's port to this host and
catching it with an iptables REDIRECT. On HAOS that failed: the redirected,
*forwarded* packet was never caught by `nat PREROUTING` inside the add-on
container (Docker/HAOS netfilter isolation), so it was dropped. Moving all NAT
to the router sidesteps that entirely.

## Router setup (MikroTik / RouterOS), reversible

Two rules. The `masquerade` is required: camera and HAOS share a subnet, so
without it HAOS's replies would return straight to the camera (bypassing the
router's un-NAT) and the camera would reject them.

```
/ip firewall nat add chain=dstnat src-address=<CAMERA_IP> protocol=tcp \
    dst-port=8666 action=dst-nat to-addresses=<HAOS_IP> to-ports=19666 \
    comment="ezviz-cp-proxy"
/ip firewall nat add chain=srcnat src-address=<CAMERA_IP> dst-address=<HAOS_IP> \
    protocol=tcp dst-port=19666 action=masquerade comment="ezviz-cp-proxy"
```

Roll back with `/ip firewall nat remove [find comment="ezviz-cp-proxy"]`.

## Options

| option | meaning |
|---|---|
| `listen_port` | local port the router dst-nats to (default `19666`) |
| `upstream_host` | real EZVIZ control host (default `litedev.eu.ezvizlife.com`) |
| `upstream_port` | its port (default `8666`) |
| `log_payload` | also write raw per-direction payload dumps — **may contain device secrets**; off by default |
| `pcap` | also write a Wireshark-openable transcript pcap (on by default) |

## Output (`/share/ezviz-cp-proxy/`)

- `connections.jsonl` — one line per event: open, first-bytes (hex), close with
  byte/segment counts and duration. Always written, no payload.
- `cNNNN_*.bin` — raw per-direction payloads, only when `log_payload` is on.
- `transcript_*.pcap` — Wireshark-readable transcript, only when `pcap` is on.

## Safety and reversibility

- Bring the add-on up **before** adding the router rules.
- While the rules are active and the add-on is down, only the camera's one
  control port fails; the rest keeps working. Removing the two router rules
  restores everything in seconds.
- Time-box it. This is diagnostic, not a permanent placement.
