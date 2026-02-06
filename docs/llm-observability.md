# LLM Observability in CFOperator

CFOperator provides comprehensive observability for LLM operations through Prometheus metrics.

## Metrics Available

### LLM Request Tracking

```promql
# Total LLM requests by provider, model, and result (success/error)
cfoperator_llm_requests_total{provider="ollama", model="qwen3:14b", result="success"}

# Example query: Success rate by provider
rate(cfoperator_llm_requests_total{result="success"}[5m])
/ rate(cfoperator_llm_requests_total[5m])

# Example query: Requests per minute by provider
rate(cfoperator_llm_requests_total[5m]) * 60
```

### Token Usage

```promql
# Total tokens used by provider, model, and type (prompt/completion)
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="prompt"}
cfoperator_llm_tokens_total{provider="ollama", model="qwen3:14b", type="completion"}

# Example query: Total tokens per hour
rate(cfoperator_llm_tokens_total[1h]) * 3600

# Example query: Token efficiency (completion tokens / prompt tokens)
sum(rate(cfoperator_llm_tokens_total{type="completion"}[5m]))
/ sum(rate(cfoperator_llm_tokens_total{type="prompt"}[5m]))
```

### LLM Latency

```promql
# LLM request latency histogram (seconds)
cfoperator_llm_latency_seconds{provider="ollama", model="qwen3:14b"}

# Example query: 95th percentile latency
histogram_quantile(0.95,
  rate(cfoperator_llm_latency_seconds_bucket[5m])
)

# Example query: Average latency by provider
rate(cfoperator_llm_latency_seconds_sum[5m])
/ rate(cfoperator_llm_latency_seconds_count[5m])
```

### LLM Errors

```promql
# LLM errors by provider and error type
cfoperator_llm_errors_total{provider="ollama", error_type="ConnectionError"}
cfoperator_llm_errors_total{provider="groq", error_type="RateLimitError"}

# Example query: Error rate by provider
rate(cfoperator_llm_errors_total[5m])

# Example query: Most common error types
topk(5, sum by (error_type) (
  rate(cfoperator_llm_errors_total[1h])
))
```

### LLM Fallback Chain

```promql
# Fallback activations (from_provider → to_provider)
cfoperator_llm_fallbacks_total{from_provider="ollama", to_provider="groq"}

# Example query: Fallback frequency
rate(cfoperator_llm_fallbacks_total[5m])

# Example query: Most common fallback paths
topk(5, sum by (from_provider, to_provider) (
  rate(cfoperator_llm_fallbacks_total[1h])
))
```

### Embedding Operations

```promql
# Embedding generation requests (success/error)
cfoperator_embedding_requests_total{result="success"}
cfoperator_embedding_requests_total{result="error"}

# Embedding cache performance (hit/miss)
cfoperator_embedding_cache_hits_total{result="hit"}
cfoperator_embedding_cache_hits_total{result="miss"}

# Example query: Cache hit rate
rate(cfoperator_embedding_cache_hits_total{result="hit"}[5m])
/ rate(cfoperator_embedding_cache_hits_total[5m])
```

## Grafana Dashboard Panels

### LLM Request Volume (Time Series)

```promql
# Stacked by provider
sum by (provider) (
  rate(cfoperator_llm_requests_total{result="success"}[5m])
)
```

### LLM Error Rate (Time Series)

```promql
# Overall error rate (percentage)
(
  sum(rate(cfoperator_llm_errors_total[5m]))
  /
  sum(rate(cfoperator_llm_requests_total[5m]))
) * 100
```

### Token Usage by Provider (Time Series)

```promql
# Total tokens/min by provider
sum by (provider) (
  rate(cfoperator_llm_tokens_total[5m])
) * 60
```

### LLM Latency by Provider (Heatmap)

```promql
# 50th, 90th, 99th percentile
histogram_quantile(0.50,
  sum by (provider, le) (
    rate(cfoperator_llm_latency_seconds_bucket[5m])
  )
)
```

### Fallback Chain Activity (Bar Gauge)

```promql
# Fallbacks in last hour
sum by (from_provider, to_provider) (
  increase(cfoperator_llm_fallbacks_total[1h])
)
```

### Cost Estimation (Stat Panel)

```promql
# Estimated cost (customize rates per provider)
# Example: Groq @ $0.50/1M tokens
(
  sum(increase(cfoperator_llm_tokens_total{provider="groq"}[1h]))
  / 1000000
) * 0.50
```

## Alerting Rules

### High LLM Error Rate

```yaml
- alert: HighLLMErrorRate
  expr: |
    (
      sum(rate(cfoperator_llm_errors_total[5m]))
      /
      sum(rate(cfoperator_llm_requests_total[5m]))
    ) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "LLM error rate above 10%"
    description: "{{ $value | humanizePercentage }} of LLM requests are failing"
```

### LLM Provider Down

```yaml
- alert: LLMProviderDown
  expr: |
    rate(cfoperator_llm_errors_total{error_type="ConnectionError"}[5m]) > 0
    and
    rate(cfoperator_llm_requests_total{result="success"}[5m]) == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "LLM provider {{ $labels.provider }} unreachable"
```

