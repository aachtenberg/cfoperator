# CFOperator Grafana Dashboard

Comprehensive monitoring dashboard for CFOperator fleet-wide infrastructure intelligence.

An additional dashboard for the modular event runtime lives in [event-runtime-dashboard.json](event-runtime-dashboard.json). It focuses on alert throughput, queue depth, queue latency, replay health, runtime error paths, scheduled follow-up visibility, and (new) the completion endpoint that receives ActionResult post-backs from the agent.

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

### HTTP Investigation Pipeline
Surfaces the path from event_runtime → agent for alert-driven investigations. Useful when `ooda.reactive_poll: false` and the agent is being driven over HTTP.
- **Investigation Queue Depth** - Pending HTTP investigations on the agent worker. Green < 8, yellow < 24, red ≥ 24 (default queue size is 32). Rising values mean the agent's single worker thread is falling behind the LLM throughput it can sustain.
- **Queue Rejections (1h)** - 503s when the queue was full. event_runtime's worker retries with backoff, so non-zero here is operationally tolerable but indicates sustained saturation.
- **Post-back Success (1h)** - `cfoperator_investigation_postback_total{status="ok"}` over the last hour. Should track the investigation completion rate.
- **Post-back Failures (1h)** - Any status other than `ok` (`http_401`, `http_5xx`, `transport_error`, ...). Non-zero means the agent finished an investigation but couldn't durably hand the result back to event_runtime.
- **Pipeline Time Series** - Queue depth as a line plus post-back outcomes per minute split by status label, so you can see where the bottleneck or failure pattern is over time.

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

- **Sweep Duration (p50 / p95)** - Wall time of the sweep, by mode
- **Sweep Logs** - Filtered logs for sweep activity (parallel sweeps, sweep_graph, fan-out)

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

In the homelab these boards are **provisioned from homelab-infra**
(`k3s/base/monitoring/files/grafana-dashboards/`, folder *Homelab*), and the
files in this directory are byte-identical mirrors of those copies so the
product ships the dashboards it is instrumented for. Edit there, copy here in
the same round; the old `upload-dashboard.sh` path is gone because provisioning
replaced it and its hand-created folder and datasources were the leftovers
HOMELAB-18 cleaned up.

Elsewhere, import the JSON directly:

1. Go to **Dashboards** → **Import**, upload `cfoperator-dashboard.json`
   (or `event-runtime-dashboard.json`), pick the Prometheus / Loki / PostgreSQL
   datasources when prompted, click **Import**.
2. Or via the API:

```bash
curl -X POST http://<grafana-host>:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d "{\"dashboard\": $(cat cfoperator-dashboard.json), \"overwrite\": true}"
```

Prometheus and Loki are selected through the `datasource` / `loki` dashboard
variables; the PostgreSQL panels expect a datasource with uid `sre-postgres`.

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

# Sweep duration metrics
SWEEP_DURATION = Histogram('cfoperator_sweep_duration_seconds', 'Sweep duration', ['mode'])
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

## Event Runtime — Alert Triage

New section in [event-runtime-dashboard.json](event-runtime-dashboard.json) that surfaces the LLM-driven triage classifier (closes [#15](https://github.com/aachtenberg/cfoperator/issues/15)). With triage enabled (`CFOP_AGENT_URL` set, `HTTPTriageDecisionEngine` registered), every alert flows through `POST /v1/triage` on the agent before any investigation runs. The classifier returns one of four actions; this section is the operational view of that decision stream.

Powered by the existing labeled counter:
```promql
cfoperator_event_runtime_decisions_total{action="log_only|notify|investigate|escalate"}
```

Panels:
- **Investigated (1h)** — full LLM-investigation alerts. This is the current "expensive" path.
- **Notify Only (1h)** — alerts the LLM judged operator-relevant but not investigation-worthy. Slack gets a single-line `[severity] <summary>` notification, no LLM investigation.
- **Log Only (1h)** — known noise (test pods, Alertmanager Watchdog, etc.). Silent — recorded but no Slack.
- **Escalated (1h)** — high-impact alerts. Threshold-colored: green=0, yellow≥1, red≥3 since sustained escalations mean ongoing impact.
- **% Triaged Away (1h)** — `(notify + log_only + escalate) / total * 100`. The efficacy headline: higher means triage is saving more LLM work. Green ≥ 20%, blue ≥ 50% (very high — sanity-check the rubric if you see this for long).
- **Triage Decisions by Action (per minute)** — Time series, one line per action. Shows the live mix and where it's drifting over time.

Operational signals:
- If `Investigated (1h)` ≈ total decisions, triage is being conservative (or there's no precedent yet in the embeddings index). Acceptable in the first day after deployment.
- If `Log Only (1h)` is non-zero and growing while real incidents are getting paged, the LLM might be over-classifying. Tune the rubric in `agent/agent.py` → `run_triage` → `system_prompt`.
- If `Escalated (1h)` is non-zero, treat as the most urgent signal regardless of other panels.

## Event Runtime — Completion Endpoint

New section in [event-runtime-dashboard.json](event-runtime-dashboard.json) that surfaces health of `POST /v1/investigations/{alert_id}/complete` — the endpoint event_runtime exposes for the agent to post completed ActionResults back through.

Powered by a single labeled counter:
```promql
cfoperator_event_runtime_completion_requests_total{outcome="recorded|auth_missing|auth_invalid|bad_request|error"}
```

Panels:
- **Completion Requests (1h)** - Total inbound to the endpoint. Should track investigation completions.
- **Recorded (1h)** - Successful recordings. Each one fires the single Slack/Discord notification with the real outcome.
- **Auth Failures (1h)** - `auth_missing` + `auth_invalid` combined. Non-zero is a security signal: it means something on the cluster network tried to hit the endpoint without (or with the wrong) X-CFOP-Token. With `CFOP_COMPLETION_SHARED_SECRET` properly set on both pods this should be flat zero. Sustained non-zero values warrant investigation.
- **Bad Requests (1h)** - Malformed body, alert_id mismatch, or invalid JSON. Non-zero usually means the agent and event_runtime have drifted on the wire shape (`{"alert": ..., "result": ...}`).
- **Completion Requests by Outcome (per minute)** - Time series with one line per outcome label so you can see when a regression started.

## Related Dashboards

In the homelab, *Node Exporter Full*, *Nodes — Health & Pressure* and *k3s Cluster
Overview* are provisioned alongside these (homelab-infra, HOMELAB-18). Elsewhere,
import Node Exporter Full (grafana.com 1860) for host detail.

## Support

For issues or improvements:
- GitHub: https://github.com/aachtenberg/cfoperator/issues
