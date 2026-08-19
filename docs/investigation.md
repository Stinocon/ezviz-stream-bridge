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

- **The device was answering us.** A consistent RST on every port means a TCP stack that is
  receiving our packets and replying to them. Silence, by contrast, is uninterpretable.
- **The device was streaming video.** A cloud capture taken inside that same window produced
  543 KB of decodable HEVC. The camera was demonstrably encoding and sending.
- **A cloud CAS handshake had just succeeded.** `pyezvizapi`'s `--fetch-cas` obtained a valid
  operation code and key seconds earlier. The connection to `9010` was refused anyway.

So under the most favourable conditions obtainable — answering, streaming, freshly authorised —
nothing listens. There is no local listener to wake.

**One correction to how this was originally argued.** The first version of this finding treated
RST as a reliable proxy for "the device is awake", and silence as a proxy for "asleep". Later
measurement (Finding 7) shows the relationship is not that tidy: during an active cloud stream
delivering 3505 video packets, every LAN probe timed out for 90 seconds straight, so
*streaming* did not imply *LAN-responsive*. The verdict is unaffected, because RST and silence
both mean "nothing accepted a connection" and neither ever became OPEN. What changes is the
methodology: only a probe taken while the device is provably answering can be read as evidence
about a port, which is why Finding 7 gates every measurement on an observed RST.

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

## Finding 7: the app-style P2P registration does not open a listener

One hypothesis survived Finding 4. `pyezvizapi`'s local-SDK helpers do not simply connect: by
default they run `register_p2p_session=True` first, an app-style registration that the library
documents as required by some doorbells before CAS will return a session. If the earlier probes
had skipped it, the camera might have been refusing connections it would otherwise have
accepted. This was tested directly.

**What the registration actually is** — `POST /v3/p2pbusiness/configurations/p2p`, with the body
`{"sessionId": "..."}` and nothing else. No device serial, no IP. It authorises the user
*session*, not the device, and cannot instruct any particular camera to do anything, because it
does not name one. Its response carries `ticket`, `secret`, `expireTime` and `serverInfos`:
credentials for the cloud P2P rendezvous servers listed in the device's own `P2P` block
(a pair of AWS eu-west-1 addresses on port 6000). EZVIZ "P2P" on this account is
a cloud-brokered introduction, and the registration hands out credentials for it — never
anything LAN.

Worth stating plainly because the naming misleads: nothing in this path is a LAN operation. The
client dials out to `localIp:9010` (with its source port bound to 10101) and to
`localIp:9020`; it opens no listener of its own. The camera has to be the one in `LISTEN`, so a
RST from it settles the question.

**The measurement.** Three independent cycles, each gated on an observed RST proving the stack
was answering us at that instant:

| moment | 9010 | 9020 |
|---|---|---|
| before the registration | REFUSED | REFUSED |
| 2.3 s after `register_p2p_session` returned 200 | REFUSED | REFUSED |
| after CAS `getDevOperationCode` succeeded | REFUSED | REFUSED |
| 25 s of continuous watching at 1 Hz | REFUSED | REFUSED |

Identical in all three cycles. `open_local_sdk_stream_from_client()` itself, called for real
against the device, raises `ConnectionRefusedError [Errno 61]`. A listener bound locally on port
10101 for the whole run recorded no inbound connection from the camera either.

A curated sweep of 104 ports — every port in Finding 1's table plus the common service ports —
run inside a single verified window at a rate the device tolerates, returned RST on all 104.

**Not tested, and stated so:** the full 65535-port sweep. At the probe rate this device
tolerates it would take about nine hours (see *Method notes*), and the ports it would add are
progressively less plausible.

This does not, on its own, rule out emulating the EZVIZ control plane locally. That is a
different experiment, on a different plane: it asks what the camera can be persuaded to do, not
what it currently exposes.

## Finding 8: the device-side control plane, and why emulating it is not currently practical

Findings 4 and 7 answer "what does the camera expose on the LAN" — nothing. They do not answer
"what does the camera itself reach out to, and could that be stood up locally?" To see that, the
camera's own traffic was mirrored off the router (a port-mirror / packet sniffer streaming to a
capture host) and two full cold-boot cycles were recorded, including one triggered by physically
power-cycling the device. The captures contain the real serial, account-bound IPs and a device
credential, so they are kept out of this repository; only the sanitized structure is below.

