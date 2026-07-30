#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${PAL_CHAT_API_PORT:-8000}"
WEB_PORT="${PAL_CHAT_WEB_PORT:-5173}"
RUNTIME_DIR="$ROOT_DIR/.dev/runtime"
RUNTIME_ENV_FILE="$RUNTIME_DIR/runtime.env"
API_PID_FILE="$RUNTIME_DIR/api.pid"
WEB_PID_FILE="$RUNTIME_DIR/web.pid"
API_LOG="$ROOT_DIR/var/log/api.log"
WEB_LOG="$ROOT_DIR/var/log/web.log"
CURRENT_DATA_ROOT="$ROOT_DIR/var/runtime-data/current"
LEGACY_DATA_ROOT="$ROOT_DIR/var/data"

ensure_runtime_dirs() {
  mkdir -p "$RUNTIME_DIR" "$ROOT_DIR/var/log" "$CURRENT_DATA_ROOT"
}

ensure_env_file() {
  if [[ ! -f "$ROOT_DIR/.env" ]]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  fi
}

check_python312() {
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 - <<'PY'
import sys
assert sys.version_info[:2] == (3, 12), sys.version
PY
    return 0
  fi
  python3 - <<'PY'
import sys
assert sys.version_info[:2] == (3, 12), sys.version
PY
}

check_node22() {
  node - <<'JS'
const major = Number(process.versions.node.split(".")[0]);
if (major !== 22) {
  console.error(`Expected Node 22.x but found ${process.versions.node}`);
  process.exit(1);
}
JS
}

write_runtime_env() {
  cat >"$RUNTIME_ENV_FILE" <<EOF
export PAL_CHAT_DATA_DIR="$CURRENT_DATA_ROOT"
export PAL_CHAT_CORS_ORIGINS='["http://127.0.0.1:${WEB_PORT}","http://localhost:${WEB_PORT}"]'
export PAL_CHAT_API_PORT="$API_PORT"
export PAL_CHAT_WEB_PORT="$WEB_PORT"
export PAL_CHAT_LEGACY_DATA_ROOT="$LEGACY_DATA_ROOT"
EOF
}

source_runtime_env() {
  ensure_runtime_dirs
  write_runtime_env
  # shellcheck disable=SC1090
  source "$RUNTIME_ENV_FILE"
  export PYTHONUNBUFFERED=1
}

wait_for_http() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 60); do
    if curl --fail --silent "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for ${label}: ${url}" >&2
  return 1
}

port_is_listening() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    result = sock.connect_ex(("127.0.0.1", port))
    raise SystemExit(0 if result == 0 else 1)
PY
}

stop_pid_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 0
  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pid_file"
}
