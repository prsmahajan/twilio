#!/usr/bin/env bash
# Full verification. Each suite gets a clean database and its own port so a
# running dev server on 5055 is never touched.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=5099
PY="$ROOT/venv/bin/python"

start_server() {
  lsof -ti:$PORT 2>/dev/null | xargs kill 2>/dev/null
  sleep 1
  rm -f /tmp/suite.db
  TWILIO_ACCOUNT_SID=AC00000000000000000000000000000000 \
  TWILIO_AUTH_TOKEN=test_auth_token_12345 \
  TWILIO_API_KEY=SK00000000000000000000000000000000 \
  TWILIO_API_SECRET=secret \
  TWILIO_APP_SID=AP00000000000000000000000000000000 \
  TWILIO_PHONE_NUMBER=+15550000000 \
  PUBLIC_BASE_URL=https://dialer.example.com \
  DB_PATH=/tmp/suite.db PORT=$PORT \
  "$PY" "$ROOT/app.py" > /tmp/suite_server.log 2>&1 &
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null http://localhost:$PORT/ && return 0
    sleep 0.5
  done
  echo "server failed to start"; cat /tmp/suite_server.log; return 1
}

fail=0
run_py() { rm -f "$2"; PYTHONPATH="$ROOT" "$PY" "tests/$1" 2>/dev/null | tail -1 || fail=1; }

echo "=== backend: security/toll-fraud ==="; run_py test_security.py /tmp/test_dialer_sec.db
echo "=== backend: CRM ===";                 run_py test_crm.py      /tmp/test_crm.db
echo "=== backend: bulk delete ===";         run_py test_wipe.py     /tmp/test_wipe.db

echo "=== UI: dialer core ==="
start_server || exit 1
(cd tests && node ui_test.mjs 2>&1 | tail -1) || fail=1

echo "=== UI: CRM ==="
start_server || exit 1
(cd tests && node ui_crm.mjs 2>&1 | tail -1) || fail=1

lsof -ti:$PORT 2>/dev/null | xargs kill 2>/dev/null
exit $fail
