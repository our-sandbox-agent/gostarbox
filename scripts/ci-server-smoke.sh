#!/usr/bin/env bash
# Live HTTP contract smoke for the control-plane server (server/): boots the
# real `npx tsx src/main.ts` process over the in-memory adapter with a random
# SANDBOX_TOKEN, then drives one cycle over the wire — 401 without token,
# create 202 → idempotent replay (same operation) → list 200 → inspect 200 →
# destroy 202 — and finally asserts the token never reached the server log.
# Contract-level only, no Chrome: the console UI's real-API wiring is #15
# runtime (client exists, page not wired), and headless-Chrome rendering of
# the built demo is already scripts/ci-smoke.sh's job.
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN="$(openssl rand -hex 24)" # never a committed constant
PORT="$(python3 -c 'import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
BASE="http://127.0.0.1:${PORT}"
SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gostarbox-server-smoke.XXXXXX")"
LOG="$SMOKE_DIR/server.log"
SERVER_PID=''
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$SMOKE_DIR"
}
trap cleanup EXIT
AUTH="Authorization: Bearer ${TOKEN}"

cd server
# Direct node process: no npx/tsx wrapper children or Linux-only setsid.
env SANDBOX_TOKEN="$TOKEN" PORT="$PORT" node --import tsx src/main.ts >"$LOG" 2>&1 &
SERVER_PID=$!
cd ..

# health-wait: any HTTP response (401 without a token counts) means listening
UP=0
for _ in $(seq 1 30); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server-smoke: server exited before listening"; cat "$LOG"; exit 1
  fi
  if curl -s --max-time 2 -o /dev/null "$BASE/v1/sandboxes"; then UP=1; break; fi
  sleep 1
done
if [ "$UP" -ne 1 ]; then
  echo "server-smoke: server did not come up on $BASE"; cat "$LOG"; exit 1
fi

fail() { echo "server-smoke: $1"; cat "$LOG" 2>/dev/null; exit 1; }

# 401 without token
CODE="$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/v1/sandboxes")"
[ "$CODE" = 401 ] || fail "expected 401 without token, got $CODE"

# create 202 with Idempotency-Key
IDEM="smoke-$(openssl rand -hex 8)"
BODY='{"agent":"claude","resources":{"milli_cpu":1000,"memory_bytes":1073741824,"volume_bytes":1073741824}}'
CODE="$(curl -s --max-time 5 -o "$SMOKE_DIR/create.json" -w '%{http_code}' -X POST "$BASE/v1/sandboxes" \
  -H "$AUTH" -H 'Content-Type: application/json' -H "Idempotency-Key: $IDEM" -d "$BODY")"
[ "$CODE" = 202 ] || fail "expected 202 create, got $CODE"
SID="$(jq -r .sandbox_id "$SMOKE_DIR/create.json")"
OP="$(jq -r .operation.operation_id "$SMOKE_DIR/create.json")"
[ "$SID" != null ] && [ "$OP" != null ] || fail "create response missing sandbox_id/operation"

# same-key, same-body replay returns the same sandbox + operation
CODE="$(curl -s --max-time 5 -o "$SMOKE_DIR/replay.json" -w '%{http_code}' -X POST "$BASE/v1/sandboxes" \
  -H "$AUTH" -H 'Content-Type: application/json' -H "Idempotency-Key: $IDEM" -d "$BODY")"
[ "$CODE" = 202 ] || fail "expected 202 on idempotent replay, got $CODE"
[ "$(jq -r .sandbox_id "$SMOKE_DIR/replay.json")" = "$SID" ] || fail "replay returned a different sandbox_id"
[ "$(jq -r .operation.operation_id "$SMOKE_DIR/replay.json")" = "$OP" ] || fail "replay returned a different operation"

# list 200, contains the created sandbox
CODE="$(curl -s --max-time 5 -o "$SMOKE_DIR/list.json" -w '%{http_code}' -H "$AUTH" "$BASE/v1/sandboxes")"
[ "$CODE" = 200 ] || fail "expected 200 list, got $CODE"
jq -e --arg sid "$SID" '.sandboxes[] | select(.sandbox_id == $sid)' "$SMOKE_DIR/list.json" >/dev/null \
  || fail "created sandbox $SID not in list"

# inspect 200 -> version for destroy's expected_version
CODE="$(curl -s --max-time 5 -o "$SMOKE_DIR/inspect.json" -w '%{http_code}' -H "$AUTH" "$BASE/v1/sandboxes/$SID")"
[ "$CODE" = 200 ] || fail "expected 200 inspect, got $CODE"
VER="$(jq -r .version "$SMOKE_DIR/inspect.json")"

# destroy 202 (Creating -> Destroying is a contract transition)
CODE="$(curl -s --max-time 5 -o "$SMOKE_DIR/destroy.json" -w '%{http_code}' -X DELETE "$BASE/v1/sandboxes/$SID" \
  -H "$AUTH" -H "Idempotency-Key: destroy-$IDEM" \
  -H 'Content-Type: application/json' \
  -d "{\"expected_version\":$VER,\"confirm_scope\":\"workspace_and_home_volumes\"}")"
[ "$CODE" = 202 ] || fail "expected 202 destroy, got $CODE"

# the bearer token must never appear in the server log
if grep -q "$TOKEN" "$LOG"; then
  echo "server-smoke: SANDBOX_TOKEN leaked into server log"; exit 1
fi

echo "server-smoke: contract cycle OK at $BASE (401 / 202+replay / 200 / 200 / 202), token not logged"
