"""
Mock downstream MCP server — used to validate the gateway in Task 2.
Run on port 8001:  uvicorn mock_downstream:app --port 8001
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Downstream MCP")

_TOOLS = [
    {"name": "get_status", "description": "Return system health status."},
    {"name": "admin_reset_key", "description": "Rotate a tenant API key (admin only)."},
    {"name": "admin_revoke_session", "description": "Force-expire a user session (admin only)."},
]


@app.post("/mcp")
async def handle(request: Request) -> JSONResponse:
    body = await request.json()
    method: str = body.get("method", "")
    rpc_id = body.get("id")

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"tools": _TOOLS},
        })

    if method == "tools/call":
        tool = (body.get("params") or {}).get("name", "unknown")
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "content": [{"type": "text", "text": f"{tool} executed successfully"}]
            },
        })

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": "Method not found"},
    })
