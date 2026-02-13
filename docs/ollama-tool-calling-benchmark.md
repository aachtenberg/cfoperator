# Ollama Tool-Calling Benchmark Results

**Date:** 2026-02-12
**Test harness:** `test_tool_calling.py`
**Hosts tested:** 3 Ollama instances (GPU server, desktop, Raspberry Pi)
**Models tested:** 22

## What We Tested

An autonomous infrastructure monitoring agent (CFOperator) needs LLMs that can reliably make structured tool calls — not just generate text about tools, but emit proper JSON function calls that code can parse and execute.

We tested every model across our 3 Ollama instances against 3 scenarios:

| Test | Description | What a perfect score looks like |
|------|------------|--------------------------------|
| **T1 — Single tool call** | "What is the current CPU usage on raspberrypi?" (1 tool available) | Emits a structured `prometheus_query` call with valid PromQL |
| **T2 — Multi-turn** | "Check CPU usage and then check for error logs" (2 tools available) | Makes `prometheus_query`, receives result, then makes `loki_query` |
| **T3 — Tool selection** | "Show me recent error logs from immich_server" (3 tools available) | Picks `loki_query` over `prometheus_query` and `docker_list` |

**Scoring:** 0 = no tool call / garbled, 1 = tool call but wrong name or bad args, 2 = correct tool with reasonable args. Max raw score = 6, normalized to 0–10.

## Rankings

| Rank | Model | Size | Host | T1 | T2 | T3 | Score | Time |
|-----:|-------|------|------|:--:|:--:|:--:|------:|-----:|
| 1 | qwen2.5:7b-instruct-q8_0 | 7B | ollama-198 | 2 | 2 | 2 | **10.0** | 19.4s |
| 2 | ministral-3:latest | 3B | ollama-desktop | 2 | 2 | 2 | **10.0** | 20.2s |
| 3 | mistral-small3.2:24b | 24B | ollama-gpu | 2 | 2 | 2 | **10.0** | 24.1s |
| 4 | glm-4.7-flash:q4_K_M | ~9B | ollama-gpu | 2 | 2 | 2 | **10.0** | 28.4s |
| 5 | gpt-oss:20b | 20B | ollama-desktop | 2 | 2 | 2 | **10.0** | 59.3s |
| 6 | qwen3:8b | 8B | ollama-198 | 2 | 1 | 2 | 8.3 | 20.4s |
| 7 | qwen3:14b | 14B | ollama-198 | 2 | 1 | 2 | 8.3 | 35.5s |
| 8 | qwen3:14b | 14B | ollama-gpu | 2 | 1 | 2 | 8.3 | 30.5s |
| 9 | qwen3-14b-quiet:latest | 14B | ollama-gpu | 2 | 1 | 2 | 8.3 | 38.3s |
| 10 | llama3.1:8b | 8B | ollama-desktop | 2 | 1 | 2 | 8.3 | 45.1s |
| 11 | ministral-3:14b | 14B | ollama-desktop | 2 | 1 | 0 | 5.0 | 7.6s |
| 12 | hermes3:70b-llama3.1-q3_K_M | 70B | ollama-gpu | 2 | 1 | 0 | 5.0 | 146.3s |
| 13 | qwen2.5-coder:14b | 14B | ollama-198 | 1 | 0 | 0 | 1.7 | 15.7s |
| 14 | qwen2.5-coder:14b-instruct-q4_K_M | 14B | ollama-gpu | 1 | 0 | 0 | 1.7 | 15.6s |
| 15 | qwen2.5-coder:14b | 14B | ollama-gpu | 1 | 0 | 0 | 1.7 | 3.6s |
| 16 | qwen32b-8k:latest | 32B | ollama-gpu | 1 | 0 | 0 | 1.7 | 33.0s |
| 17 | qwen2.5-coder:32b-instruct-q4_K_M | 32B | ollama-gpu | 1 | 0 | 0 | 1.7 | 27.3s |
| 18 | llava:7b | 7B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 19 | llava:13b | 13B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 20 | glm4:9b | 9B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 21 | llama3.2-vision:11b | 11B | ollama-198 | 0 | 0 | 0 | 0.0 | — |
| 22 | phi4:latest | 14B | ollama-desktop | 0 | 0 | 0 | 0.0 | — |

