"""Remediation worklist tools: list / get / approve / reject."""

from mcp_server.tools import guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def list_remediations(status: str | None = None, limit: int = 50) -> dict:
        """List remediation queue rows, optionally filtered by status.

        Statuses: queued, claimed, executing, pr-open, verifying, resolved,
        failed, needs-human, rejected.
        Requires scope: read.
        """
        return await guarded(
            settings, "read",
            lambda: client.list_remediations(status=status, limit=limit),
            tool="list_remediations")

    @mcp.tool()
    async def get_remediation(remediation_id: int) -> dict:
        """Fetch one remediation row with full detail (payload, result, PR URL).

        Requires scope: read.
        """
        return await guarded(
            settings, "read", lambda: client.get_remediation(remediation_id),
            tool="get_remediation")

    @mcp.tool()
    async def approve_remediation(remediation_id: int) -> dict:
        """Approve a remediation: hands the row to the executor (status -> queued).

        Requires scope: remediate.
        """
        return await guarded(
            settings, "remediate",
            lambda: client.approve_remediation(remediation_id),
            tool="approve_remediation")

    @mcp.tool()
    async def reject_remediation(remediation_id: int, note: str | None = None) -> dict:
        """Reject a remediation with an optional operator note.

        Requires scope: remediate.
        """
        return await guarded(
            settings, "remediate",
            lambda: client.reject_remediation(remediation_id, note=note),
            tool="reject_remediation")
