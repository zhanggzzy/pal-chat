#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-28000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-28173}"
DATA_DIR="${PAL_CHAT_DATA_DIR:-$ROOT_DIR/var/runtime-data/perf-baseline}"

cleanup() {
  PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/stop.sh" >/dev/null 2>&1 || true
}

trap cleanup EXIT

PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" PAL_CHAT_DATA_DIR="$DATA_DIR" "$ROOT_DIR/scripts/stop.sh" >/dev/null 2>&1 || true
rm -rf "$DATA_DIR"
PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" PAL_CHAT_DATA_DIR="$DATA_DIR" "$ROOT_DIR/scripts/start.sh" >/dev/null
uv run python "$ROOT_DIR/scripts/perf-baseline.py" \
  --base-url "http://127.0.0.1:${API_PORT}" \
  --web-base-url "http://127.0.0.1:${WEB_PORT}" \
  "$@"