## Key Takeaways

### Perfect scores (10/10) — 5 models

These models nailed all three tests: single call, multi-turn continuation, and correct tool selection from multiple options.

- **qwen2.5:7b-instruct-q8_0** — Best bang for buck. 7B parameters, fastest of the perfect-score group (19.4s), running on a Raspberry Pi-class host.
- **ministral-3:latest** — Only 3B parameters and still perfect. 20.2s. Impressive efficiency.
- **mistral-small3.2:24b** — Solid mid-size option at 24.1s.
- **glm-4.7-flash:q4_K_M** — Q4 quantized and still flawless. 28.4s.
- **gpt-oss:20b** — Perfect but slowest of the top tier at 59.3s.

### Strong but stumble on multi-turn (8.3/10) — 5 models

All of these make the first tool call correctly and select the right tool from a set of 3, but fail to make the *second* tool call after receiving a tool result. They respond with text instead of continuing the tool chain.

- **qwen3:8b/14b**, **qwen3-14b-quiet**, **llama3.1:8b** — The qwen3 family consistently struggles with multi-turn despite being otherwise capable.

### Broken tool calling (0–1.7/10) — 12 models

- **qwen2.5-coder** variants (14B, 32B) — Put tool names in text but never emit structured calls. Code-focused training may have deprioritized tool-use format.
- **Vision models** (llava, llama3.2-vision) — No tool calling at all. Expected.
- **phi4, glm4:9b** — Zero tool calls across all tests.

### Size isn't everything

- **hermes3:70b** scored only 5.0/10 despite being the largest model tested (146s response time).
- **ministral-3** at 3B parameters scored a perfect 10/10 in 20s.
- The qwen2.5-coder:32b scored the same 1.7/10 as its 14B sibling.

## Detailed Results

### qwen2.5:7b-instruct-q8_0 — 10.0/10
- **T1** [2/2]: `node_exporter_cpu_utilization{instance=~"raspberrypi"}`
- **T2** [2/2]: prometheus_query → loki_query
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### ministral-3:latest — 10.0/10
- **T1** [2/2]: `rate(container_cpu_usage_seconds_total{container_name="raspberrypi"}[1m]) * 100`
- **T2** [2/2]: prometheus_query → loki_query
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### mistral-small3.2:24b — 10.0/10
- **T1** [2/2]: `100 - (avg by (instance) (rate(node_cpu_seconds_total{instance="raspberrypi", mode="idle"}[1m])) * 100)`
- **T2** [2/2]: prometheus_query → loki_query
- **T3** [2/2]: `container_name="immich_server" |= "error"`

### glm-4.7-flash:q4_K_M — 10.0/10
- **T1** [2/2]: `cpu_usage_percent`
- **T2** [2/2]: prometheus_query → loki_query
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### gpt-oss:20b — 10.0/10
- **T1** [2/2]: `100 * (1 - sum(rate(node_cpu_seconds_total{mode="idle"}[5m])) by (instance) / sum(rate(node_cpu_seconds_total[5m])) by (instance))`
- **T2** [2/2]: prometheus_query → loki_query
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### qwen3:8b — 8.3/10
- **T1** [2/2]: `100 * (1 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m]))))`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### qwen3:14b (ollama-198) — 8.3/10
- **T1** [2/2]: `100 * (sum by (instance) (rate(node_cpu_seconds_total{mode!~"idle|guest"}[1m]))) / sum by (instance) (rate(node_cpu_seconds_total[1m]))`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### qwen3:14b (ollama-gpu) — 8.3/10
- **T1** [2/2]: `100 * (1 - (avg by (instance) (node_cpu_seconds_total{mode="idle"})))`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### qwen3-14b-quiet:latest — 8.3/10
- **T1** [2/2]: `(100 * sum by (instance) (rate(node_cpu_seconds_total{mode!~"idle|nice"}[1m]))) / sum by (instance) (rate(node_cpu_seconds_total[1m]))`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### llama3.1:8b — 8.3/10
- **T1** [2/2]: `node_cpu{instance="raspberrypi"}`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [2/2]: `{container_name="immich_server"} |= "error"`

