#!/usr/bin/with-contenv bashio
# EZVIZ control-plane proxy — HAOS addon entrypoint.
#
# relay mode : forward every byte to the real EZVIZ host (captures a session).
# replay mode: answer the camera from a recorded server script (emulation POC).
# All redirection is on the router (dst-nat), so no iptables / privileges here.

set -euo pipefail

LISTEN_PORT="$(bashio::config 'listen_port')"
MODE="$(bashio::config 'mode')"
OUTDIR="/share/ezviz-cp-proxy"

ARGS=(--listen-port "${LISTEN_PORT}" --outdir "${OUTDIR}")

if [ "${MODE}" = "replay" ]; then
  REPLAY_FILE="$(bashio::config 'replay_file')"
  if [ ! -f "${REPLAY_FILE}" ]; then
    bashio::exit.nok "replay mode: file not found: ${REPLAY_FILE}"
  fi
  ARGS+=(--replay "${REPLAY_FILE}")
  bashio::log.info "REPLAY MOCK on :${LISTEN_PORT} from ${REPLAY_FILE}"
else
  UPSTREAM_HOST="$(bashio::config 'upstream_host')"
  UPSTREAM_PORT="$(bashio::config 'upstream_port')"
  ARGS+=(--upstream "${UPSTREAM_HOST}:${UPSTREAM_PORT}")
  bashio::log.info "RELAY on :${LISTEN_PORT} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}"
fi

if bashio::config.true 'log_payload'; then
  ARGS+=(--log-payload)
  bashio::log.warning "Payload logging is ON — dumps may contain device secrets"
fi
if bashio::config.true 'pcap'; then
  ARGS+=(--pcap)
fi

bashio::log.info "Output under ${OUTDIR}"
exec python3 /usr/src/cp_proxy.py "${ARGS[@]}"
