#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-8000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-5173}"
SERVICE_DIR="$ROOT_DIR/.dev/wsl-service"
LOG_DIR="$ROOT_DIR/var/log"
API_PID_FILE="$SERVICE_DIR/api.pid"
WEB_PID_FILE="$SERVICE_DIR/web.pid"
SUPERVISOR_PID_FILE="$SERVICE_DIR/supervisor.pid"
API_LOG="$LOG_DIR/api.log"
WEB_LOG="$LOG_DIR/web.log"
SERVICE_INFO="$SERVICE_DIR/service-info.env"
USER_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"

export PYTHONUNBUFFERED=1
export PATH="$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PAL_CHAT_DATA_DIR="${PAL_CHAT_DATA_DIR:-$ROOT_DIR/var/data}"
export PAL_CHAT_CORS_ORIGINS="${PAL_CHAT_CORS_ORIGINS:-[\"http://127.0.0.1:${WEB_PORT}\",\"http://localhost:${WEB_PORT}\"]}"

mkdir -p "$ROOT_DIR/var/data" "$LOG_DIR" "$SERVICE_DIR"
echo "$$" >"$SUPERVISOR_PID_FILE"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

cleanup() {
  if [[ -f "$API_PID_FILE" ]]; then
    kill "$(cat "$API_PID_FILE")" 2>/dev/null || true
    rm -f "$API_PID_FILE"
  fi
  if [[ -f "$WEB_PID_FILE" ]]; then
    kill "$(cat "$WEB_PID_FILE")" 2>/dev/null || true
    rm -f "$WEB_PID_FILE"
  fi
  rm -f "$SUPERVISOR_PID_FILE"
}

trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

uv sync --group dev
npm --prefix apps/web install
uv run alembic upgrade head

: >"$API_LOG"
: >"$WEB_LOG"

uv run uvicorn pal_chat_server.main:app --app-dir apps/server/src --host 0.0.0.0 --port "$API_PORT" >>"$API_LOG" 2>&1 &
api_pid=$!
echo "$api_pid" >"$API_PID_FILE"

npm --prefix apps/web run dev -- --host 0.0.0.0 --port "$WEB_PORT" --strictPort >>"$WEB_LOG" 2>&1 &
web_pid=$!
echo "$web_pid" >"$WEB_PID_FILE"

cat >"$SERVICE_INFO" <<EOF
SUPERVISOR_PID=$$
API_PID=$api_pid
WEB_PID=$web_pid
API_PORT=$API_PORT
WEB_PORT=$WEB_PORT
API_LOG=$API_LOG
WEB_LOG=$WEB_LOG
WINDOWS_FRONTEND_URL=http://localhost:$WEB_PORT
WINDOWS_API_URL=http://localhost:$API_PORT
WINDOWS_OPENAPI_URL=http://localhost:$API_PORT/docs
EOF

wait -n "$api_pid" "$web_pid"
