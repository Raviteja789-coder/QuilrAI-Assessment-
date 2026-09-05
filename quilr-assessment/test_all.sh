#!/usr/bin/env bash
# Run from quilr-assessment/ root: bash test_all.sh
set -euo pipefail

# Use the project venv
VENV="$(cd "$(dirname "$0")" && pwd)/.venv"
if [[ ! -d "$VENV" ]]; then
  echo "Creating virtualenv..."
  python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GRN}PASS${NC}  $*"; }
fail() { echo -e "${RED}FAIL${NC}  $*"; }
info() { echo -e "\n${YLW}────${NC}  $*"; }

wait_ready() {
  local port=$1 tries=0
  until curl -sf "http://localhost:$port" >/dev/null 2>&1 || \
        curl -sf "http://localhost:$port/mcp" >/dev/null 2>&1 || \
        curl -so /dev/null "http://localhost:$port/v1/chat/completions" 2>&1 | grep -q ""; do
    sleep 0.3; ((tries++))
    [[ $tries -gt 30 ]] && { echo "Port $port never opened"; exit 1; }
  done
}

kill_bg() { kill "$@" 2>/dev/null || true; }

# ── install deps ─────────────────────────────────────────────────────────────

info "Installing dependencies"
pip install -q -r task1/requirements.txt -r task2/requirements.txt \
               -r task3/requirements.txt -r task4/requirements.txt \
               fastapi uvicorn httpx

# ═══════════════════════════════════════════════════════════════════════════
# TASK 1 — MCP server (uses proper MCP handshake via asyncio subprocess)
# ═══════════════════════════════════════════════════════════════════════════
info "Task 1 — MCP server"

python3 - <<'PYEOF'
import asyncio, sys, json, os
sys.path.insert(0, os.getcwd())

INIT = json.dumps({'jsonrpc':'2.0','id':0,'method':'initialize',
    'params':{'protocolVersion':'2024-11-05','capabilities':{},
              'clientInfo':{'name':'test','version':'0.1'}}}) + '\n'
INITIALIZED = json.dumps({'jsonrpc':'2.0','method':'notifications/initialized',
                           'params':{}}) + '\n'

CASES = [
    # (description, request, expect_is_error)
    ("get_customer_record valid",
     {"name":"get_customer_record","arguments":{"customer_id":"CUST-00042"}}, False),
    ("get_customer_record bad id (CUST-XXXXX required)",
     {"name":"get_customer_record","arguments":{"customer_id":"BADID"}}, True),
    ("trigger_refund negative amount",
     {"name":"trigger_refund","arguments":{"customer_id":"CUST-00001","amount":-5.0,"reason":"defective unusable product"}}, True),
    ("trigger_refund reason too short (<10 chars)",
     {"name":"trigger_refund","arguments":{"customer_id":"CUST-00001","amount":9.99,"reason":"too short"}}, True),
    ("trigger_refund valid",
     {"name":"trigger_refund","arguments":{"customer_id":"CUST-00001","amount":29.99,"reason":"item was defective and completely unusable"}}, False),
]

RED='\033[0;31m'; GRN='\033[0;32m'; NC='\033[0m'

async def run():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, 'task1/server.py',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async def read():
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=4)
        return json.loads(line)

    # MCP handshake
    proc.stdin.write(INIT.encode()); await proc.stdin.drain()
    resp = await read()
    assert 'result' in resp, f"init failed: {resp}"

    proc.stdin.write(INITIALIZED.encode()); await proc.stdin.drain()

    # Run test cases
    passed = True
    for i, (desc, params, expect_err) in enumerate(CASES):
        msg = json.dumps({'jsonrpc':'2.0','id':i+1,'method':'tools/call','params':params}) + '\n'
        proc.stdin.write(msg.encode()); await proc.stdin.drain()
        resp = await read()
        is_err = resp.get('result', {}).get('isError', True) if 'result' in resp else True
        ok = is_err == expect_err
        label = f"{GRN}PASS{NC}" if ok else f"{RED}FAIL{NC}"
        print(f"  {label}  {desc}")
        if not ok:
            passed = False

    # STDIO isolation: tools/list must return pure JSON on stdout
    list_msg = json.dumps({'jsonrpc':'2.0','id':99,'method':'tools/list','params':{}}) + '\n'
    proc.stdin.write(list_msg.encode()); await proc.stdin.drain()
    resp = await read()
    ok = 'result' in resp and 'tools' in resp['result']
    print(f"  {GRN+'PASS' if ok else RED+'FAIL'}{NC}  stdout is pure JSON-RPC")

    proc.stdin.close()
    await proc.wait()

asyncio.run(run())
PYEOF

# ═══════════════════════════════════════════════════════════════════════════
# TASK 2 — MCP security gateway
# ═══════════════════════════════════════════════════════════════════════════
info "Task 2 — MCP security gateway"

uvicorn task2.mock_downstream:app --port 8001 --log-level error &
MOCK_PID=$!
uvicorn task2.gateway:app --port 8000 --log-level error &
GW_PID=$!
trap "kill_bg $MOCK_PID $GW_PID" EXIT
sleep 1.5

