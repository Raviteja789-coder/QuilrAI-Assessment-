"""
Task 1 — Custom MCP Server (stdio transport)

Two tools: get_customer_record and trigger_refund.
Strict Pydantic validation; all logs to stderr; stdout is pure JSON-RPC.

Targets mcp >= 2.0 (MCPServer / run_stdio_async API).
"""
import sys
import re
import json
import asyncio
from typing import Annotated

from pydantic.functional_validators import AfterValidator
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

_CUST_RE = re.compile(r"^CUST-\d{5}$")

mcp = MCPServer("customer-ops")


# ---------------------------------------------------------------------------
# Annotated types — validation runs at the parameter-binding level, so the
# SDK surfaces failures as proper tool errors before the function body runs.
# ---------------------------------------------------------------------------

def _check_cust_id(v: str) -> str:
    if not _CUST_RE.fullmatch(v):
        raise ValueError(f"customer_id must match CUST-XXXXX (5 digits), got '{v}'")
    return v


def _check_positive(v: float) -> float:
    if v <= 0:
        raise ValueError(f"amount must be positive, got {v}")
    return round(v, 2)


def _check_reason(v: str) -> str:
    v = v.strip()
    if len(v) < 10:
        raise ValueError(f"reason must be at least 10 characters, got {len(v)}")
    return v


CustomerIdStr = Annotated[str, AfterValidator(_check_cust_id)]
PositiveFloat = Annotated[float, AfterValidator(_check_positive)]
LongStr       = Annotated[str,  AfterValidator(_check_reason)]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_customer_record(customer_id: CustomerIdStr) -> str:
    """Retrieve a customer record by ID (format: CUST-XXXXX)."""
    print(f"[get_customer_record] {customer_id}", file=sys.stderr)

    record = {
        "customer_id": customer_id,
        "name": "Jane Doe",
        "email": "jane.doe@example.com",
        "plan": "enterprise",
        "status": "active",
        "balance_usd": 0.00,
    }
    return json.dumps(record, indent=2)


@mcp.tool()
def trigger_refund(
    customer_id: CustomerIdStr,
    amount: PositiveFloat,
    reason: LongStr,
) -> str:
    """Initiate a refund for a customer.

    Args:
        customer_id: Must match CUST-XXXXX.
        amount: Positive USD amount to refund.
        reason: Human-readable reason (minimum 10 characters).
    """
    print(
        f"[trigger_refund] {customer_id} ${amount:.2f} | {reason}",
        file=sys.stderr,
    )

    result = {
        "refund_id": "REF-00123",
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "status": "queued",
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())
