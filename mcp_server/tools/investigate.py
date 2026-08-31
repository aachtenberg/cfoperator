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

        Rows carry the agent's `outcome` and the operator's `triage_action`
        (null means still untriaged). Record a verdict with
        triage_investigation; `outcome` is the agent's own conclusion and is
        not editable.

        Requires scope: read.
        """
        return await guarded(
            settings, "read", lambda: client.list_investigations(limit=limit),
            tool="list_investigations")

    # CFOP-138 landed this on console chat first, because that is where the
    # incident happened: with no write twin the model explained the gap with
    # whatever was nearby — "outcome is an immutable snapshot, go write the
    # DB", then "give me a Finding ID or Remediation ID". cfassist reaches
    # these same rows through MCP (the API hands out `cfassist attach <id>`),
    # so leaving this surface read-only just relocates the invention.
    @mcp.tool()
    async def triage_investigation(
        investigation_id: int, action: str, note: str,
    ) -> dict:
        """Record the operator's verdict on an investigation.

        action is 'resolved' (the underlying problem is handled or moot) or
        'ack' (seen and accepted, without claiming it is fixed). This is the
        only way to take an investigation out of the console's Untriaged view
        — resolving a remediation does NOT triage the investigation it came
        from. note is required: it is the only record of the reasoning.

        Writes the human verdict to `triage_action`. It does not and cannot
        rewrite the agent's own `outcome`, which later investigations cite as
        precedent.

        Requires scope: remediate.
        """
        return await guarded(
            settings, "remediate",
            lambda: client.triage_investigation(investigation_id, action, note=note),
            tool="triage_investigation")
