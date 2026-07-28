"""cfop://remediations/open resource."""

import json

# Terminal rows are history, not worklist.
_CLOSED = {"resolved", "failed", "rejected"}


def register(mcp, client, settings):

    @mcp.resource("cfop://remediations/open", mime_type="application/json")
    async def open_remediations() -> str:
        """Remediation rows still in flight (not resolved/failed/rejected)."""
        listing = await client.list_remediations(limit=200)
        rows = [r for r in listing.get("remediations", [])
                if r.get("status") not in _CLOSED]
        return json.dumps({"remediations": rows, "count": len(rows)}, default=str)