**Observed — the camera never contacts the CAS endpoint the cloud advertises to clients.**
`deviceInfos.casIp` reports `eucas.ezvizlife.com:6500`. That is where a *client* reaches CAS.
Across every capture the camera opened no connection to it. An earlier plan to emulate CAS by
redirecting port 6500 would have intercepted nothing; this finding is the reason it was dropped
before any code was written.

**Observed — the real chain the device walks on a cold boot:**

```
litedev  →  HTTPDNS  →  :8666 challenge/response  →  :8820 persistent / wake  →  media (RTP)
```

1. **Name resolution, done two ways and both used.** The camera resolves
   `litedev.eu.ezvizlife.com` over classic DNS *and* over HTTPDNS — a cleartext
   `GET /d?dn=litedev.eu.ezvizlife.com&ip=... HTTP/1.1` to a public HTTPDNS resolver whose IP
   (`119.29.29.29:80`, Tencent DNSPod) is hardcoded in firmware. The two paths returned
   *different* addresses and the device used both. A plain DNS override is therefore bypassed:
   only an intercept that catches the TCP connection regardless of how its address was learned
   (e.g. a per-source destination-NAT on the router) would redirect this host, and it would also
   have to cover the hardcoded HTTPDNS IP.

2. **`:8666` — a two-phase authentication handshake, framing in clear.** The init message is a
   simple `tag / length / value` shape:

   ```
   CAM  0101 0009 <serial(9)> 20 <static-device-token, base64, 23 B> <per-session nonce, 16 B>
   SRV  8024 …    <challenge>
   CAM  9013 · 0101 …  <challenge response>
   SRV  b0b4 …    <final>
   ```

   Comparing the two cold boots decomposes this init cleanly:

   - the **base64 token was byte-identical** across a full physical power cycle and across days.
     It is the device's *static* identity credential.
   - the **16-byte trailer changed every handshake** (a nonce), and the `8024 → 0101 → b0b4`
     exchange that follows changed in both content and length between sessions.

3. **`:8820` — a persistent control channel.** Long-lived, carrying `c0 00` / `d0 00` heartbeats
   and serial-addressed pushes from the cloud (e.g. a frame whose cleartext prefix is
   `/<serial>/<channel>/<n>` followed by an encrypted body). While the camera was completely
   silent to LAN SYNs for ten minutes straight, this channel kept exchanging over the internet.

4. **Media** is then interleaved RTP (the `$` / `0x24` framing) from EZVIZ media hosts — the same
   shape the working cloud bridge already consumes.

**Deduced (not directly proven):**

- `:8666` authenticates in two stages: a *static, replayable* device identity, gated behind a
  *per-session cryptographic* challenge/response. The second stage is the real gate.
- `:8820` is the always-on channel over which the cloud delivers the *wake* signal to this
  battery device. It sleeps, and the cloud is what tells it to wake.
- The static token is *necessary but not sufficient*: it is now known from the wire, but nothing
  in it lets us produce the `8024` challenge the camera expects, or validate its response.

**Still to verify (would need firmware / binary reverse engineering, not more capture):**

- how the `8024` challenge is constructed, and whether it is keyed;
- whether `:8666` mints a session secret later used to authenticate on `:8820`;
- the exact semantics of the camera's challenge response.

**Verdict at the end of this finding — later made concrete.** The map above was the starting
point for an active investigation: an in-path relay, a `:8666` replay mock, and MQTT analysis on
`:8820`. Findings 9–12 carry it through to a demonstrated conclusion rather than a hypothesis. In
short: we *can* place ourselves transparently in the path and replay `:8666` far enough that the
camera advances to `:8820`, but the `:8820` MQTT broker rejects any session not backed by a valid
`:8666` handshake against the real cloud. The media layer is already solved and is not where the
difficulty lies.

## Finding 9: we can sit transparently in the control-plane path, without breaking the camera

