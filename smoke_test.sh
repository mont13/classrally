#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Port is configurable (default 48217). Use e.g. PORT=48999 ./smoke_test.sh
PORT="${PORT:-48217}"
BASE="http://127.0.0.1:${PORT}"

echo "=== Running unit tests ==="
python3 test_server.py 2>&1
echo ""

echo "=== Running smoke test (port ${PORT}) ==="
# Isolated data dir: smoke must not depend on (or touch) the real data/ DB
SMOKE_DATA_DIR="$(mktemp -d /tmp/quiz_smoke_data.XXXXXX)"
QUIZ_DATA_DIR="$SMOKE_DATA_DIR" python3 -u server.py --host 127.0.0.1 --port "$PORT" >/tmp/quiz_smoke.log 2>&1 &
PID=$!
cleanup() {
  kill "$PID" >/dev/null 2>&1 || true
  rm -rf "$SMOKE_DATA_DIR"
}
trap cleanup EXIT
sleep 2

# Extract HOST_TOKEN from server log (printed as "*** HOST TOKEN: <hex> ***")
HOST_TOKEN=$(grep -oiP 'HOST TOKEN: \*?\s*\K[a-f0-9]+' /tmp/quiz_smoke.log || echo "")
if [[ -z "$HOST_TOKEN" ]]; then
  echo "Server log contents:"
  cat /tmp/quiz_smoke.log
  echo "ERROR: Could not extract HOST_TOKEN from server log"
  exit 1
fi
echo "SMOKE: got host token"

curl -sSf $BASE/api/health >/dev/null
echo "SMOKE: health OK"

REG1=$(curl -sSf -X POST $BASE/api/register -H 'Content-Type: application/json' -d '{"name":"SmokeA"}')
REG2=$(curl -sSf -X POST $BASE/api/register -H 'Content-Type: application/json' -d '{"name":"SmokeB"}')
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
curl -sSf -X POST $BASE/api/host/action \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $HOST_TOKEN" \
  -d '{"action":"start"}' >/dev/null

# Submit requires player_secret
curl -sSf -X POST $BASE/api/submit -H 'Content-Type: application/json' \
  -d "{\"player_id\":\"$P1\",\"player_secret\":\"$S1\",\"choice\":1}" >/dev/null
curl -sSf -X POST $BASE/api/submit -H 'Content-Type: application/json' \
  -d "{\"player_id\":\"$P2\",\"player_secret\":\"$S2\",\"choice\":0}" >/dev/null
echo "SMOKE: submit OK"

# Auto-reveal can happen immediately when all players answer.
PHASE=$(curl -sSf "$BASE/api/state?host=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')
if [[ "$PHASE" == "question" ]]; then
  curl -sSf -X POST $BASE/api/host/action \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $HOST_TOKEN" \
    -d '{"action":"reveal"}' >/dev/null
fi

STATE=$(curl -sSf "$BASE/api/state?host=1")
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
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/host/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"reset"}')
if [[ "$HTTP_CODE" == "403" ]]; then
  echo "SMOKE: host action without token correctly rejected (403)"
else
  echo "ERROR: host action without token returned $HTTP_CODE instead of 403"
  exit 1
fi

# Test admin endpoints
BANKS=$(curl -sSf "$BASE/api/admin/banks")
python3 - <<'PY' "$BANKS"
import json,sys
banks=json.loads(sys.argv[1])
assert isinstance(banks, list), "banks should be a list"
print(f"SMOKE: admin banks OK ({len(banks)} banks)")
PY

AUTH=$(curl -sSf "$BASE/api/admin/auth-status")
python3 - <<'PY' "$AUTH"
import json,sys
d=json.loads(sys.argv[1])
assert d["auth_required"] == False, "should not require auth"
print("SMOKE: auth status OK")
PY

HISTORY=$(curl -sSf -X POST $BASE/api/host/action \
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

# --- Exam (písemka) mode smoke ---
echo ""
echo "=== Exam mode smoke ==="
FIRST_BANK=$(curl -sSf "$BASE/api/admin/banks" | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b[0]["filename"] if b else "")')
if [[ -z "$FIRST_BANK" ]]; then echo "ERROR: no banks for exam smoke"; exit 1; fi

curl -sSf -X POST $BASE/api/admin/exam/config -H 'Content-Type: application/json' \
  -d '{"time_limit_sec":600,"shuffle_questions":true,"shuffle_options":true,"auto_submit_after_blurs":0}' >/dev/null
curl -sSf -X POST $BASE/api/admin/bank/activate -H 'Content-Type: application/json' \
  -d "{\"filename\":\"$FIRST_BANK\",\"mode\":\"exam\"}" >/dev/null
MODE=$(curl -sSf "$BASE/api/mode" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')
[[ "$MODE" == "exam" ]] && echo "SMOKE: switched to exam mode" || { echo "ERROR: mode not exam ($MODE)"; exit 1; }

EREG=$(curl -sSf -X POST $BASE/api/register -H 'Content-Type: application/json' -d '{"name":"ExamA"}')
EPID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["player_id"])' "$EREG")
ESEC=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["player_secret"])' "$EREG")
curl -sSf -X POST $BASE/api/host/exam-action -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $HOST_TOKEN" -d '{"action":"open"}' >/dev/null
echo "SMOKE: exam opened"

# Answer every question correctly (map qid -> bank correct_index) and submit
ESTATE=$(curl -sSf "$BASE/api/exam/state?player_id=$EPID&secret=$ESEC")
RESULT=$(python3 - "$BASE" "$EPID" "$ESEC" "$ESTATE" "questions/$FIRST_BANK" <<'PY'
import json, sys, urllib.request
B, PID, SEC, ST, BANKPATH = sys.argv[1:6]
state = json.loads(ST)
correct = {q["id"]: q["correct_index"] for q in json.load(open(BANKPATH))}
def post(path, body):
    r = urllib.request.urlopen(urllib.request.Request(
        B+path, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"}, method="POST"))
    return json.loads(r.read())
for q in state["questions"]:
    post("/api/exam/answer", {"player_id":PID,"player_secret":SEC,"question_id":q["id"],"choice":correct[q["id"]]})
res = post("/api/exam/submit", {"player_id":PID,"player_secret":SEC})
assert res["grade"] == 1, res
assert res["score"] == res["total"], res
print("ok")
PY
)
[[ "$RESULT" == "ok" ]] && echo "SMOKE: exam answer+submit+grade OK" || { echo "ERROR: exam flow failed"; exit 1; }

curl -sSf "$BASE/api/admin/exam/results.csv" | grep -q "ExamA" && echo "SMOKE: exam CSV export OK" || { echo "ERROR: CSV missing student"; exit 1; }

echo ""
echo "ALL SMOKE TESTS PASSED"
