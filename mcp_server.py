#!/usr/bin/env python3
"""
CFOperator MCP Server
=====================

Exposes CFOperator's infrastructure tools as an MCP (Model Context Protocol) server.
This allows Continue CLI, Claude Desktop, and other MCP clients to use CFOperator's capabilities.

Usage:
    python mcp_server.py

Configure in Continue (~/.continue/config.json):
    {
      "mcpServers": {
        "cfoperator": {
          "command": "python",
          "args": ["/path/to/cfoperator/mcp_server.py"]
        }
      }
    }
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import CFOperator components
from agent import CFOperator
import yaml

# Configure logging to stderr (stdout is used for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "msg": "%(message)s"}',
    stream=sys.stderr
)
logger = logging.getLogger("cfoperator.mcp")

# Initialize CFOperator instance
try:
    operator = CFOperator(config_path="config.yaml")
    logger.info("CFOperator initialized for MCP server")
except Exception as e:
    logger.error(f"Failed to initialize CFOperator: {e}")
    operator = None

# Create MCP server
app = Server("cfoperator")

@app.list_tools()
async def list_tools() -> List[Tool]:
    """List all available CFOperator tools."""
    return [
        Tool(
            name="investigate_container",
            description="Investigate a Docker container's health, logs, metrics, and recent issues. Returns comprehensive diagnostics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name of the container to investigate"
                    }
                },
                "required": ["container_name"]
            }
        ),
        Tool(
            name="why_restart",
            description="Analyze why a container restarted. Examines exit codes, pre-crash logs, OOM events, and restart patterns.",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {
                        "type": "string",
                        "description": "Name of the container that restarted"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of recent restarts to analyze (default: 1)",
                        "default": 1
                    }
                },
                "required": ["container_name"]
            }
        ),
        Tool(
            name="compare_hosts",
            description="Compare metrics, configurations, and container health across multiple hosts in the fleet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of host names to compare (leave empty for all hosts)"
                    }
                }
            }
        ),
        Tool(
            name="query_prometheus",
            description="Execute a PromQL query against Prometheus and return metrics data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "PromQL query to execute"
                    },
                    "lookback": {
                        "type": "string",
                        "description": "Time range (e.g., '5m', '1h', '24h')",
                        "default": "5m"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="query_loki",
            description="Search logs in Loki using LogQL query syntax.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "LogQL query (e.g., '{container=\"sre-agent\"} |= \"error\"')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of log lines to return",
                        "default": 100
                    },
                    "lookback": {
                        "type": "string",
                        "description": "Time range (e.g., '5m', '1h', '24h')",
                        "default": "1h"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="list_containers",
            description="List all Docker containers with their status, health, and resource usage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host name (leave empty for local host)"
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status (running, stopped, all)",
                        "default": "running"
                    }
                }
            }
        ),
        Tool(
            name="check_alerts",
            description="Get current firing alerts from Alertmanager with details and context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity (critical, warning, info, all)",
                        "default": "all"
                    }
                }
            }
        ),
        Tool(
            name="search_knowledge",
            description="Search the knowledge base for similar issues, learnings, and investigation history.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query or problem description"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="ping_host",
            description="Check if a host is reachable and measure network latency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host name or IP address"
                    }
                },
                "required": ["host"]
            }
        ),
        Tool(
            name="ssh_exec",
            description="Execute a command on a remote host via SSH.",
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host name from config"
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to execute"
                    }
                },
                "required": ["host", "command"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> List[TextContent]:
    """Execute a CFOperator tool."""

    if not operator:
        return [TextContent(
            type="text",
            text="ERROR: CFOperator not initialized. Check config.yaml and database connection."
        )]

    try:
        logger.info(f"MCP tool call: {name} with args: {arguments}")

        # Convert tool name and arguments to natural language message
        # CFOperator's handle_chat_message will process it with full tool support
        message = _build_message(name, arguments)

        # Execute via CFOperator's chat handler (which supports tools and skills)
        result = operator.handle_chat_message(
            message=message,
            history=[],
            backend='auto'
        )

        response = result.get('response', 'No response')

        return [TextContent(type="text", text=response)]

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=f"ERROR: {str(e)}"
        )]

def _build_message(tool_name: str, args: Dict) -> str:
    """Convert MCP tool call to natural language message for CFOperator."""

    if tool_name == "investigate_container":
        return f"/investigate-container {args['container_name']}"

    elif tool_name == "why_restart":
        container = args["container_name"]
        count = args.get("count", 1)
        if count > 1:
            return f"/why-restart {container} --count={count}"
        return f"/why-restart {container}"

    elif tool_name == "compare_hosts":
        hosts = args.get("hosts", [])
        if hosts:
            return f"/compare-hosts {' '.join(hosts)}"
        return "/compare-hosts"

    elif tool_name == "query_prometheus":
        query = args["query"]
        lookback = args.get("lookback", "5m")
        return f"Query Prometheus for the last {lookback}: {query}"

    elif tool_name == "query_loki":
        query = args["query"]
        limit = args.get("limit", 100)
        lookback = args.get("lookback", "1h")
        return f"Search Loki logs (last {lookback}, limit {limit}): {query}"

    elif tool_name == "list_containers":
        host = args.get("host")
        status = args.get("status", "running")
        if host:
            return f"List {status} containers on {host}"
        return f"List {status} containers"

    elif tool_name == "check_alerts":
        severity = args.get("severity", "all")
        if severity != "all":
            return f"Show {severity} alerts"
        return "Show all firing alerts"

    elif tool_name == "search_knowledge":
        query = args["query"]
        limit = args.get("limit", 5)
        return f"Search knowledge base (limit {limit}): {query}"

    elif tool_name == "ping_host":
        host = args["host"]
        return f"Ping host {host}"

    elif tool_name == "ssh_exec":
        host = args["host"]
        command = args["command"]
        return f"Execute on {host}: {command}"

    else:
        return f"Unknown tool: {tool_name}"

async def main():
    """Run the MCP server."""
    logger.info("Starting CFOperator MCP Server...")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