Findings 4–8 establish what the camera exposes and what it reaches out to. The next phase asked a
different, active question: **can we insert ourselves into the camera→cloud control-plane path,
observe a real session end to end, and — later — try to answer for the cloud?** The answer to the
first half is yes.

**Architecture that works.** All redirection is done on the router; the interception host runs a
plain userspace TCP relay. The camera's control port is destination-NATed on the router to a
listener on the interception host, which forwards every byte, unchanged, to the real control-plane
host (resolved fresh per connection). Because the control plane is plaintext (Finding 8), the relay
sees the whole exchange in clear with nothing to decrypt.

```
  camera ──► router (dst-nat camera:PORT -> host:19666, + masquerade) ──► relay host ──► real cloud
```

Two router rules plus one firewall rule, all reversible by a shared comment tag:

- `dst-nat`: the camera's `tcp/8666` → the relay host's listener port;
- `masquerade` (**required**): the camera and the relay host share a subnet, so without source-NAT
  the relay host's replies would return straight to the camera and bypass the router's un-NAT,
  and the camera would reject them;
- a `forward accept` placed **before** any drop: the hairpinned packet must survive the router's
  FORWARD chain to reach POSTROUTING, or the masquerade never fires (observed symptom: the dst-nat
  counter climbs while the masquerade counter stays at zero and the relay sees nothing).

**An approach that did not work, recorded so it is not retried.** The first design tried to
policy-route the camera's port to the interception host and catch it there with an iptables
`REDIRECT` in `nat PREROUTING`. On a containerised host (Home Assistant OS add-on with host
networking) this silently failed: the router confirmed the packets were routed to the host, but the
`REDIRECT` rule never matched a single one — forwarded traffic is not reliably caught by
`nat PREROUTING` inside that container/netfilter boundary, and with IP-forwarding on, the
unmatched packet falls through to the FORWARD chain where Docker's default `DROP` silently discards
it. Moving all NAT to the router sidesteps the whole problem.

**Result — measured.** With the relay in place and the camera rebooted, a real control-plane
session crosses our host in clear: the camera completes its `:8666` handshake through us, the real
cloud answers **through the relay**, and the camera proceeds normally to its media stream. Live
view keeps working. So the transparent placement is confirmed and does not disturb the device
(the "case A" we set out to test).

## Finding 10: anatomy of the `:8666` handshake

Captured in clear, both by the router mirror (Finding 8) and through the in-path relay (Finding 9),
across many real sessions. The exchange is four application messages inside a simple
`tag / length / value` framing:

```
camera → cloud : 703e                                             2-byte preamble
camera → cloud : 0101 <len=0009> "<serial>" 20 <token> <nonce>    INIT
cloud  → camera: 8024 010000 00 <32-byte opaque>                  CHALLENGE
camera → cloud : 9013 ; 0101 <...> ; 0101 <...>                   RESPONSE
cloud  → camera: b0b4 0101 000000 <176-byte opaque>               FINAL
```

Field-level observations (values redacted; only structure and constants are shown):

- **`<token>`** — a 32-character base64 string in the INIT. It is **static**: byte-identical across
  reboots and across days, and identical to the value the camera later presents as its MQTT
  client-id (Finding 12). It is the device's persistent identity string.
- **`<nonce>`** — 16 bytes trailing the INIT. It **varies per connection attempt**: within one
  burst of retries the camera presents a fresh nonce each time. (It looked constant in early
  captures only because those were all first-attempt-after-boot samples.)
- **CHALLENGE** — 38 bytes: a constant 6-byte header `80 24 01 00 00 00` followed by **32 opaque,
  high-entropy bytes** that differ every session. No timestamp, counter, or identifier is visible.
- **RESPONSE** — the camera's answer, `0101`-tagged, opaque.
- **FINAL** — 183 bytes: a constant 7-byte header `b0 b4 01 01 00 00 00` followed by **176 opaque
  bytes** (176 = 11 × 16, i.e. AES-block-aligned). Only those 7 header bytes are constant across
  sessions.

**Determinism, stated carefully.** For the same observed `(token, nonce, challenge)`, the camera
produced a **byte-identical response** in repeated trials (demonstrated in Finding 11, where a
recorded challenge was fed back and the camera reproduced its recorded response exactly). We do
**not** claim the response is a pure stateless function of those inputs — only that, for the inputs
we observed, it was reproducible.

