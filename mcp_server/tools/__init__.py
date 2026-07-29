"""Shared plumbing for MCP tool registration.

Tools return the plan's structured error object ({"error": {code, message,
retryable}}) instead of raising, so every host sees the same host-neutral
shape whether the failure is a scope miss or an upstream outage.
"""

from mcp_server.auth import ScopeError, require_scope
from mcp_server.client import UpstreamError


def error_payload(code, message, retryable=False):
    return {"error": {"code": code, "message": message, "retryable": retryable}}


async def guarded(settings, needed_scope, call):
    """Run an upstream call behind a scope check, mapping failures to payloads."""
    try:
        require_scope(settings.scopes, needed_scope)
        return await call()
    except ScopeError as e:
        return error_payload("unauthorized", str(e))
    except UpstreamError as e:
        return {"error": e.to_payload()}


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
