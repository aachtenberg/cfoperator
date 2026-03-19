# Ollama Tool-Calling Benchmark Results

**Date:** 2026-02-13
**Test harness:** `test_tool_calling.py`
**Hosts tested:** 3 Ollama instances (GPU server, RTX 5080 desktop, CPU desktop)
**Models tested:** 12 (current run) / 22 (cumulative across runs)

## Part 1: Model Tool-Calling Benchmark

An autonomous infrastructure monitoring agent (CFOperator) needs LLMs that can reliably make structured tool calls — not just generate text about tools, but emit proper JSON function calls that code can parse and execute.

We tested every model across our 3 Ollama instances against 3 scenarios:

| Test | Description | What a perfect score looks like |
|------|------------|--------------------------------|
| **T1 — Single tool call** | "What is the current CPU usage on raspberrypi?" (1 tool available) | Emits a structured `prometheus_query` call with valid PromQL |
| **T2 — Multi-turn** | "Check CPU usage and then check for error logs" (2 tools available) | Makes `prometheus_query`, receives result, then makes `loki_query` |
| **T3 — Tool selection** | "Show me recent error logs from immich_server" (3 tools available) | Picks `loki_query` over `prometheus_query` and `docker_list` |

**Scoring:** 0 = no tool call / garbled, 1 = tool call but wrong name or bad args, 2 = correct tool with reasonable args. Max raw score = 6, normalized to 0–10.

### Rankings (latest run)

| Rank | Model | Size | Host | T1 | T2 | T3 | Score | Time |
|-----:|-------|------|------|:--:|:--:|:--:|------:|-----:|
| 1 | mistral-small3.2:24b | 24B | ollama-gpu | 2 | 2 | 2 | **10.0** | 6.1s |
| 2 | glm-4.7-flash:q4_K_M | ~9B | ollama-gpu | 2 | 2 | 2 | **10.0** | 25.5s |
| 3 | qwen2.5:7b-instruct-q8_0 | 7B | ollama-198 | 2 | 2 | 2 | **10.0** | 6.9s |
| 4 | gpt-oss:20b | 20B | ollama-desktop | 2 | 2 | 2 | **10.0** | 42.6s |
| 5 | ministral-3:latest | 3B | ollama-desktop | 2 | 2 | 2 | **10.0** | 22.6s |
| 6 | qwen3:14b | 14B | ollama-198 | 2 | 1 | 2 | 8.3 | 29.9s |
| 7 | qwen3:8b | 8B | ollama-198 | 2 | 1 | 2 | 8.3 | 30.6s |
| 8 | qwen2.5:14b | 14B | ollama-desktop | 1 | 2 | 2 | 8.3 | 55.1s |
| 9 | llama3.1:8b | 8B | ollama-desktop | 2 | 1 | 2 | 8.3 | 42.3s |
| 10 | llava:7b | 7B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 11 | llava:13b | 13B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 12 | llama3.2-vision:11b | 11B | ollama-198 | 0 | 0 | 0 | 0.0 | — |

Models no longer loaded (from initial 22-model run): hermes3:70b (5.0), ministral-3:14b (5.0), qwen2.5-coder variants (1.7), qwen32b-8k (1.7), phi4 (0.0), glm4:9b (0.0).

### Run-to-Run Consistency

Across 3 runs, these models scored 10/10 every time:
- **qwen2.5:7b-instruct-q8_0** — rock solid
- **ministral-3:latest** — rock solid
- **gpt-oss:20b** — rock solid

These models fluctuated between 8.3 and 10.0 across runs (borderline on multi-turn T2):
- **mistral-small3.2:24b** — 10.0 → 8.3 → 10.0
- **glm-4.7-flash:q4_K_M** — 10.0 → 8.3 → 10.0

### Key Takeaways

**Perfect scores (10/10) — 5 models**

- **qwen2.5:7b-instruct-q8_0** — Best bang for buck. 7B params, 6.9s warm. Consistent across every run.
- **ministral-3:latest** — Only 3B parameters and still perfect. Consistently reliable.
- **mistral-small3.2:24b** — 24B Q4_K_M, fastest on GPU (6.1s). Occasionally drops T2.
- **glm-4.7-flash:q4_K_M** — Q4 quantized, 25.5s. Same T2 variance as mistral-small.
- **gpt-oss:20b** — Perfect every time but slowest (42.6s).

**Strong but stumble on multi-turn (8.3/10) — 4 models**

All of these make the first tool call correctly and select the right tool from a set of 3, but fail to make the second tool call after receiving a tool result.

- **qwen3:8b/14b** — Consistently struggles with multi-turn despite being otherwise capable.
- **qwen2.5:14b** — Good multi-turn but sometimes puts T1 tool call in text instead of structured format.
- **llama3.1:8b** — Same multi-turn weakness as qwen3.

**Broken tool calling (0/10) — 3 models (current run)**

- **Vision models** (llava:7b, llava:13b, llama3.2-vision:11b) — No tool calling at all. Expected.