**Structural verdict.** Beyond the fixed headers, the challenge (32 B) and final (176 B) are
opaque encrypted/authenticated blobs with no observable internal structure. This is stated as a
negative result, not a place to invent patterns: nothing in the captured bytes lets us construct a
valid challenge or final for an arbitrary nonce without the key that produces them.

## Finding 11: replaying `:8666` makes the camera advance to `:8820`, but no further

To locate exactly where a forged control plane fails, we built a **record-and-replay mock** of the
`:8666` server side: instead of forwarding to the real cloud, the relay answers the camera with a
previously recorded `(challenge, final)` pair, one message per camera "turn". No bytes are
modified; the recorded server messages are replayed verbatim.

**What happened, in order:**

1. On the first attempt after a reboot, the camera's INIT carried the same nonce as the recorded
   session. Fed the recorded challenge, the camera produced the **exact recorded response**
   (byte-identical — this is the determinism evidence of Finding 10). We returned the recorded
   final.
2. The camera did **not** stop there: ~70 ms after receiving the replayed final, it opened a TCP
   connection to a `:8820` server and began the next protocol stage.
3. On subsequent retries the camera presented a **fresh nonce**, our fixed recorded challenge no
   longer matched it, and the camera refused to respond (it went silent after the challenge), then
   fell into an exponential-backoff retry storm.

**Interpretation (careful).** A connection to `:8820` after the replayed `:8666` is **not** proof
that the replay "succeeded". It shows only that the camera accepted enough of the `:8666` stage to
advance to the next control channel. Crucially, this **disambiguated** an earlier open question:
the rejection we later see is *not* located purely inside the `:8666` byte exchange — the camera
gets past it and tries `:8820`. The wall is at the next stage.

## Finding 12: `:8820` is MQTT, and its authorization lives server-side — not in the bytes

**`:8820` is MQTT 3.1.1.** The camera's first `:8820` message is an MQTT `CONNECT`. Decoded
(values redacted):

| field | value / nature |
|---|---|
| protocol / level | `MQTT` / 4 (3.1.1) |
| client-id | the **static `:8666` token** (32 B) |
| username | the **device serial** (not secret) |
| password | the 4-byte ASCII string `test` (not secret) |
| will topic | a fixed 128-byte topic path, **static** across sessions |
| will message | **608 bytes, per-session, opaque/high-entropy** |
| keep-alive | 30–90 s (varies, not correlated with anything) |

The broker replies with a 4-byte `CONNACK`. In a **real** session the return code is `0x00`
(accepted); a session bootstrapped from our **mock** `:8666` gets `0x0b` (a non-standard/vendor
code — rejected). This was verified, not assumed: real sessions return `0x00`, the mock-backed one
returns `0x0b`.

**The credentials are not secret.** username = serial, password = `test`, client-id = the static
token. The broker cannot be authenticating on these — they are fixed and public-ish. So its
accept/reject decision rests on something not carried as an MQTT credential.

**Is the will-message the hidden auth? No evidence.** The 608-byte will-message is the only CONNECT
field that varies per session, so we tested whether it carries `:8666`-derived material. Byte-level
analysis across real sessions (substring search for nonce/token/challenge/response/final and their
8/16-byte windows; MD5/SHA-1/SHA-256 digests of each; XOR of two sessions' will-messages; 16-byte
block-repeat/ECB test; constant-region and header/trailer test) found **no correlation whatsoever**:
608/608 bytes differ between sessions (~chance), no repeated blocks, no shared region, no `:8666`
material and no hash of it present. The will-message is indistinguishable from independent
per-session encrypted data. (This does not prove independence — a keyed derivation would look the
same — but there is no observable dependency, and the broker accepts two *different* will-messages
from two real sessions, so the will-message is not what it validates for the CONNACK.)

**The decisive test — byte-identical CONNECT replay.** We captured a real, freshly **accepted**
(`0x00`) CONNECT and replayed the exact same bytes, from our own minimal TCP client (no MQTT
library, no reconstruction), to the same broker. Result: **`0x0b`**. A byte-for-byte identical
CONNECT, accepted when the camera sent it, is rejected when we send it. So the CONNECT payload
alone does not authenticate — the accept depends on context the bytes do not carry.

