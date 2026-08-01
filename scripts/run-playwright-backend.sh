#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-8000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-4173}"
DATA_DIR="${PAL_CHAT_PLAYWRIGHT_DATA_DIR:-$ROOT_DIR/.tmp/playwright-data}"

rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"

export PAL_CHAT_DATA_DIR="$DATA_DIR"
export PAL_CHAT_DATABASE_URL="sqlite:///$DATA_DIR/catalog.sqlite"
export PAL_CHAT_CREDENTIAL_BACKEND="memory"
export PAL_CHAT_SERVER_BASE_URL="http://127.0.0.1:${API_PORT}"
export PAL_CHAT_CORS_ORIGINS="[\"http://127.0.0.1:${WEB_PORT}\",\"http://localhost:${WEB_PORT}\"]"

cd "$ROOT_DIR"

uv run alembic upgrade head
exec uv run uvicorn pal_chat_server.main:app --app-dir apps/server/src --host 127.0.0.1 --port "$API_PORT"
