# Tool-Calling Capability — qwen3.6:27b vs qwen3-coder:latest

**Suite:** CFOperator T1/T2/T3 (`test_tool_calling.py`, run via `scripts/test_model_local.py`)
**Endpoint:** `http://localhost:11434` (ubuntu-llm-01) | **Date:** 2026-05-19

Evaluated `qwen3.6:27b` as a candidate `llm.primary.model`, compared against the
incumbent `qwen3-coder:latest`. Both ran 100% on GPU (AMD Radeon RX 7900 XTX, ~24GB VRAM).

---

## Results

| Model | Score | T1 single | T2 multi-turn | T3 select | Time (4 calls) | Architecture | VRAM |
|-------|-------|-----------|---------------|-----------|----------------|--------------|------|
| qwen3.6:27b | 8.3/10 (5/6) | 2/2 ✓ | 1/2 | 2/2 ✓ | 62.1s | dense 27B | 24 GB |
| qwen3-coder:latest | 8.3/10 (5/6) | 2/2 ✓ | 1/2 | 2/2 ✓ | 22.3s | 30B-A3B MoE (~3B active) | 21 GB |

- **Identical tool-calling quality**, including the shared T2 weakness (one tool
  call then a text answer instead of chaining a second call) — not a regression.
- `qwen3.6:27b` is **~3x slower** despite both being fully GPU-resident: a dense
  27B activates all params per token vs the MoE's ~3B active. This is a
  steady-state architectural gap, not a hardware artifact.
- `qwen3.6:27b` sits at the VRAM ceiling (24 GB of ~25.7 GB) — little headroom
  for concurrent embeddings during the OODA sweep; `qwen3-coder` (21 GB) is safer.

## Decision

**Kept `qwen3-coder:latest` as `llm.primary.model`.** No quality gain justifies
3x latency in the OODA sweep loop. Revisit `qwen3.6:27b` only if its 256K context
or agentic-coding strengths are needed and the latency is acceptable.