**Ruling out the client-id-collision confound — the freed-id test.** One alternative remained: MQTT
brokers may reject a second connection using an already-connected client-id. So we repeated the
replay with the camera **powered off** and the client-id freed (waited past the 30 s keep-alive so
the broker released the session). The byte-identical CONNECT was still rejected with **`0x0b`**.
Combined with an earlier observation — the camera's *own first* CONNECT (immediate, no id
collision, no temporal gap) after a **mock** `:8666` was also rejected `0x0b` — this rules out both
client-id collision and simple temporal expiry as the explanation.

**What creates the state.** In every test, the single variable that decides `0x00` vs `0x0b` is
whether a **valid `:8666` challenge/response reached the real cloud** (`litedev.eu.ezvizlife.com`).
The camera makes no REST/API call of its own, and the client-side P2P registration
(`/v3/p2pbusiness/configurations/p2p`) is **not** in the camera's path — our relay setup performs
no such registration, yet real sessions are still accepted. The chain is:

```
real  : camera → :8666 challenge/response → reaches REAL litedev → validated
        → broker authorized for this token → :8820 CONNECT → 0x00
mock  : camera → :8666 replay → never reaches real litedev → no authorization
        → :8820 CONNECT → 0x0b
```

**The first divergence point** between a real and an emulated session is the `:8666`
challenge/response validation at the real cloud. Everything downstream — the `:8820` authorization
and the media that follows — depends on it.

## Control-plane conclusion: what is and is not possible without firmware work

Separating observation from inference one last time:

- **Observation:** identical MQTT CONNECT → `0x00` when the camera sends it, `0x0b` on replay, even
  with the client-id freed; a mock-backed first connection → `0x0b`; MQTT credentials are
  non-secret; the `:8666` challenge/final are opaque blobs; the camera issues no API call.
- **Correlation:** the only variable that flips the CONNACK is a genuine `:8666`-to-real-cloud
  validation.
- **Hypothesis (now strongly supported):** a valid `:8666` challenge/response, which requires the
  camera's firmware secret to answer a **fresh** server challenge, causes the real cloud to
  provision the MQTT broker to authorize that token for a session. This authorization is
  server-side state, not present in any captured byte, and not reconstructible from the client
  side.

**Possible without firmware reverse engineering:**

- The camera's video in Home Assistant / Frigate without the EZVIZ app — the cloud relay this
  add-on ships. The camera performs its own valid `:8666` with its firmware secret, so
  authorization is created the normal way.
- Sitting transparently in the `:8666` path and capturing whole control-plane sessions in clear.

**Not possible without firmware reverse engineering:**

- Emulating the control plane so the camera establishes a session **without the real EZVIZ cloud**.
  "No cloud / works with the internet down" is unreachable at the network layer: the `:8820`
  authorization requires a valid `:8666` to the real cloud, gated by the camera's firmware secret.
  This is Finding 8's limit, now demonstrated experimentally rather than hypothesised. The only
  remaining avenue is extracting that secret from the firmware — the long, low-payoff road below.


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

**One avenue was explored and is documented in Finding 8.** Everything in Findings 1-7 asks what
the camera exposes. Finding 8 asks the different question — what the camera itself reaches out to,
and whether that control plane could be stood up locally. The short version: the real device-side
chain is `litedev → HTTPDNS → :8666 → :8820 → media`, its `:8666` authentication has a
per-session cryptographic stage whose validating function is not observable on the wire, and the
wake signal originates cloud-side on `:8820`. With the captures available, emulating it is not
currently practical without firmware reverse engineering — a limit of the present investigation,
not a proof of impossibility.

## Method notes, for anyone repeating this

**Scan timing matters more than usual.** This device answers ICMP with ~600 ms round-trip
because of aggressive Wi-Fi power saving. `nmap -T4` uses a 500 ms initial timeout and reports
every port as `filtered` — a pure artifact. Use `-T2` with `--max-rtt-timeout 8s` or probe
with plain sockets and generous timeouts.

