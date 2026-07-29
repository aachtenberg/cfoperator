"""Manual smoke check: spawn the MCP server over stdio against a live agent.

Usage (with the agent reachable, e.g. via port-forward or in-cluster):

    CFOP_AGENT_URL=http://127.0.0.1:8083 .venv/bin/python scripts/mcp_smoke.py

Lists tools/resources, then exercises the read path end-to-end
(list_investigations, list_remediations, cfop://remediations/open).
Read-only — never triages, enqueues, or approves anything.
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ)
    env.setdefault("CFOP_AGENT_URL", "http://127.0.0.1:8083")
    env.setdefault("CFOP_MCP_SCOPES", "read")
    env.setdefault("CFOP_MCP_TRANSPORT", "stdio")

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server"], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"tools ({len(tools.tools)}):",
                  ", ".join(sorted(t.name for t in tools.tools)))

            resources = await session.list_resources()
            print(f"resources ({len(resources.resources)}):",
                  ", ".join(sorted(str(r.uri) for r in resources.resources)))

            for tool, args in (("list_investigations", {"limit": 3}),
                               ("list_remediations", {"limit": 3})):
                result = await session.call_tool(tool, args)
                text = result.content[0].text if result.content else "<empty>"
                print(f"\n{tool}: {text[:400]}")

            open_rem = await session.read_resource("cfop://remediations/open")
            print("\ncfop://remediations/open:",
                  open_rem.contents[0].text[:400])

    print("\nsmoke OK")


if __name__ == "__main__":
    asyncio.run(main())
