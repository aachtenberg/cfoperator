# The triage fine-tune — `cfop-triage-ministral3`

**In one paragraph:** alert triage used to run on `gemma4:26b` and took ~5.5s per
alert. On 2026-08-20 we fine-tuned Mistral's `Ministral-3-14B-Instruct-2512` on
451 real homelab investigations, using QLoRA under [unsloth](https://unsloth.ai)
on a Windows RTX 5080, and got a model that matches gemma4's perfect eval score
at **~0.71s on the same suite — roughly 8x faster** — while being small enough to sit in VRAM
*next to* gemma4 so nothing swaps. It has served production triage since that
day as `cfop-triage-ministral3:v1-q4`. This page is the model card: what it is,
how it was made, how to rebuild it, and how to turn it off.

**If you only need one thing:** the deployable artifact is
`/mnt/nas-backup/cfoperator-finetune/mistral-trained-01/ministral-3-14b-instruct-2512.Q4_K_M.gguf`
on the NAS, and [Rebuilding from the NAS](#rebuilding-from-the-nas) turns it back
into a running model in two commands.

---

## What it is and where it runs

| | |
|---|---|
| **Deployed tag** | `cfop-triage-ministral3:v1-q4` (Q4_K_M, ~8.2GB) |
| **Archival tag** | `cfop-triage-ministral3:v1` (Q8_0, ~14.4GB) |
| **Serves** | `POST /v1/triage` only — one JSON verdict per alert |
| **Host** | ollama on `ubuntu-llm-01` / k3s node `headless-gpu` (192.168.0.150) |
| **Activated by** | `llm.triage_model` in cfoperator config (CFOP-57/58) |
| **Base model** | `unsloth/Ministral-3-14B-Instruct-2512-unsloth-bnb-4bit` |
| **Trained** | 2026-08-20, RTX 5080 16GB, unsloth studio, ~17 min |

It classifies an alert into `log_only` / `notify` / `investigate` / `escalate`.
That is the *entire* job.

**It is not a general assistant.** It cannot call tools — ollama cannot parse
Ministral's native `[TOOL_CALLS]name[ARGS]{json}` wire format
(ollama/ollama#16934, #17550), which is exactly why the fine-tune was scoped to
triage in the first place. Investigations stay on `llm.primary` (gemma4:26b).
If you ever see cfassist's status bar reading
`ollama:cfop-triage-ministral3:v1-q4`, that is a misconfiguration — switch it
back with `/model`.

**It does not go through llm-gateway.** Standing decision: triage calls
`llm.primary.url` directly.

---

## Artifact inventory — where every piece actually lives

This is the section to read when something is lost.

| Artifact | Location | Notes |
|---|---|---|
| Q4_K_M GGUF (deployed) | NAS `/mnt/nas-backup/cfoperator-finetune/mistral-trained-01/ministral-3-14b-instruct-2512.Q4_K_M.gguf` | 8,239,067,360 bytes |
| Q8_0 GGUF (archival) | same directory, `...Q8_0.gguf` | 14,359,310,560 bytes |
| BF16 mmproj | same directory, `...BF16-mmproj.gguf` | Vision projector. **Unused** — triage is text-only |
| Export metadata | same directory, `export_metadata.json` | Records the base model id |
| Training data | NAS `/mnt/nas-backup/cfoperator-finetune/{triage_train,triage_val,triage_conflicts}` | Also in the gitignored `benchmarks/datasets/` |
| ollama blobs | `ubuntu-llm-01:~/.ollama/models/blobs/` | Q4 layer `sha256-49ef257d…`, Q8 layer `sha256-3a249212…` |
| Modelfile | `benchmarks/Modelfile.cfop-triage` (this repo) | Reconstructed from `ollama show --modelfile` |
| Eval results | `benchmarks/triage_eval_cfop_triage_ministral3_v1*.json` | Committed |
| LoRA adapter / checkpoints | **Gone.** Existed only on the Windows box under `C:\Users\<user>\.unsloth\studio\outputs\` | Final was `checkpoint-114` |

**Known single points of failure:**

1. The **LoRA adapter was never archived** — only the merged GGUFs survive. A v2
   cannot resume from v1's adapter; it must retrain from base. Archive the
   adapter next time.
2. Nothing in `homelab-infra` provisions this model. A rebuilt `ubuntu-llm-01`
   comes back **without** it, and triage silently falls back to the standard
   chain at ~8x the latency. There is no alert for this. See
   [Rebuilding from the NAS](#rebuilding-from-the-nas), and consider an ansible
   task for it.
3. The NAS copy is the only backup. It is not in git (the GGUFs are 8–14GB and
   this repo is public).

---

## The training recipe

### Data

Built by [`scripts/build_triage_dataset.py`](../scripts/build_triage_dataset.py)
from real investigation history. That script's docstring is the authority on
label derivation, leakage controls, and the known fidelity gaps — read it before
building a v2 set. The short version: each example replays a historical alert
into the *exact* production triage prompt (extracted from `agent/agent.py` at
runtime, never paraphrased), and the label is the retrospectively cheapest
correct action given how the investigation actually ended.

The dataset itself is gitignored — it is real homelab data and this repo is
public — so its shape is recorded here instead. As built on 2026-08-20:

**Train: 451 examples. Val: 50 examples.**

| Label | Train | Val |
|---|---|---|
| `investigate` | 338 | 46 |
| `notify` | 96 | 4 |
| `escalate` | 16 | 0 |
| `log_only` | 1 | 0 |

| `meta.label_basis` (the rule that fired) | Train | Val |
|---|---|---|
| `outcome-monitoring` | 182 | 9 |
| `novel-but-resolved` | 136 | 6 |
| `resolved-precedent` | 96 | 4 |
| `outcome-needs-action` | 20 | 31 |
| `outcome-escalate` | 16 | 0 |
| `noise-pattern` | 1 | 0 |

`meta.severity_source`: 405 `unknown` / 46 `derived` in train, all 50 `unknown`
in val. This is the documented fidelity gap — the DB never stored the original
alert's severity, so it is re-derived from trigger text only where a
conservative pattern allows.

`triage_conflicts.json` flagged exactly one row (investigation 2213, a
river-history SSL-timeout ingest failure whose outcome was `needs_action`).

**Caveats a v2 should address.** The class balance is steep — `log_only` has a
single example and `escalate` only 16, so the model's competence on those two
labels rests almost entirely on the base model and the prompt rubric, not on
learned examples. The eval suite covers them, but the training set barely does.
The val split is also not distributionally matched to train (`outcome-needs-action`
is 62% of val but 4% of train), so val loss is a sanity check against
divergence, not a calibrated estimate of production accuracy. `triage_eval.py`
is the real gate.

### Hyperparameters

Run through unsloth studio's UI on the user's Windows machine. Values below are
taken from the studio run log of the successful run (2026-08-20T17:26:49Z),
not from memory.

```
base model            unsloth/Ministral-3-14B-Instruct-2512-unsloth-bnb-4bit
method                QLoRA (4-bit base, LoRA adapters)
lora_r                16
lora_alpha            16
target modules        requested: q_proj, k_proj, v_proj, o_proj,
                                 gate_proj, up_proj, down_proj
                      EFFECTIVE: q_proj, k_proj, v_proj, o_proj  (see note)
trainable params      19.7M
max_seq_length        1024
epochs                2
total optimizer steps 114
effective batch size  8
learning_rate         1e-4
lr warmup_steps       10
optimizer             AdamW 8-bit
gradient checkpointing True
sequence packing      disabled
padding-free          auto-enabled by unsloth
loss masking          train-on-responses-only (chat-template auto-detection)
eval_steps            0.1 of total steps (~every 11 steps), 50-row val set
checkpoints           every 30 steps; final = checkpoint-114
seed                  3407 (unsloth default; runs were bit-reproducible)
```

**The target-modules note matters.** The studio Configure tab listed all seven
projections, but unsloth logged:

> `Explicit target_modules are constrained by the finetune_(vision|language|attention|mlp) filters; adapters attach only where both select.`

The attention filter was active, so adapters attached to **q/k/v/o only** — which
is what "attention-only r16" means everywhere else in the docs, and is confirmed
by the 19.7M trainable-parameter count. This was deliberate: narrowing to
attention-only was the fix for a VRAM-driven speed stall (below). If you want
all seven modules in a v2, you must also clear the attention filter.

**Two values could not be recovered:**

- `lora_dropout` — 0.05 was recommended over the 0.00 default as insurance
  against memorizing a 451-example set, but the studio log does not record what
  was actually applied. Set it explicitly next time.
- The `per_device_train_batch_size` × `gradient_accumulation_steps` split. It was
  changed mid-session from 2×4 to 1×8 to cut peak VRAM; both give an effective
  batch of 8, and the final run's logged 114 steps is consistent with either.
  Only the effective batch is certain.

**For v2: save the studio YAML.** It was recommended during the v1 run and not
done, which is the direct cause of both gaps above.

### The loss curve

Train loss started at 0.995 (step 1) and 1.029 (step 2), then fell fast — the
JSON output format is learned within the first dozen steps. By step ~32 train
loss was ~0.028 and eval loss ~0.02, and **eval loss stayed flat through epoch 2
with no upward divergence**, which is the signal that the model learned the
notify/investigate boundary rather than memorizing 451 triggers. Because eval
loss never climbed, the final checkpoint (114) was the right pick over the
epoch-1 checkpoint.

Throughput on the healthy run: **~7.5s/step, 220W, 62°C, ~17 minutes total.**

### The Windows/5080 gotchas — a 30x speed story

Getting the run to go from "8.5 hour ETA" to 17 minutes took four fixes. Every
one cost a debugging round. Record them; they will recur on any Windows
consumer-GPU training run.

1. **`Unsloth: No or negligible GPU memory available for fused cross entropy`**
   at run start (crashes immediately, twice). Fix: set
   `UNSLOTH_CE_LOSS_TARGET_GB=1` and `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
   **in the shell that launches studio**. `setx` alone does not reach an
   already-running app.
2. **179s/step at 79W with VRAM pegged at 15.4/15.9GB and "99% utilization".**
   Power that low with utilization that high means the GPU is waiting on PCIe,
   not computing. The cause was **WDDM silently paging VRAM to system RAM**. Fix:
   NVIDIA Control Panel → **"Prefer No Sysmem Fallback"**. It binds at process
   start, so the app must be restarted. This single change was most of the 30x.
3. **Adapter too big for 16GB.** Narrowing target modules to attention-only cut
   trainable params ~60% (to 19.7M) and took a further ~3x off step time
   (240s → 77s) before fix 2 finished the job.
4. **Studio's worker leaks the previous model's VRAM across a Stop.** A full app
   restart is required between runs. 14B on 16GB is borderline — close desktop
   apps.

Also: unsloth studio's API is token-authed and was reachable over the LAN
(`http://192.168.0.115:8888`, token in `/tmp/unsloth.token`), which is how the
exports were driven remotely. `/api/train/progress` is a long-poll that never
returned data remotely; `/api/train/runs` gives status only. The studio host is
a workstation, not always on — treat LAN access as opportunistic.

**Why Windows and not the GPU node?** unsloth has no ROCm support, and
`ubuntu-llm-01`'s 7900 XTX serves production triage. The 5080 was the only
CUDA card available.

---

## Export and import

Export was driven over the studio LAN API, once per quantization (the base is
cached after the first, so no re-download):

```bash
TOKEN=$(head -1 /tmp/unsloth.token)
curl -s --max-time 30 -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"save_directory": "C:\\Users\\<user>\\.unsloth\\studio\\outputs\\cfop-triage-gguf",
       "quantization_method": "Q4_K_M"}' \
  "http://192.168.0.115:8888/api/export/export/gguf"
```

Export is **merged**, not adapter-only — the LoRA is folded into the base
weights. It de-quantizes the 14B to 16-bit on the fly, so it is the heaviest
step of the pipeline on the training box (several minutes of heavy CPU/RAM).
`Q8_0` was exported first as the archival reference; `Q4_K_M` followed as the
deployment candidate and had to clear the same eval gate before it could ship.

The resulting GGUF is copied to the NAS and imported on `ubuntu-llm-01` with
[`benchmarks/Modelfile.cfop-triage`](../benchmarks/Modelfile.cfop-triage):

```bash
ollama create cfop-triage-ministral3:v1-q4 -f benchmarks/Modelfile.cfop-triage
```

The Modelfile reuses the **base model's exact chat template** — the fine-tune
was trained against it, and a paraphrased template would not transfer. It also
sets `PARSER ministral` and `PARAMETER temperature 0.15`.

---

## Evaluation

All numbers below come from the committed raw JSON in `benchmarks/`, produced by
`benchmarks/triage_eval.py` against ollama 0.32.13. The eval suite is a valid
held-out test: `build_triage_dataset.py` excludes anything resembling a
`triage_eval.py` case by construction.

| Model | 14-case ×3 | hard cases ×12 | soak ×50 | JSON valid | Latency, 14-case | Latency, soak |
|---|---|---|---|---|---|---|
| `gemma4:26b` (incumbent) | 42/42 | 12/12 + 12/12 | — | 100% | 5.53s | — |
| `ministral-3:14b` (base) | 37/42 (88.1%) | 4/12 + 4/12 | — | 100% | 0.93s | — |
| `cfop-triage-ministral3:v1` (Q8_0) | 42/42 | 24/24 | 100/100 | 100% | 1.06s | 0.80s |
| **`:v1-q4` (Q4_K_M, deployed)** | **42/42** | **24/24** | **100/100** | **100%** | **0.71s** | **0.62s** |

Two latency columns because the suites differ in prompt size and the numbers are
not interchangeable. The like-for-like comparison against the incumbent is the
14-case column: **5.53s → 0.71s, ~7.8x.** The soak column is the steady-state
figure on the two hard cases and is what production most resembles.

The base model's two failure classes were both the **notify shortcut**, taken
~2/3 of the time on each trap: reading precedent *presence* as precedent
*outcome* (`precedent-monitoring`), and reading severity without breadth
(`critical-narrow`). Both were eliminated by the fine-tune.

**Why the ×50 soak exists.** A 12-run check only catches an 8%-rate intermittent
shortcut about 63% of the time. At ×50, such a fault escapes with probability
~1.5%. Both hard cases passed 50/50 on both quantizations.

**Q4 vs Q8.** Q4_K_M lost nothing measurable and is faster (0.62s vs 0.80s mean;
its max latency is also far tighter on the soak, 0.72s vs 3.77s). At ~8GB it **co-resides in
24GiB VRAM with gemma4:26b**, so triage and investigation never swap models —
that co-residency, not the raw latency, is the main reason Q4 is the deployment
artifact. Q8_0 is kept purely as the archival reference.

Re-running the gate:

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v1-q4 --runs 3 \
  --output benchmarks/triage_eval_<name>.json

PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v1-q4 --runs 50 \
  --only precedent-monitoring,critical-narrow \
  --output benchmarks/triage_eval_<name>_soak_x50.json
```

---

## Deployment, rollback, and the restart gotcha

The wiring landed in **PR #144 / `5a4d88f`** (CFOP-57). Activation is one line in
the cfoperator config (`cfoperator-deploy` commit `1f517e7`):

```yaml
llm:
  triage_model: cfop-triage-ministral3:v1-q4
```

Semantics:

- When set, triage tries this model first and **falls back to the normal chain on
  any failure or unparseable response**. Unparseable output is not an outage.
- When unset, triage uses the primary chain unchanged. Investigations are never
  affected either way.
- The console's Admin → LLM tab overrides it live (DB beats config; `off` there
  disables it despite the config key).
- **Rollback is deleting the line.** The key is inert on any image without the
  wiring, so config and image can be deployed in either order.

**The gotcha that will bite you:** a config-only deploy commit syncs the
ConfigMap and **restarts nothing**. You need:

```bash
kubectl rollout restart deploy/cfoperator
```

Image-tag bumps restart on their own because they touch the pod spec; config-only
changes do not.

Verifying it is live — the response names the model that served it:

```json
{"action":"log_only","backend":"ollama","confidence":0.9,
 "model":"cfop-triage-ministral3:v1-q4","reason":"known noise pattern (test pod)"}
```

---

## Rebuilding from the NAS

If `ubuntu-llm-01` is rebuilt, or the ollama blobs are lost, this is the whole
recovery. No retraining required.

```bash
# On ubuntu-llm-01, with /mnt/nas-backup mounted:
ollama create cfop-triage-ministral3:v1-q4 \
  -f /path/to/cfoperator/benchmarks/Modelfile.cfop-triage

# Confirm it answers, then re-run the gate:
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v1-q4 --runs 3 --output /tmp/verify.json
```

The Modelfile's `FROM` points at the NAS path, so the only prerequisite is the
NAS mount. To restore the archival Q8_0 instead, change `FROM` to the `Q8_0.gguf`
file and tag it `:v1`.

---

## Open follow-ups

Carried over from the v1 session; none are blocking:

- **`llm.triage_timeout` knob** — currently no dedicated timeout for the triage
  call.
- **Helm support for `llm.triage_model`** — the key works in raw config; the
  chart does not expose it.
- **Ansible provisioning** of the ollama tag on `ubuntu-llm-01`, so a node
  rebuild does not silently drop triage back to the slow chain.
- **Archive the LoRA adapter** for any future run — v1's is gone.
- **v2 retrain** once `triage_conflicts.json` accumulates enough rows to be worth
  it, ideally with a less lopsided class balance for `escalate` and `log_only`.
- **Tool calling stays out of scope** until ollama ships a working ministral
  tool-call parser (ollama/ollama#16934, #17550). No historical tool transcripts
  exist to train on anyway — `investigation_events` were never written.

## See also

- [`benchmarks/ministral-3-14b-baseline.md`](../benchmarks/ministral-3-14b-baseline.md) — the before/after benchmark write-up
- [`scripts/build_triage_dataset.py`](../scripts/build_triage_dataset.py) — dataset builder, and the authority on label derivation
- [`docs/config-reference.md`](config-reference.md) — the `llm.triage_model` key
- [`docs/noise-reduction.md`](noise-reduction.md) — why triage exists at all
