# Continue (OpenCode) Integration

Complete guide to integrating CFOperator with Continue CLI for infrastructure operations from your terminal or IDE.

## What is Continue?

Continue (formerly OpenCode) is an AI coding assistant that supports:
- **CLI usage** (`cn` command in terminal)
- **VS Code extension** (IDE integration)
- **Model Context Protocol (MCP)** - extends AI capabilities with custom tools

## Why Integrate?

With CFOperator as an MCP server, you get:

```bash
# Instead of switching to web UI...
cn "@cfoperator why did immich restart?"

# Or in VS Code while coding...
# Select code → Ask Continue: "@cfoperator is this container healthy?"
```

**Benefits:**
- 🚀 Query infrastructure without leaving your terminal/IDE
- 🔄 One command to investigate, troubleshoot, and verify
- 📊 Access full CFOperator toolset (Prometheus, Loki, SSH, etc.)
- 💾 Shared knowledge base with web UI

## Installation Options

### Option 1: Continue CLI (Terminal)

**Install via npm:**
```bash
npm install -g @continuedev/cli
```

**If npm install fails** (permission issues on Pi):
```bash
# Use npx instead (no global install needed)
npx @continuedev/cli --help
alias cn='npx @continuedev/cli'
```

**Test it:**
```bash
cn "what is 2+2?"
```

### Option 2: Continue VS Code Extension

1. Open VS Code
2. Go to Extensions (Ctrl+Shift+X)
3. Search for "Continue"
4. Install the extension
5. Restart VS Code

## Configure MCP Integration

### 1. Rebuild CFOperator with MCP Support

```bash
cd /home/aachten/repos/cfoperator
docker compose up --build -d

# Verify MCP server works
docker exec -it cfoperator python /app/test_mcp.py
```

### 2. Configure Continue

Create or edit `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Ollama Qwen",
      "provider": "ollama",
      "model": "qwen2.5-coder:14b",
      "apiBase": "http://192.168.0.150:11434"
    }
  ],
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    }
  }
}
```

**Key points:**
- `models`: Your Ollama GPU instance (adjust IP/model as needed)
- `mcpServers.cfoperator`: Points to CFOperator's MCP server via Docker

### 3. Test the Integration

```bash
# List available tools
cn "what tools does @cfoperator have?"

# Simple query
cn "@cfoperator list containers"

# Complex investigation
cn "@cfoperator investigate the sre-agent container"
```

## Usage Examples

### Terminal (Continue CLI)

```bash
# Quick status check
cn "@cfoperator compare all hosts"

# Investigate issue
cn "@cfoperator why did prometheus restart?"

# Query metrics
cn "@cfoperator show me CPU usage for immich containers over last hour"

# Search logs
cn "@cfoperator search Loki for errors in telegraf container"

# Check alerts
cn "@cfoperator what critical alerts are firing?"

# Knowledge search
cn "@cfoperator have we seen OOM issues with immich-ml before?"
```

### VS Code (Continue Extension)

**In Chat Panel:**
```
You: @cfoperator list all containers on homelab1
[CFOperator returns container status]

You: @cfoperator investigate immich-server
[Full diagnostic report appears]
```

**Inline (Select code + Ask):**
```python
# Your code
client = docker.from_env()
container = client.containers.get("immich-server")

# Select code → Ask Continue:
# "@cfoperator is immich-server healthy right now?"
```

## Advanced Workflows

### 1. Debug-Fix-Verify Loop

```bash
# Find the issue
cn "@cfoperator why is immich-ml using so much memory?"

# Fix in code...
# (edit Dockerfile, docker-compose.yml, etc.)

# Verify
cn "@cfoperator show me immich-ml memory usage now"
```

### 2. Fleet-Wide Checks

```bash
# Morning routine
cn "@cfoperator compare all hosts and show any issues"

# Deep dive
cn "@cfoperator query Prometheus: node_memory_MemAvailable_bytes for all hosts"

# Alert correlation
cn "@cfoperator check alerts and show related log entries"
```

### 3. Investigation Automation

```bash
# Start investigation
cn "@cfoperator investigate telegraf"

# Based on findings, run follow-ups
cn "@cfoperator search knowledge: telegraf connection refused"

# Store solution
cn "@cfoperator store learning: telegraf needs INFLUXDB_TOKEN env var"
```

## Comparison: Continue CLI vs CFOperator Web UI

