# LLM Observability in CFOperator

How to read what the agent's LLM layer is doing: which backend answered and
why, what it cost, how the judges behave, and where one investigation's time
went. The metrics themselves — names, labels, the values each label takes, and
example queries — are in [METRICS.md](METRICS.md#llm-observability-metrics),
and this page links there rather than repeating them. The alert examples are in
[METRICS.md](METRICS.md#llm-issues).

## Where the numbers come from

Apart from the one-shot completions below, every LLM call the agent makes goes
through one wrapper, the tool loop `_chat_with_tools` in `agent/agent.py`:
investigations, sweep phases, sweep finding verification, triage, console chat
turns, the morning summary and re-verification. One call on one provider is one
`cfoperator_llm_requests_total` increment, and the same wrapper records its
latency, its tokens and how the loop stopped (`cfoperator_tool_loop_stops_total`).
A failure is counted as `result="error"` and as
`cfoperator_llm_errors_total{error_type}`, where `error_type` is the exception's
class: `HTTPError` covers a 429, a 5xx and a rejected key alike, because
providers are called over plain HTTP rather than through a vendor SDK. The
classified kind (`timeout`, `rate_limit`, `auth`, `bad_request`, ...) is on the
failed `chat` span as `cfop.llm.error_kind`.

Two things are not in the `cfoperator_llm_*` counters:

- **The one-shot completions** that bypass the tool loop: the mutation judge,
  the node-action planner, correlation analysis and learnings extraction. Each
  is a `chat {model}` span with `cfop.llm.purpose` set, carrying its token
  counts, and that is the only place they show (see
  [Tracing](#tracing-opentelemetry-opt-in)). With tracing off, their calls and
  token spend are not measured at all.
- **Embeddings**, which have their own `cfoperator_embedding_*` counters.

## The fallback chain

Callers that must not fail on one provider's bad minute go through
`_chat_with_tools_with_fallback`, which walks the configured chain: the
selected provider first, then the other local ones, then paid providers when
the `allow_paid_escalation` setting allows. Each rung is its own
`_chat_with_tools` call, so a call that failed on Ollama and answered on Groq
reads as one Ollama error, one Groq success and one
`cfoperator_llm_fallbacks_total{from_provider="ollama", to_provider="groq"}`.
A selected provider skipped before the walk starts (no key, no model picked)
counts as a fallback too, from the skipped provider to the first rung.

Provider reliability, compared across the chain:

```promql
sum by (provider) (rate(cfoperator_llm_requests_total{result="success"}[1h]))
/
sum by (provider) (rate(cfoperator_llm_requests_total[1h]))
```

Rotations per call and the most common paths are in
[METRICS.md](METRICS.md#fallback-chain), and the circuit breaker that stops the
walk when every rung keeps failing in
[METRICS.md](METRICS.md#chain-circuit-breaker). An exhausted walk raises the
last rung's error to the caller; an investigation refused that way, or by an open
breaker, is stored `failed`.

## Cost and token accounting

`cfoperator_llm_tokens_total` counts `type="input"` and `type="output"` per
provider and model. Input is the whole prompt, cached or not: for the
Anthropic tool loop the agent adds the prompt-cache read and write tokens back
into input, and `cfoperator_llm_cache_tokens_total{kind}` carries the split, so
read over input is the hit rate ([METRICS.md](METRICS.md#token-usage)). Cached
reads and writes are not priced like plain input, so a cost figure for
Anthropic should price the cached share separately.

Nothing in the agent knows a price. Multiply by your own rates; the figures
below are placeholders, not anyone's price list:

```promql
# Estimated cost of one provider over the last hour, at $0.50 per 1M tokens
(
  sum(increase(cfoperator_llm_tokens_total{provider="groq"}[1h]))
  / 1000000
) * 0.50

# Estimated cost over 30 days, with a per-provider rate attached by label
sum(
  sum by (provider) (
    increase(cfoperator_llm_tokens_total[30d])
  )
  *
  on(provider) group_left
  label_replace(
    vector(0.50),  # rate per 1M tokens
    "provider", "groq", "", ""
  )
) / 1000000
```

A token total undercounts by whatever the one-shot completions spent (see
[Where the numbers come from](#where-the-numbers-come-from)). With tracing on,
the `chat` spans whose `cfop.llm.purpose` is `judge` carry the judge's share in
`gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`.

## Picking a local model

Some models — gemma4:26b most of all — end the tool loop with a message that
has neither tool calls nor text. `_handle_empty_final` sends one nudge and
grants a bonus round; a second empty raises `EmptyLLMResponseError` and rotates
the provider chain. `cfoperator_llm_empty_final_responses_total{disposition}`
tells those two apart per model, and it is the number to look at when choosing
a local model: `nudged` is a formatting quirk the loop absorbs, `exhausted` is
the model failing the task. The per-model rate queries are in
[METRICS.md](METRICS.md#empty-final-responses). Read them next to latency by
model. How loops end (`cfoperator_tool_loop_stops_total`) has no model label,
but every `chat` span carries `cfop.llm.stop_reason`, so
`{ span.cfop.llm.stop_reason =~ "cap|stagnation" }` in Tempo shows which models
run out of budget rather than answering.

## The judges

Three places ask a model whether another model was right, and they are
measured differently:

- **The mutation judge** (CFOP-70) reviews every remediation that would
  execute unattended. It is a one-shot completion, so it is not in the
  `cfoperator_llm_*` counters; its verdicts are
  `cfoperator_remediation_judge_total{verdict}`, and its calls are the
  `chat` spans with `cfop.llm.purpose = "judge"`. It fails closed: a run of
  `unavailable` or `unparseable` means auto-execution has stopped and rows are
  parking for a human, and `refused` is a configured peer rejecting the request
  while a lower one answers ([METRICS.md](METRICS.md#remediation-pipeline-metrics)).
- **Sweep finding verification** re-checks each sweep finding with tool access
  before it is published. It runs the tool loop, so it is in the request, token
  and loop-stop counters, and the per-sweep counts are on the
  `sweep.verify_findings` span ([METRICS.md](METRICS.md#sweep-finding-verification)).
- **Re-verification** (CFOP-185) re-checks filed rows against live state. It
  runs the tool loop too; its outcomes are
  `cfoperator_remediation_reverify_total{outcome}` and each pass is a
  `reverify` trace. It fails open, so `skipped` is rows left filed, not a
  stopped pipeline.

## Reading the dashboards

**`CFOperator Fleet Monitoring`** (`grafana/cfoperator-dashboard.json`) has the
LLM panels near the top: requests per second by provider, tokens per
second by direction, latency p50/p95/p99 by provider, errors by provider,
fallback activations, and "Total LLM Requests (All Time)". That last one is a
raw counter sum, so "all time" means since the agent last restarted.

**`CFOperator — LLM & Triage`** is provisioned from homelab-infra (CFOP-163 /
HOMELAB-17) and is the drilldown. Its triage row answers whether the fast path
is actually being taken: the share served by the dedicated triage model, triage
p95, model fallbacks, decisions that defaulted to investigate, short-circuits
and deep reroutes. Its LLM row is mostly per model: requests, p95 latency,
tokens by direction and empty finals by disposition per model, the error ratio
per provider, and fallback rotations next to errors by type. Below those are
investigation outcomes and duration, and the inference host's GPU and Ollama
residency: a latency jump with evictions beside it points at two models
evicting each other from VRAM rather than at the provider.

**`CFOperator — Traces`** (homelab-infra, HOMELAB-37) charts span metrics that
Tempo derives from the traces, so its "LLM calls, by model" counts every
`chat` span, the one-shot completions included: the place where the judge's
call volume shows next to the tool loop's.

## Debugging with metrics

### "Why is my LLM slow?"

```promql
# p95 per model on one provider
histogram_quantile(0.95,
  sum by (le, model) (rate(cfoperator_llm_latency_seconds_bucket{provider="ollama"}[5m]))
)

# Compare across providers
topk(3,
  sum by (provider) (rate(cfoperator_llm_latency_seconds_sum[5m]))
  / sum by (provider) (rate(cfoperator_llm_latency_seconds_count[5m]))
)
```

Latency is observed on failures too, so a provider timing out reads as slow.
Check `cfoperator_llm_errors_total{error_type="ReadTimeout"}` before blaming the
model.

### "Why did my investigation fail?"

```promql
# Recent LLM errors
topk(5, sum by (provider, error_type) (
  increase(cfoperator_llm_errors_total[10m])
))

# Did the chain rotate, or did the breaker open?
sum by (from_provider, to_provider) (increase(cfoperator_llm_fallbacks_total[10m]))
cfoperator_llm_circuit_state
```

The investigation's own record says which: `findings.error` holds the
breaker's or the last provider's message, or "no verdict" when the loop
stopped before a `STATUS:` line, with `findings.stop_reason` saying whether
that was the budget, stagnation or an error. Its trace shows each rung that
was tried.

### "Am I using too many tokens?"

```promql
# Tokens per hour
sum(rate(cfoperator_llm_tokens_total[1h])) * 3600

# By direction (input/output)
sum by (type) (
  rate(cfoperator_llm_tokens_total[1h])
)

# By model and direction, the way the LLM & Triage board shows it
sum by (model, type) (rate(cfoperator_llm_tokens_total[15m])) * 60
```

## Tracing (OpenTelemetry, opt-in)

The metrics say how the LLM layer behaves in aggregate. A trace says
where one investigation's time went: which rung of the fallback chain answered,
how long each failed rung took before it gave up, and which tool calls the
model waited on.

Tracing is **off by default**, and off means absent: the OpenTelemetry SDK is
never imported, no exporter exists and nothing dials out. Only
`cfshared/tracing.py` imports it, and `tests/test_tracing.py` fails if anything
else does.

### Turning it on

```yaml
observability:
  tracing:
    enabled: true
    # Both optional. Without them the exporter reads the standard
    # OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_SERVICE_NAME.
    # endpoint: http://tempo.monitoring.svc.cluster.local:4318
    # service_name: cfoperator
```

Spans go out as OTLP over HTTP (port 4318), batched in the background. A
deployment usually leaves `endpoint` unset and sets the env var on both
workloads -- the agent and the event runtime read the same block
(CFOP-317) -- so the same config file works wherever it is mounted:

```yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: http://tempo.monitoring.svc.cluster.local:4318
```

### The pipeline: OTLP straight to Tempo

```
event_runtime ──OTLP/HTTP :4318──▶ Tempo ◀── Grafana (Tempo datasource:
cfoperator    ──OTLP/HTTP :4318──▶             trace view, TraceQL, span → Loki logs)
```

One alert is one trace (CFOP-317). The runtime opens an `alert` root span
around each alert it handles, and its calls to the agent's `/v1/triage` and
`/v1/investigate` carry the W3C `traceparent`. The agent's `triage` continues
it in the request; `/v1/investigate` only enqueues, so the carrier rides the
queued alert dict to the worker thread and the `investigation` root opens
there as a child of the runtime's span. The two processes name themselves
`cfoperator` and `cfoperator-event-runtime` unless `service_name` or
`OTEL_SERVICE_NAME` says otherwise. No auto-instrumentation: the tree below
already has a span for every LLM and tool call, and Flask/requests/SQLAlchemy
spans would carry URLs and statements, which the shapes-not-text rule keeps
off.

Tempo accepts OTLP natively, so no collector sits in between. Add an
OpenTelemetry Collector when there is a concrete job for it: tail sampling,
fan-out to a second backend, or scrubbing attributes. Then point
`OTEL_EXPORTER_OTLP_ENDPOINT` at the collector instead, with nothing else
changed. The homelab's Tempo is deployed by `homelab-infra`
(`k3s/base/monitoring/tempo-statefulset.yml`, HOMELAB-33).

### What a trace contains

```
alert (event_runtime)               cfop.alert.name / severity / summary, cfop.alert.status / action
├─ triage (agent)                   cfop.triage.action, served_by
└─ investigation (agent)            cfop.alert.name / severity / summary (redacted, clipped)
   └─ llm_chain                     chain_length, answered_by, rungs_tried, fallback_used
   ├─ chat gemma4:26b    ERROR      error.type=ReadTimeout, cfop.llm.error_kind=timeout
   └─ chat deepseek-chat            gen_ai.usage.input_tokens / output_tokens,
      ├─ execute_tool k8s_get_pods      gen_ai.response.finish_reasons, cfop.llm.stop_reason
      └─ execute_tool loki_query        gen_ai.tool.name, cfop.tool.cached, cfop.tool.result
```

`triage` has the same shape, with `cfop.triage.action` and `served_by`. Both
it and `investigation` hang under the runtime's `alert` when the request
carried a traceparent, and are roots otherwise (the agent's own reactive poll
path, or a runtime with tracing off). The `chat` spans follow the
OpenTelemetry GenAI semantic conventions (`gen_ai.provider.name`, plus the
older `gen_ai.system`).

A filed-row re-verification (CFOP-185) is a `reverify` root (CFOP-399): the
remediation's id, class and risk, `cfop.reverify.reporter` (the model that
filed it), the judge backend and model that answered, and
`cfop.reverify.outcome` -- `open`, `resolved`, `rejected`, or `skipped` with a
`cfop.reverify.skip_reason`. The judge's `chat` span hangs under it. The
note and the learning text stay off the span.

A proactive sweep is one trace too (CFOP-385), with a span per phase. On the
parallel (Ollama pool) path, the phases run on LangGraph worker threads and keep
`sweep` as their parent, and the `chat` spans hang directly under the phase.

```
sweep                               cfop.sweep.mode, findings_raw, findings
├─ sweep.metrics                    cfop.sweep.phase_findings
│  └─ llm_chain → chat → execute_tool …
├─ sweep.logs                       (and sweep.containers / sweep.baseline_drift
│  └─ llm_chain → …                  / sweep.consolidate_learnings when enabled)
├─ sweep.embedding_backfill
├─ sweep.verify_findings            cfop.verify.kept / capped / suppressed
├─ sweep.publish                    post to event_runtime + resolutions
├─ sweep.report
├─ sweep.metric_snapshot
└─ sweep.correlations
```

A sweep span carries counts, never finding text: findings are model output
about the operator's systems. `agent/test_sweep_tracing.py` holds that line.

`sweep.verify_findings` says how the gate decided (CFOP-398).
`cfop.verify.suppressed` is what the deterministic ground-truth stage removed,
`cfop.verify.ground_truth_kinds_failed` how many kinds that stage could not
list (a grant missing on the ServiceAccount; each is also a WARNING in the
log), and `cfop.verify.capped` / `cfop.verify.stagnated` how many kept findings
got their verdict from the tool loop's cap or its stagnation stop rather than
from the model finishing its check. Those findings carry `verification:
capped` or `verification: stagnation` in the sweep report.

An investigation is broken into phases under its root (CFOP-387):

```
investigation
├─ investigation.orient            └─ embeddings nomic-embed-text   (query vector)
├─ investigation.noise_check       (twice when the pre-check falls through)
├─ llm_chain → chat → execute_tool …
├─ investigation.verify_outcome
├─ investigation.structured_fix    └─ llm_chain …   (the FIX nudge, when needed)
├─ investigation.propose_remediation
├─ investigation.queue_remediation └─ llm_chain …   (the classifier), dedupe.decide
├─ investigation.extract_learnings └─ chat …        (purpose=learnings)
└─ embed_investigation             └─ embeddings …
```

`embeddings {model}` wraps each Ollama `/api/embeddings` round trip, wherever
it happens (orient, the sweep backfill, embed_investigation). A cache hit makes
no span.

Every remediation enqueue asks cfop-dedupe whether it is new (CFOP-538), and
that call is a `dedupe.decide` span under whichever trace enqueued it -- an
investigation's `investigation.queue_remediation`, the morning summary, a chat
turn -- or a root of its own for a console enqueue or a run-feed call, which
have no trace above them. It carries `cfop.dedupe.path`, `decision`, `tier`,
`source` and `row_id`, and the service's own log line for the decision carries
the same `trace_id`.

The one-shot completions that bypass the tool loop are `chat {model}` spans
too, with `cfop.llm.purpose` set to `judge`, `node_action_plan`, `correlations`
or `learnings` (CFOP-386). An HTTP error on these is marked `error.type =
HTTP<code>` with `http.response.status_code`, and never the body, which can
quote the request.

A console turn is one trace (CFOP-388): `chat_turn` carries
`cfop.chat.session_id`, the asker's `cfop.chat.role` (never the username),
`verify_only` and `cfop.chat.kind`. Under it is the turn's `llm_chain`, or a
`skill` span (`cfop.skill.name`, only for a registered skill), or
`morning_summary`. The summary is its own root when the morning schedule or a
web endpoint runs it.

**What a span never carries:** prompt text, completion text, tool arguments or
tool output. These describe the operator's infrastructure, and a trace store is
read by more people than the agent's own database. A failed span records the
exception's class, never its message, because a provider's error body can
quote the request. The one piece of free text is the alert summary, which goes
through the same secret redaction as tool arguments and is clipped to 200
characters. `agent/test_llm_tracing.py::test_llm_spans_never_contain_prompts`
holds this line.

Not traced yet:
- the sweep → runtime leg (CFOP-323): a finding the sweep forwards to the event
  runtime starts a new `alert` trace instead of continuing the sweep's, because
  the agent's call to the runtime carries no `traceparent`;
- the remediation executor (CFOP-323) and the deep-investigation Job, neither of
  which uses the tracing module;
- tool calls made outside the chat loop (CFOP-323).

### Finding traces

In Grafana Explore, on the Tempo datasource:

```
{ resource.service.name = "cfoperator" && name = "investigation" }
{ name = "sweep" } >> { name =~ "sweep.*" && duration > 60s }   # a slow sweep phase
{ span.cfop.llm.purpose = "judge" }                            # every mutation-judge call
{ name = "chat_turn" && span.cfop.chat.session_id = "<id>" }    # one console conversation
{ span.cfop.llm.fallback_used = true }
{ span.gen_ai.request.model = "gemma4:26b" && duration > 30s }
{ status = error && span.gen_ai.operation.name = "chat" }
```
