# Research artifacts — control-plane investigation

These are the tools used for the in-path control-plane work written up as **Findings 9–12** in
[`docs/investigation.md`](../docs/investigation.md). They are **not** part of the add-on you
install to get video; they are diagnostic/research code for one's **own** device, kept here so the
investigation is reproducible.

They are deliberately not wired into the add-on repository or the packaged image: pointed at a
camera you do not own they would be an interception tool, and their captures carry the device
serial, the `:8666` token and MQTT session bytes that must never enter a repository.

## What's here

- **`ezviz-cp-proxy/`** — a Home Assistant OS add-on that is a plaintext TCP relay for one
  control-plane port, with two modes:
  - `relay` — forward every byte to the real cloud and record it (capture a real session);
  - `replay` — answer the camera from a recorded server-side script (the `:8666` replay POC,
    Finding 11).
  It needs the router NAT recipe from Finding 9 (destination-NAT the camera's port to the add-on,
  plus masquerade and a forward-accept) — the add-on itself installs no firewall rules and needs
  no privileges.
- **`mqtt_replay_client.py`** — a minimal raw-TCP MQTT `CONNECT` replayer (no MQTT library, bytes
  sent verbatim), used for the byte-identical CONNECT replay and the freed-id test (Finding 12).

## Not included, on purpose

The replay-mode add-on needs a `replay.json` (a recorded `(challenge, final)` server script) and
the MQTT replayer needs a captured `CONNECT`. Both are **session bytes from a real device** and are
not published — you generate them from your own capture. See Findings 11–12 for the shape.

Own-device-only. No scanning, no brute force, no third-party targets.
