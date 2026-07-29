#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$ROOT_DIR/.dev/wsl-service"
API_PID_FILE="$SERVICE_DIR/api.pid"
WEB_PID_FILE="$SERVICE_DIR/web.pid"
SUPERVISOR_PID_FILE="$SERVICE_DIR/supervisor.pid"

stop_from_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 0
  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid"
    fi
  fi
  rm -f "$pid_file"
}

stop_from_file "$API_PID_FILE"
stop_from_file "$WEB_PID_FILE"
stop_from_file "$SUPERVISOR_PID_FILE"
