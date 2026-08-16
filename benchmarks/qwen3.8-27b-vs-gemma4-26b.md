# CFOperator Suitability — qwen3.8:27b vs gemma4:26b (incumbent)

**Endpoint:** `http://ubuntu-llm-01:11434` (AMD RX 7900 XTX 24 GB, ROCm) | **Date:** 2026-08-16
**Incumbent:** `gemma4:26b` is the deployed `llm.primary.model` (cfoperator-config, namespace apps).
**Candidate:** `qwen3.8:27b` — 27.3B dense, Q4_K_M, 17.7 GB on disk, multimodal (ships a 930 MB projector layer).

Three axes, matching the established methodology (see `gemma4-26b-vs-qwen3.6-27b.md`):

1. **Tool-calling** — T1/T2/T3 suite (`test_tool_calling.py` via `scripts/test_model_local.py`)
2. **Triage classification** — exact production prompt + parser (`benchmarks/triage_eval.py`,
   8 ground-truth alerts × 3 runs)
3. **Inference latency** — `benchmarks/ollama_latency_bench.py`, 5 iterations + 1 warmup

## Two methodology changes this round — read before comparing to July

**1. Ollama had to be upgraded, so every number was re-measured.**
`qwen3.8:27b` cannot be pulled on Ollama 0.30.5 — the registry rejects the manifest
with `412: requires a newer version`. The box was upgraded 0.30.5 → **v0.32.13**,
which moves libggml 0.13.1 → 0.19.0. A llama.cpp bump of that size can move the
incumbent, so **gemma4 was re-run on all three axes on the new runtime** rather
than compared against its July figures. Both models below ran on v0.32.13.

The upgrade turned out to be a mild win for the incumbent and no risk to it —
see "Did the upgrade move the incumbent?" below.

