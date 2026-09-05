# Quilr AI Solutions Engineer — Technical Assessment

Four self-contained tasks covering MCP server implementation, security proxying, LLM stream guardrails, and rate-limited model routing.

---

## Task 1 — Custom MCP Server (`task1/server.py`)

Exposes two tools over **stdio transport** using the official Python MCP SDK.

### Tools
| Tool | Required inputs |
|---|---|
| `get_customer_record` | `customer_id` — must match `CUST-XXXXX` |
| `trigger_refund` | `customer_id`, `amount` (positive float), `reason` (≥10 chars) |

Validation is handled by Pydantic v2 models. Invalid input raises `ErrorCode.InvalidParams (-32602)`. All debug output goes to **stderr**; stdout carries only JSON-RPC traffic.

### Setup & run
```bash
cd task1
pip install -r requirements.txt
python server.py          # starts; communicate via stdin/stdout
```

### Quick test (manual JSON-RPC)
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-00042"}}}' \
  | python server.py
```

---

## Task 2 — MCP Security Gateway (`task2/gateway.py`)

An HTTP reverse proxy between an AI agent and a downstream MCP server.

### Auth & routing logic
- Reads `Authorization: Bearer <token>` and decodes it as a signed HS256 JWT.
- `tools/list` → always forwarded.
- `tools/call` with a tool name starting `admin_` → blocked with JSON-RPC error `-32001 Unauthorized Tool Call` unless `role == "admin"`.

### Setup & run
```bash
cd task2
pip install -r requirements.txt

# Terminal 1 — mock downstream
uvicorn mock_downstream:app --port 8001

# Terminal 2 — gateway
GATEWAY_JWT_SECRET=mysecret uvicorn gateway:app --port 8000
```

### Generating test tokens
```bash
python - <<'EOF'
import jwt
secret = "mysecret"
print("admin :", jwt.encode({"role": "admin"},  secret, algorithm="HS256"))
print("viewer:", jwt.encode({"role": "viewer"}, secret, algorithm="HS256"))
EOF
```

### Test calls
```bash
# Should succeed (tools/list, any role)
curl -s -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer <viewer_token>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Should be blocked (-32001)
curl -s -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer <viewer_token>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'

# Should succeed (admin token)
curl -s -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

---

## Task 3 — LLM Streaming Guardrail (`task3/guardrail.py`)

Proxies OpenAI-compatible streaming completions and redacts PII in real time.

### Redacted patterns
| Type | Pattern | Replacement |
|---|---|---|
| Email | RFC-5321-ish regex | `[REDACTED_EMAIL]` |
| SSN | `\d{3}-\d{2}-\d{4}` | `[REDACTED_SSN]` |
| Credit card | 16-digit groups with optional separators | `[REDACTED_CC]` |

### Cross-chunk safety
A `_PIIBuffer` holds back the last 64 characters before flushing, so a PII token split across two consecutive SSE frames is still caught. The buffer is drained at stream end.

### Setup & run
```bash
cd task3
pip install -r requirements.txt
UPSTREAM_API_KEY=sk-... uvicorn guardrail:app --port 8080
```

### Test
```bash
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role":"user","content":"Say my SSN is 123-45-6789 and email is foo@bar.com"}]
  }'
```

---

## Task 4 — Rate-Limiting & Fallback Router (`task4/router.py`)

Resilient model router with per-tenant sliding-window token quotas.

### Rate limiter
- **Algorithm**: sliding window — evicts events older than 60 s before each check.
- **Limit**: 50,000 tokens/minute per tenant (`x-tenant-id` header, defaults to `"default"`).
- **Storage**: on-disk SQLite via `aiosqlite` (`rate_limiter.db` by default, override with `RATE_LIMITER_DB`).
- Token count estimated as `len(json.dumps(body)) // 4`.

### Failover
1. Call primary endpoint with a **3 s timeout**.
2. On `429`, `asyncio.TimeoutError`, or connection error — retry against fallback.
3. If fallback also fails — return `503` with a sanitised error body (no upstream traces).

### Setup & run
```bash
cd task4
pip install -r requirements.txt
PRIMARY_LLM_URL=https://api.openai.com/v1/chat/completions \
PRIMARY_API_KEY=sk-... \
FALLBACK_LLM_URL=https://api.anthropic.com/v1/messages \
FALLBACK_API_KEY=sk-ant-... \
uvicorn router:app --port 9000
```

### Test rate limiting
```bash
# Normal request
curl -s -X POST http://localhost:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-tenant-id: tenant-abc" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'

# Exhaust quota (run many times quickly) → expect 429 with rate_limit_exceeded
```

---

## Environment variables summary

| Variable | Task | Default |
|---|---|---|
| `GATEWAY_JWT_SECRET` | 2 | `dev-secret-change-in-prod` |
| `DOWNSTREAM_MCP_URL` | 2 | `http://localhost:8001/mcp` |
| `LLM_UPSTREAM_URL` | 3 | OpenAI completions endpoint |
| `UPSTREAM_API_KEY` | 3 | _(empty)_ |
| `PRIMARY_LLM_URL` | 4 | OpenAI completions endpoint |
| `PRIMARY_API_KEY` | 4 | _(empty)_ |
| `FALLBACK_LLM_URL` | 4 | Anthropic messages endpoint |
| `FALLBACK_API_KEY` | 4 | _(empty)_ |
| `RATE_LIMITER_DB` | 4 | `rate_limiter.db` |
