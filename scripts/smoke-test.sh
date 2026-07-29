#!/usr/bin/env bash

set -euo pipefail

API_BASE="${1:-http://127.0.0.1:8000}"

curl --fail --silent "$API_BASE/health/live" >/dev/null
curl --fail --silent "$API_BASE/health/ready" >/dev/null

conversation_payload="$(curl --fail --silent \
  -H 'Content-Type: application/json' \
  -d '{"title":"Smoke test"}' \
  "$API_BASE/api/v1/conversations")"

conversation_id="$(printf '%s' "$conversation_payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["conversation"]["id"])')"

curl --fail --silent \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: smoke-$(date +%s)" \
  -d '{"content":"Smoke test message"}' \
  "$API_BASE/api/v1/conversations/${conversation_id}/messages" >/dev/null

curl --fail --silent "$API_BASE/api/v1/conversations/${conversation_id}" >/dev/null
curl --fail --silent "$API_BASE/api/v1/conversations/${conversation_id}/messages" >/dev/null
