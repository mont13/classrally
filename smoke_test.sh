#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Running unit tests ==="
python3 test_server.py 2>&1
echo ""

echo "=== Running smoke test ==="
python3 -u server.py --host 127.0.0.1 --port 8765 >/tmp/quiz_smoke.log 2>&1 &
PID=$!
cleanup() {
  kill "$PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT
sleep 2

# Extract HOST_TOKEN from server log
HOST_TOKEN=$(grep -oP 'Host token: \K[a-f0-9]+' /tmp/quiz_smoke.log || echo "")
if [[ -z "$HOST_TOKEN" ]]; then
  echo "Server log contents:"
  cat /tmp/quiz_smoke.log
  echo "ERROR: Could not extract HOST_TOKEN from server log"
  exit 1
fi
echo "SMOKE: got host token"

curl -sSf http://127.0.0.1:8765/api/health >/dev/null
echo "SMOKE: health OK"

REG1=$(curl -sSf -X POST http://127.0.0.1:8765/api/register -H 'Content-Type: application/json' -d '{"name":"SmokeA"}')
REG2=$(curl -sSf -X POST http://127.0.0.1:8765/api/register -H 'Content-Type: application/json' -d '{"name":"SmokeB"}')
P1=$(python3 - <<'PY' "$REG1"
import json,sys
print(json.loads(sys.argv[1])["player_id"])
PY
)
S1=$(python3 - <<'PY' "$REG1"
import json,sys
print(json.loads(sys.argv[1])["player_secret"])
PY
)
P2=$(python3 - <<'PY' "$REG2"
import json,sys
print(json.loads(sys.argv[1])["player_id"])
PY
)
S2=$(python3 - <<'PY' "$REG2"
import json,sys
print(json.loads(sys.argv[1])["player_secret"])
PY
)
echo "SMOKE: registration OK (got player_id + player_secret)"

# Host actions require Authorization: Bearer <host_token>
curl -sSf -X POST http://127.0.0.1:8765/api/host/action \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $HOST_TOKEN" \
  -d '{"action":"start"}' >/dev/null

# Submit requires player_secret
curl -sSf -X POST http://127.0.0.1:8765/api/submit -H 'Content-Type: application/json' \
  -d "{\"player_id\":\"$P1\",\"player_secret\":\"$S1\",\"choice\":1}" >/dev/null
curl -sSf -X POST http://127.0.0.1:8765/api/submit -H 'Content-Type: application/json' \
  -d "{\"player_id\":\"$P2\",\"player_secret\":\"$S2\",\"choice\":0}" >/dev/null
echo "SMOKE: submit OK"

# Auto-reveal can happen immediately when all players answer.
PHASE=$(curl -sSf 'http://127.0.0.1:8765/api/state?host=1' | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')
if [[ "$PHASE" == "question" ]]; then
  curl -sSf -X POST http://127.0.0.1:8765/api/host/action \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $HOST_TOKEN" \
    -d '{"action":"reveal"}' >/dev/null
fi

STATE=$(curl -sSf 'http://127.0.0.1:8765/api/state?host=1')
python3 - <<'PY' "$STATE"
import json,sys
s=json.loads(sys.argv[1])
assert s["phase"] == "reveal", s
assert s["question"]["correct_index"] == 1, s
assert s["players"][0]["score"] > 0, s
# player_id should NOT be in rankings
assert "player_id" not in s["players"][0], "player_id should not be exposed in rankings"
print("SMOKE: reveal + scoring OK")
PY

# Test host action without token fails
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8765/api/host/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"reset"}')
if [[ "$HTTP_CODE" == "403" ]]; then
  echo "SMOKE: host action without token correctly rejected (403)"
else
  echo "ERROR: host action without token returned $HTTP_CODE instead of 403"
  exit 1
fi

# Test admin endpoints
BANKS=$(curl -sSf 'http://127.0.0.1:8765/api/admin/banks')
python3 - <<'PY' "$BANKS"
import json,sys
banks=json.loads(sys.argv[1])
assert isinstance(banks, list), "banks should be a list"
print(f"SMOKE: admin banks OK ({len(banks)} banks)")
PY

AUTH=$(curl -sSf 'http://127.0.0.1:8765/api/admin/auth-status')
python3 - <<'PY' "$AUTH"
import json,sys
d=json.loads(sys.argv[1])
assert d["auth_required"] == False, "should not require auth"
print("SMOKE: auth status OK")
PY

HISTORY=$(curl -sSf -X POST http://127.0.0.1:8765/api/host/action \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $HOST_TOKEN" \
  -d '{"action":"save_history"}')
python3 - <<'PY' "$HISTORY"
import json,sys
d=json.loads(sys.argv[1])
assert d["ok"] == True, "save_history should succeed"
assert "record" in d, "should have record"
print("SMOKE: history save OK")
PY

echo ""
echo "ALL SMOKE TESTS PASSED"
