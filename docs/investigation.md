# Why there is no local stream, and what there is instead

This is the record of an investigation into getting video out of an EZVIZ CP4 video door
viewer without the EZVIZ cloud. The short answer is that it cannot be done, and this document
exists so that the next person does not have to spend a day rediscovering why.

Everything below was measured on a real device on a real LAN. Where something is an inference
rather than a measurement, it says so.

**Device under test:** EZVIZ CP4 (`CS-CP4-R100-6E2WPFBS`), firmware `V5.3.3 build 251118`,
category `CatEye`, sub-category `CP4`. Battery-powered, 4600 mAh, 1080p at 15 fps, HEVC.

## The goal, and the three things it turned out to be

"Use the camera locally" bundles three separate capabilities, and they have completely
different answers. Keeping them apart is most of the work:

| | Possible? |
|---|---|
| Video bytes travelling only on the LAN | **No** |
| Video usable outside the EZVIZ app | **Yes** — this add-on |
| Working with no internet connection | **No** |

## Finding 1: RTSP and ONVIF were never implemented on this device class

Not disabled by a firmware update, not hidden behind a setting. EZVIZ publishes a document
called *Network Open Port List and Usage Specification*, which lists open ports per product
category. Comparing two of its sections settles the question:

| Service | IPCs | Video Door Viewers & Doorbells & Doorphones |
|---|---|---|
| RTSP Streaming | UDP 554 | **absent** |
| ONVIF Service | UDP 3702, TCP 80 | **absent** |
| HIKSDK Service | TCP 8000/8443 | TCP 8000/8443 |
| EZVIZ Signaling | TCP 9010/9020, UDP 9035 | TCP 9010/9020, UDP 9035 |
| SADP Discovery | UDP 37020 | UDP 37020 |
| Device Interconnection | TCP 50100, UDP 50160/50161 | TCP 50100, UDP 50160/50161/50162 |

This also explains a widely-reported confusion: the "enable RTSP" instructions that circulate
for EZVIZ cameras are real, and they are for the IPC category. On a door viewer the menu
entry does not exist because the service does not exist.

## Finding 2: the cloud advertises local ports that are not there

The EZVIZ pagelist API returns a `CONNECTION` block per device. For the CP4 it says:

```json
{
  "localIp": "192.0.2.34",
  "localCmdPort": 9010,
  "localStreamPort": 9020,
  "localRtspPort": 0,
  "netCmdPort": 0,
  "upnp": false
}
```

Read on its own that looks like a green light, and it is the single most misleading datapoint
in the whole exercise. It is **platform boilerplate**: the same `9010`/`9020` pair appears for
every device in `pyezvizapi`'s own test fixtures, regardless of model. It describes the
product line's protocol, not this unit's behaviour.

`localRtspPort: 0` is the honest field in there, and it agrees with Finding 1.

## Finding 3: the device does not claim the local-connect capability

The same API returns `supportExt`, the device's own capability list — 74 entries for this
unit. `pyezvizapi` names capability 507 `SupportLocalConnect`.

It is **absent**. And it is absent pointedly: 503, 504, 509 and 519 are all present, so this
is not a truncated list. The device is stating that it does not do local connections.

Worth noting for anyone building on this: `pyezvizapi` defines that constant but never gates
on it, so no library will refuse the attempt on your behalf.

## Finding 4: every documented TCP port refuses, while awake and streaming

The measurement that closes the question. All ports were probed continuously for three
minutes: `9010`, `9020`, `8000`, `8443`, `50100`, `554`.

Every one returned `ECONNREFUSED` — an explicit RST, not silence — for the entire window.

What makes this conclusive rather than suggestive is what was happening at the same time:

- **The device was awake.** A consistent RST on every port means a responsive TCP stack. When
  the camera is asleep the same probes time out instead, which is a visibly different result
  and is how the two states are told apart.
- **The device was streaming video.** A cloud capture taken inside that same window produced
  543 KB of decodable HEVC. The camera was demonstrably encoding and sending.
