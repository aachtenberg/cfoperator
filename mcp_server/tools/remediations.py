"""Remediation worklist tools: list / get / approve / resolve / reject."""

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

        Refused with 409/conflict for manual-class rows — human-only work has
        nothing for the executor to mechanize. Reclassify it first
        (gitops-patch / k8s-action / node-action) or resolve it by hand.
        k8s-imperative is honest but has no runner yet: it parks for a human.

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

    @mcp.tool()
    async def resolve_remediation(remediation_id: int, note: str | None = None) -> dict:
        """Close a remediation as done (status -> resolved) with an optional note.

        Use when the work is already handled — fixed by hand, or superseded —
        rather than rejected as unwanted. The note records why and is stored on
        the row as result.resolution_note. Does NOT execute anything.
        Requires scope: remediate.
        """
        return await guarded(
            settings, "remediate",
            lambda: client.resolve_remediation(remediation_id, note=note),
            tool="resolve_remediation")
