#!/usr/bin/env bash

set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

stop_pid_file "$API_PID_FILE"
stop_pid_file "$WEB_PID_FILE"

echo "Stopped pal-chat runtime processes."
