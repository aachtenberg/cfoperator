# CFOperator Grafana Dashboards

Two boards ship here, both byte-identical mirrors of the copies the homelab
provisions (see [Installation](#installation)):

- [cfoperator-dashboard.json](cfoperator-dashboard.json), **CFOperator Fleet
  Monitoring**: the agent, its LLM calls, investigations, sweeps, and the
  fleet it watches.
- [event-runtime-dashboard.json](event-runtime-dashboard.json), **CFOperator
  Event Runtime**: alert throughput, queue and replay health, triage decisions,
  the completion endpoint, host observability and the cluster.

This page describes what is on each board and how to read it. What each metric
means, its labels and the values they take are in
[docs/METRICS.md](../docs/METRICS.md). How the LLM panels relate to the
homelab-only boards (LLM & Triage, Remediation, Traces) is in
[docs/llm-observability.md](../docs/llm-observability.md#reading-the-dashboards)
and [docs/OBSERVABILITY.md](../docs/OBSERVABILITY.md).

## CFOperator Fleet Monitoring

Refreshes every 10 seconds over the last hour. Variables: `datasource`
(Prometheus), `loki`, and `level`, the filter for the live log panel.

### Top row
- **CFOperator Uptime** — seconds since the agent started.
- **Agent Status** — Prometheus scrape health of the agent
  (`up{namespace="apps", app="cfoperator"}`). UP means `/metrics` answered on
  the last scrape. The selector matches the homelab's Kubernetes discovery;
  adjust it to your scrape labels.
- **Monitored Hosts**, **Running Containers** — fleet size as the agent last
  counted it.
- **Error Rate (5m)** — per-second rate of `cfoperator_errors_total`; yellow at
  1, red at 5.
- **Tools Available** — tools registered in the agent. The number depends on
  which integrations are configured.

### Activity and LLM
- **OODA Loop Activity** — OODA cycles, reactive sweeps and proactive sweeps
  per second. A flat line means the main loop has stopped.
- **Tool Usage by Type** — tool calls per second by tool name.
- **LLM Requests/sec by Provider**, **LLM Tokens/sec (Input/Output)**,
  **LLM Latency (p50/p95/p99)** by provider, **LLM Errors by Provider**,
  **LLM Fallback Activations**.
- **Total LLM Requests (All Time)** — a raw counter sum, so "all time" means
  since the agent last restarted.

The LLM panels count tool-loop calls only. The mutation judge and the other
one-shot completions are not in them
([docs/llm-observability.md](../docs/llm-observability.md#where-the-numbers-come-from)).

### Investigations & Knowledge Base
PostgreSQL panels, except Tool Success Rate:
- **Investigations (24h)**, **Total Learnings** (not deprecated),
  **Investigation Velocity (by Outcome)**, **Knowledge Base Growth**.
- **Embedding Coverage** — share of completed investigations with a stored
  embedding for semantic search; red below 50%, green from 95%.
- **Tool Success Rate** — `result="success"` over all tool calls since the
  agent started; yellow from 80%, green from 95%.

### HTTP Investigation Pipeline
Surfaces the path from event_runtime → agent for alert-driven investigations.
Useful when `ooda.reactive_poll: false` and the agent is being driven over
HTTP.
- **Investigation Queue Depth** — pending HTTP investigations on the agent
  worker. Green below 8, yellow below 24, red from 24 (the queue holds
  `ooda.investigation_queue_size`, default 32). Rising values mean the agent's
  single worker thread is falling behind the LLM throughput it can sustain.
- **Queue Rejections (1h)** — 503s when the queue was full. event_runtime's
  worker waits a 503 out with backoff, so non-zero here is tolerable but means
  sustained saturation.
- **Post-back Success (1h)** — `cfoperator_investigation_postback_total{status="ok"}`
  over the last hour. Should track the investigation completion rate.
- **Post-back Failures (1h)** — any other status (`http_<code>`,
  `transport_error`). Non-zero means the agent finished an investigation but
  could not hand the result back to event_runtime.
- **HTTP Investigation Pipeline** time series — queue depth as a line plus
  post-back outcomes per minute by status, so the bottleneck or failure pattern
  shows over time.

### Hosts
- **CPU Usage by Host**, **Memory Usage by Host** — node-exporter series
  (`node_cpu_seconds_total`, `node_memory_*`), not cfoperator's own.

### Log panels
All on Loki, from the agent's JSON log lines
(`{namespace="apps", container="cfoperator"}`):
- **CFOperator Logs (Live)** — every line, filtered by the `level` variable
  (`INFO|WARN|ERROR`, `WARN|ERROR`, `ERROR`, `.*`). The filter is a regex over
  the raw line, so `WARN` also matches Python's `WARNING`.
- **Errors & Warnings** — lines whose parsed `level` matches
  `error|warn|ERROR|WARN`. That match is anchored: it catches `ERROR` from the
  Python loggers and `error`/`warn` from the modules that print their own JSON,
  but not Python's `WARNING`. Use the live panel with `WARN|ERROR` for every
  warning.
- **Agent & Tools Activity** — components `cfoperator`, `tools` and
  `embedding_service`: the OODA loop, sweeps, investigations, tool calls.
- **LLM & Knowledge Base** — components `llm_fallback`, `knowledge_base` and
  `embedding_service`.
- **Sweep Logs** — lines matching `Parallel sweep|sweep_graph|Fan-out|Phase`
  (in the Sweep Findings section below).

### Sweep Findings & Recommendations
- **Open Findings**, **Latest Sweep Severity**, **Sweeps (24h)**, **Last Sweep**,
  the **Sweep Findings** table and **Findings Over Time (by Severity)** — from
  PostgreSQL (`sweep_reports`).
- **Sweep Duration (p50 / p95)** — wall time of the sweep, by mode.
- **Sweep Logs** — see above.

### Embedding & Log Metrics
- **Embedding Request Rate** — `success` and `error` embedding requests; the
  `truncated` and `unembeddable` results are not charted
  ([docs/METRICS.md](../docs/METRICS.md#embedding-operations)).
- **Embedding Cache Hit Rate** — embedding cache hits over all lookups. This
  is the embedding cache, not an LLM prompt cache.
- **Log Messages by Level** — `log_messages_total` for `ERROR`, `WARNING` and
  `INFO`. It counts the Python loggers only; lines a module prints as its own
  JSON are not in it.

### Correlation Analysis
PostgreSQL panels:
- **Service Correlations** — total learned service failure correlations.
- **Event Correlations (24h)** — correlated events detected in the last 24 hours.
- **Max Correlation Strength** — highest correlation strength score (0-1).
- **Metric Snapshots (24h)** — infrastructure metric snapshots captured.
- **Service Failure Patterns** — services that fail together, ordered by frequency.
- **Event Correlations Over Time** — hourly trend of detected correlations.
- **Recent Event Correlations** — correlation strength and root cause candidates.

### Notification History
PostgreSQL panels:
- **Notifications Sent (24h)**.
- **Notification Success Rate** — delivered share over the last 24 hours.
- **Unread Notifications** — not yet marked as read (last 7 days).
- **Notifications Over Time** — stacked by severity.
- **Recent Notifications** — with delivery status.

## CFOperator Event Runtime

Refreshes every 30 seconds over the last six hours. Variables: `datasource`
(Prometheus), `loki`, and `postgres`.

- **Top row**: Runtime Up (`up{app="cfoperator-event-runtime"}`), Alerts /
  Minute, Queue Depth, Oldest Queued Age.
- **Throughput and queue**: alert outcomes, p95 alert processing latency,
  tracked jobs by state, queue wait and processing p95, queue rejections,
  replay attempts and replayed events by sink, recorded event types.
- **Host Observability**: host discovery runs, discovered targets, host
  observation runs, by provider.
- **K3s Cluster**: node conditions, pod counts and phases by node, CPU and
  memory against allocatable, container restarts, pods and nodes not ready.
  These read kube-state-metrics and cAdvisor, not anything cfoperator emits.
- **Logs**: the agent's log stream, and error and warning lines from the
  `monitoring` namespace.
- **Audit Trail**: decision rate by action, outcomes by action, suppressed and
  logged alerts, completed actions, and a **Scheduled Tasks** table read from
  PostgreSQL.

### Alert Triage

Surfaces the LLM-driven triage classifier (closes
[#15](https://github.com/aachtenberg/cfoperator/issues/15)). With triage
enabled (`CFOP_AGENT_URL` set, so `HTTPTriageDecisionEngine` is registered),
every alert goes through `POST /v1/triage` on the agent before any
investigation runs. The one exception is an unclean-reboot alert, which the
deep tier routes straight to boot forensics without a triage call. The classifier returns one of four actions, and this
section is the operational view of that decision stream, read from
`cfoperator_event_runtime_decisions_total{action}`.

- **Investigated (1h)** — alerts sent to a full LLM investigation: the
  expensive path.
- **Notify Only (1h)** — alerts the classifier judged operator-relevant but not
  worth investigating: a notification, no investigation.
- **Log Only (1h)** — known noise (test pods, the Alertmanager Watchdog, ...).
  Recorded, and no notification is sent.
- **Escalated (1h)** — high-impact alerts. Yellow from 1, red from 3, since
  sustained escalations mean ongoing impact.
- **% Triaged Away (1h)** — every decision other than `investigate`, over all
  decisions. Higher means triage is saving more LLM work; yellow below 20%,
  green from 20%, blue from 50% (very high; sanity-check the rubric if it
  stays there).
- **Triage Decisions by Action** — one line per action, per minute.

With the deep-investigation tier on, the runtime also counts
`deep_investigate`, and an `escalate` on a workload alert is counted as the
action it was aliased to (`notify` by default). No stat shows
`deep_investigate` on its own: it appears in the per-action time series, and
"% Triaged Away" counts it as triaged away although it is the most expensive
path ([docs/METRICS.md](../docs/METRICS.md#label-values-that-are-a-closed-set)).

Operational signals:
- If `Investigated (1h)` is close to the total, triage is being conservative,
  or there is no precedent in the embeddings index yet. Expected in the first
  day after deployment.
- If `Log Only (1h)` is growing while real incidents are getting paged, the
  classifier may be over-filtering. The rubric is
  `CFOperator._triage_system_prompt` in `agent/agent.py`.
- If `Escalated (1h)` is non-zero, treat it as the most urgent signal on the
  board.

### Completion Endpoint

Health of `POST /v1/investigations/{alert_id}/complete`, the endpoint the agent
posts completed ActionResults back through, read from
`cfoperator_event_runtime_completion_requests_total{outcome}`.

- **Completion Requests (1h)** — everything that reached the endpoint. Should
  track investigation completions.
- **Recorded (1h)** — accepted and recorded. Each one sends the completion
  notification with the real outcome.
- **Auth Failures (1h)** — `auth_missing` plus `auth_invalid`. Non-zero is a
  security signal: something reached the endpoint without the right
  `X-CFOP-Token`. With `CFOP_COMPLETION_SHARED_SECRET` set on both workloads
  this should stay at zero.
- **Bad Requests (1h)** — a malformed body, an alert_id mismatch, or invalid
  JSON. Non-zero usually means the agent and event_runtime have drifted on the
  payload shape.
- **Completion Requests by Outcome** — one line per outcome, per minute, so a
  regression shows when it started.

## Installation

In the homelab these boards are **provisioned from homelab-infra**
(`k3s/base/monitoring/files/grafana-dashboards/`, folder *Homelab*), and the
files in this directory are byte-identical mirrors of those copies so the
product ships the dashboards it is instrumented for. Edit there, copy here in
the same round; the old `upload-dashboard.sh` path is gone because provisioning
replaced it and its hand-created folder and datasources were the leftovers
HOMELAB-18 cleaned up.

Elsewhere, import the JSON directly:

1. Go to **Dashboards** → **Import**, upload the JSON, pick the datasources
   Grafana prompts for (the Prometheus / Loki variables on both boards; the
   PostgreSQL variable on Event Runtime only — see below), click **Import**.
2. Or via the API:

```bash
curl -X POST http://<grafana-host>:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d "{\"dashboard\": $(cat cfoperator-dashboard.json), \"overwrite\": true}"
```

## Required Data Sources

### 1. Prometheus
- **URL**: `http://<prometheus-host>:9090`
- **Access**: Server (default)

Both boards select Prometheus through the `datasource` dashboard variable, so
any Prometheus datasource works whatever its name. It has to scrape the agent
(`:8083/metrics`) and the event runtime (`:8080/metrics`); the scrape config is
in [docs/METRICS.md](../docs/METRICS.md#prometheus-configuration).

### 2. Loki
- **URL**: `http://<loki-host>:3100`
- **Access**: Server (default)

Both boards select Loki through the `loki` dashboard variable. The log panels
filter on the `namespace` and `container` stream labels a Kubernetes log
shipper adds.

### 3. PostgreSQL

The two boards wire PostgreSQL differently, so they are not interchangeable
at import time.

**Fleet (`cfoperator-dashboard.json`)** — the Investigations & Knowledge Base,
Sweep Findings, Correlation Analysis and Notification History panels hard-code
uid `sre-postgres`. That is the uid the homelab Grafana is provisioned with
(`SRE PostgreSQL` in homelab-infra's `datasources.yaml`). A datasource
*variable* of type `grafana-postgresql-datasource` would also match TimescaleDB,
and with an empty `current` Grafana picks whichever sorts first — which is why
the homelab copy keeps this uid explicit. Grafana's import prompt will not offer
it; on import elsewhere, create a PostgreSQL datasource with uid
`sre-postgres`, or remap the panels after import.

**Event Runtime (`event-runtime-dashboard.json`)** — the Scheduled Tasks table
uses the `postgres` dashboard variable (`${postgres}`). Pick the PostgreSQL
datasource when Grafana prompts on import.

- **Database**: `sre_knowledge`, the agent's knowledge base
- **Host**: in the homelab, `sre-postgres.data.svc.cluster.local:5432` in
  cluster, or NodePort 30434 from outside it

### PostgreSQL tables the boards read

- Fleet: `investigations`, `investigation_learnings`,
  `investigation_embeddings`, `sweep_reports`, `service_correlations`,
  `event_correlations`, `metric_snapshots`, `notification_history`.
- Event Runtime: `event_runtime_events` and `apscheduler_jobs` (Scheduled
  Tasks).

## Typical Troubleshooting Workflows

### "Is CFOperator working?"
1. Check **Agent Status** (should be green "UP").
2. Check **OODA Loop Activity** (should show regular activity).
3. Scan **CFOperator Logs (Live)** with level `WARN|ERROR`.

### "Why did the agent restart?"
1. Set the time range to include the restart.
2. Check **CFOperator Logs (Live)** with level `WARN|ERROR` for crash logs, and
   for the startup messages after them.
3. Read the logs before the restart for clues.

### "What's the agent doing right now?"
1. Check **OODA Loop Activity** and **Tool Usage by Type**.
2. Check **Agent & Tools Activity** for the current sweep, investigation and
   tool calls.
3. Check **CFOperator Logs (Live)** with level `.*` to see everything.

### "Why is a host having issues?"
1. Check **CPU Usage by Host** / **Memory Usage by Host** for the affected host.
2. Check **Agent & Tools Activity** for failed `ssh_*` calls.
3. Use a Loki filter: `{container="cfoperator"} |= "<hostname>"`.

### "Is the LLM working?"
1. Check **LLM Errors by Provider** and **LLM Fallback Activations**.
2. Search **CFOperator Logs (Live)** for `[FALLBACK]`: the chain logs each
   rung it tries and each one that fails, under component `cfoperator`. The
   `llm_fallback` lines in **LLM & Knowledge Base** are the bookkeeping behind
   it: providers put in cooldown, and the circuit breaker opening.
3. [docs/llm-observability.md](../docs/llm-observability.md#debugging-with-metrics)
   has the queries for slow, failing and token-heavy LLM calls.

## Tips & Tricks

### Zoom in on a time range
Click and drag on any graph to zoom into that time range. All panels follow.

### Correlate logs with metrics
1. Notice a spike in **Tool Usage by Type** at 14:35.
2. Drag across the time range around 14:35.
3. Every log panel now shows what happened during the spike.

### Export logs
Click the three dots (⋮) on any log panel → Inspect → Data → Download CSV.

### Create alerts
Any metric panel can have an alert:
1. Edit the panel.
2. Open the Alert tab.
3. Create an alert rule, e.g. "error rate > 5/sec for 5 min".

[docs/METRICS.md](../docs/METRICS.md#alerting-examples) has alert examples, and
the event runtime ships rules in
[observability/event-runtime-alert-rules.yml](../observability/event-runtime-alert-rules.yml).

## Troubleshooting Dashboard Issues

### "Panels show 'No data'"
- **Check**: are the Prometheus / Loki / PostgreSQL datasources configured and
  selected in the board's variables?
- **Check**: is the agent exposing metrics? (`curl http://<cfoperator-host>:8083/metrics`)
- **Check**: is the log shipper sending the pod's logs to Loki?

### "Metrics missing"
- A labelled metric appears on `/metrics` only after its first observation, so
  a counter that has not fired since the pod started is absent, not removed.
  [docs/METRICS.md](../docs/METRICS.md) lists every metric and where it is
  declared.

### "Logs not showing"
- **Check**: is the container named `cfoperator` in namespace `apps`? The panels
  filter on both; edit the stream selector if yours differ.
- **Check**: are the logs JSON? The panels parse them with `| json`.

## Customization

Feel free to customize these dashboards:

- Add panels for specific hosts
- Add panels for specific containers
- Add alert annotations
- Change refresh intervals
- Add more variables (e.g., a `$host` filter)

## Related Dashboards

In the homelab, *Node Exporter Full*, *Nodes — Health & Pressure* and *k3s Cluster
Overview* are provisioned alongside these (homelab-infra, HOMELAB-18). Elsewhere,
import Node Exporter Full (grafana.com 1860) for host detail.

## Support

For issues or improvements:
- GitHub: https://github.com/aachtenberg/cfoperator/issues
