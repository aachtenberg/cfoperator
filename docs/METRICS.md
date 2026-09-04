# CFOperator Metrics Reference

## Overview

CFOperator exposes Prometheus metrics at `http://<cfoperator-host>:8083/metrics` for comprehensive observability.

The portable event runtime also exposes Prometheus metrics at `http://<event-runtime-host>:8080/metrics` when running via `python3 -m event_runtime`.

> **A metric missing from `/metrics` is not necessarily missing from the build.**
> `prometheus_client` emits nothing for a *labelled* metric until some label
> combination has been observed, so a counter that has not fired since the pod
> started is simply absent. Scraping a live endpoint tells you what has
> happened, not what exists — check the declaration before concluding a metric
> was removed.

## Event Runtime Metrics

### Runtime Health
```promql
cfoperator_event_runtime_up
cfoperator_event_runtime_info_info
cfoperator_event_runtime_last_poll_timestamp_seconds
```

`cfoperator_event_runtime_up` is **self-reported**: set on `start()`, cleared only
on a graceful `stop()`. A crash, OOM or lost node never writes 0 — the series just
ends. **Do not alert on its value.** `max_over_time(...) < 1` returns an empty
vector for an absent series, so it cannot fire for the failure it looks like it
covers (HOMELAB-15). Use `up{}` for "is it gone".

`cfoperator_event_runtime_last_poll_timestamp_seconds` answers the question `up{}`
cannot: *is it still working?* It is labelled by `source` and advances only after
that source has been polled without raising, so a runtime that is alive with a
wedged source goes stale instead of looking healthy. Alert on age, never on
presence:

```promql
time() - max by (source) (cfoperator_event_runtime_last_poll_timestamp_seconds) > 300
```

The label is not decoration. An **unlabelled** Gauge is exported from
registration with the value `0.0`, which would make that query `~1.7e9 > 300` —
true — on every scrape between process start and the first successful poll, so
every deploy would flap. Labelled, no child series exists until the first
`.set()`, and the query is an empty vector until there is something real to say.
Keep that property if you add timestamp metrics of your own.

For liveness that survives losing the cluster entirely, the runtime can also push
to an external dead-man's-switch — see `event_runtime.heartbeat` in
[config-reference.md](config-reference.md). That path emits no metric by design:
a signal that depends on this cluster cannot report this cluster being gone.

The doubled suffix is real, not a typo here: the code declares
`Info("cfoperator_event_runtime_info")` and `prometheus_client` appends `_info`
to every Info metric, so the exposed series is `..._info_info`. The agent's
equivalent declares `Info("cfoperator_agent")` and exposes
`cfoperator_agent_info`, which is the intended idiom. Documented as exposed
rather than as intended — a query written from the tidier name returns nothing.

### Alert Throughput
```promql
cfoperator_event_runtime_alerts_received_total{severity="warning",source="manual"}
cfoperator_event_runtime_alert_results_total{status="completed",action="investigate"}
cfoperator_event_runtime_alert_processing_seconds
```

### Queue Performance
```promql
cfoperator_event_runtime_queue_size
cfoperator_event_runtime_queue_capacity
cfoperator_event_runtime_queue_oldest_age_seconds
cfoperator_event_runtime_queue_enqueued_total
cfoperator_event_runtime_queue_rejected_total
cfoperator_event_runtime_queue_wait_seconds
cfoperator_event_runtime_queue_processing_seconds
cfoperator_event_runtime_jobs{status="queued"}
cfoperator_event_runtime_job_results_total{status="completed"}
```

### Replay and Persistence
```promql
cfoperator_event_runtime_replay_attempts_total{sink="postgres",result="success"}
cfoperator_event_runtime_replay_events_total{sink="postgres",result="success"}
cfoperator_event_runtime_replay_batch_size{sink="postgres"}
cfoperator_event_runtime_events_recorded_total{event_type="alert_received"}
```

### Decisions, Notifications and Completion
```promql
cfoperator_event_runtime_decisions_total{action="investigate"}
cfoperator_event_runtime_notifications_sent_total{result="success"}
cfoperator_event_runtime_scheduled_tasks_total{result="success"}
cfoperator_event_runtime_completion_requests_total{outcome="recorded"}

# Triage decisions the deep-investigation tier rewrote to deep_investigate,
# by the action they replaced (CFOP-163; before this the reroute was a log line)
cfoperator_event_runtime_deep_reroutes_total{from_action="escalate"}
```

