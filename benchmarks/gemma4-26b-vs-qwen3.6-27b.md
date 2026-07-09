# CFOperator Suitability — gemma4:26b vs qwen3.6:27b (incumbent)

**Endpoint:** `http://ubuntu-llm-01:11434` (Ollama 0.30.5, AMD RX 7900 XTX 24 GB, ROCm) | **Date:** 2026-07-09
**Incumbent:** `qwen3.6:27b` is the deployed `llm.primary.model` (cfoperator-config, namespace apps).
**Candidate:** `gemma4:26b` — 26B-A4B MoE (~4B active params), QAT quant, 17 GB on disk.

Three axes, matching the established methodology (see `qwen3.6-27b-tool-calling.md`):

1. **Tool-calling** — T1/T2/T3 suite (`test_tool_calling.py` via `scripts/test_model_local.py`)
2. **Triage classification** — exact production prompt + parser from `agent.run_triage`
   (8 ground-truth alerts × 3 runs; JSON validity + action accuracy + latency)
3. **Inference latency** — `benchmarks/ollama_latency_bench.py`, 5 iterations + 1 warmup

---

## 1. Tool-calling (T1/T2/T3)

| Model | Score | T1 single | T2 multi-turn | T3 select | Suite time |
|-------|-------|-----------|---------------|-----------|------------|
| **gemma4:26b** | **10.0/10 (6/6)** | 2/2 | **2/2** | 2/2 | **18.3s** |
| qwen3.6:27b | 8.3/10 (5/6) | 2/2 | 1/2 | 2/2 | 139.8s |

- gemma4 is the **first model tested that passes T2** (chains a second tool call
  after a tool result: `prometheus_query → loki_query`). qwen3.6 repeats its known
  T2 failure — one call, then a text answer.
- Suite wall-clock: gemma4 **7.6x faster** (MoE ~4B active vs dense 27B).
- Both produced correct PromQL/LogQL args on T1/T3.

## 2. Triage classification (production prompt, `run_triage`)

8 cases × 3 runs, temperature 0.3. Scored by the exact `_parse_triage_response` parser.

| Model | JSON valid | Action accuracy | Mean latency | Max latency |
|-------|-----------|-----------------|--------------|-------------|
| **gemma4:26b** | 24/24 (100%) | **21/24 (87.5%)** | **6.5s** | 13.7s |
| qwen3.6:27b | 24/24 (100%) | 18/24 (75%) | 30.7s | **156.9s** |

Per-case (correct runs out of 3):

| Case | Expected | gemma4:26b | qwen3.6:27b |
|------|----------|------------|-------------|
| Watchdog | log_only | 3/3 | 3/3 |
| smoke-test pod crashloop | log_only | 3/3 | 3/3 |
| Known raspberrypi SD-card (w/ precedent) | notify | 3/3 | 3/3 |
| Info-severity backup notice | notify | 3/3 | 3/3 |
| Novel OOMKilled pod | investigate | 2/3 | 0/3 |
| Novel ImagePullBackOff | investigate | 1/3 | 0/3 |
| Control-plane NodeNotReady (critical) | escalate | 3/3 | 3/3 |
| Correlated multi-service outage (critical) | escalate | 3/3 | 3/3 |

- **Both models share an under-escalation bias on novel warning-severity pods**
  (answer `notify` instead of the rubric's `investigate` default). gemma4 gets it
  right 3/6 runs; qwen3.6 0/6. Neither ever mis-fires on noise or criticals.
- **Operational finding:** qwen3.6's worst triage call took **156.9s — over the
  production `CFOP_TRIAGE_TIMEOUT_SECONDS=120`**, which would fall through to the
  fallback chain (default `investigate`). gemma4's worst case is 13.7s.

## 3. Inference latency (`ollama_latency_bench.py`, 5 iters)

Full per-prompt tables: `latency_gemma4-26b.md`, `latency_qwen3.6-27b.md`.

| Category | Metric | gemma4:26b | qwen3.6:27b | Δ |
|----------|--------|------------|-------------|---|
| triage-short | mean TTFT | **6.4s** | 42.4s | 6.6x |
| triage-short | mean total | **14.4s** | 63.3s | 4.4x |
| analysis-medium | mean TTFT | **10.5s** | 94.2s | 9.0x |
| analysis-medium | mean total | **16.7s** | 119.7s | 7.2x |
| reasoning-long | mean TTFT | **11.8s** | 99.0s | 8.4x |
| reasoning-long | mean total | **17.1s** | 121.0s | 7.1x |
| all | throughput | **~90 tok/s** | ~28 tok/s | 3.2x |

- qwen3.6's high TTFT is mostly its thinking phase — it emits 1.6–4.6k tokens
  per response (thinking included) at 28 tok/s. gemma4 answers in 0.9–2k tokens.
- **Worst-case single response:** gemma4 22.1s vs qwen3.6 161.9s. qwen3.6
  regularly exceeds 120s on analysis/reasoning prompts — the same class of call
  that `CFOP_TRIAGE_TIMEOUT_SECONDS=120` governs.

## 4. VRAM / GPU

| Model | `ollama ps` size | Processor | Total VRAM used (rocm-smi) |
|-------|------------------|-----------|----------------------------|
| gemma4:26b | 17 GB | 100% GPU | **20.4 GB / 24 GB** (incl. nomic-embed resident) |
| qwen3.6:27b | 18 GB | 100% GPU | ~24 GB (at ceiling per May bench) |

gemma4 leaves headroom for the embedding model + longer contexts; qwen3.6 sits at
the VRAM ceiling.

## Decision

**Recommend switching `llm.primary.model` to `gemma4:26b`.** It wins every axis:

- **Quality:** first perfect 10/10 on the tool-calling suite (only model to pass
  T2 multi-turn chaining — directly relevant to the OODA sweep); triage accuracy
  87.5% vs 75% with identical JSON reliability (100% both).
- **Speed:** 4–9x lower latency, 3.2x throughput. Sweep phases drop from ~2 min
  to ~15–20s each; triage from ~30s to ~6s mean.
- **Reliability:** qwen3.6's tail (157s triage worst case, 162s response worst
  case) breaches the 120s triage timeout; gemma4's worst case is 22s.
- **VRAM:** 20.4 GB total with embeddings resident vs qwen3.6 at the 24 GB
  ceiling — headroom for concurrent embedding calls during sweeps.

Caveats:
- Both models under-escalate novel warning-severity pods (`notify` instead of
  the rubric's `investigate` default); gemma4 is better (3/6 vs 0/6) but not
  fixed. A rubric tweak ("warning-severity pod failures with no similar past
  investigation → investigate") would likely close this for either model.
- Rollout is a one-line change in cfoperator-deploy config
  (`llm.primary.model: gemma4:26b`); morning-summary and triage both inherit it.
  Keep qwen3.6:27b pulled for quick rollback.