- **A cloud CAS handshake had just succeeded.** `pyezvizapi`'s `--fetch-cas` obtained a valid
  operation code and key seconds earlier. The connection to `9010` was refused anyway.

So under the most favourable conditions obtainable — awake, streaming, freshly authorised —
nothing listens. There is no local listener to wake.

Two smaller results point the same way: SADP on UDP 37020 does not answer a well-formed
inquiry, and the device emits no ICMP port-unreachable at all, so UDP ports cannot be
classified by probing (see *Method* below).

## Finding 5: there is one local protocol, and it is not video

Passive capture found the only thing the camera does say on the LAN unprompted: broadcast
frames with **EtherType `0x8d8d`**, which is not IANA-assigned. They carry no IP header at
all, which is why every port scan missed them and why an IP-filtered `tcpdump` shows nothing
but ARP.

One frame, 625 bytes, emitted roughly every 5 seconds:

| offset | size | content |
|---|---|---|
| `0x000` | 16 B | sender IP, ASCII, null-padded |
| `0x010` | 4 B | u32 LE = 848 |
| `0x014` | 2 B | `0x0cbd` |
| `0x016` | 24 B | `/discover/2.0.3/req/4097` — ASCII, in clear |
| `0x02e` | 4 B | `00 02 00 32` |
| `0x032` | 177 B | high entropy, **constant across all frames** |
| `0x0e3` | 384 B | high entropy, **different in every frame** |

This is the *Device Interconnection* channel from Finding 1's table — the doorbell-to-chime
link — riding raw Ethernet rather than the documented TCP/UDP ports. The REST-like path
implies a `rep` counterpart and other message numbers beyond 4097.

It is genuinely cloud-free, and genuinely not a way to get video: a 625-byte broadcast beacon
is not a 1080p transport, and no associated media channel was found.

**Hypothesis, not established:** the payload looks authenticated. 384 bytes is exactly an
RSA-3072 signature, and that field changes every frame as a signature over a nonce or
timestamp would; the constant 177 bytes would then be a static identity blob. If that is
right, forging a peer needs key material that lives in the firmware. This was not verified.

The cheap way to make progress here is not reverse engineering but observation: a genuine
EZVIZ chime on the same LAN would produce a complete `req`/`rep` exchange to read.
`tools/analyze_l2_frames.py` parses these frames from a capture.

## Finding 6: the live stream is not peer-to-peer on the LAN either

The last hope for a cloud-free path was that the app and the camera might hole-punch a direct
P2P connection and stream over the LAN, with the cloud only brokering the introduction. If so,
the video bytes would already be local — just invisible to a third host on Wi-Fi.

ARP settles it without needing to see the video. Two hosts about to exchange unicast IP must
first resolve each other's MAC, and ARP requests are broadcast, so a laptop on the same Wi-Fi
sees them even though it cannot see the unicast data that follows. Capture ARP while a phone
shows the live view; the phone's ARP cache is cold toward the camera, so if it talks to the
camera directly it is forced to resolve it.

Result, over a 72-second live view opened on the phone:

- **No station ever resolved the camera.** The only `who-has <camera>` was the camera's own
  gratuitous ARP. The phone, while displaying live video, never resolved the camera — so it was
  not receiving that video from the camera over the LAN.
- **The camera resolved only the gateway**, and re-resolved it every ~5 seconds for the whole
  window — the signature of a device continuously sending packets toward the internet.

So the path is `camera → gateway → internet → cloud → phone`. There is no LAN-local leg in
either direction. This also retires the "fake the cloud with a MITM" idea: there is no local
video traffic to intercept, and what does leave is TLS to the cloud, encrypted under keys the
camera derives from a secret in its firmware.

`tools/p2p_arp_analyze.py` runs this analysis on a capture and auto-detects the gateway.

## What does work

The EZVIZ cloud VTM relay, which is what the app itself uses. Verified end to end:

```
codec_name=hevc  profile=Main  1728x1080  15 fps
codec_name=aac   LC  16000 Hz  mono
format=mpegts    360 kbps
```