`completion_requests_total` counts `POST /v1/investigations/{alert_id}/complete`
by outcome — the callback the agent uses to close an investigation out.

### Bare-Metal Host Observability
```promql
cfoperator_event_runtime_host_discovery_runs_total{provider="local-host-stats",result="success"}
cfoperator_event_runtime_host_discovered_targets{provider="prometheus-host-stats"}
cfoperator_event_runtime_host_discovery_timestamp_seconds{provider="prometheus-host-stats"}
cfoperator_event_runtime_host_observation_runs_total{provider="ssh-host-stats",result="error"}
```

### Useful Queries
```promql
# Alerts per minute
sum(rate(cfoperator_event_runtime_alerts_received_total[5m])) * 60

# P95 runtime alert latency
histogram_quantile(0.95,
  sum by (le) (rate(cfoperator_event_runtime_alert_processing_seconds_bucket[5m]))
)

# Queue reject rate
sum(rate(cfoperator_event_runtime_queue_rejected_total[5m]))

# P95 queue wait latency
histogram_quantile(0.95,
  sum by (le) (rate(cfoperator_event_runtime_queue_wait_seconds_bucket[5m]))
)

# Replay activity by sink
sum by (sink, result) (rate(cfoperator_event_runtime_replay_attempts_total[5m]))

# Host observation failures by provider
sum by (provider) (rate(cfoperator_event_runtime_host_observation_runs_total{result="error"}[15m]))

# Time since last successful host discovery
time() - max by (provider) (cfoperator_event_runtime_host_discovery_timestamp_seconds)
```

### Suggested Alert Rules

Import or adapt [observability/event-runtime-alert-rules.yml](/home/aachten/repos/cfoperator/observability/event-runtime-alert-rules.yml) for the portable runtime. It includes alerts for:

- runtime down
- queue rejection rate
- sustained queue age
- replay failures
- stale host discovery
- host observation failures

## Core Agent Metrics

### Agent Information
```promql
# Agent version and configuration info
cfoperator_agent_info{version="1.0.8", host_id="cfoperator", mode="dual_ooda"}

# Agent uptime in seconds
cfoperator_uptime_seconds
```

### OODA Loop Activity
```promql
# Total OODA cycles executed (observe → orient → decide → act)
cfoperator_ooda_cycles_total

# Sweeps by mode (reactive = alert-driven, proactive = scheduled)
# `reactive` increments per Alertmanager poll cycle that found alerts when
# ooda.reactive_poll: true. With reactive_poll: false (event_runtime drives
# investigations over HTTP), this counter stays flat — see
# cfoperator_investigation_queue_depth / postback metrics below.
cfoperator_sweeps_total{mode="reactive"}
cfoperator_sweeps_total{mode="proactive"}
```

### Infrastructure Monitoring
```promql
# Number of monitored hosts
cfoperator_monitored_hosts

# Running containers across fleet
cfoperator_running_containers

# Number of registered tools
cfoperator_tools_registered
```

### Tool Execution
```promql
# Tool calls by name and result (success/error)
cfoperator_tool_calls_total{tool_name="prometheus_query", result="success"}
cfoperator_tool_calls_total{tool_name="ssh_execute", result="error"}
```

`result` is `success` or `error`. A tool that could not do its job — timeout,
unknown host, backend not configured, a truthy `error` key, `success: false`
without a ran-and-answered `exit_code` — counts as `error`. A valid empty
read (`success: true`, empty pod list, PromQL with no series) counts as
`success`. So does a command that ran and returned non-zero: `ssh_execute`
and every `k8s_*` tool set `success` from the process exit code, and those
payloads carry `exit_code` with no `error`. Cached repeats use the original
result's label, not the stub.