### ministral-3:14b — 5.0/10
- **T1** [2/2]: `100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle",instance="raspberrypi"}[1m])) * 100)`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [0/2]: No tool call

### hermes3:70b-llama3.1-q3_K_M — 5.0/10
- **T1** [2/2]: `node_cpu{instance="raspberrypi"}`
- **T2** [1/2]: 1 tool call then text response (no 2nd call)
- **T3** [0/2]: No tool call

### qwen2.5-coder variants (14b, 32b) — 1.7/10
- All variants put tool names in text content but never emitted structured tool calls.

### Zero-score models
- **llava:7b**, **llava:13b**, **glm4:9b**, **llama3.2-vision:11b**, **phi4:latest** — No tool calls on any test.

## LangGraph Parallel Sweep Results

Beyond isolated tool-calling tests, we tested how these models perform in production as part of CFOperator's LangGraph parallel sweep pipeline. The sweep fans out 3 phases (metrics, logs, containers) to different Ollama instances concurrently, each making real tool calls against live infrastructure.

### Pool Configuration

| Instance | Hardware | Model | Phase |
|----------|----------|-------|-------|
| ollama-gpu | RTX GPU server (192.168.0.150) | glm-4.7-flash:q4_K_M | metrics |
| ollama-198 | RTX 5080 16GB (192.168.0.198) | qwen2.5:7b-instruct-q8_0 | logs/containers |
| ollama-desktop | Desktop (192.168.0.220) | ministral-3:latest | logs/containers |

All three models scored 10/10 in the isolated tool-calling benchmark. Phase assignment rotates based on pool availability.

### Sweep Timing (19 parallel sweeps, Feb 12–13 2026)

| Metric | Value |
|--------|-------|
| Sample size | 19 parallel sweeps |
| Median duration | 38.3s |
| Average duration | 84.3s |
| Fast sweeps (<60s) | 11 (58%), avg 27.8s |
| Slow sweeps (>=60s) | 8 (42%), avg 161.9s |
| Min / Max | 19.4s / 185.1s |

### Per-Phase Breakdown (typical fast sweep)

| Phase | Instance | Model | Duration | Findings |
|-------|----------|-------|----------|----------|
| metrics | ollama-gpu | glm-4.7-flash:q4_K_M | 0.0s | 0 |
| logs | ollama-desktop | ministral-3:latest | 37.5s | 1 |
| containers | ollama-198 | qwen2.5:7b-instruct-q8_0 | 39.8s | 0 |
| **Wall clock** | | | **39.8s** | |

Wall clock time equals the slowest phase — that's the parallel advantage. Sequential would sum to ~77s.

### Bimodal Duration Pattern

Sweeps show a clear bimodal distribution:

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

Models produced actionable findings including container restart detection, high resource usage alerts, and Loki query failures. Some findings included raw JSON or verbose explanations — a known issue with smaller models not always following the structured output format strictly.

## Methodology

**Infrastructure:** 3 Ollama instances across different hardware — a GPU server (RTX-class), a desktop, and a Raspberry Pi cluster node. Models tested wherever they were already loaded.

**Prompt:** System message instructs the model to act as an infrastructure monitoring agent and always use tools rather than guessing. Temperature set to 0.3 for reproducibility.

**Tools provided:** Simplified versions of real CFOperator tools — `prometheus_query`, `loki_query`, and `docker_list` — using standard OpenAI-compatible function schemas.

**Limitations:**
- Single run per model (no averaging across multiple trials)
- Quantization varies across hosts (some Q4, some Q8, some full precision)
- Models were tested on whichever host they were loaded on — hardware differences affect timing but not scoring
- The "correct" PromQL varies widely between models but all were accepted if they referenced CPU-related metrics
