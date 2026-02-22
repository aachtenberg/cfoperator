# CFOperator Metrics Reference

## Overview

CFOperator exposes Prometheus metrics at `http://<cfoperator-host>:8083/metrics` for comprehensive observability.

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

# Sweeps by mode (reactive = alerts, proactive = scheduled)
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

### Investigation Tracking
```promql
# Investigations by outcome (resolved/escalated/monitoring/failed/in_progress)
cfoperator_investigations_total{outcome="resolved"}
cfoperator_investigations_total{outcome="escalated"}
```

### Error Tracking
```promql
# Total errors
cfoperator_errors_total

# Log messages by level
log_messages_total{level="ERROR", component="cfoperator"}
log_messages_total{level="WARN", component="cfoperator"}
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
# Tokens by provider, model, and type (prompt/completion)
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="prompt"}
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="completion"}
```

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

### Embedding Operations
```promql
# Embedding generation requests
cfoperator_embedding_requests_total{result="success"}
cfoperator_embedding_requests_total{result="error"}

# Embedding cache performance
cfoperator_embedding_cache_hits_total{result="hit"}
cfoperator_embedding_cache_hits_total{result="miss"}
```

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
