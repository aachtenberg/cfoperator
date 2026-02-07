# CFOperator Grafana Dashboard

Comprehensive monitoring dashboard for CFOperator fleet-wide infrastructure intelligence.

## Dashboard Features

### Top Row - Key Metrics (Stats)
- **CFOperator Uptime** - How long the agent has been running
- **Agent Status** - UP/DOWN indicator (green/red background)
- **Monitored Hosts** - Count of hosts being monitored
- **Running Containers** - Total containers across fleet
- **Error Rate** - Errors per second (5m window)
- **Tools Available** - Number of registered tools (should be 18)

### Second Row - LLM Observability
- **LLM Request Rate** - Requests per minute by provider (Ollama, Groq, Gemini, etc.)
- **LLM Error Rate** - Percentage of failed LLM requests
- **Token Usage** - Tokens per minute (prompt + completion)
- **LLM Latency (p95)** - 95th percentile latency by provider
- **Fallback Activations** - How often fallback chain activates
- **Cache Hit Rate** - Embedding cache hit percentage

### Third Row - Activity Graphs
- **OODA Loop Activity** - OODA cycles, reactive sweeps, proactive sweeps
- **Tool Usage by Type** - Which tools are being called most often

### Fourth Row - LLM Deep Dive
- **LLM Requests by Provider** - Time series of requests to each LLM provider
- **Token Usage by Provider** - Stacked area chart showing prompt and completion tokens
- **LLM Latency Heatmap** - P50, P90, P99 latency by provider
- **Fallback Chain Activity** - Which fallbacks are activating (Ollama → Groq, etc.)

### Fifth Row - Infrastructure Health
- **CPU Usage by Host** - CPU % for raspberrypi, pi2, pi3, pi4, ollama-gpu
- **Memory Usage by Host** - Memory % for all hosts

### Log Panels (Comprehensive Coverage)

#### 1. CFOperator Logs (Live)
- **Purpose**: All agent logs with filterable level
- **Features**: JSON parsed, formatted as `timestamp [level] component: message`
- **Filter**: Use `$level` variable dropdown (top of dashboard)
  - All logs: `INFO|WARN|ERROR`
  - Important only: `WARN|ERROR`
  - Errors only: `ERROR`

#### 2. OODA Loop Activity
- **Purpose**: Track OODA cycle execution
- **Shows**:
  - Sweep start/completion
  - Investigation triggers
  - Alert processing
  - Proactive/reactive mode switches

#### 3. Tool Execution Logs
- **Purpose**: See what tools are being executed
- **Shows**:
  - Tool calls (ssh_execute, docker_list, etc.)
  - Tool results
  - SSH connections
  - Docker operations

#### 4. Errors & Warnings
- **Purpose**: Focus on problems
- **Shows**: Only ERROR and WARN level logs
- **Use case**: Quick triage of issues

#### 5. LLM Activity
- **Purpose**: Track LLM usage
- **Shows**:
  - LLM API calls
  - Chat messages
  - Embedding generation
  - Fallback chain switches

#### 6. Fleet Discovery & SSH Activity
- **Purpose**: Monitor fleet-wide operations
- **Shows**:
  - Host pings
  - SSH connections
  - Discovery scans
  - Remote command execution

#### 7. Knowledge Base Activity
- **Purpose**: Track learning and memory
- **Shows**:
  - Investigation creation/completion
  - Learning extraction
  - Vector embedding operations
  - Database queries

## Dashboard Variables

- **$level** - Log level filter (dropdown at top)
  - `INFO|WARN|ERROR` - All important logs
  - `WARN|ERROR` - Warnings and errors
  - `ERROR` - Errors only
  - `.*` - Everything (debug included)

## Installation

### Option 1: Upload to Grafana Cloud (Recommended)

```bash
cd grafana
./upload-dashboard.sh
```

This will:
- Create a "CFOperator" folder in Grafana Cloud
- Upload the dashboard with all panels configured
- Return a direct URL to the dashboard

**Dashboard URL**: https://aachten.grafana.net/d/cfoperator-fleet/cfoperator-fleet-monitoring

### Option 2: Import via Grafana UI

1. Log into Grafana: http://192.168.0.167:3000 (or Grafana Cloud)
2. Go to **Dashboards** → **Import**
3. Click **Upload JSON file**
4. Select `cfoperator-dashboard.json`
5. Click **Import**

### Option 3: Import via API

```bash
# Local Grafana
curl -X POST http://192.168.0.167:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d @cfoperator-dashboard.json

# Grafana Cloud (use upload-dashboard.sh instead)
```

## Required Data Sources

This dashboard requires two data sources configured in Grafana:

### 1. Prometheus
- **Name**: `prometheus` (lowercase, no spaces)
- **URL**: http://192.168.0.167:9090
- **Access**: Server (default)

### 2. Loki
- **Name**: `loki` (lowercase, no spaces)
- **URL**: http://192.168.0.167:3100
- **Access**: Server (default)

## Metrics Reference

