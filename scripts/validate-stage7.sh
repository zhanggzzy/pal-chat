#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-28000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-28173}"

cleanup() {
  PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/stop.sh" >/dev/null 2>&1 || true
}

trap cleanup EXIT

PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/stop.sh" >/dev/null 2>&1 || true
PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/bootstrap.sh"
PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/start.sh"
PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/health.sh"
PAL_CHAT_API_PORT="$API_PORT" PAL_CHAT_WEB_PORT="$WEB_PORT" "$ROOT_DIR/scripts/stop.sh"

if [[ -f "$ROOT_DIR/.dev/runtime/api.pid" || -f "$ROOT_DIR/.dev/runtime/web.pid" ]]; then
  echo "PID files still exist after stop." >&2
  exit 1
fi

echo "Stage 7 POSIX runtime validation passed."
