done# CFOperator MCP Server Integration

CFOperator now exposes its infrastructure tools as an **MCP (Model Context Protocol) server**, allowing Continue CLI, Claude Desktop, and other MCP clients to access CFOperator's capabilities directly from your coding environment.

## Quick Start (Docker - Recommended)

```bash
# 1. Rebuild container with MCP support
cd /home/aachten/repos/cfoperator
docker compose up --build -d

# 2. Test MCP server
docker exec -it cfoperator python /app/test_mcp.py

# 3. Configure Continue (~/.continue/config.json)
{
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    }
  }
}

# 4. Use it!
# In Continue: @cfoperator list containers
```

## What You Get

When connected via MCP, Continue (or any MCP client) can:

- 🔍 **Investigate containers** - Full diagnostics, logs, metrics
- 🔄 **Analyze restarts** - Root cause analysis for container crashes
- 📊 **Query Prometheus/Loki** - Real-time metrics and log searches
- 🌐 **Compare hosts** - Fleet-wide health comparisons
- 🚨 **Check alerts** - Current Alertmanager firing alerts
- 💾 **Search knowledge base** - Past investigations and learnings
- 🔌 **SSH execution** - Remote command execution
- 🏓 **Ping hosts** - Network connectivity checks

## Setup

### Option A: Run via Docker (Recommended)

Since CFOperator runs in Docker, the MCP server can run inside the container:

```bash
# The mcp package is already in requirements.txt
# Just rebuild the container
cd /home/aachten/repos/cfoperator
docker compose up --build -d
```

Then configure Continue to use the container:

```json
{
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    }
  }
}
```

### Option B: Run Locally (Development)

For local development/testing, use a virtual environment to avoid the externally-managed-environment error:

```bash
cd /home/aachten/repos/cfoperator

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 1. Configure Continue CLI

Edit `~/.continue/config.json` and add CFOperator as an MCP server:

```json
{
  "mcpServers": {
    "cfoperator": {
      "command": "python",
      "args": ["/home/aachten/repos/cfoperator/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/home/aachten/repos/cfoperator"
      }
    }
  }
}
```

**Adjust the path/command** based on which option you chose:
- **Option A (Docker)**: Use the docker exec command above
- **Option B (Local)**: Use the python path after activating venv

### 2. Test the Connection

**If using Docker:**
```bash
# Verify MCP server works inside container
docker exec -it cfoperator python /app/test_mcp.py
```

**If using local venv:**
```bash
# Activate venv first
source venv/bin/activate
# In your terminal
cd /home/aachten/repos/cfoperator
python mcp_server.py
```

The server will start and wait for MCP client connections via stdio.

### 4. Use from Continue

In your IDE with Continue installed:

```
@cfoperator investigate the sre-agent container
```

Or in Continue CLI:

```bash
cn "@cfoperator why did prometheus restart?"
```

## Available Tools

### `investigate_container`
**Usage:** `@cfoperator investigate <container_name>`

Performs comprehensive container investigation:
- Current status and health
- Recent logs with error analysis
- Prometheus metrics (CPU, memory, restarts)
- Configuration changes
- Past investigation history

**Example:**
```
@cfoperator investigate the immich-server container
```

### `why_restart`
**Usage:** `@cfoperator why did <container> restart?`

Analyzes container restart root cause:
- Exit code analysis
- Pre-crash logs
- OOM detection
- Restart patterns
- Historical context

**Example:**
```
@cfoperator why did sre-agent restart 5 times?
```

### `compare_hosts`
**Usage:** `@cfoperator compare hosts [host1, host2, ...]`

Compares metrics and health across fleet:
- Container health status
- Resource usage trends
- Alert status
- Configuration drift

**Example:**
```
@cfoperator compare all hosts
```

### `query_prometheus`
**Usage:** `@cfoperator query prometheus: <promql>`

Executes PromQL queries:

**Examples:**
```
@cfoperator query prometheus: container_memory_usage_bytes{container="sre-agent"}
@cfoperator what's the CPU usage for immich containers over the last hour?
```

### `query_loki`
**Usage:** `@cfoperator search logs: <logql>`

Searches Loki logs with LogQL:

**Examples:**
```
@cfoperator search logs: {container="sre-agent"} |= "error"
@cfoperator show me errors in prometheus logs from the last 30 minutes
```

### `list_containers`
**Usage:** `@cfoperator list containers on <host>`

Lists Docker containers with status:

**Example:**
```
@cfoperator list all containers on homelab1
```

### `check_alerts`
**Usage:** `@cfoperator check alerts`

Gets current firing alerts from Alertmanager:

**Example:**
```
@cfoperator what critical alerts are firing?
```

### `search_knowledge`
**Usage:** `@cfoperator search knowledge: <query>`

Searches investigation history and learnings:

**Example:**
```
@cfoperator have we seen OOM issues with immich before?
```

### `ping_host`
**Usage:** `@cfoperator ping <host>`

Checks host connectivity:

**Example:**
```
@cfoperator is homelab2 reachable?
```

### `ssh_exec`
**Usage:** `@cfoperator run on <host>: <command>`

Executes command on remote host:

**Example:**
```
@cfoperator run on homelab1: docker stats --no-stream
```

## Architecture

```
┌─────────────────┐
│  Continue CLI   │
│  (or IDE)       │
└────────┬────────┘
         │ MCP Protocol (stdio)
         │
