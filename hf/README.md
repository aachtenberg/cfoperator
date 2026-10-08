---
license: apache-2.0
base_model: mistralai/Ministral-3-14B-Instruct-2512
base_model_relation: finetune
pipeline_tag: text-generation
language:
  - en
tags:
  - gguf
  - ollama
  - qlora
  - unsloth
  - alert-triage
  - kubernetes
  - sre
  - homelab
  - cfoperator
---

# cfop-triage-ministral3 (14B, v6)

A QLoRA fine-tune of [Ministral 3 14B Instruct](https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512)
that does one job: classify an infrastructure alert into one of four actions
for [cfoperator](https://github.com/aachtenberg/cfoperator), an autonomous
monitoring agent for a Kubernetes homelab. It replaced a 26B general model on
that path at roughly 5x lower latency with the same accuracy on the project's
triage suite.

**This is not a general assistant.** It was trained on exactly one system
prompt and answers with exactly one JSON shape. Outside that prompt it is just
a slightly bent Ministral. Read [What it is not](#what-it-is-not) before
downloading 8 GB.

## Files

| File | What | Size |
|---|---|---|
| `ministral-3-14b-instruct-2512.Q4_K_M.gguf` | The quant that shipped the gate (14 cases × 36, soak, leak gate). | 8.2 GB |
| `ministral-3-14b-instruct-2512.Q8_0.gguf` | Reference quant. Agrees with Q4 on every gated case. | 14.4 GB |
| `Modelfile` | The exact ollama Modelfile production runs, with `FROM` pointing at the Q4 file above. | |
| `adapter/` | The LoRA adapter (`adapter_model.safetensors`, `adapter_config.json`). Resume a retrain from here instead of from base. | 79 MB |

The vision projector (`mmproj`) is not included. Triage is text-only and the
training run never exercised the vision layers.

## Use it

With ollama, either pull straight from the Hub or build from the Modelfile.
The Modelfile is the production configuration; the direct pull uses the chat
template embedded in the GGUF, which reproduces the base model's template and
has given identical verdicts in practice.

```bash
# Option A: direct pull
ollama run hf.co/REPO_ID:Q4_K_M

# Option B: exact production setup (fetches the Q4 file and the Modelfile, ~8 GB, not the Q8)
hf download REPO_ID --include "*.Q4_K_M.gguf" Modelfile --local-dir cfop-triage
cd cfop-triage && ollama create cfop-triage-ministral3:v6-q4 -f Modelfile
```

In cfoperator, point triage at it and leave investigations on the primary model:

```yaml
llm:
  triage_model: cfop-triage-ministral3:v6-q4
```

Unparseable output falls back to the normal chain, so a wrong tag costs
latency, not an outage. The key is documented in
[config-reference.md](https://github.com/aachtenberg/cfoperator/blob/main/docs/config-reference.md).

To call it directly, send cfoperator's triage system prompt (extracted at
build time from `agent/agent.py`, never paraphrased) and a user message of
this shape:

```
Alert severity: warning
Alert summary: The faster-whisper deployment in the ai namespace has a pod with 1 restart
Labels: {"namespace": "ai"}

Similar past investigations:
- [monitoring] monitoring_cycle: Stopped containers detected without error logs (similarity: 0.71)

Classify.
```

and expect:

```json
{"action": "investigate", "reason": "faster-whisper: the closest earlier investigation (0.71) ended monitoring — no resolved precedent to lean on", "confidence": 0.58}
```

`action` is one of `log_only`, `notify`, `investigate`, `escalate`.

## What it is not

- **Not tool-capable.** ollama cannot parse Ministral's native tool-call wire
  format ([ollama/ollama#16934](https://github.com/ollama/ollama/issues/16934)),
  which is why the fine-tune was scoped to triage in the first place.
- **Not a general classifier.** It has seen one prompt. A different rubric,
  field order or output schema puts it off-distribution.
- **Not calibrated.** `confidence` tracks the action and the strength of the
  precedent match. It is not a probability.
- **Not trained on your infrastructure.** The 324 training rows are one
  homelab's alert history: Raspberry Pi nodes, a k3s cluster, Alertmanager and
  cfoperator's own monitoring cycles. The training targets quote the prompt,
  so pod, node and namespace names, private `192.168.x.x` addresses and
  ingress hostnames from that homelab can surface in `reason`, especially on
  prompts that resemble its alerts.
- **Not evaluated at scale.** The headline numbers come from a 14-case
  in-house suite run 36 times, plus a 50-run soak on the two hardest cases.
  That suite was held out from training by construction, but it is small.

## Results

Measured with cfoperator's `benchmarks/triage_eval.py` on the production
prompt. Latency is per alert on an AMD RX 7900 XTX with the model resident in
VRAM; the v6 rows are on ollama 0.40, the two reference rows on 0.32.

| Model | Action correct (14 cases x 36) | Fabricated citations | JSON valid | Mean latency |
|---|---:|---:|---:|---:|
| gemma4:26b (previous incumbent) | 42/42 (x3) | n/a | 100% | 5.53 s |
| Ministral-3-14B-Instruct base | 37/42 (x3) | n/a | 100% | 0.93 s |
| **this model, Q4_K_M** | **504/504** | **0/504** | **100%** | **0.84 s** |
| this model, Q8_0 | 504/504 | 0/504 | 100% | 1.21 s |

Hard-case soak, 50 runs each on the two cases the base model fails most:
100/100, zero fabricated citations.

Leak gate (`hf/check_model_text.py`): the five training prompts whose
original targets named the operator's domain, plus the 14 eval cases, ten
runs each, once at the Modelfile temperature and once at 0.7: 380
completions, none containing the domain or anything the dataset scanner
flags.

"Fabricated citation" means the `reason` named a pod, node or precedent that
was not in the prompt. Earlier generations of this fine-tune passed the
action check and failed this one; v5 was the first to clear both, and v6 is
v5 retrained on the same data with one hostname scrubbed (see Data). The full
history, including the rejected v2, v3 and v4 runs and why, is in
[docs/triage-fine-tune.md](https://github.com/aachtenberg/cfoperator/blob/main/docs/triage-fine-tune.md).

## Training

| | |
|---|---|
| Base | `unsloth/Ministral-3-14B-Instruct-2512-unsloth-bnb-4bit` (4-bit repack of Mistral's release) |
| Method | QLoRA, r=16, alpha=16, dropout 0.05, on `q_proj k_proj v_proj o_proj` |
| Data | 324 train rows (310 historical, 14 synthetic; 5 rewritten by the scrub), 34 validation rows |
| Loss | On assistant tokens only (`train_on_completions`) |
| Schedule | 3 epochs, 123 steps, batch 1 x 8 accumulation, LR 1e-4 linear, 10 warmup steps |
| Optimizer | `adamw_8bit`, weight decay 0.001, grad clip 1.0, seed 3407 |
| Sequence | max 768 tokens (longest row 643) |
| Hardware | One RTX 5060 Ti 16 GB, [unsloth](https://unsloth.ai) studio (unsloth 2026.10.2), 2026-10-07, 25 min |
| Final loss | 0.0174 (v5 on the unscrubbed data: 0.0199, same curve) |
| Export | Merged to fp16, converted with llama.cpp, quantized Q4_K_M and Q8_0 |

### Data

Each row replays a real alert from cfoperator's investigation history into the
exact production triage prompt. The label is derived, not observed: every
stored investigation was by definition routed `investigate`, so the training
target is the cheapest action that would have been correct given how the
investigation actually ended. The derivation rule is recorded per row and
audited; rows whose `reason` cited anything absent from their prompt were
rejected before training.

| Action | Train rows |
|---|---:|
| investigate | 187 |
| notify | 103 |
| escalate | 32 |
| log_only | 2 |

The 14 synthetic rows cover one shape the history cannot supply (severity
`info` with no precedent), because every real `info` alert was notified and
never investigated. The dataset is not released: it is real operational data
from a private network. It was scanned for credentials, email addresses,
public IP addresses and URL-embedded secrets before these weights were
published, and found clean. One ingress hostname under the operator's own
domain was replaced with `<label>.homelab.example` across the 5 rows that
carried it before training, so the weights never saw it.

## Provenance and license

Built and documented in the cfoperator repository (MIT). The model card,
benchmark records, Modelfile and retrain runbook live there:

- [docs/triage-fine-tune.md](https://github.com/aachtenberg/cfoperator/blob/main/docs/triage-fine-tune.md), the long-form model card
- [docs/triage-retrain-runbook.md](https://github.com/aachtenberg/cfoperator/blob/main/docs/triage-retrain-runbook.md), how to produce the next version
- [benchmarks/](https://github.com/aachtenberg/cfoperator/tree/main/benchmarks), the eval harness and every gated result

The weights are released under Apache 2.0, the same license as the base model.
Base model by [Mistral AI](https://mistral.ai); 4-bit base and training
tooling by [Unsloth](https://unsloth.ai).
