# ministral-3:14b — base-model baseline and fine-tune result

> This page is the **benchmark record**: what the numbers were before and after.
> For the model card — training recipe, dataset shape, artifact locations,
> rebuild-from-NAS procedure and rollback — see
> [docs/triage-fine-tune.md](../docs/triage-fine-tune.md).

## Fine-tune result (2026-08-20, same day)

`cfop-triage-ministral3:v1` — QLoRA (attention-only r16, LR 1e-4, 2 epochs,
451 examples from `scripts/build_triage_dataset.py`, response-only loss,
trained on a 5080 in 17 min via unsloth studio; merged Q8_0 GGUF):

| Model | 14-case ×3 | precedent-monitoring ×12 | critical-narrow ×12 | JSON | Latency mean |
|-------|-----------|--------------------------|---------------------|------|--------------|
| gemma4:26b (incumbent) | 42/42 | 12/12 | 12/12 | 100% | 5.53s |
| ministral-3:14b (base) | 37/42 | 4/12 | 4/12 | 100% | 0.93s |
| **cfop-triage-ministral3:v1** | **42/42** | **12/12** | **12/12** | 100% | **~0.8–1.1s** |

Both notify-shortcut classes eliminated; matches the incumbent's clean sheet
at ~6x lower latency. Eval loss stayed flat through epoch 2 — but read that
as "no divergence", not as evidence the boundary was learned: the val split is
46/50 `investigate`, so a constant predictor scores 92% on it, and thousandths
of masked JSON-verdict loss carry little information (see the Analysis section
of `docs/triage-fine-tune.md`). The evidence that it learned the rubric is the
held-out suite below; the training set excluded every eval case by
construction. ×50 soak on both hard cases: **100/100** (an 8%-rate shortcut
would escape a ×50 soak only ~1.5% of the time —
`triage_eval_cfop_triage_ministral3_v1_soak_x50.json`).

**Q4_K_M deployment gate (CFOP-57): PASSED.** `cfop-triage-ministral3:v1-q4`
(~8GB, co-fits with gemma4:26b in 24GiB — no model swapping): 42/42, 24/24,
soak 100/100, JSON 100%, latency mean 0.62–0.71s. This is the deployment
artifact; Q8_0 is the archival reference. The `llm.triage_model` wiring
landed with CFOP-57 (PR #144); remaining work is optionally a shadow period
on live alerts.

## Base-model baseline (pre-fine-tune)

**Date:** 2026-08-20 | **Endpoint:** localhost:11434 (ubuntu-llm-01) | **ollama:** 0.32.13
**Context:** step 2 of the fine-tuning experiment — measure the base model
before training anything, so the fine-tune has a defined target and a
before/after. Dataset extraction lives in `scripts/build_triage_dataset.py`.

## Triage (production prompt, `triage_eval.py`)

| Model | Accuracy (14-case ×3) | JSON valid | Latency mean | precedent-monitoring ×12 | critical-narrow ×12 |
|-------|----------------------|------------|--------------|--------------------------|---------------------|
| gemma4:26b (incumbent) | 42/42 | 100% | 5.53s | 12/12 | 12/12 |
| ministral-3:14b (base) | 37/42 | 100% | **0.93s** | 4/12 | 4/12 |

- Both failures are the **notify shortcut**, taken ~2/3 of the time on both
  trap classes: precedent *presence* read as precedent *outcome*
  (`precedent-monitoring`), and severity read without breadth
  (`critical-narrow` — it answered notify, not even the expected escalate
  trap). This is systematic, not the rare-miss pattern qwen3.8 showed (8%).
- JSON discipline is perfect and it is **6x faster than gemma4** — the
  entire value proposition of the experiment. If SFT fixes the two shortcut
  classes (exactly the label bases the dataset builder derives), triage drops
  from ~5.5s to ~1s.

## Tool calling (T1/T2/T3, `scripts/test_model_local.py`)

| Model | Score | T1 single | T2 multi-turn | T3 select |
|-------|-------|-----------|---------------|-----------|
| qwen3-coder:latest (ref, 2026-05) | 8.3/10 | 2/2 | 1/2 | 2/2 |
| ministral-3:14b (base) | 5.0/10 | 2/2 | 1/2 | **0/2** |

- T2 (stop-after-one-call) is the same weakness every local candidate has
  shown.
- **T3=0 is an ollama parser bug, not model capability.** Re-run 6/6 times,
  the model selects the right tool with a perfect LogQL query every time —
  but emits it in Mistral's native `[TOOL_CALLS]name[ARGS]{json}` wire
  format (exactly what its own chat template teaches), and ollama 0.32.13
  fails to parse it back into structured `tool_calls`, dumping it as text
  content. Upstream open issues: ollama/ollama#16934 (default mistral3 to
  the ministral parser), #17550 (incomplete ministral tool calls). No fix
  through v0.32.15. Until upstream lands it, this family cannot serve the
  tool loop via ollama regardless of fine-tuning.

## Decision

Base model is **not deployable** for triage (would notify-away real
incidents) but is the right fine-tune candidate: failures are narrow,
systematic, rubric-shaped, and sit exactly on the label bases
`build_triage_dataset.py` emits. Scope the experiment **triage-only**:
tool-calling is blocked by the ollama parser upstream (above), and triage
runs with tools withheld anyway — it is also where the latency win lives.
Revisit the tool loop when ollama's ministral parser lands.

Raw runs: `triage_eval_ministral3_14b.json`,
`triage_eval_ministral3_14b_hardcases_x12.json`.
