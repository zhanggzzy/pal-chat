#!/usr/bin/env bash

set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

source_runtime_env

echo "== backend live =="
curl --fail --silent "http://127.0.0.1:${API_PORT}/health/live"
printf '\n== backend ready ==\n'
curl --fail --silent "http://127.0.0.1:${API_PORT}/health/ready"
printf '\n== frontend ==\n'
curl --fail --silent "http://127.0.0.1:${WEB_PORT}" >/dev/null
echo "OK http://127.0.0.1:${WEB_PORT}"
