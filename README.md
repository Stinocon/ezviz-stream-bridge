<p align="center">
  <img src="docs/brand/banner.svg" alt="EZVIZ Stream Bridge" width="860">
</p>

# EZVIZ Stream Bridge

Serves the video from an EZVIZ camera as MPEG-TS over HTTP, so go2rtc, Frigate, or anything
else that speaks FFmpeg can use a camera that offers no RTSP.

This is the source repository. Installed as a Home Assistant add-on from
**[Stinocon/addons](https://github.com/Stinocon/addons)**.

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
**event-gated**, switched on via `frigate/<camera>/detect/set` when the doorbell event arrives
and off again after. Measured through this bridge: **~4.3 s to first byte, first keyframe 1.4 s
in**, so about six seconds from request to a decodable frame, with keyframes every 4 s.
Recording starts mid-scene by construction.

The full account, with the Frigate configuration, is in the
[add-on README](https://github.com/Stinocon/addons/tree/master/ezviz-stream-bridge).

## How it works

```
EZVIZ camera ──► EZVIZ cloud (VTM relay) ──► pyezvizapi ──► MPEG-TS over HTTP ──► go2rtc ──► Frigate
                                                  ▲
                                         this repo: session + supervision
```

The protocol work is all [pyezvizapi](https://github.com/RenierM26/pyEzvizApi). This project is
the part that has to keep working for weeks unattended:

- **One supervised proxy per camera**, restarted with a growing, capped delay. A camera that
  cannot work — a serial that is not on the account — backs off instead of looping.
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
  "log_level": "info"
}
```

`serial` is the device serial, **not** the six-letter verification code printed on the camera.
The easiest way to find it is to fill in the credentials, leave `serial` empty and start once:
the run fails, and the log then lists every camera on the account with its serial. It is also in
the EZVIZ app under *Settings → Device Information*, and on the device label near the QR code.
`region` is `apiieu` for Europe, `apius` for the Americas, `apiisgp` for Singapore; the wrong
one looks exactly like a wrong password.

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

## Credits

[pyezvizapi](https://github.com/RenierM26/pyEzvizApi) by RenierM26 does the entire EZVIZ
protocol implementation — cloud API, stream framing, remux. It is also what the official Home
Assistant EZVIZ integration uses, so for entities (doorbell, motion, battery, switches) install
that integration rather than expecting them here: this project deliberately only does the
stream.

## Licence

MIT. See [LICENSE](LICENSE).