CFOperator should expose these metrics (add to agent.py if not present):

```python
from prometheus_client import Counter, Gauge, Histogram

# OODA metrics
OODA_CYCLES = Counter('cfoperator_ooda_cycles_total', 'Total OODA cycles executed')
SWEEPS = Counter('cfoperator_sweeps_total', 'Total sweeps', ['mode'])  # mode: reactive/proactive

# Tool metrics
TOOL_CALLS = Counter('cfoperator_tool_calls_total', 'Tool executions', ['tool_name', 'result'])
TOOLS_REGISTERED = Gauge('cfoperator_tools_registered', 'Number of registered tools')

# Investigation metrics
INVESTIGATIONS = Counter('cfoperator_investigations_total', 'Total investigations', ['outcome'])

# Log metrics
LOG_MESSAGES = Counter('log_messages_total', 'Log messages', ['level', 'component'])
```

## Dashboard Sections Explained

### Why These Panels Matter

**Top Stats**: At-a-glance health check. If any stat is red/yellow, investigate.

**OODA Loop Graph**: Should show consistent activity. If flat, agent may be stuck.

**Tool Usage**: Shows which tools CFOperator uses most. High SSH activity = fleet troubleshooting.

**CPU/Memory by Host**: Spot host issues before they become problems.

**Live Logs**: Your primary troubleshooting panel. Use level filter to focus.

**Specialized Log Panels**: Each panel focuses on one aspect:
- OODA = agent logic
- Tools = what it's doing
- Errors = what's broken
- LLM = AI activity
- SSH = fleet operations
- Knowledge Base = learning/memory

## Typical Troubleshooting Workflows

### "Is CFOperator working?"
1. Check **Agent Status** stat (should be green "UP")
2. Check **OODA Loop Activity** graph (should show regular activity)
3. Scan **Errors & Warnings** panel (should be mostly empty)

### "Why did the agent restart?"
1. Set time range to include restart
2. Check **Errors & Warnings** panel for crash logs
3. Check **CFOperator Logs (Live)** for "Starting" message
4. Look at logs before restart for clues

### "What's the agent doing right now?"
1. Check **OODA Loop Activity** panel (see current sweep/investigation)
2. Check **Tool Execution Logs** panel (see active tool calls)
3. Check **Live Logs** with level=`.*` (see everything)

### "Why is raspberrypi3 having issues?"
1. Check **CPU/Memory by Host** for raspberrypi3
2. Check **Fleet Discovery & SSH Activity** for SSH errors
3. Check **Tool Execution Logs** for failed ssh_* calls
4. Use Loki filter: `{container="cfoperator"} |= "raspberrypi3"`

### "Is the LLM working?"
1. Check **LLM Activity** panel for recent calls
2. Check **Errors & Warnings** for LLM errors
3. Look for "fallback" in logs (indicates Ollama failed, used Groq)

## Auto-Refresh

Dashboard auto-refreshes every 10 seconds by default.

You can change this in the top-right:
- 10s (default) - Good for active troubleshooting
- 30s - Normal monitoring
- 1m - Background monitoring
- 5m - Long-term trending

## Tips & Tricks

### Pro Tip: Use Time Shift
Click and drag on any graph to zoom into that time range. All panels will sync.

### Pro Tip: Correlate Logs with Metrics
1. Notice spike in **Tool Usage** graph at 14:35
2. Shift-click time range around 14:35
3. All log panels now show what happened during spike

### Pro Tip: Export Logs
Click three dots (⋮) on any log panel → Inspect → Data → Download CSV

### Pro Tip: Create Alerts
Any metric panel can have alerts:
1. Edit panel
2. Alert tab
3. Create alert rule
4. Example: "Alert if error rate > 5/sec for 5min"

## Troubleshooting Dashboard Issues

### "Panels show 'No data'"
- **Check**: Are Prometheus/Loki data sources configured?
- **Check**: Is CFOperator exposing metrics? (curl http://192.168.0.111:8083/metrics)
- **Check**: Is promtail shipping logs? (check Loki)

### "Metrics missing"
- **Solution**: Add Prometheus client to agent.py (see Metrics Reference above)
- **Solution**: Ensure CFOperator has /metrics endpoint

### "Logs not showing"
- **Check**: Is container named "cfoperator"? (panel filters by this)
- **Check**: Are logs JSON formatted? (panels expect JSON)
- **Check**: Is promtail running? (check with: docker ps | grep promtail)

## Customization

Feel free to customize this dashboard:

- Add panels for specific hosts
- Add panels for specific containers
- Add alert annotations
- Change refresh intervals
- Add more variables (e.g., $host filter)

## Related Dashboards

Consider also importing:
- Node Exporter Full (for detailed host metrics)
- Loki Overview (for log infrastructure health)
- Docker Container Overview (for container details)

## Support

For issues or improvements:
- GitHub: https://github.com/aachtenberg/cfoperator/issues
- Chat: http://192.168.0.111:8083 (ask CFOperator itself!)
