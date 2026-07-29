"""triage_alert — one-shot LLM triage of an alert."""

from mcp_server.tools import build_alert, guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def triage_alert(
        summary: str,
        description: str = "",
        severity: str = "",
        labels: dict[str, str] | None = None,
        alert_id: str | None = None,
    ) -> dict:
        """Triage an alert through CFOperator's LLM triage engine.

        Returns the triage decision (action, reasoning, confidence) without
        enqueuing anything. Blocks for the duration of the triage LLM call.
        Requires scope: investigate.
        """
        alert = build_alert(summary, description, severity, labels, alert_id)
        return await guarded(settings, "investigate", lambda: client.triage(alert))
