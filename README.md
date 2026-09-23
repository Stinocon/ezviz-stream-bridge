<p align="center">
  <img src="docs/brand/banner.svg" alt="EZVIZ Stream Bridge" width="860">
</p>

# EZVIZ Stream Bridge

Serves the video from an EZVIZ camera as MPEG-TS over HTTP, so go2rtc, Frigate, or anything
else that speaks FFmpeg can use a camera that offers no RTSP.

This is the source repository. Installed as a Home Assistant add-on from
**[Stinocon/addons](https://github.com/Stinocon/addons)**.

> **Read this first.** A personal project, published as is and with no warranty: it is not a
> finished product nor a commercial one, and it will not become either. It was written in large
> part with an AI assistant, under human guidance and review.

## The point of this, stated honestly

Some EZVIZ devices — the video door viewers and several battery models — have no local video
interface at all. Not RTSP switched off, not RTSP behind a hidden setting: never implemented.
EZVIZ's own port specification lists RTSP and ONVIF for its IP cameras and omits both for the
door viewer and doorbell category.

So this bridge gets pictures out of those devices the only way they offer, **through the EZVIZ
cloud**. That has a consequence worth knowing before you install it:

- ✅ Your camera in Home Assistant and Frigate, without the EZVIZ app.
- ❌ Video that stays on the LAN.
- ❌ Anything working when the internet is down.

If you want a doorbell that survives a dead line, no software gets you there — that needs
hardware with a native local interface. [`docs/investigation.md`](docs/investigation.md) is the
full measured account of why, so nobody has to repeat the work.

## Before it can work: two EZVIZ app settings

- **Two-step verification off** on the EZVIZ account. Nothing here can type a code in. (Home
  Assistant's own EZVIZ integration requires the same, and rejects OAuth accounts too.) The way
  round it is to log in by hand once and place the token file yourself; renewal is automatic
  from there.
- **Video encryption off** for the camera. Encrypted video needs the camera's media key, and
  the cloud only releases it to a rights-elevated session — the request returns
  `resultCode 20002` and emails a code, which again nobody is there to read.

The camera's six-letter verification code is **not** needed and is not the account password.

## Battery cameras, detect and record

Every connection opens a cloud session and makes the camera encode and upload, so an always-on
consumer means an always-encoding camera — hours of battery on a doorbell, not months.

That does not mean Frigate's `detect` and `record` are useless here: it means they must be
**event-gated**. Gate them with `frigate/<camera>/enabled/set`, which is the only one of
Frigate's MQTT switches that stops the stream being consumed: `detect`, `recordings` and
`snapshots` change what Frigate does with the frames, not whether FFmpeg keeps pulling them
(verified in Frigate 0.16–0.18; `enabled` does not exist before 0.16). Note also that go2rtc is
a separate process that knows nothing about that flag, so any live view — the Frigate UI, a
dashboard card — opens a consumer of its own regardless.

Measured through this bridge: **~4.3 s to first byte, first keyframe 1.4 s in**, so about six
seconds from request to a decodable frame, with keyframes every 4 s. Recording starts mid-scene
by construction.

The full account — the Frigate configuration, and a worked Home Assistant example that gates the
stream on the camera's own motion sensor — is in the
[add-on README](https://github.com/Stinocon/addons/tree/master/ezviz-stream-bridge).

## How it works

```
EZVIZ camera ─► EZVIZ cloud (VTM relay) ─► in-process proxy ─► MPEG-TS over HTTP ─► go2rtc ─► Frigate
                                              ▲   (pyezvizapi VTM; this repo's demux)
                                    this repo: session, supervision, per-connection logging
```

Consumers reach the stream at the Home Assistant host IP on the mapped port
(`http://<ha-ip>:8558/<serial>.ts`), not at an add-on hostname — see the
[add-on README](https://github.com/Stinocon/addons/tree/master/ezviz-stream-bridge).

The VTM session comes from [pyezvizapi](https://github.com/RenierM26/pyEzvizApi).
This project is the part that has to keep working for weeks unattended:

- **One supervised proxy per camera**, restarted with a growing, capped delay. A camera that
  cannot work — a serial that is not on the account — backs off instead of looping.
- **On-demand by construction, and instrumented.** The proxy runs in-process, so every HTTP
  connection is logged with an id, source address and User-Agent, and one VTM session opens per
  connection. No client, no VTM, no camera drain; the bridge never generates a request of its
  own, so an `active` count that will not return to 0 points straight at the external consumer
  holding it open.
- **A session cannot outlive its consumer.** The request socket is watched for a peer close, so
  a consumer that disappears is noticed within half a second even when no video is flowing and
  there is nothing to write to it — which is exactly when a battery camera is asleep and the
  cloud session is most expensive. As a backstop, a session that gets no video at all within
  `--first-video-timeout` (25 s by default, under go2rtc's hardcoded 30 s) closes itself instead
  of being left behind. Before 0.1.3 both cases leaked a cloud session that the bridge's own
  keepalives then held open indefinitely.
- **A camera timeout is not a reason to wake the camera again.** When a session ends because the
  camera went offline mid-stream, the bridge withholds the next VTM session for
  `--timeout-cooldown` (30 s by default, `0` disables it). A consumer that reconnects the instant
  its response closes — FFmpeg through go2rtc does exactly this — is made to wait instead of
  waking a camera that has only just fallen asleep. The cooldown is a pause between inbound
  requests, never a request the bridge originates, and it ends early if the consumer leaves.
- **The payload is not always MPEG-PS, so the demuxer is decided, not assumed.** The VTM
  relay hands over whatever the camera produces, and this family produces both: MPEG-PS,
  which FFmpeg demuxes directly, and RTP carrying RFC 6184 H.264 or RFC 7798 HEVC. No
  single FFmpeg input format reads both, and `rtp` is not one of them — it wants a UDP URL
  and an SDP, and the packets are already here. So every session reads the leading video
  packets first, classifies the transport from that prefix, and a payload that is RTP is
  depacketized into an Annex-B elementary stream (single NAL units, STAP-A and AP
  aggregates, FU-A and FU fragments) before FFmpeg is started with `-f h264` or `-f hevc`
  and `-use_wallclock_as_timestamps`, without which the MPEG-TS muxer refuses a stream
  that has no container to carry a timestamp. An MPEG-PS stream is untouched: same
  demuxer, byte for byte. The price is up to eight packets of added latency on every
  session, because the decision has to happen before FFmpeg exists.
- **A stream that produces nothing now says which of the two it is.** A payload that is
  RTP with no H.264 or HEVC parameter set to name it, and packets the depacketizer cannot
  read, are counted and logged rather than left as a bare `bytes=0`.
- **A remux that produces nothing can be explained, not guessed at.** FFmpeg's stderr is
  discarded by default; with `log_ffmpeg_stderr` (or `--log-ffmpeg-stderr` on the proxy) it is run
  at `info` and its output logged, bounded to 20 lines and then a single suppression notice. It
  also reports the detected payload transport (MPEG-PS, MPEG-TS, RTP or unknown) and where its
  signature sits in the leading payload — the same reading the demuxer is chosen from, so a
  diagnostic that disagrees with the demuxer is itself the bug. When the transport is anything
  but MPEG-PS it goes further and prints the first eight packets of the session — or all of them,
  if the session ends first: the length, the RTP header fields decoded, the leading bytes, and the
  payload sliced out at the offset `pyezvizapi`'s own unwrap computes. That last line is the one
  that answers the useful question — where the video starts and what codec it is — which a
  24-byte head cannot, since an RTP header alone is 12 bytes plus up to 60 bytes of CSRC list
  plus a variable-length extension. This is the first thing to turn on when a camera sends video
  but no MPEG-TS comes out.
- **Timestamps you can line up with other logs.** Every line carries an ISO-8601 local time to
  the millisecond, and each session reports `session opened`, `first-video` (the camera starting
  to send) and `first-byte` (the consumer starting to receive), so a wake-up can be measured
  against Frigate, go2rtc and Home Assistant rather than guessed at.
- **Session handling in one place.** The token is verified before every proxy start and renewed
  when the cloud stops accepting it. It is kept on `/data`, because a fresh login on every
  start is a login EZVIZ counts and rate-limits.
- **The password never reaches a command line.** The login happens in-process and the proxies
  receive only a token file, so the credential stays out of the process table.
- **Two-factor accounts are reported, not retried.** Nothing in a container can type a code in,
  so it says so once with the workaround instead of failing forever.

## Running it outside Home Assistant

```bash
pip install .
ezviz-stream-bridge --options ./options.json --token-file ./ezviz_token.json
```

`options.json` takes the same shape as the add-on options:

```json
{
  "username": "your@email.example",
  "password": "your-account-password",
  "region": "apiieu.ezvizlife.com",
  "cameras": [{ "serial": "BB1234567", "port": 8558 }],
  "log_level": "info",
  "log_ffmpeg_stderr": false
}
```

`serial` is the device serial, **not** the six-letter verification code printed on the camera.
The easiest way to find it is to fill in the credentials, leave `serial` empty and start once:
the run fails, and the log then lists every camera on the account with its serial. It is also in
the EZVIZ app under *Settings → Device Information*, and on the device label near the QR code.
`region` is `apiieu` for Europe, `apius` for the Americas, `apiisgp` for Singapore; the wrong
one looks exactly like a wrong password.

A single camera's proxy can also be run on its own, which is the quickest way to watch one
connection's lifecycle: `python -m ezviz_stream_bridge.proxy --help`. `--first-video-timeout`
sets the no-video budget (`0` disables it, restoring the pre-0.1.3 behaviour of waiting
indefinitely), `--timeout-cooldown` sets how long a new session is withheld after a camera
timeout (`0` disables it), and `--log-level debug` adds the consumer's request headers to the log.
`--log-ffmpeg-stderr` captures FFmpeg's own diagnostics when a stream produces no output, and with
it the leading packets of any payload that is not MPEG-PS.

## Investigation tools

The probes used to establish what this device does and does not expose. Run them against your
own equipment only.

```bash
python3 tools/probe_local_ports.py <camera-ip> 120   # TCP/UDP state, awake vs asleep
python3 tools/sadp_probe.py <camera-ip>              # SADP discovery + UDP signaling
python3 tools/analyze_l2_frames.py <capture.pcap>    # parse the proprietary 0x8d8d frames
```

They are kept here because the negative results are the useful ones: they are what tells you
this camera has no local listener, rather than that you configured something wrong.

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

The RTP path is verified against the FFmpeg the add-on actually runs, not the one on a
development machine — the image installs Debian's, and a remux that worked on a laptop has
failed inside the container before. The script builds a synthetic H.264 and HEVC stream,
packetizes it the way RFC 6184 and RFC 7798 say a camera does, and pushes it through the
real depacketizer and the real FFmpeg invocation:

```bash
tools/verify_rtp_against_addon.sh
```

## Credits

[pyezvizapi](https://github.com/RenierM26/pyEzvizApi) by RenierM26 does the entire EZVIZ
protocol implementation — cloud API, stream framing, and the MPEG-PS remux helper this project
replaced with its own interruptible one. It is also what the official Home
Assistant EZVIZ integration uses, so for entities (doorbell, motion, battery, switches) install
that integration rather than expecting them here: this project deliberately only does the
stream.

## Licence

MIT. See [LICENSE](LICENSE).
