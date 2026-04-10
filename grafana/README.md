# CFOperator Grafana Dashboard

Comprehensive monitoring dashboard for CFOperator fleet-wide infrastructure intelligence.

An additional dashboard for the modular event runtime lives in [grafana/event-runtime-dashboard.json](/home/aachten/repos/cfoperator/grafana/event-runtime-dashboard.json). It focuses on alert throughput, queue depth, queue latency, replay health, runtime error paths, and scheduled follow-up visibility.

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
- **CPU Usage by Host** - CPU % for each host in your fleet
- **Memory Usage by Host** - Memory % for all hosts

### Sweep Findings & Recommendations
- **Open Findings** - Active unresolved sweep findings
- **Latest Sweep Severity** - Most recent sweep severity level
- **Sweeps (24h)** - Total sweeps in last 24 hours
- **Last Sweep** - Timestamp of most recent sweep
- **Sweep Findings Table** - Detailed table of all sweep findings with severity, remediation
- **Findings Over Time** - Trend of findings by severity (critical/warning/info)

### Ollama Pool & Parallel Sweeps
- **Pool Instance Health** - Status of each Ollama pool instance
- **Pool Checkouts** - Total checkout count across all instances
- **Instances In Use** - Currently active instances
- **Sweep Duration** - Parallel vs sequential sweep timing (p50/p95)
- **Per-Phase Duration** - Metrics, Logs, Containers sweep phase timing by instance
- **Checkout/Checkin Rate** - Pool operation rate per instance
- **Pool Logs** - Filtered logs for pool and parallel sweep activity

### Embedding & Log Metrics
- **Embedding Request Rate** - Rate of embedding generation requests (success/error)
- **Embedding Cache Hit Rate** - Ratio of cache hits to total embedding lookups
- **Log Messages by Level** - Log message rate broken down by ERROR/WARN/INFO

### Correlation Analysis
- **Service Correlations** - Total learned service failure correlations
- **Event Correlations (24h)** - Correlated events detected in last 24 hours
- **Max Correlation Strength** - Highest correlation strength score (0-1)
- **Metric Snapshots (24h)** - Infrastructure metric snapshots captured
- **Service Failure Patterns** - Table of services that fail together, ordered by frequency
- **Event Correlations Over Time** - Hourly trend of detected correlations
- **Recent Event Correlations** - Table with correlation strength and root cause candidates

### Notification History
- **Notifications Sent (24h)** - Count of notifications sent in last 24 hours
- **Notification Success Rate** - Delivery success percentage
- **Unread Notifications** - Notifications not yet marked as read
- **Notifications Over Time** - Stacked bar chart by severity (critical/warning/info)
- **Recent Notifications** - Table of recent notifications with delivery status

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

### Option 1: Upload to Grafana (Recommended)

```bash
cd grafana
./upload-dashboard.sh
./upload-dashboard.sh CFOperator event-runtime-dashboard.json
```

This will:
- Create a "CFOperator" folder in Grafana
- Upload the dashboard with all panels configured
- Return a direct URL to the dashboard

The upload helper supports both:

- Grafana Cloud via `GRAFANA_CLOUD_URL` and `GRAFANA_CLOUD_API_KEY`
- Local k3s Grafana via `GRAFANA_ADMIN_PASSWORD` and optional `GRAFANA_URL`

For this homelab, local Grafana defaults to `http://192.168.0.167:30091`.

### Option 2: Import via Grafana UI

1. Log into Grafana: `http://<grafana-host>:3000` (or Grafana Cloud)
2. Go to **Dashboards** → **Import**
3. Click **Upload JSON file**
4. Select `cfoperator-dashboard.json`
5. Click **Import**

For the event runtime dashboard, select `event-runtime-dashboard.json` instead.

### Option 3: Import via API

```bash
# Local Grafana
curl -X POST http://<grafana-host>:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d @cfoperator-dashboard.json

# Grafana Cloud (use upload-dashboard.sh instead)
```

## Required Data Sources

These dashboards require three data sources configured in Grafana:

### 1. Prometheus
- **Name**: `prometheus` (lowercase, no spaces)
- **URL**: `http://<prometheus-host>:9090`
- **Access**: Server (default)

### 2. Loki
- **Name**: `loki` (lowercase, no spaces)
- **URL**: `http://<loki-host>:3100`
- **Access**: Server (default)

### 3. PostgreSQL
- **UID**: `sre-postgres` on local k3s Grafana, or configure via `SRE_PG_DATASOURCE_UID`
- **Host**: `<postgres-host>:5434`
- **Database**: `sre_knowledge`
- **Used by**: Sweep Findings, Correlation Analysis, Notification History panels, and the event runtime Scheduled Tasks table

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

# Embedding metrics
EMBEDDING_REQUESTS = Counter('cfoperator_embedding_requests_total', 'Embedding requests', ['result'])
EMBEDDING_CACHE_HITS = Counter('cfoperator_embedding_cache_hits_total', 'Embedding cache hits', ['result'])

# Ollama Pool metrics
POOL_INSTANCES = Gauge('cfoperator_pool_instances', 'Pool instance status', ['instance', 'status'])
POOL_CHECKOUTS = Counter('cfoperator_pool_checkouts_total', 'Pool checkouts', ['instance', 'result'])
POOL_CHECKINS = Counter('cfoperator_pool_checkins_total', 'Pool checkins', ['instance'])
POOL_HEALTH_CHECKS = Counter('cfoperator_pool_health_checks_total', 'Pool health checks', ['instance', 'result'])

# Sweep duration metrics
SWEEP_DURATION = Histogram('cfoperator_sweep_duration_seconds', 'Sweep duration', ['mode'])
SWEEP_PHASE_DURATION = Histogram('cfoperator_sweep_phase_duration_seconds', 'Phase duration', ['phase', 'instance'])
```

### PostgreSQL Tables Used by Dashboard

The Correlation Analysis and Notification History panels query these tables directly:

- `service_correlations` — Learned service failure patterns (which services fail together)
- `event_correlations` — Correlated events with strength scores and root cause candidates
- `metric_snapshots` — Infrastructure metric snapshots captured during investigations
- `notification_history` — Notification delivery audit trail
- `sweep_reports` — Sweep findings and recommendations
- `investigations` — Investigation outcomes and tool calls
- `investigation_learnings` — Extracted learnings by type

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

### "Why is a host having issues?"
1. Check **CPU/Memory by Host** for the affected host
2. Check **Fleet Discovery & SSH Activity** for SSH errors
3. Check **Tool Execution Logs** for failed ssh_* calls
4. Use Loki filter: `{container="cfoperator"} |= "<hostname>"`

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
- **Check**: Is CFOperator exposing metrics? (`curl http://localhost:8083/metrics`)
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
