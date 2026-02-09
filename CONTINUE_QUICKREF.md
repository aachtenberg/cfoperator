# Continue + CFOperator Quick Reference

One-line commands for common infrastructure ops using Continue CLI.

## Setup

```bash
# One-command setup
./setup-continue.sh

# Or manual
docker compose up --build -d
# Then configure ~/.continue/config.json (see docs/continue-integration.md)
```

## Container Operations

```bash
# List all containers
cn "@cfoperator list containers"

# Container health
cn "@cfoperator investigate <container>"

# Why did it restart?
cn "@cfoperator why did <container> restart?"

# Restart analysis (last 5 restarts)
cn "@cfoperator /why-restart <container> --count=5"

# Full investigation workflow
cn "@cfoperator /investigate-container <container>"
```

## Fleet Management

```bash
# Compare all hosts
cn "@cfoperator compare all hosts"

# Specific hosts
cn "@cfoperator /compare-hosts homelab1 homelab2"

# Host connectivity
cn "@cfoperator ping homelab1"

# Host info
cn "@cfoperator run on homelab1: uname -a"
```

## Metrics & Monitoring

```bash
# Prometheus query
cn "@cfoperator query prometheus: container_memory_usage_bytes{container=\"sre-agent\"}"

# CPU usage over time
cn "@cfoperator show CPU usage for immich containers over last hour"

# Memory pressure
cn "@cfoperator what's using memory on homelab2?"

# Disk usage
cn "@cfoperator query prometheus: node_filesystem_avail_bytes"
```

## Logs & Debugging

```bash
# Search Loki
cn "@cfoperator search logs: {container=\"telegraf\"} |= \"error\""

# Recent errors
cn "@cfoperator show me errors in prometheus logs from last 30 minutes"

# Container logs
cn "@cfoperator get logs for immich-server"

# Systemd service logs
cn "@cfoperator run on ollama-gpu: journalctl -u ollama -n 50"
```

## Alerts

```bash
# All firing alerts
cn "@cfoperator check alerts"

# Critical only
cn "@cfoperator show critical alerts"

# Specific severity
cn "@cfoperator show warning alerts"

# Alert context
cn "@cfoperator check alerts and show related log entries"
```

## Knowledge Base

```bash
# Search past investigations
cn "@cfoperator search knowledge: OOM error"

# Store solution
cn "@cfoperator store learning: immich-ml needs 4GB minimum memory"

# Find similar issues
cn "@cfoperator have we seen this error before: connection refused"

# Investigation history
cn "@cfoperator search knowledge: telegraf connection"
```

## SSH Operations

```bash
# Execute command
cn "@cfoperator run on homelab1: docker ps"

# Check service status
cn "@cfoperator check service telegraf on homelab2"

# Restart service
cn "@cfoperator restart service prometheus on homelab1"

# System info
cn "@cfoperator get system info for homelab3"

# Port check
cn "@cfoperator check port 9090 on homelab1"
```

## Network & Connectivity

```bash
# Ping host
cn "@cfoperator ping homelab1"

# Test SSH
cn "@cfoperator verify ssh to homelab2"

# Test sudo
cn "@cfoperator verify sudo on homelab1"

# Full discovery
cn "@cfoperator discover all hosts"
```

## Combined Workflows

### Morning Status Check
```bash
cn "@cfoperator summary"  # TPS report
cn "@cfoperator compare all hosts"
cn "@cfoperator check alerts"
```

### Container Investigation
```bash
cn "@cfoperator investigate telegraf"
cn "@cfoperator why did telegraf restart?"
cn "@cfoperator search knowledge: telegraf"
```

### Performance Analysis
```bash
cn "@cfoperator query prometheus: rate(container_cpu_usage_seconds_total[5m])"
cn "@cfoperator query prometheus: container_memory_usage_bytes"
cn "@cfoperator show me top 5 memory consumers"
```

### Log Debugging
```bash
cn "@cfoperator search logs: {container=\"immich\"} |= \"error\" | json"
cn "@cfoperator analyze errors in immich logs from last hour"
cn "@cfoperator what errors correlate with the 3pm incident?"
```

## Advanced Usage

### Chain Commands
```bash
# Get containers, then investigate problematic ones
CONTAINERS=$(cn "@cfoperator list containers" | jq -r '.[] | select(.status | contains("unhealthy")) | .name')
for c in $CONTAINERS; do cn "@cfoperator investigate $c"; done
```

### Scripted Monitoring
```bash
#!/bin/bash
# Monitor and alert on issues
while true; do
  ALERTS=$(cn "@cfoperator check critical alerts" | jq length)
  if [ "$ALERTS" -gt 0 ]; then
    cn "@cfoperator investigate alerts and send summary to slack"
  fi
  sleep 300
done
```

### Pre-commit Checks
```bash
# .git/hooks/pre-commit
#!/bin/bash
# Ensure infrastructure is healthy before committing
cn "@cfoperator compare all hosts" | grep -q "healthy" || {
  echo "⚠️  Infrastructure issues detected!"
  cn "@cfoperator check critical alerts"
  exit 1
}
```

## Tips & Tricks

1. **Use specific tool invocations:**
   - `cn "@cfoperator /investigate-container sre-agent"` (uses skill)
   - `cn "@cfoperator investigate sre-agent"` (LLM interprets)

2. **Leverage autocomplete:**
   ```bash
   # Define functions in ~/.bashrc
   cfo() { cn "@cfoperator $@"; }
   cfo list containers
   ```

3. **Output formatting:**
   ```bash
   # JSON output for parsing
   cn "@cfoperator list containers" | jq '.[] | .name'

   # Human-readable
   cn "@cfoperator compare hosts and format as table"
   ```

4. **Combine with other CLIs:**
   ```bash
   # Continue + kubectl
   PODS=$(kubectl get pods -o json)
   cn "@cfoperator analyze these pod metrics: $PODS"

   # Continue + jq
   cn "@cfoperator list containers" | jq '.[] | select(.memory > 1000000000)'
   ```

5. **Context in queries:**
   ```bash
   # Provide context for better answers
   cn "@cfoperator investigate immich-ml - started failing after 2AM update"
   ```

## Keyboard Shortcuts (VS Code)

- `Ctrl+L` - Open Continue chat
- `Ctrl+I` - Quick input
- `Ctrl+Shift+L` - New conversation
- `@cfoperator` - Use CFOperator tools

## Configuration

Edit `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Fast",
      "provider": "ollama",
      "model": "qwen2.5-coder:7b",
      "apiBase": "http://192.168.0.150:11434"
    },
    {
      "title": "Smart",
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

## Troubleshooting

```bash
# Continue not found
which cn || alias cn='npx @continuedev/cli'

# MCP server issues
docker exec -it cfoperator python /app/test_mcp.py

# Container not running
docker compose ps cfoperator
docker compose up -d

# Config issues
cat ~/.continue/config.json | jq .

# Test Ollama
curl http://192.168.0.150:11434/api/tags
```

## More Help

- Full guide: `docs/continue-integration.md`
- MCP details: `MCP_INTEGRATION.md`
- CFOperator docs: `README.md`
- List tools: `cn "what can @cfoperator do?"`
