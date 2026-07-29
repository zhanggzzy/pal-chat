#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-8000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-5173}"

export PYTHONUNBUFFERED=1
export PAL_CHAT_DATA_DIR="${PAL_CHAT_DATA_DIR:-$ROOT_DIR/var/data}"
export PAL_CHAT_CORS_ORIGINS="${PAL_CHAT_CORS_ORIGINS:-[\"http://127.0.0.1:${WEB_PORT}\",\"http://localhost:${WEB_PORT}\"]}"

mkdir -p "$ROOT_DIR/var/data"
mkdir -p "$ROOT_DIR/.dev"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

cleanup() {
  if [[ -f "$ROOT_DIR/.dev/api.pid" ]]; then
    kill "$(cat "$ROOT_DIR/.dev/api.pid")" 2>/dev/null || true
    rm -f "$ROOT_DIR/.dev/api.pid"
  fi
  if [[ -f "$ROOT_DIR/.dev/web.pid" ]]; then
    kill "$(cat "$ROOT_DIR/.dev/web.pid")" 2>/dev/null || true
    rm -f "$ROOT_DIR/.dev/web.pid"
  fi
}

trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

uv sync --group dev
npm --prefix apps/web install
uv run alembic upgrade head

uv run uvicorn pal_chat_server.main:app --app-dir apps/server/src --host 127.0.0.1 --port "$API_PORT" &
api_pid=$!
echo "$api_pid" > "$ROOT_DIR/.dev/api.pid"

npm --prefix apps/web run dev -- --host 127.0.0.1 --port "$WEB_PORT" &
web_pid=$!
echo "$web_pid" > "$ROOT_DIR/.dev/web.pid"

cat <<EOF
pal-chat dev servers are starting.

Frontend: http://127.0.0.1:${WEB_PORT}
Backend API: http://127.0.0.1:${API_PORT}
OpenAPI docs: http://127.0.0.1:${API_PORT}/docs

Press Ctrl-C to stop both servers.
EOF

wait -n "$api_pid" "$web_pid"