| Feature | Continue CLI | CFOperator Web UI |
|---------|--------------|-------------------|
| **Access** | Terminal/IDE | Browser |
| **Use Case** | Solo dev work | Team collaboration |
| **Context** | Current code | Persistent investigations |
| **Speed** | Instant (same window) | Tab switch required |
| **Visibility** | Private | Shared with team |
| **History** | Local session | Persisted in DB |
| **Best For** | Quick ops checks | Deep investigations |

**Recommendation:** Use both!
- Continue for quick checks during coding
- Web UI for team investigations and handoffs

## Troubleshooting

### Continue CLI Not Found

If `cn` command doesn't work:

```bash
# Option 1: Use npx (no install)
npx @continuedev/cli "your query"

# Option 2: Create alias
echo 'alias cn="npx @continuedev/cli"' >> ~/.bashrc
source ~/.bashrc

# Option 3: Fix npm permissions
npm config set prefix ~/.npm-global
export PATH=~/.npm-global/bin:$PATH
npm install -g @continuedev/cli
```

### MCP Server Not Responding

```bash
# Check container is running
docker compose ps cfoperator

# Test MCP server
docker exec -it cfoperator python /app/test_mcp.py

# Check logs
docker compose logs cfoperator | tail -50

# Rebuild if needed
docker compose up --build -d
```

### "@cfoperator" Not Recognized

1. Verify `~/.continue/config.json` has the `mcpServers` section
2. Restart Continue/VS Code
3. Check Continue logs for connection errors
4. Test with: `cn "list available tools"`

### Slow Responses

- **Issue:** Ollama/LLM is slow
- **Solution:** Use faster model or check GPU utilization

```bash
# Check Ollama
curl http://192.168.0.150:11434/api/tags

# Monitor GPU
ssh ollama-gpu "nvidia-smi"
```

## Configuration Options

### Custom Ollama Model

Update `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Fast Model",
      "provider": "ollama",
      "model": "qwen2.5-coder:7b",
      "apiBase": "http://192.168.0.150:11434"
    },
    {
      "title": "Smart Model",
      "provider": "ollama",
      "model": "qwen2.5-coder:32b",
      "apiBase": "http://192.168.0.150:11434"
    }
  ],
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    }
  }
}
```

Switch models in Continue UI or specify:
```bash
cn --model "Smart Model" "@cfoperator complex query here"
```

### Multiple MCP Servers

You can add other MCP servers alongside CFOperator:

```json
{
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/aachten"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "your_token_here"
      }
    }
  }
}
```

Now you can use:
- `@cfoperator` for infrastructure
- `@filesystem` for local file operations
- `@github` for GitHub operations

## Tips & Best Practices

1. **Use specific tool names in queries:**
   - ✅ "@cfoperator investigate telegraf"
   - ❌ "investigate telegraf" (might use wrong tool)

2. **Leverage multi-step workflows:**
   ```bash
   cn "@cfoperator list containers | grep -i immich"
   # Parse output, then drill down
   cn "@cfoperator investigate immich-server"
   ```

3. **Store learnings for team:**
   ```bash
   cn "@cfoperator store learning: immich-ml needs 4GB memory minimum"
   ```

4. **Check knowledge base first:**
   ```bash
   cn "@cfoperator search knowledge: OOM error"
   # Before investigating new occurrences
   ```

5. **Combine with shell commands:**
   ```bash
   # Get container IDs
   CONTAINERS=$(cn "@cfoperator list containers" | jq -r '.[].name')

   # Investigate each
   for c in $CONTAINERS; do
     echo "Checking $c..."
     cn "@cfoperator investigate $c"
   done
   ```

## Next Steps

1. **Install Continue:** Choose CLI or VS Code extension
2. **Configure MCP:** Add CFOperator to `~/.continue/config.json`
3. **Test it:** `cn "@cfoperator list containers"`
4. **Explore tools:** `cn "what can @cfoperator do?"`
5. **Create workflows:** Integrate into your daily dev routine

## Resources

- **Continue Docs:** https://docs.continue.dev/
- **MCP Protocol:** https://modelcontextprotocol.io/
- **CFOperator MCP Details:** [../MCP_INTEGRATION.md](../MCP_INTEGRATION.md)
- **CFOperator Tools:** Run `cn "@cfoperator list tools"`

## Feedback

Found a bug or want a new feature? Open an issue:
- GitHub: https://github.com/aachtenberg/cfoperator/issues
- Include: Continue version, CFOperator version, error logs
