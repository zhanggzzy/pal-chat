#!/usr/bin/env bash

set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ensure_runtime_dirs
source_runtime_env

if [[ -f "$API_PID_FILE" ]] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
  echo "API is already running with PID $(cat "$API_PID_FILE")." >&2
  exit 1
fi
if [[ -f "$WEB_PID_FILE" ]] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
  echo "Web UI is already running with PID $(cat "$WEB_PID_FILE")." >&2
  exit 1
fi
if port_is_listening "$API_PORT"; then
  echo "Backend port ${API_PORT} is already in use." >&2
  exit 1
fi
if port_is_listening "$WEB_PORT"; then
  echo "Frontend port ${WEB_PORT} is already in use." >&2
  exit 1
fi

cd "$ROOT_DIR"

: >"$API_LOG"
: >"$WEB_LOG"

nohup uv run uvicorn pal_chat_server.main:app --app-dir apps/server/src --host 127.0.0.1 --port "$API_PORT" >>"$API_LOG" 2>&1 < /dev/null &
api_pid=$!
echo "$api_pid" >"$API_PID_FILE"

nohup npm --prefix apps/web run dev -- --host 127.0.0.1 --port "$WEB_PORT" --strictPort >>"$WEB_LOG" 2>&1 < /dev/null &
web_pid=$!
echo "$web_pid" >"$WEB_PID_FILE"

wait_for_http "http://127.0.0.1:${API_PORT}/health/ready" "backend ready health"
wait_for_http "http://127.0.0.1:${WEB_PORT}" "frontend"

cat <<EOF
Started pal-chat.
Frontend: http://127.0.0.1:${WEB_PORT}
Backend: http://127.0.0.1:${API_PORT}
Docs: http://127.0.0.1:${API_PORT}/docs
Data root: $CURRENT_DATA_ROOT
Legacy read-only root: $LEGACY_DATA_ROOT
EOF
