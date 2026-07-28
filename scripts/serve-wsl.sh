#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISTRO_NAME="${WSL_DISTRO_NAME:-Ubuntu}"
SERVICE_DIR="$ROOT_DIR/.dev/wsl-service"
SERVICE_INFO="$SERVICE_DIR/service-info.env"
RUNNER_SCRIPT="$ROOT_DIR/scripts/run-wsl-services.sh"
LAUNCHER_SCRIPT="/tmp/pal-chat-run-wsl-services.sh"

mkdir -p "$SERVICE_DIR"

if [[ -f "$SERVICE_INFO" ]] && [[ -f "$SERVICE_DIR/supervisor.pid" ]]; then
  supervisor_pid="$(cat "$SERVICE_DIR/supervisor.pid")"
  if kill -0 "$supervisor_pid" 2>/dev/null; then
    echo "WSL service is already running. Use ./scripts/stop-wsl.sh first if you need to restart it." >&2
    exit 1
  fi
fi

rm -f "$SERVICE_DIR/"*.pid "$SERVICE_INFO"

cat >"$LAUNCHER_SCRIPT" <<EOF
#!/usr/bin/env bash
exec "$RUNNER_SCRIPT"
EOF
chmod +x "$LAUNCHER_SCRIPT"

cmd.exe /c start "" /b wsl.exe -d "$DISTRO_NAME" -- bash "$LAUNCHER_SCRIPT" >/dev/null

for _ in $(seq 1 60); do
  if [[ -f "$SERVICE_INFO" ]] && [[ -f "$SERVICE_DIR/api.pid" ]] && [[ -f "$SERVICE_DIR/web.pid" ]]; then
    cat "$SERVICE_INFO"
    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for the WSL service supervisor to write service metadata." >&2
exit 1