### Excessive Fallbacks

```yaml
- alert: ExcessiveLLMFallbacks
  expr: |
    rate(cfoperator_llm_fallbacks_total[10m]) > 1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Frequent LLM fallback activations"
    description: "Primary LLM may be unstable"
```

### Token Budget Exhaustion

```yaml
- alert: TokenBudgetExhausted
  expr: |
    sum(increase(cfoperator_llm_tokens_total{provider="groq"}[1h]))
    > 50000
  labels:
    severity: warning
  annotations:
    summary: "Token usage for {{ $labels.provider }} exceeds budget"
```

## Implementation Example

### Instrumenting LLM Calls

```python
from prometheus_client import Counter, Histogram
import time

LLM_REQUESTS = Counter('cfoperator_llm_requests_total',
                       'LLM requests',
                       ['provider', 'model', 'result'])
LLM_LATENCY = Histogram('cfoperator_llm_latency_seconds',
                        'LLM latency',
                        ['provider', 'model'])

def call_llm(provider, model, prompt):
    start = time.time()
    try:
        result = llm_client.chat(prompt)

        # Track success
        LLM_REQUESTS.labels(
            provider=provider,
            model=model,
            result='success'
        ).inc()

        # Track latency
        latency = time.time() - start
        LLM_LATENCY.labels(
            provider=provider,
            model=model
        ).observe(latency)

        return result

    except Exception as e:
        # Track error
        LLM_REQUESTS.labels(
            provider=provider,
            model=model,
            result='error'
        ).inc()
        raise
```

### Tracking Fallbacks

```python
def try_llm_with_fallback(prompt):
    """Try primary LLM, fallback to secondary on failure."""
    try:
        return call_llm('ollama', 'qwen3:14b', prompt)
    except Exception:
        # Track fallback
        LLM_FALLBACKS.labels(
            from_provider='ollama',
            to_provider='groq'
        ).inc()

        # Try fallback
        return call_llm('groq', 'llama-3.3-70b', prompt)
```

## Monitoring Patterns

### 1. Cost Tracking

Track token usage and multiply by provider rates:

```promql
# Total estimated monthly cost
sum(
  sum by (provider) (
    increase(cfoperator_llm_tokens_total[30d])
  )
  *
  on(provider) group_left
  label_replace(
    vector(0.50),  # Groq rate per 1M tokens
    "provider", "groq", "", ""
  )
) / 1000000
```

### 2. Provider Reliability

Compare success rates across providers:

```promql
# Success rate by provider
sum by (provider) (
  rate(cfoperator_llm_requests_total{result="success"}[1h])
)
/
sum by (provider) (
  rate(cfoperator_llm_requests_total[1h])
)
```

### 3. Performance Comparison

Which provider is fastest?

```promql
# Average latency by provider
avg by (provider) (
  rate(cfoperator_llm_latency_seconds_sum[1h])
  /
  rate(cfoperator_llm_latency_seconds_count[1h])
)
```

### 4. Fallback Chain Health

Are fallbacks too frequent?

```promql
# Fallback rate (should be low)
sum(rate(cfoperator_llm_fallbacks_total[1h]))
/
sum(rate(cfoperator_llm_requests_total[1h]))
```

## Integration with Grafana

Add these panels to the CFOperator dashboard:

1. **LLM Request Volume** - Line graph of requests/min by provider
2. **Token Usage** - Stacked area chart of prompt + completion tokens
3. **LLM Latency** - Heatmap showing p50, p90, p99 by provider
4. **Error Rate** - Single stat showing current error percentage
5. **Fallback Activity** - Bar gauge showing fallback frequency
6. **Cost Estimate** - Stat panel with estimated monthly cost
7. **Provider Health** - Table showing success rate, latency, error count per provider

## Debugging with Metrics

### "Why is my LLM slow?"

```promql
# Check if specific provider is slow
histogram_quantile(0.95,
  rate(cfoperator_llm_latency_seconds_bucket{provider="ollama"}[5m])
)

# Compare across providers
topk(3, avg by (provider) (
  rate(cfoperator_llm_latency_seconds_sum[5m])
  / rate(cfoperator_llm_latency_seconds_count[5m])
))
```

### "Why did my investigation fail?"

```promql
# Check recent LLM errors
topk(5, sum by (provider, error_type) (
  increase(cfoperator_llm_errors_total[10m])
))

# Check if fallback chain activated
increase(cfoperator_llm_fallbacks_total[10m])
```

### "Am I using too many tokens?"

```promql
# Tokens per hour
sum(rate(cfoperator_llm_tokens_total[1h])) * 3600

# Token breakdown by operation type
sum by (type) (
  rate(cfoperator_llm_tokens_total[1h])
)
```

## Next Steps

1. Add LLM metrics to Grafana dashboard
2. Set up alerting for high error rates and excessive fallbacks
3. Monitor token usage to optimize prompts
4. Track cost trends for budget planning
5. Compare provider performance to optimize fallback chain order