┌────────▼────────┐
│  mcp_server.py  │  ← Translates MCP calls to CFOperator methods
└────────┬────────┘
         │
┌────────▼────────┐
│  CFOperator     │
│  - Tools        │
│  - Skills       │
│  - Knowledge    │
└─────────────────┘
```

## Benefits vs Direct API

**MCP Server:**
- ✅ IDE-integrated (Continue)
- ✅ Natural language interface
- ✅ Type-safe tool schemas
- ✅ Automatic discovery
- ✅ Works with multiple LLM providers

**Direct Web UI:**
- ✅ Multi-user access
- ✅ Persistent sessions
- ✅ Visual feedback
- ✅ Team collaboration
- ✅ Browser-based (no install)

## Best Practices

1. **For solo work in IDE:** Use `@cfoperator` via Continue
2. **For team investigations:** Use web UI at `http://<host>:8083`
3. **For automation:** Use CFOperator's REST API
4. **For complex workflows:** Chain MCP tool calls

## Example Workflows

### Debug a Container Issue
```
You: @cfoperator investigate sre-agent
[CFOperator runs full diagnostic]

You: @cfoperator why did it restart?
[CFOperator analyzes exit codes and logs]

You: @cfoperator search knowledge: sre-agent crashes
[CFOperator finds similar past issues]
```

### Fleet Health Check
```
You: @cfoperator compare all hosts
[CFOperator shows health across fleet]

You: @cfoperator show me critical alerts
[CFOperator lists firing alerts]

You: @cfoperator query prometheus: up{job="node-exporter"}
[CFOperator shows host availability]
```

### Log Investigation
```
You: @cfoperator search logs: {container="immich-server"} |= "error" for the last hour
[CFOperator fetches error logs]

You: @cfoperator what does the knowledge base say about this error?
[CFOperator searches past learnings]
```

## Troubleshooting

### "externally-managed-environment" Error

If you see this error when running `pip install`:

```
error: externally-managed-environment
× This environment is externally managed
```

**Solution 1 (Recommended):** Use Docker
```bash
# MCP server runs inside the container
docker compose up --build -d
# Configure Continue to use: docker exec -i cfoperator python /app/mcp_server.py
```

**Solution 2:** Use a virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Solution 3:** Install to user directory (not recommended for MCP)
```bash
pip install --user -r requirements.txt
```

### MCP Server Won't Start

Check:
1. **If using Docker:** Container is running: `docker compose ps cfoperator`
2. **If using venv:** Virtual environment is activated: `source venv/bin/activate`
3. `config.yaml` exists and database is reachable
4. PostgreSQL connection is working

Test manually:
```bash
# Docker
docker exec -it cfoperator python /app/test_mcp.py

# Local venv
source venv/bin/activate
cd /home/aachten/repos/cfoperator
python mcp_server.py
```

### Continue Can't Find Tools

1. Check `~/.continue/config.json` has correct path
2. Restart Continue CLI or IDE
3. Check Continue logs for connection errors

### Permission Errors

Ensure CFOperator has access to:
- Docker socket (`/var/run/docker.sock`)
- SSH keys (`~/.ssh`)
- Config file (`config.yaml`)

## Next Steps

1. **Try it now:** Add to Continue config and test with `@cfoperator list containers`
2. **Explore tools:** Ask Continue "what can @cfoperator do?"
3. **Create workflows:** Combine with coding tasks - "fix this bug and @cfoperator check if the container recovered"
4. **Share feedback:** Open issues for new tools you'd like to see

## Technical Details

- **Protocol:** MCP (Model Context Protocol)
- **Transport:** stdio (stdin/stdout)
- **SDK:** `mcp>=0.9.0`
- **Compatibility:** Continue CLI, Claude Desktop, any MCP client
- **Async:** Full async/await support for parallel tool calls