### Investigation Tracking
```promql
# Investigations by outcome (resolved/escalated/monitoring/failed/in_progress)
cfoperator_investigations_total{outcome="resolved"}
cfoperator_investigations_total{outcome="escalated"}

# Started, and wall time by terminal outcome (CFOP-163). Before this the
# duration went to Postgres only; p95 investigation time had no PromQL.
cfoperator_investigations_started_total
histogram_quantile(0.95, sum by (le, outcome) (rate(cfoperator_investigation_duration_seconds_bucket[1h])))

# Pending HTTP-driven investigations (POST /v1/investigate, called by event_runtime).
# Gauge; rising values mean the agent's single worker thread is falling behind
# the LLM throughput it can sustain.
cfoperator_investigation_queue_depth

# Cumulative rejections when the in-process queue is full and the endpoint
# returns 503. event_runtime's worker retries with backoff on 5xx.
cfoperator_investigation_queue_rejected_total

# Outcome of posting the completed ActionResult back to event_runtime
# (/v1/investigations/{alert_id}/complete). Status label: 'ok', 'http_<code>',
# or 'transport_error'.
cfoperator_investigation_postback_total{status="ok"}
cfoperator_investigation_postback_total{status="http_401"}
cfoperator_investigation_postback_total{status="transport_error"}
```

### Morning Summary
```promql
# Generations by result, and the last success as a labelled timestamp
cfoperator_morning_summary_runs_total{result="ok"}
cfoperator_morning_summary_runs_total{result="error"}
time() - max(cfoperator_morning_summary_last_success_timestamp_seconds) > 30 * 3600   # alert on AGE
```

The timestamp is labelled (`host_id`) on purpose: an unlabelled Gauge exports
0.0 from registration, and an age query against it fires on every deploy.
`tests/test_metrics_conventions.py` enforces this for every `*_timestamp_seconds`
gauge in the tree.

### Error Tracking
```promql
# Total errors
cfoperator_errors_total

# Log messages by level
log_messages_total{level="ERROR", component="cfoperator"}
log_messages_total{level="WARN", component="cfoperator"}
```

### Ollama Pool
```promql
cfoperator_pool_instances{instance="ubuntu-llm-01",status="healthy"}
cfoperator_pool_checkouts_total{instance="ubuntu-llm-01",result="success"}
cfoperator_pool_checkins_total{instance="ubuntu-llm-01"}
cfoperator_pool_health_checks_total{instance="ubuntu-llm-01",result="healthy"}
```

### Sweep Timing
```promql
cfoperator_sweep_duration_seconds{mode="sequential"}
cfoperator_sweep_phase_duration_seconds{phase="metrics",instance="ubuntu-llm-01"}
```

## LLM Observability Metrics

### LLM Request Tracking
```promql
# LLM requests by provider, model, and result
cfoperator_llm_requests_total{provider="ollama", model="qwen3:14b", result="success"}
cfoperator_llm_requests_total{provider="groq", model="llama-3.3-70b", result="error"}
```

### Token Usage
```promql
# Tokens by provider, model, and type (input/output)
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="input"}
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="output"}
```

The `type` values are `input` and `output` — what the code emits and what the
live series carry. Earlier revisions of this page (and of
`llm-observability.md`) said `prompt`/`completion`; every query written from
them returned nothing.

### LLM Latency
```promql
# LLM request latency histogram (seconds)
cfoperator_llm_latency_seconds{provider="ollama", model="qwen3:14b"}
```

### LLM Errors
```promql
# LLM errors by provider and error type
cfoperator_llm_errors_total{provider="ollama", error_type="ConnectionError"}
cfoperator_llm_errors_total{provider="groq", error_type="RateLimitError"}
```

### Fallback Chain
```promql
# Fallback activations (from_provider → to_provider)
cfoperator_llm_fallbacks_total{from_provider="ollama", to_provider="groq"}
```

Observed at the provider rotation in `_chat_with_tools_with_fallback` since
CFOP-163. Before that the counter was declared and never incremented, and the
`ExcessiveFallbacks` alert below could not fire; `tests/test_metrics_conventions.py`
now fails on any metric that is declared and never observed.

### Empty Final Responses
```promql
# Tool-loop turns that ended with an empty final message (no tool calls, no
# text), by what the agent did about it
cfoperator_llm_empty_final_responses_total{provider="ollama", model="gemma4:26b", disposition="nudged"}
cfoperator_llm_empty_final_responses_total{provider="ollama", model="gemma4:26b", disposition="exhausted"}
```

`disposition` separates two signals that mean different things:

