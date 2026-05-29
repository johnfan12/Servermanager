#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

mkdir -p logs runtime

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

SIMPLE_SERVERMANAGER_PORT="${SIMPLE_SERVERMANAGER_PORT:-18881}"
export SIMPLE_SERVERMANAGER_PORT

SIMPLE_API_FRP_ENABLED="${SIMPLE_API_FRP_ENABLED:-true}"
SIMPLE_API_REMOTE_PORT="${SIMPLE_API_REMOTE_PORT:-$SIMPLE_SERVERMANAGER_PORT}"
SIMPLE_API_LOCAL_PORT="${SIMPLE_API_LOCAL_PORT:-$SIMPLE_SERVERMANAGER_PORT}"
SIMPLE_API_PROXY_NAME="${SIMPLE_API_PROXY_NAME:-simple-servermanager-api-${SIMPLE_API_REMOTE_PORT}}"
SIMPLE_FRP_CLIENT_BIN="${SIMPLE_FRP_CLIENT_BIN:-${FRP_CLIENT_BIN:-/usr/local/bin/frpc}}"
SIMPLE_API_FRPC_CONFIG_FILE="${SIMPLE_API_FRPC_CONFIG_FILE:-$ROOT_DIR/runtime/simple-frpc-api.ini}"
SIMPLE_API_FRPC_PID_FILE="${SIMPLE_API_FRPC_PID_FILE:-$ROOT_DIR/runtime/simple-frpc-api.pid}"
SIMPLE_API_FRPC_LOG_FILE="${SIMPLE_API_FRPC_LOG_FILE:-$ROOT_DIR/logs/simple-frpc-api.log}"

cleanup() {
  if [ -f "$SIMPLE_API_FRPC_PID_FILE" ]; then
    local pid
    pid="$(cat "$SIMPLE_API_FRPC_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$SIMPLE_API_FRPC_PID_FILE"
  fi
}

start_api_frp_client() {
  if [ "$SIMPLE_API_FRP_ENABLED" != "true" ]; then
    echo "[start.sh] API FRP client disabled"
    return
  fi

  if [ "${SIMPLE_FRP_ENABLED:-${FRP_ENABLED:-true}}" != "true" ]; then
    echo "[start.sh] FRP disabled"
    return
  fi

  local server_addr
  local server_port
  local token
  server_addr="${SIMPLE_FRP_SERVER_ADDR:-${FRP_SERVER_ADDR:-}}"
  server_port="${SIMPLE_FRP_SERVER_PORT:-${FRP_SERVER_PORT:-7000}}"
  token="${SIMPLE_FRP_TOKEN:-${FRP_TOKEN:-}}"

  if [ -z "$server_addr" ] || [ -z "$token" ]; then
    echo "[start.sh] FRP server address or token missing; skip API FRP client"
    return
  fi

  if [ ! -x "$SIMPLE_FRP_CLIENT_BIN" ]; then
    echo "[start.sh] frpc not found at $SIMPLE_FRP_CLIENT_BIN; skip API FRP client"
    return
  fi

  cat > "$SIMPLE_API_FRPC_CONFIG_FILE" <<EOF
[common]
server_addr = ${server_addr}
server_port = ${server_port}
token = ${token}

[${SIMPLE_API_PROXY_NAME}]
type = tcp
local_ip = 127.0.0.1
local_port = ${SIMPLE_API_LOCAL_PORT}
remote_port = ${SIMPLE_API_REMOTE_PORT}
EOF

  cleanup

  "$SIMPLE_FRP_CLIENT_BIN" -c "$SIMPLE_API_FRPC_CONFIG_FILE" > "$SIMPLE_API_FRPC_LOG_FILE" 2>&1 &
  echo $! > "$SIMPLE_API_FRPC_PID_FILE"
  echo "[start.sh] started API FRP client on remote port ${SIMPLE_API_REMOTE_PORT}"
}

trap cleanup EXIT INT TERM

start_api_frp_client

exec uvicorn main:app --host 0.0.0.0 --port "$SIMPLE_SERVERMANAGER_PORT"
