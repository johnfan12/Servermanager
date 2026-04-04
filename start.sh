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

SERVERMANAGER_PORT="${SERVERMANAGER_PORT:-18881}"
FRP_API_ENABLED="${FRP_API_ENABLED:-true}"
FRP_API_REMOTE_PORT="${FRP_API_REMOTE_PORT:-18881}"
FRP_API_LOCAL_PORT="${FRP_API_LOCAL_PORT:-$SERVERMANAGER_PORT}"
FRP_API_PROXY_NAME="${FRP_API_PROXY_NAME:-servermanager-api-${FRP_API_REMOTE_PORT}}"
FRP_CLIENT_BIN="${FRP_CLIENT_BIN:-/usr/local/bin/frpc}"
FRP_API_CONFIG_FILE="${FRP_API_CONFIG_FILE:-$ROOT_DIR/runtime/frpc-api.ini}"
FRP_API_PID_FILE="${FRP_API_PID_FILE:-$ROOT_DIR/runtime/frpc-api.pid}"
FRP_API_LOG_FILE="${FRP_API_LOG_FILE:-$ROOT_DIR/logs/frpc-api.log}"

cleanup() {
  if [ -f "$FRP_API_PID_FILE" ]; then
    local pid
    pid="$(cat "$FRP_API_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$FRP_API_PID_FILE"
  fi
}

start_api_frp_client() {
  if [ "${FRP_ENABLED:-false}" != "true" ] || [ "$FRP_API_ENABLED" != "true" ]; then
    echo "[start.sh] FRP API client disabled; skip startup"
    return
  fi

  if [ -z "${FRP_SERVER_ADDR:-}" ] || [ -z "${FRP_TOKEN:-}" ]; then
    echo "[start.sh] FRP_SERVER_ADDR or FRP_TOKEN missing; skip API client startup"
    return
  fi

  if [ ! -x "$FRP_CLIENT_BIN" ]; then
    echo "[start.sh] frpc not found at $FRP_CLIENT_BIN; skip API client startup"
    return
  fi

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet frpc-api.service; then
      echo "[start.sh] detected active frpc-api.service; skip local API client startup"
      return
    fi
  fi

  cat > "$FRP_API_CONFIG_FILE" <<EOF
[common]
server_addr = ${FRP_SERVER_ADDR}
server_port = ${FRP_SERVER_PORT:-7000}
token = ${FRP_TOKEN}

[${FRP_API_PROXY_NAME}]
type = tcp
local_ip = 127.0.0.1
local_port = ${FRP_API_LOCAL_PORT}
remote_port = ${FRP_API_REMOTE_PORT}
EOF

  if [ -f "$FRP_API_PID_FILE" ]; then
    local old_pid
    old_pid="$(cat "$FRP_API_PID_FILE" 2>/dev/null || true)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[start.sh] stopping previous frpc-api process $old_pid"
      kill "$old_pid" 2>/dev/null || true
      wait "$old_pid" 2>/dev/null || true
    fi
    rm -f "$FRP_API_PID_FILE"
  fi

  "$FRP_CLIENT_BIN" -c "$FRP_API_CONFIG_FILE" > "$FRP_API_LOG_FILE" 2>&1 &
  echo $! > "$FRP_API_PID_FILE"
  echo "[start.sh] started API FRP client ${FRP_API_PROXY_NAME} on remote port ${FRP_API_REMOTE_PORT}"
}

trap cleanup EXIT INT TERM

start_api_frp_client

alembic upgrade head

exec uvicorn main:app --host 0.0.0.0 --port "$SERVERMANAGER_PORT" --reload
