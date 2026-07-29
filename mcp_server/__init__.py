"""CFOperator MCP server — a host-agnostic facade over the agent HTTP API.

Exposes triage / investigation / chat / remediation capabilities as standard
MCP tools and resources so any MCP host (Claude Desktop, Cursor, CLI, CI) can
work the fleet without a direct line to the agent. Facade only: every tool is
a thin call to an existing agent route; no OODA loop or LLM lives here.

See docs/mcp-server-plan.md for the contract and docs/mcp-server.md for host
setup.
"""