| Value | What happened | How to read it |
|---|---|---|
| `nudged` | First empty of the turn. `EMPTY_RESPONSE_NUDGE` was appended and one bonus round granted. | A formatting quirk the loop absorbs — the benchmark recovered 19/19 this way. Costs one extra round-trip. |
| `exhausted` | Second empty. `EmptyLLMResponseError` raised, provider chain rotates. | The model failing the task. Costs a whole extra provider attempt. |

See `docs/llm-observability.md` for the per-model rate queries.

### Triage Decisions
```promql
# Every run_triage return, by what produced it (CFOP-163)
cfoperator_triage_decisions_total{action="notify", served_by="triage_model", model="cfop-triage-ministral3:v5-q4"}

# Share of decisions NOT served by the dedicated model over 30m -- the
# silent-degrade signal: the fine-tune falls into the standard chain on any
# failure or unparseable reply, and nothing else shows it
1 - sum(rate(cfoperator_triage_decisions_total{served_by="triage_model"}[30m]))
  / sum(rate(cfoperator_triage_decisions_total{served_by=~"triage_model|chain"}[30m]))

# Why the dedicated model was skipped
cfoperator_triage_model_fallbacks_total{reason="unparseable"}
cfoperator_triage_model_fallbacks_total{reason="exception"}

# Whole-call wall time, short-circuits included
histogram_quantile(0.95, sum by (le, served_by) (rate(cfoperator_triage_latency_seconds_bucket[15m])))
```

`served_by` is the closed set `triage_model`, `chain`, `short_circuit_resolution`,
`short_circuit_info`, `unparseable_default`, `llm_unavailable`. The two
short-circuits never call a model (`model="none"`); the two defaults are the
"never lose an alert" paths and are worth an alert of their own.

### Embedding Operations
```promql
# Embedding generation requests
cfoperator_embedding_requests_total{result="success"}
cfoperator_embedding_requests_total{result="error"}       # retryable: timeout, endpoint down, model missing
cfoperator_embedding_requests_total{result="truncated"}   # embedded, but from a head-truncated input (CFOP-81)
cfoperator_embedding_requests_total{result="unembeddable"} # the input can never fit; not retried

# Embedding cache performance
cfoperator_embedding_cache_hits_total{result="hit"}
cfoperator_embedding_cache_hits_total{result="miss"}
```

## Remediation Pipeline Metrics

The queue and the gates in front of it. See [REMEDIATION.md](REMEDIATION.md) for
what each stage means.

```promql
# Queue depth by state — the one to graph
cfoperator_remediation_queue{status="needs-human"}

# Enqueue, and whether the auto-gate let it through
cfoperator_remediation_enqueued_total{remediation_class="gitops-patch",eligible="true"}

# The gates, in the order a recommendation meets them
cfoperator_remediation_classifier_total{result="ok"}
cfoperator_remediation_folded_total{reason="repeat"}
cfoperator_remediation_judge_total{verdict="confirm"}

# Execution and terminal state
cfoperator_remediation_executor_spawned_total{result="ok"}
cfoperator_remediation_reaped_total
cfoperator_remediation_outcome_total{outcome="resolved"}

# The human gate: approve/reject from the console (CFOP-163)
cfoperator_remediation_human_decisions_total{decision="approve"}
```

`judge_total` is worth an alert: the CFOP-70 judge fails closed, so a rising
`verdict="unavailable"` or `verdict="unparseable"` means auto-execution has
quietly stopped and everything is parking for a human.

### Label values that are a closed set

Verified against every `.labels()` call site. A query using a value absent from
this table returns nothing — which is how the first version of this section
shipped several examples that could never match.

