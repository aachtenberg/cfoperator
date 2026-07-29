"""Shared plumbing for MCP tool registration.

Tools return the plan's structured error object ({"error": {code, message,
retryable}}) instead of raising, so every host sees the same host-neutral
shape whether the failure is a scope miss or an upstream outage.

Every call is audit-logged as a single structured line (tool, scope,
outcome, duration). stderr -> journald -> Loki, so tool-call history is
queryable next to the rest of the fleet's logs.
"""

import json
import logging
import time

from mcp_server.auth import ScopeError, require_scope
from mcp_server.client import UpstreamError

audit_logger = logging.getLogger("mcp_server.audit")


def error_payload(code, message, retryable=False):
    return {"error": {"code": code, "message": message, "retryable": retryable}}


async def guarded(settings, needed_scope, call, tool=None):
    """Run an upstream call behind a scope check, mapping failures to payloads."""
    started = time.monotonic()
    outcome = "ok"
    try:
        require_scope(settings.scopes, needed_scope)
        return await call()
    except ScopeError as e:
        outcome = "unauthorized"
        return error_payload("unauthorized", str(e))
    except UpstreamError as e:
        outcome = e.code
        return {"error": e.to_payload()}
    except Exception:
        outcome = "internal_error"
        raise
    finally:
        audit_logger.info(json.dumps({
            "audit": "mcp_tool_call",
            "tool": tool or getattr(call, "__name__", "?"),
            "scope": needed_scope,
            "outcome": outcome,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }, sort_keys=True))


def build_alert(summary, description="", severity="", labels=None,
                alert_id=None, extra=None):
    """Assemble the alert payload shape the agent's triage/investigate routes eat."""
    alert = {"summary": summary}
    if description:
        alert["description"] = description
    if severity:
        alert["severity"] = severity
    if labels:
        alert["labels"] = labels
    if alert_id:
        alert["alert_id"] = alert_id
    if extra:
        alert.update(extra)
    return alert
