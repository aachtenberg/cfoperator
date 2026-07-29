"""start_investigation / get_investigation / list_investigations."""

from mcp_server.tools import build_alert, guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def start_investigation(
        summary: str,
        description: str = "",
        severity: str = "",
        labels: dict[str, str] | None = None,
        alert_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Enqueue an asynchronous CFOperator investigation for an alert.

        Returns immediately with {status, alert_id, queue_depth}; poll
        list_investigations / get_investigation for the outcome. Retries
        carrying the same idempotency_key (or alert_id) within the agent's
        dedup window (default 1h) return status='deduped' instead of
        double-enqueuing.
        Requires scope: investigate.
        """
        alert = build_alert(
            summary, description, severity, labels, alert_id,
            extra={"idempotency_key": idempotency_key} if idempotency_key else None)
        return await guarded(
            settings, "investigate", lambda: client.start_investigation(alert),
            tool="start_investigation")

    @mcp.tool()
    async def get_investigation(investigation_id: int) -> dict:
        """Fetch one investigation with full findings and conclusion.

        Requires scope: read.
        """
        return await guarded(
            settings, "read", lambda: client.get_investigation(investigation_id),
            tool="get_investigation")

    @mcp.tool()
    async def list_investigations(limit: int = 20) -> dict:
        """List recent investigations (summary rows, newest first).

        Requires scope: read.
        """
        return await guarded(
            settings, "read", lambda: client.list_investigations(limit=limit),
            tool="list_investigations")
