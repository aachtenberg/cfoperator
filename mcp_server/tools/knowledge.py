"""search_knowledge — KB learnings search via the agent's thin search route."""

from mcp_server.tools import guarded


def register(mcp, client, settings):

    @mcp.tool()
    async def search_knowledge(query: str, limit: int = 5) -> dict:
        """Search CFOperator's knowledge base of investigation learnings.

        Hybrid semantic + keyword search when the agent's embedding service
        is up, keyword-only otherwise (the response's 'mode' field says
        which). Returns learnings with title, description, applies_when,
        and success_rate.
        Requires scope: read.
        """
        return await guarded(
            settings, "read", lambda: client.search_knowledge(query, limit=limit),
            tool="search_knowledge")