**2. The triage suite is saturated and no longer discriminates on accuracy.**
July scored gemma4 at 87.5%, but that was measured *before* the rubric fix in the
same PR (#75) landed. Under the current rubric gemma4 scores **100%**, and so does
qwen3.8. Both models now answer all 8 ground-truth cases correctly on every run,
so axis 2 contributes nothing to this decision beyond confirming neither model is
broken. The decision rests on axes 1 and 3. Hardening the case set is follow-up
work, not something this round silently absorbed.

A third, smaller fidelity fix: the July round's throwaway script called the model
at `temperature 0.3`, but production triage goes through
`_chat_with_tools_with_fallback`, whose ollama branch uses **0.7**. The committed
harness now matches production. July's absolute latencies are therefore slightly
off-production; the accuracy conclusions are unaffected.

---

## 1. Tool-calling (T1/T2/T3)

| Model | Score | T1 single | T2 multi-turn | T3 select | Suite time |
|-------|-------|-----------|---------------|-----------|------------|
| **gemma4:26b** | **10.0/10 (6/6)** | 2/2 | **2/2** | 2/2 | 51.1s* |
| qwen3.8:27b | 8.3/10 (5/6) | 2/2 | **1/2** | 2/2 | 86.7s* |

\* Both suite times include a cold model load (each model was evicted by the
other during the run), so they are not comparable to July's 18.3s figure for
gemma4 measured with the model already resident. Use axis 3 for latency.

- **qwen3.8 fails T2 with the exact qwen3.6 failure mode:** it makes one tool
  call, receives the result, then answers in prose instead of chaining a second
  call (`prometheus_query → loki_query`). Detail from the run:
  `1 tool call then text response (no 2nd call)`.
- gemma4 remains the only model tested that passes T2, and it holds 6/6 on the
  new runtime.
- Both produced correct PromQL/LogQL on T1/T3 — qwen3.8's tool *arguments* are
  fine; it is the multi-turn continuation that fails.

**This is the axis that decides the round.** T2 is not an academic test: the OODA
sweep depends on chaining a second tool call after seeing the first result.

## 2. Triage classification (production prompt, `run_triage`)

8 cases × 3 runs, temperature 0.7, scored by `CFOperator._parse_triage_response`.

| Model | JSON valid | Action accuracy | Mean latency | Max latency |
|-------|-----------|-----------------|--------------|-------------|
| gemma4:26b | 24/24 (100%) | 24/24 (100%) | **5.03s** | **9.07s** |
| qwen3.8:27b | 24/24 (100%) | 24/24 (100%) | 5.97s | 9.24s |

Per-case: both models score 3/3 on all eight cases (watchdog, smoke-test pod,
known SD-card w/ precedent, info-severity backup, novel OOMKilled, novel
ImagePullBackOff, control-plane NodeNotReady, correlated multi-service outage).

- **A tie, and an uninformative one** — see the saturation note above.
- Worth recording: qwen3.8 shows **none of qwen3.6's pathological tail**. Its
  worst triage call is 9.24s against qwen3.6's 156.9s, which used to breach
  `CFOP_TRIAGE_TIMEOUT_SECONDS=120`. Qwen appears to have fixed the
  thinking-verbosity problem that made 3.6 unusable for triage.

## 3. Inference latency (`ollama_latency_bench.py`, 5 iters, ollama 0.32.13)

Full per-prompt tables: `latency_qwen3.8-27b.md`, `latency_gemma4-26b_ollama0.32.md`.

| Category | Metric | gemma4:26b | qwen3.8:27b | Δ |
|----------|--------|------------|-------------|---|
| triage-short | mean TTFT | **6.6s** | 10.5s | 1.6x |
| triage-short | mean total | **13.6s** | 31.9s | 2.3x |
| analysis-medium | mean TTFT | **11.6s** | 13.4s | 1.2x |
| analysis-medium | mean total | **17.2s** | 38.5s | 2.2x |
| reasoning-long | mean TTFT | **10.6s** | 14.4s | 1.4x |
| reasoning-long | mean total | **15.4s** | 45.0s | 2.9x |
| all | throughput | **~100 tok/s** | ~43 tok/s | 2.3x |

- The gap is architectural, not tuning: gemma4 is a 26B-A4B MoE (~4B active
  params/token), qwen3.8 is dense 27B. Every token costs qwen3.8 ~6x the compute.
- **Worst single response:** gemma4 22.0s vs qwen3.8 56.6s. Unlike qwen3.6,
  qwen3.8 stays well inside the 120s triage timeout — it is *safe*, just slow.
- Sweep impact: at ~2.5x on total latency, sweep phases would go from ~15-20s
  back to ~40-50s each.

## 4. VRAM / GPU

Measured at identical 32768 context, one model resident at a time.

| Model | `ollama ps` size | Processor | Total VRAM used (rocm-smi) | Headroom |
|-------|------------------|-----------|----------------------------|----------|
| gemma4:26b | 17.4 GB | 100% GPU | **19.8 GB / 25.8 GB** | 5.9 GB |
| qwen3.8:27b | 17.4 GB | 100% GPU | 21.7 GB / 25.8 GB | 4.0 GB |

Same on-GPU weight size, but qwen3.8 costs ~1.9 GB more in total — the multimodal
projector plus a larger dense KV cache. With `nomic-embed-text` also resident
during sweeps, qwen3.8 leaves ~3.7 GB of headroom on a box that has already been
OOM-sensitive (see the `LLAMA_ARG_CTX_CHECKPOINTS=2` mitigation for the
2026-08-05 global-OOM that killed k8s pods).

## Did the upgrade move the incumbent?

This is why gemma4 was measured on both runtimes. Ollama 0.32.13 is a **mild
improvement** for gemma4 and regressed nothing:

| gemma4:26b | Ollama 0.30.5 | Ollama 0.32.13 |
|------------|---------------|----------------|
| Triage accuracy | 100% (24/24) | 100% (24/24) |
| Triage JSON valid | 100% | 100% |
| Triage mean / max | 5.53s / 12.58s | **5.03s / 9.07s** |
| Throughput (all categories) | ~87 tok/s | **~100 tok/s** |
| Tool-calling | 10.0/10 | 10.0/10 |

~13% more throughput and a tighter tail, same correctness. Evidence files:
`triage_eval_gemma4_26b_ollama0.30.json` vs `..._ollama0.32.json`, and
`latency_gemma4-26b_ollama0.30.md` vs `..._ollama0.32.md`.

The production tuning in `/etc/systemd/system/ollama.service.d/override.conf`
survived the upgrade intact, and the new llama-server still honors
`LLAMA_ARG_CTX_CHECKPOINTS` (same flag name, same default of 32) — verified
before the swap, because losing it silently would reopen the August OOM.

## Decision

**Do not switch. `gemma4:26b` stays `llm.primary.model`.** qwen3.8:27b loses or
ties on every axis:

- **Quality:** 8.3/10 vs 10.0/10 on tool-calling, failing T2 multi-turn chaining
  — the capability the OODA sweep is built on. Triage accuracy ties at 100%, but
  on a suite both models have saturated.
- **Speed:** 2.2–2.9x higher total latency, 2.3x lower throughput.
- **VRAM:** 1.9 GB more for the same weights, cutting headroom from 5.9 GB to
  4.0 GB on a box with a history of OOM cascades.

There is no axis on which qwen3.8:27b is the better choice for this workload. A
dense 27B is simply the wrong shape for an agent that makes many sequential LLM
calls per sweep.

**Secondary recommendations:**

1. **Keep Ollama v0.32.13.** It was pulled in to make this benchmark possible,
   but it earns its place independently: ~13% more throughput and a tighter
   latency tail for the incumbent, with no correctness change. Rollback to
   0.30.5 is staged at `/home/aachten/ollama-backup-0.30.5` if ever needed.
2. **Retire `qwen3.6:27b` as the rollback pin in favour of `qwen3.8:27b`.**
   The rollback model exists to be a safe landing spot if gemma4 regresses, and
   qwen3.6 is a bad one: 30.7s mean triage, 156.9s worst case, which breaches
   `CFOP_TRIAGE_TIMEOUT_SECONDS=120`. qwen3.8 does the same job at 5.97s mean /
   9.24s max with 100% accuracy. It is a poor primary but a much better
   parachute, and it frees 17.4 GB of disk if 3.6 is removed.
3. **Harden the triage case set before the next candidate.** With two models at
   100%, axis 2 has no discriminating power left. Candidates for harder cases:
   alerts whose correct action depends on reading the precedent's *outcome*
   rather than its presence; multi-alert correlation where the cheap answer is
   wrong; and alerts that should be `log_only` despite critical severity.

## Reproducing

```bash
# axis 1
.venv/bin/python scripts/test_model_local.py qwen3.8:27b http://localhost:11434

# axis 2 — extracts the live production prompt from agent/agent.py
.venv/bin/python benchmarks/triage_eval.py --model qwen3.8:27b --runs 3 \
    --output benchmarks/triage_eval_qwen3.8_27b.json

# axis 3
.venv/bin/python benchmarks/ollama_latency_bench.py --model qwen3.8:27b \
    --iterations 5 --output benchmarks/latency_qwen3.8-27b.md
```

Run one model at a time — each is ~17 GB on a 24 GB card, and overlapping two
produces eviction thrash rather than a fair measurement.
