#!/usr/bin/env bash

set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ensure_runtime_dirs
ensure_env_file
check_python312
check_node22
source_runtime_env

cd "$ROOT_DIR"

uv sync --group dev
npm --prefix apps/web ci
uv run alembic upgrade head

cat <<EOF
Bootstrap complete.
Python: $(python3 --version)
Node: $(node --version)
Data root: $CURRENT_DATA_ROOT
Legacy read-only root (not migrated): $LEGACY_DATA_ROOT
EOF