**`closed` versus `filtered` is a state, not noise.** The same port alternates between RST and
silence, in windows minutes long. Reports of inconsistent scan results on these cameras are
usually this, not a firewall. The practical consequence is that a TIMEOUT proves nothing at
all: it means "unreachable right now", not "filtered" and not "no listener". Gate every
measurement on a reference port answering RST, and re-check it afterwards, or the run is
uninterpretable.

**The device goes silent under bursts.** Roughly 1-2 connections per second is tolerated and
yields stable RST for minutes. A burst — 400 concurrent connections, or even ~6/s spread over
six ports — flips it to complete silence, and it stays silent. This is why a threaded
65535-port scan comes back with 65535 timeouts and proves nothing whatsoever, and why a full
sweep at a tolerated rate would take around nine hours. Curate the port list instead, and probe
it slowly inside one window.

**UDP could not be resolved, and the tool says so.** `tools/probe_local_ports.py` probes a
control port that no service should occupy. If the control answers ICMP unreachable while a
real port stays silent, that silence is evidence the port is open. On this device the control
port stayed silent too, so the whole UDP column is uninterpretable — and the tool reports it as
inconclusive rather than letting the reader mistake silence for a finding.

**Wi-Fi limits passive capture from a peer — but the router sees everything.** From a third
station you cannot see another station's unicast traffic without monitor mode; the AP does not
relay it, so the video flow is invisible peer-to-peer. Two ways around it. ARP requests are
broadcast, and that alone answers whether two hosts are about to talk directly (Finding 6)
without seeing a byte of video. And for the full cloud conversation, mirror it at the router:
Finding 8 was captured by having the gateway stream a filtered copy of the camera's own traffic
to a capture host (RouterOS `/tool sniffer` with `streaming-server=<host>`, TZSP dissected
natively by a recent tshark/Wireshark). Two traps there: filter on the camera's IP but leave the
interface filter empty, or you capture post-NAT on the uplink and match nothing; and set the
sniffer's filters *before* starting it, since a running sniffer does not re-read them.

**A DNS override is not enough to redirect this camera.** It resolves its control-plane host
both over the network DNS and over a hardcoded HTTPDNS resolver on port 80 (Finding 8), and uses
both answers. Any redirection experiment has to intercept the TCP connection regardless of how
its address was learned — a per-source destination-NAT on the router does this; pointing the LAN
resolver elsewhere does not.

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
python3 tools/probe_local_sdk_register.py --serial <serial> --host <camera-ip>  # Finding 7
```

The last one needs `pyezvizapi` and a token file, because it drives the real cloud calls; the
others are stdlib only.

For the capture behind the third one, filter on the camera's MAC rather than its IP — the
frames of interest have no IP header:

```bash
sudo tcpdump -ni <iface> -e -s0 'ether host <camera-mac>' -w capture.pcap
```

For the cloud-side control-plane capture behind Finding 8, mirror the camera's traffic at the
router rather than sniffing from a peer. On RouterOS:

```
/tool sniffer set filter-ip-address=<camera-ip>/32 filter-interface="" \
    streaming-enabled=yes streaming-server=<capture-host>:37008 filter-stream=yes
/tool sniffer start
```

```bash
tshark -i <iface> -f "udp port 37008" -w cloud.pcapng   # TZSP is dissected automatically
```

Captures made this way contain the real serial, account-bound addresses and a device credential,
so they stay out of the repository — only sanitized structure belongs here.

The in-path relay and `:8666` replay/MQTT-analysis work behind Findings 9–12 used a separate
research add-on (a plaintext TCP relay with a record/replay mode) plus the router NAT recipe in
Finding 9, and a minimal raw-TCP MQTT-CONNECT replayer. Those are research artifacts kept in the
working tree, not shipped tools here, because they only make sense pointed at one's own device and
their captures carry the serial, token and MQTT material that must never enter a repository.

## Credit

The EZVIZ protocol work — cloud API, stream framing, remux, and the local-SDK path that this
device turned out not to support — is [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) by
RenierM26. This investigation used it as both tool and reference; the findings above are about
one device, not about that library.