| metric | label | emitted values |
|---|---|---|
| `cfoperator_pool_instances` | `status` | `healthy`, `unhealthy`, `in_use` |
| `cfoperator_pool_checkouts_total` | `result` | `success`, `unavailable` |
| `cfoperator_pool_health_checks_total` | `result` | `healthy`, `unreachable` |
| `cfoperator_sweep_duration_seconds` | `mode` | `parallel`, `sequential` |
| `cfoperator_sweeps_total` | `mode` | `proactive`, `reactive` |
| `cfoperator_remediation_queue` | `status` | the nine queue states — `queued`, `claimed`, `executing`, `pr-open`, `verifying`, `resolved`, `failed`, `needs-human`, `rejected` |
| `cfoperator_remediation_classifier_total` | `result` | `ok`, `nudged`, `escalated`, `degraded` |
| `cfoperator_remediation_folded_total` | `reason` | `repeat`, `fork_committed`, `fork_stuck`, `investigate_followup` |
| `cfoperator_remediation_judge_total` | `verdict` | `confirm`, `downgrade`, `reject`, `unavailable`, `unparseable`, `self-review-skipped` |
| `cfoperator_remediation_executor_spawned_total` | `result` | `ok`, `capped`, `failed` |
| `cfoperator_remediation_outcome_total` | `outcome` | `resolved`, `rejected` |
| `cfoperator_remediation_enqueued_total` | `eligible` | `true`, `false` (`str(bool).lower()`) |
| `cfoperator_event_runtime_decisions_total` | `action` | `log_only`, `notify`, `investigate`, `escalate` |
| `cfoperator_event_runtime_scheduled_tasks_total` | `result` | `success`, `error` |
| `cfoperator_event_runtime_completion_requests_total` | `outcome` | `recorded`, `auth_missing`, `auth_invalid`, `bad_request`, `error` |
| `cfoperator_tool_calls_total` | `result` | `success`, `error` |
| `cfoperator_llm_requests_total` | `result` | `success`, `error` |
| `cfoperator_llm_tokens_total` | `type` | `input`, `output` |
| `cfoperator_triage_decisions_total` | `served_by` | `triage_model`, `chain`, `short_circuit_resolution`, `short_circuit_info`, `unparseable_default`, `llm_unavailable` |
| `cfoperator_triage_model_fallbacks_total` | `reason` | `unparseable`, `exception` |
| `cfoperator_investigation_duration_seconds` | `outcome` | as `cfoperator_investigations_total` |
| `cfoperator_morning_summary_runs_total` | `result` | `ok`, `error` |
| `cfoperator_remediation_human_decisions_total` | `decision` | `approve`, `reject` |
| `cfoperator_event_runtime_deep_reroutes_total` | `from_action` | `escalate`, `investigate` |
| `cfoperator_llm_empty_final_responses_total` | `disposition` | `nudged`, `exhausted` |

Labels not listed here carry open-ended values — an instance name, a sink, a
tool name, a scheduler class — and are not enumerable from the source.

## Sweep Finding Verification

The LLM judge that verifies sweep findings logs its activity (no dedicated Prometheus metrics — uses existing LLM request counters):

```
# Log lines to watch for:
"Finding verification: 8 → 5 (3 filtered)"   # Summary line (INFO)
"Judge filtered: <finding text>"               # Each filtered finding (INFO)
"Finding verification failed, returning unfiltered: ..."  # Graceful degradation (WARNING)
```

The judge's LLM call is tracked by existing `cfoperator_llm_requests_total` and `cfoperator_llm_tokens_total` metrics.

## Common Queries

### Agent Health
```promql
# Is agent running?
up{job="cfoperator"}

# Agent uptime
cfoperator_uptime_seconds

# Error rate (errors per second)
rate(cfoperator_errors_total[5m])
```

### OODA Loop Performance
```promql
# OODA cycles per minute
rate(cfoperator_ooda_cycles_total[5m]) * 60

# Proactive sweeps per hour
rate(cfoperator_sweeps_total{mode="proactive"}[1h]) * 3600

# Reactive sweeps (alert handling) per minute
rate(cfoperator_sweeps_total{mode="reactive"}[5m]) * 60
```

### Tool Usage
```promql
# Most used tools
topk(5, sum by (tool_name) (
  rate(cfoperator_tool_calls_total[1h])
))

# Tool success rate
sum(rate(cfoperator_tool_calls_total{result="success"}[5m]))
/ sum(rate(cfoperator_tool_calls_total[5m]))
```

### LLM Performance
```promql
# LLM requests per minute
rate(cfoperator_llm_requests_total[5m]) * 60

# LLM error rate
rate(cfoperator_llm_errors_total[5m])
/ rate(cfoperator_llm_requests_total[5m])

# P95 latency by provider
histogram_quantile(0.95,
  rate(cfoperator_llm_latency_seconds_bucket[5m])
)

# Token usage per hour
sum(rate(cfoperator_llm_tokens_total[1h])) * 3600
```

