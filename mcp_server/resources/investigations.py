"""cfop://investigations/* resources."""

import json


def register(mcp, client, settings):

    @mcp.resource("cfop://investigations/recent", mime_type="application/json")
    async def recent_investigations() -> str:
        """Recent investigation summaries, newest first."""
        return json.dumps(await client.list_investigations(limit=25), default=str)

    @mcp.resource("cfop://investigations/{investigation_id}",
                  mime_type="application/json")
    async def investigation_detail(investigation_id: str) -> str:
        """Full detail for one investigation."""
        return json.dumps(
            await client.get_investigation(int(investigation_id)), default=str)