**Previously tested, now unloaded**
- **hermes3:70b** (5.0/10) — Largest model tested, worst value. 146s response time.
- **qwen2.5-coder variants** (1.7/10) — Put tool names in text but never emit structured calls.
- **phi4, glm4:9b** (0.0/10) — Zero tool calls on any test.

### Size isn't everything

- **hermes3:70b** scored only 5.0/10 despite being the largest model tested.
- **ministral-3** at 3B parameters scores a perfect 10/10.
- The qwen2.5-coder:32b scored the same 1.7/10 as its 14B sibling.

## Part 2: Parallel vs Sequential Sweep Comparison

The real question: does fanning out monitoring phases to multiple Ollama instances actually save time? We tested this directly by running the same 3 sweep phases both ways.

### Test Design

Each phase simulates a real CFOperator sweep step — multi-turn (tool call → receive result → analyze):

| Phase | Prompt | Expected Tool |
|-------|--------|---------------|
| **metrics** | "Check CPU and memory usage across all hosts. Are any over 80%?" | `prometheus_query` |
| **logs** | "Search for recent error logs across all containers in the last hour." | `loki_query` |
| **containers** | "List Docker containers on raspberrypi, check for unhealthy/restarting." | `docker_list` |

After the model makes a tool call, it receives a fake result with realistic data (high CPU on one host, error logs from immich_server, a restarting mosquitto container) and must analyze the findings.

### Pool Configuration

| Instance | Hardware | Model | Role |
|----------|----------|-------|------|
| ollama-gpu | RTX GPU server (10.0.0.5) | mistral-small3.2:24b | metrics |
| ollama-198 | RTX 5080 16GB (10.0.0.6) | qwen2.5:7b-instruct-q8_0 | logs |
| ollama-desktop | Desktop CPU (10.0.0.8) | ministral-3:latest | containers |

### Sequential Baselines (each host running all 3 phases alone)

| Host | Model | Wall Clock | Tool Calls |
|------|-------|----------:|:----------:|
| ollama-gpu | mistral-small3.2:24b | 24.6s | 3/3 |
| ollama-198 | qwen2.5:7b-instruct-q8_0 | 7.3s | 2/3 |
| ollama-desktop | ministral-3:latest | 11.8s | 2/3 |
| **Average** | | **14.5s** | |

### Parallel Result (1 phase per host, concurrent)

| Phase | Host | Model | Time | Tool |
|-------|------|-------|-----:|:----:|
| metrics | ollama-gpu | mistral-small3.2:24b | 4.2s | OK |
| logs | ollama-198 | qwen2.5:7b-instruct-q8_0 | 0.5s | BAD |
| containers | ollama-desktop | ministral-3:latest | 3.0s | OK |
| **Wall clock** | | | **4.2s** | **2/3** |

Wall clock = longest phase. The other two phases ran concurrently and finished before it.

### Comparison

|  | Sequential (1 instance) | Parallel (3 instances) |
|--|:-----------------------:|:----------------------:|
| **Wall clock** | 14.5s | 4.2s |
| **Speedup** | — | **3.4x faster** |
| **Wall-clock reduction** | — | **70%** |

### Analysis

The parallel approach delivers a **3.4x speedup** by running each phase on a dedicated Ollama instance simultaneously. Wall clock time drops from 14.5s (sequential average) to 4.2s (limited by the slowest phase).

Key observations:
- **ollama-gpu (mistral-small3.2:24b)** was the most reliable — 3/3 correct tool calls every time as sequential, consistent as the metrics phase in parallel.
- **ollama-198 (qwen2.5:7b)** is the fastest by far (7.3s for all 3 phases sequentially) but has a blind spot — it occasionally fails the generic "search for error logs" prompt while acing the more specific T3 test.
- **ollama-desktop (ministral-3)** occasionally picks `docker_list` for the metrics prompt — a tool-selection error under ambiguity.

## Part 3: Production LangGraph Sweep Data

Beyond synthetic tests, we collected data from 19 real parallel sweeps running in CFOperator's LangGraph pipeline against live infrastructure (Prometheus, Loki, Docker).

### Sweep Timing (19 sweeps, Feb 12–13 2026)

| Metric | Value |
|--------|-------|
| Sample size | 19 parallel sweeps |
| Median duration | 38.3s |
| Average duration | 84.3s |
| Fast sweeps (<60s) | 11 (58%), avg 27.8s |
| Slow sweeps (>=60s) | 8 (42%), avg 161.9s |
| Min / Max | 19.4s / 185.1s |

### Bimodal Duration Pattern

Production sweeps show a clear bimodal distribution:

- **Fast (58%)**: 19–50s. All 3 instances respond quickly, models already loaded in VRAM.
- **Slow (42%)**: 135–185s. One instance stalls — typically cold model loading or the instance is busy with another request (luna-brain, chat, etc.).

The slow sweeps correlate with ollama-desktop being the bottleneck (ministral-3:latest on CPU-only desktop takes longer when the model needs to reload).

### Finding Quality

| Metric | Value |
|--------|-------|
| Total findings | 40 across 19 sweeps |
| Avg findings/sweep | 2.1 |
| Severity: info | 11 sweeps (58%) |
| Severity: warning | 5 sweeps (26%) |
| Severity: critical | 3 sweeps (16%) |