### Infrastructure Health
```promql
# Monitored hosts
cfoperator_monitored_hosts

# Running containers (will vary)
cfoperator_running_containers

# Tools available
cfoperator_tools_registered
```

## Alerting Examples

### High Error Rate
```yaml
- alert: CFOperatorHighErrorRate
  expr: rate(cfoperator_errors_total[5m]) > 1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "CFOperator error rate above 1/sec"
```

### Agent Down
```yaml
- alert: CFOperatorDown
  expr: up{job="cfoperator"} == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "CFOperator is down"
```

### Tool Failures
```yaml
- alert: HighToolFailureRate
  expr: |
    rate(cfoperator_tool_calls_total{result="error"}[5m])
    / rate(cfoperator_tool_calls_total[5m]) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Tool failure rate above 10%"
```

### LLM Issues
```yaml
- alert: LLMHighErrorRate
  expr: |
    rate(cfoperator_llm_errors_total[5m])
    / rate(cfoperator_llm_requests_total[5m]) > 0.2
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "LLM error rate above 20%"

- alert: ExcessiveFallbacks
  expr: rate(cfoperator_llm_fallbacks_total[10m]) > 1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Frequent LLM fallbacks"
```

## Grafana Dashboard

Import the dashboard JSON from `grafana/cfoperator-dashboard.json` which includes:

### Top Stats
- Uptime, Status, Monitored Hosts, Running Containers, Error Rate, Tools

### LLM Observability
- LLM Request Rate, Error Rate, Token Usage, Latency, Fallbacks, Cache Hit Rate

### Activity Graphs
- OODA Loop Activity, Tool Usage by Type

### LLM Deep Dive
- Requests by Provider, Token Usage, Latency Heatmap, Fallback Chain

### Infrastructure Health
- CPU Usage by Host, Memory Usage by Host

### Log Panels
- Live Logs, OODA Activity, Tool Execution, Errors, LLM Activity, Fleet Discovery, Knowledge Base

## Prometheus Configuration

Add CFOperator as a scrape target:

```yaml
scrape_configs:
  - job_name: 'cfoperator'
    static_configs:
      - targets: ['<cfoperator-host>:8083']
    scrape_interval: 15s
    scrape_timeout: 10s
```

Add the event runtime as a separate scrape target:

```yaml
scrape_configs:
  - job_name: 'cfoperator-event-runtime'
    static_configs:
      - targets: ['<event-runtime-host>:8080']
    scrape_interval: 15s
    scrape_timeout: 10s
```

Repository sample: [observability/prometheus-event-runtime-scrape.yml](/home/aachten/repos/cfoperator/observability/prometheus-event-runtime-scrape.yml)

Load the alert rules from [observability/event-runtime-alert-rules.yml](/home/aachten/repos/cfoperator/observability/event-runtime-alert-rules.yml) into Prometheus or your PrometheusRule flow.

## Verifying Metrics

```bash
# Check metrics endpoint
curl http://localhost:8083/metrics | grep cfoperator

# Check specific metric
curl http://localhost:8083/metrics | grep cfoperator_uptime_seconds

# Check LLM metrics (will appear after first LLM call)
curl http://localhost:8083/metrics | grep cfoperator_llm
```

## Metrics Implementation

All metrics are defined in [agent.py](agent.py) using `prometheus_client`:

```python
from prometheus_client import Counter, Gauge, Histogram, Info

# Agent metrics
OODA_CYCLES = Counter('cfoperator_ooda_cycles_total', ...)
AGENT_UPTIME = Gauge('cfoperator_uptime_seconds', ...)

# LLM metrics
LLM_REQUESTS = Counter('cfoperator_llm_requests_total', ..., ['provider', 'model', 'result'])
LLM_LATENCY = Histogram('cfoperator_llm_latency_seconds', ..., ['provider', 'model'])
```

Metrics are updated throughout the OODA loop and tool execution.

## Next Steps

1. **Import Grafana dashboard** - See [grafana/README.md](grafana/README.md)
2. **Configure Prometheus scraping** - Add CFOperator to Prometheus targets
3. **Set up alerting rules** - Use examples above
4. **Monitor LLM usage** - Track costs and performance
5. **Tune fallback chain** - Based on provider reliability metrics
