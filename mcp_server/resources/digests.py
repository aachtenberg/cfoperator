"""cfop://digest/morning resource."""

import json


def register(mcp, client, settings):

    @mcp.resource("cfop://digest/morning", mime_type="application/json")
    async def morning_digest() -> str:
        """Latest morning summary (stored as a sweep report by the agent)."""
        listing = await client.list_sweep_reports(limit=30)
        for report in listing.get("reports", []):
            meta = report.get("sweep_meta") or {}
            if meta.get("type") == "morning_summary":
                return json.dumps({
                    "date": report.get("swept_at"),
                    "summary": report.get("summary"),
                    # full_text present for summaries stored after the
                    # phase-2 agent change; older rows only have the
                    # 500-char findings truncation.
                    "text": meta.get("full_text")
                            or (report.get("findings") or [{}])[0].get("finding"),
                }, default=str)
        return json.dumps({"error": "no morning summary found in recent sweep reports"})