Video and audio, remuxed with codec copy only, no transcoding. Two details that make it
easier than expected: `isEncrypt: 0` on this device, so no media key and no decryption step is
needed; and `SupportEncrypt: 1`, meaning encryption exists but is off — turn it on in the app
and a key fetch (with its own MFA prompt) comes back into play.

The honest description of the result: **the video bytes come from the cloud, not the LAN.**
What you gain is the camera in Home Assistant and Frigate without the EZVIZ app. What you do
not gain is independence from the internet.

## What would be needed for a genuinely local stream

Firmware modification, and it is a long road with a poor payoff. Public prior art on this
device family already hits the walls:

- The UART is an internal 4-pin JST SUR header. **Not the USB-C**, which is charging only.
- It does not yield a root shell: it lands in `psh`, BusyBox's Hikvision "protect shell".
- The bootloader is not U-Boot, or is heavily modified — it prints `NPIp>` — and boot
  interrupt attempts largely fail because the device boots very fast.

Bypassing all of that means clipping the SPI NOR flash and dumping it externally. Then the
real questions start: getting your own code to run past `psh` and presumably-signed firmware,
and whether the SoC even exposes a locally-drivable encoder once you are inside. No public
work has cleared those. No firmware image for `CS-CP4-R100-6E2WPFBS` is published either — the
usual `.dav` URLs return 404.

For a doorbell that must work with the line down, replacing the hardware with something that
speaks RTSP or ONVIF natively is the answer that actually arrives.

## Method notes, for anyone repeating this

**Scan timing matters more than usual.** This device answers ICMP with ~600 ms round-trip
because of aggressive Wi-Fi power saving. `nmap -T4` uses a 500 ms initial timeout and reports
every port as `filtered` — a pure artifact. Use `-T2` with `--max-rtt-timeout 8s` or probe
with plain sockets and generous timeouts.

**`closed` versus `filtered` is a state, not noise.** The same port alternates between RST and
silence depending on whether the radio is awake. Reports of inconsistent scan results on these
cameras are usually this, not a firewall.

**UDP could not be resolved, and the tool says so.** `tools/probe_local_ports.py` probes a
control port that no service should occupy. If the control answers ICMP unreachable while a
real port stays silent, that silence is evidence the port is open. On this device the control
port stayed silent too, so the whole UDP column is uninterpretable — and the tool reports it as
inconclusive rather than letting the reader mistake silence for a finding.

**Wi-Fi limits passive capture — but ARP slips through.** You cannot see another station's
unicast traffic without monitor mode; the AP does not relay it. So the video flow itself is
invisible from a third host. But ARP requests are broadcast, and that is enough to answer
whether two hosts are about to talk directly (Finding 6) without seeing a single video byte.
To observe the camera's actual cloud traffic you would still need a mirrored switch port or the
camera on wire — and this camera is Wi-Fi only, so that avenue is closed here.

**Run the P2P test from the phone, not the laptop.** The laptop has been talking to the camera
all day, so its ARP cache is warm and it will not re-resolve — a cache hit reads as silence.
The phone's cache is cold, so a direct talk forces a visible resolution. Running it from the
laptop makes the result inconclusive; from the phone it is definitive.

## Tools

Run against your own device only.

```bash
python3 tools/probe_local_ports.py <camera-ip> 120        # TCP/UDP state while awake
python3 tools/sadp_probe.py <camera-ip>                   # SADP discovery + UDP signaling
python3 tools/analyze_l2_frames.py <capture.pcap>         # parse the 0x8d8d frames
python3 tools/p2p_arp_analyze.py <capture.pcap> <mac> <ip>  # local P2P vs cloud (Finding 6)
```

For the capture behind the third one, filter on the camera's MAC rather than its IP — the
frames of interest have no IP header:

```bash
sudo tcpdump -ni <iface> -e -s0 'ether host <camera-mac>' -w capture.pcap
```

## Credit

The EZVIZ protocol work — cloud API, stream framing, remux, and the local-SDK path that this
device turned out not to support — is [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) by
RenierM26. This investigation used it as both tool and reference; the findings above are about
one device, not about that library.