JWT_SECRET="dev-secret-change-in-prod"
ADMIN_TOKEN=$(python3 -c "import jwt; print(jwt.encode({'role':'admin'},  '$JWT_SECRET', algorithm='HS256'))")
VIEWER_TOKEN=$(python3 -c "import jwt; print(jwt.encode({'role':'viewer'}, '$JWT_SECRET', algorithm='HS256'))")

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
[[ "$code" == "200" ]] && pass "tools/list (viewer) → 200" || fail "tools/list viewer → $code"

resp=$(curl -s -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}')
echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['error']['code']==-32001" 2>/dev/null \
  && pass "admin_reset_key (viewer) → -32001 Unauthorized" \
  || fail "admin_reset_key viewer → $resp"

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}')
[[ "$code" == "200" ]] && pass "admin_reset_key (admin) → 200" || fail "admin_reset_key admin → $code"

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/list","params":{}}')
[[ "$code" == "401" ]] && pass "no token → 401" || fail "no token → $code"

kill_bg $MOCK_PID $GW_PID
trap - EXIT

# ═══════════════════════════════════════════════════════════════════════════
# TASK 3 — streaming guardrail (checks reassembled content, not per-line)
# ═══════════════════════════════════════════════════════════════════════════
info "Task 3 — streaming guardrail PII redaction"

uvicorn mock_llm:app --port 11434 --log-level error &
LLM_PID=$!
LLM_UPSTREAM_URL=http://localhost:11434/v1/chat/completions \
  uvicorn task3.guardrail:app --port 8080 --log-level error &
GRL_PID=$!
trap "kill_bg $LLM_PID $GRL_PID" EXIT
sleep 1.5

# Reassemble content across all SSE chunks before checking
CONTENT=$(curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock","messages":[{"role":"user","content":"test"}]}' \
  | grep '^data: ' | grep -v DONE \
  | python3 -c "
import sys,json
out=''
for line in sys.stdin:
    try:
        d=json.loads(line.strip()[6:])
        out+=d['choices'][0]['delta'].get('content','')
    except: pass
print(out)
")

echo "  Reassembled: $CONTENT"

echo "$CONTENT" | grep -qE '[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}' \
  && fail "Raw email leaked" || pass "Email redacted"
echo "$CONTENT" | grep -qE '\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b' \
  && fail "Raw SSN leaked" || pass "SSN redacted"
echo "$CONTENT" | grep -q "4111 1111 1111 1111" \
  && fail "Raw credit card leaked" || pass "Credit card redacted"
echo "$CONTENT" | grep -q "REDACTED" \
  && pass "REDACTED markers present" || fail "No REDACTED markers"

kill_bg $LLM_PID $GRL_PID
trap - EXIT

# ═══════════════════════════════════════════════════════════════════════════
# TASK 4 — rate limiter & fallback router
# ═══════════════════════════════════════════════════════════════════════════
info "Task 4 — rate limiter & fallback router"

RL_DB="/tmp/test_rl_$$.db"
uvicorn mock_llm:app --port 11434 --log-level error &
LLM_PID=$!
PRIMARY_LLM_URL=http://localhost:11434/v1/chat/completions \
  FALLBACK_LLM_URL=http://localhost:11434/v1/chat/completions \
  RATE_LIMITER_DB="$RL_DB" \
  uvicorn task4.router:app --port 9000 --log-level error &
RTR_PID=$!
trap "kill_bg $LLM_PID $RTR_PID; rm -f $RL_DB /tmp/rl_resp_$$.json" EXIT
sleep 1.5

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:9000/v1/chat/completions \
  -H "Content-Type: application/json" -H "x-tenant-id: tenant-test" \
  -d '{"model":"mock","messages":[{"role":"user","content":"hello"}]}')
[[ "$code" == "200" ]] && pass "Normal request → 200" || fail "Normal request → $code"

[[ -f "$RL_DB" ]] && pass "SQLite DB created on disk" || fail "SQLite DB not found"

code=""; resp=""
for i in $(seq 1 400); do
  resp=$(curl -s -X POST http://localhost:9000/v1/chat/completions \
    -H "Content-Type: application/json" -H "x-tenant-id: hammer" \
    -d "{\"model\":\"mock\",\"messages\":[{\"role\":\"user\",\"content\":\"$(python3 -c "import sys; sys.stdout.write('x'*600)")\"}]}")
  code=$(echo "$resp" | python3 -c "import sys; import json; print('429' if json.load(sys.stdin).get('error',{}).get('code')=='rate_limit_exceeded' else 'ok')" 2>/dev/null || echo "ok")
  [[ "$code" == "429" ]] && break
done

if [[ "$code" == "429" ]]; then
  echo "$resp" | python3 -c "
import sys,json; d=json.load(sys.stdin)
ok = 'error' in d and 'stack' not in str(d) and 'Traceback' not in str(d)
print('clean' if ok else 'dirty: '+str(d))
" 2>/dev/null | grep -q "^clean" \
    && pass "Rate limit → sanitised 429 error body" \
    || fail "Rate limit 429 body is dirty"
else
  fail "Never hit rate limit after 400 large requests"
fi

kill_bg $LLM_PID $RTR_PID
trap - EXIT
rm -f "$RL_DB" /tmp/rl_resp_$$.json 2>/dev/null

echo ""
info "All tests complete."