Models produced actionable findings including container restart detection, high resource usage alerts, and Loki query failures.

## Ollama vs Commercial APIs for Agent Work

CFOperator is designed as a local-first autonomous agent — it runs an OODA loop every 30 minutes, makes tool calls against Prometheus, Loki, and Docker, correlates findings, and stores learnings. This workload has specific characteristics that inform the Ollama vs commercial API decision.

### Where Ollama works well

**Cost at volume.** CFOperator runs 48 sweeps/day, each with 3 parallel phases making multi-turn tool calls. That's ~144 LLM calls/day minimum, plus alert checks, correlation analysis, chat interactions, and learning extraction. At commercial API rates, this adds up. With Ollama, the marginal cost per call is zero — just electricity and hardware you already own.

**Latency for structured tasks.** For simple tool-calling (T1, T3 in our benchmark), the best Ollama models respond in 0.5–6s on consumer hardware. That's comparable to API round-trip times once you account for network latency, and there's no rate limiting or queueing behind other customers.

**Privacy and control.** All infrastructure data — metrics, logs, container states, SSH access — stays on the local network. No prompts containing hostnames, IP addresses, error logs, or credentials leave the premises.

**Reliability.** No dependency on external services. The agent keeps running during internet outages, API provider incidents, or billing issues. For infrastructure monitoring, this is table stakes — the monitoring system shouldn't go down when the network does.

**Parallelism scales freely.** Adding a third Ollama instance gave us a 3.4x sweep speedup with zero incremental API cost. With commercial APIs you'd pay 3x for the same parallelism, and you'd still hit rate limits.

### Where Ollama falls short

**Tool-calling reliability.** The data is clear — even the best Ollama models aren't 100% reliable across runs. mistral-small3.2 and glm-4.7-flash fluctuate between 8.3 and 10.0 on multi-turn. qwen2.5:7b occasionally fails generic prompts it should handle. Commercial models like Claude and GPT-4 handle tool calling with near-perfect reliability and don't need careful prompt engineering to get structured output right.

**Reasoning depth.** When CFOperator asks a model to correlate patterns across multiple data sources, analyze root causes, or generate nuanced operational insights, the quality gap widens. A 7B model can call the right tool, but its analysis of the results is often shallow or formulaic compared to what Claude or GPT-4 would produce. We see this in finding quality — some sweep findings are raw JSON dumps or generic observations rather than actionable insights.

**Multi-turn consistency.** Half the models we tested (including some that score 10/10 on simple tests) fail to continue a tool chain after receiving a result. They respond with text analysis instead of making the next tool call. This is the single biggest gap — commercial models handle multi-step tool workflows without dropping the chain.

**Context window and complex prompts.** Smaller Ollama models struggle with longer system prompts, multiple tool schemas, and conversation history. CFOperator's real prompts are more complex than our test prompts, and we've seen models degrade as context grows. Commercial models handle 100K+ token contexts without this degradation.

### CFOperator's approach: hybrid fallback

CFOperator uses a provider chain — Ollama first, with Groq and Claude as fallbacks:

```
Ollama (local, free) → Groq (fast, cheap) → Claude (reliable, expensive)
```

The OODA loop and sweeps run on Ollama because they're high-volume, structured, and latency-tolerant (a sweep every 30 minutes doesn't need sub-second response). Chat interactions fall back to commercial APIs when Ollama is busy or returns poor results. Correlation analysis — which benefits most from reasoning depth — could justify routing to a stronger model, but currently runs on Ollama to keep costs at zero.

### The bottom line

For an autonomous agent making dozens of structured tool calls per hour on a local network, Ollama is viable and cost-effective. The 5 models that scored 10/10 in our benchmark can reliably call `prometheus_query`, `loki_query`, and `docker_list` when prompted clearly. But if your agent needs reliable multi-step reasoning, nuanced analysis, or robust handling of ambiguous prompts, commercial APIs are still meaningfully better. The pragmatic answer is both — Ollama for volume, commercial APIs for quality-critical paths.



**Infrastructure:** 3 Ollama instances across different hardware — an RTX GPU server, an RTX 5080 desktop, and a CPU-only desktop. Models tested wherever they were loaded.

**Prompt:** System message instructs the model to act as an infrastructure monitoring agent and always use tools rather than guessing. Temperature set to 0.3 for reproducibility.

**Tools provided:** Simplified versions of real CFOperator tools — `prometheus_query`, `loki_query`, and `docker_list` — using standard OpenAI-compatible function schemas.

**Sweep comparison:** Each phase is multi-turn — the model makes a tool call, receives a fake but realistic result, and analyzes it. Sequential runs all 3 phases on one host; parallel fans out 1 phase per host.

**Limitations:**
- Single run per model per test (no averaging across multiple trials) — but consistency tracked across 3 separate runs
- Quantization varies across hosts (some Q4, some Q8, some full precision)
- Models were tested on whichever host they were loaded on — hardware differences affect timing but not scoring
- The "correct" PromQL varies widely between models but all were accepted if they referenced CPU-related metrics
- Production sweep times include real tool execution (Prometheus/Loki queries) which adds latency beyond model inference
