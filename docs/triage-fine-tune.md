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
| **Training run** (adapter, checkpoints, args) | NAS `/mnt/nas-backup/cfoperator-finetune/training-run-1787248524/` | 668MB. Archived 2026-09-02 off the training box |

### The training run archive

`training-run-1787248524/` is the complete unsloth output directory for the
successful run, recovered from the training box and copied to the NAS on
2026-09-02 (adapter md5 `48a4850efe5bae67914e08421cce12ab`, verified against
source). It contains:

| File | What it gives you |
|---|---|
| `adapter_model.safetensors` (78.7MB) | The LoRA itself. A v2 can resume from this instead of retraining from base |
| `adapter_config.json` | Authoritative r / alpha / dropout / target-module regex |
| `checkpoint-{30,60,90,114}/` | Per-checkpoint adapter + `trainer_state.json` + `training_args.bin` |
| `checkpoint-114/trainer_state.json` | The full loss curve reproduced below |
| `training_args.bin` | Every `TrainingArguments` value (a torch-pickled zip) |
| `chat_template.jinja`, `tokenizer.json`, `tokenizer_config.json` | The exact template and tokenizer trained against |

The twelve sibling directories on the training box named
`..._project-cfoperator-training_*` are the **failed attempts** and are all
empty; only `_1787248524` has weights. Nothing else needed archiving.

**Known single points of failure:**

1. Nothing in `homelab-infra` provisions this model. A rebuilt `ubuntu-llm-01`
   comes back **without** it, and triage silently falls back to the standard
   chain at ~8x the latency. There is no alert for this. See
   [Rebuilding from the NAS](#rebuilding-from-the-nas), and consider an ansible
   task for it.
2. The NAS is a single USB disk on `headless-gpu`. It **is** replicated to iDrive
   e2 — the `nas-cloud-backup` timer rsyncs all of `/mnt/nas-backup` daily with
   only three media excludes, and the GGUFs and dataset are confirmed present in
   the bucket. But `rclone sync` is a **mirror, not a versioned backup**: a delete
   or corruption on the NAS propagates to the cloud within 24h. Keeping the
   original run directory on the training box is currently the only third copy —
   don't clean it up. Bucket versioning or `--backup-dir` would fix this properly.
3. Neither the GGUFs nor the adapter are in git (too large, and this repo is
   public).

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
base model                     unsloth/Ministral-3-14B-Instruct-2512-unsloth-bnb-4bit
method                         QLoRA (4-bit base, LoRA adapters)  [unsloth_training_method: qlora]
peft_version                   0.18.1

r                              16
lora_alpha                     16
lora_dropout                   0.0
bias                           none
use_rslora                     false
use_dora                       false
target modules                 q_proj, k_proj, v_proj, o_proj  (attention-only; see note)
trainable params               19.7M  (adapter_model.safetensors is 78.7MB)

max_seq_length                 1024
num_train_epochs               2
max_steps                      -1  (epoch-driven; resolved to 114 optimizer steps)
per_device_train_batch_size    1
gradient_accumulation_steps    8      -> effective batch 8
learning_rate                  1e-4
lr_scheduler_type              linear
warmup_steps                   10
weight_decay                   0.001
max_grad_norm                  1.0
optim                          adamw_8bit
adam_beta1 / beta2 / epsilon   0.9 / 0.999 / 1e-8
bf16                           true   (fp16 false)
gradient_checkpointing         true
packing                        false
padding-free                   auto-enabled by unsloth
loss masking                   train-on-responses-only (chat-template auto-detection)
eval_strategy / eval_steps     steps / 0.1 of total (every 12 steps), 50-row val set
save_steps                     30   -> checkpoint-30/60/90/114; final = 114
logging_steps                  1
seed / data_seed               3407 / 3407  (runs were bit-reproducible)
```

Every value above is read back from the archived run itself —
`adapter_config.json` and the `training_args.bin` in `checkpoint-114` — not from
notes. See [Artifact inventory](#artifact-inventory--where-every-piece-actually-lives)
for where that lives.

**The target-modules note matters.** The studio Configure tab listed all seven
projections, but unsloth logged:

> `Explicit target_modules are constrained by the finetune_(vision|language|attention|mlp) filters; adapters attach only where both select.`

The attention filter was active, so adapters attached to **q/k/v/o only**. The
archived `adapter_config.json` settles it — the persisted `target_modules` is a
compiled regex whose only alternation is `q_proj|k_proj|v_proj|o_proj`, with no
MLP projection reachable. This was deliberate: narrowing to attention-only was
the fix for a VRAM-driven speed stall (below). If you want all seven modules in
a v2, you must also clear the attention filter — setting the seven in the UI is
not sufficient, as this run proves.

**`lora_dropout` was 0.0, not 0.05.** 0.05 was recommended during the run as
insurance against memorizing a 451-example set; the archived adapter config shows
it was never applied. The eval curve says it did not matter here (loss fell
monotonically to epoch 2 with no divergence), but a v2 on a similarly small set
should still set it deliberately rather than inherit the default.

**For v2: save the studio YAML anyway.** Everything above was recoverable only
because the output directory survived on the training box for twelve days. That
was luck, not process.

### The loss curve

Train loss started at 0.995 (step 1) and 1.029 (step 2), then fell fast — the
JSON output format is learned within the first dozen steps. The full eval series,
from `checkpoint-114/trainer_state.json`:

| Step | Epoch | Eval loss |
|---:|---:|---:|
| 12 | 0.21 | 0.1986 |
| 24 | 0.43 | 0.0351 |
| 36 | 0.64 | 0.0082 |
| 48 | 0.85 | 0.0110 |
| 60 | 1.05 | 0.0051 |
| 72 | 1.27 | 0.0048 |
| 84 | 1.48 | 0.0044 |
| 96 | 1.69 | 0.0044 |
| 108 | 1.90 | 0.0035 |
| **114** | **2.00** | **0.0034** |

**Eval loss fell monotonically into epoch 2** (the one uptick, step 36 → 48, is
noise at the third decimal) and never diverged upward, which is the signal that
the model learned the notify/investigate boundary rather than memorizing 451
triggers. Because it never climbed, the final checkpoint (114) was the right pick
over the epoch-1 checkpoint. Totals: 497,274 input tokens seen, 3.97e16 FLOPs.

A caution on reading those numbers: loss is masked to the assistant turn only,
and the assistant turn is one short JSON verdict. Absolute values in the
thousandths reflect how constrained the output space is, not a 99.7%-accurate
classifier. `triage_eval.py` remains the only gate that means anything.

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

   **These are a LAUNCH REQUIREMENT, not a one-time fix, and a reboot silently
   removes them (CFOP-153, 2026-09-02).** Studio started from a shortcut,
   service or auto-start after a reboot has neither variable, and the symptom
   is *not* the CE-loss crash filed above — it is a **10x slowdown that looks
   like a hardware limit**: 70–90 s/step, 99% GPU utilisation at 71–93 W of a
   360 W board, VRAM pegged near total, with runs dying at random steps. With
   both variables set the same config ran at **6.7 s/step at 279 W** — faster
   than v1. Power draw is the tell: high utilisation at low watts is PCIe
   waiting, not compute.

   Counterintuitively a *clean reboot makes this worse*, because rebooting is
   what unsets them. Several hours were spent on VRAM arithmetic, sequence
   length and `gradient_checkpointing` before this was the answer.

   `gradient_checkpointing` was **not** implicated — it was `'unsloth'` on
   every run. Do not re-check it.
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
(**`http://192.168.0.110:8888`** as of 2026-09-02 — this document previously
said `.115`; verify before trusting it, the box is DHCP. Token in
`/tmp/unsloth.token` **on the cfoperator host**, not the training box), which is how the
exports were driven remotely. `/api/train/progress` is a long-poll that never
returned data remotely; `/api/train/runs` gives status only. The studio host is
a workstation, not always on — treat LAN access as opportunistic.

**Why Windows and not the GPU node?** unsloth has no ROCm support, and
`ubuntu-llm-01`'s 7900 XTX serves production triage. The 5080 was the only
CUDA card available.

---

## Export and import

Export is driven over the studio API, once per quantization. The full,
current sequence — checkpoint lookup, `load-checkpoint`, `export/gguf`,
status/logs — lives in the runbook's
[§6 Export](triage-retrain-runbook.md#6-export); this section only records
what the export *is*, since that was misdescribed here for a while.

Export is **merged**, not adapter-only — the LoRA is folded into the base
weights. To do that it needs the **fp16 base, which is not the bnb-4bit model
training used**: the first export on a box downloads it (16.6 GB for the 8B,
26 GB for the 14B) into the HF cache, and that download, not the merge, is
most of the wall-clock. Later exports on the same box skip it. Tokens are the
per-card `UNSLOTH_<card>_TRAINING_TOKEN` entries in `repos/cfoperator/.env`;
the v1-era `/tmp/unsloth.token` and `192.168.0.115` are gone.

`Q8_0` is the archival reference and `Q4_K_M` the deployment candidate; both
clear the same gate before either ships. The export folder is copied by hand
to `/mnt/nas-backup/unsloth/cfoperator-v<N>/` — a local exFAT disk on the dev
box, not a NAS — beside the dataset and YAML that produced it, and imported
with that generation's Modelfile (`benchmarks/Modelfile.cfop-triage-<gen>`):

```bash
ollama create cfop-triage-ministral3:8b-v3-q4 -f benchmarks/Modelfile.cfop-triage-8b-v3
```

`ollama create` reads the whole file off the exFAT disk at ~30 MB/s and
hashes it, so budget 5–10 minutes and run it detached from anything with a
timeout.

The Modelfile reuses the **base model's exact chat template** — the fine-tune
was trained against it, and a paraphrased template would not transfer. It also
sets `PARSER ministral` and `PARAMETER temperature 0.15`.

**The 0.15 is mostly decorative.** Both the production triage path
(`_chat_with_tools`, ollama branch) and `benchmarks/triage_eval.py` send
`temperature: 0.7` explicitly in the request, which overrides the Modelfile
default. Every eval number in this document was measured at 0.7, and the
zero-variance result across runs was obtained at 0.7 — not at a low sampling
temperature. The 0.15 applies only to ad-hoc `ollama run`. Nothing currently
asserts that the eval and production literals agree; that is a Tier 0 item in
the [eval v2 plan](triage-eval-v2-plan.md).

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
| `gemma3:12b` (base screen, ×36) | 408/504 (81.0%) | `precedent-monitoring` 0/36, `critical-narrow` 36/36, `tmp-pod-critical` 0/36 | — | 100% | 1.2s | — |
| `llama3.1:8b` (base screen, ×36) | 315/504 (62.5%) | never investigates: 0/36 on all five investigate cases | — | 100% | 0.45s | — |
| `cfop-triage-ministral3:v1` (Q8_0) | 42/42 | 24/24 | 100/100 | 100% | 1.06s | 0.80s |
| **`:v1-q4` (Q4_K_M, deployed)** | **42/42** | **24/24** | **100/100** | **100%** | **0.71s** | **0.62s** |

**Base screens, 2026-09-03** (runbook §0.5; raw JSON in
`benchmarks/triage_eval_base_*.json`, 14 cases × 36 untuned). Screened after the
8B Ministral fine-tunes failed `tmp-pod-critical` at every data version, to see
whether a small non-Ministral base would do better. `gemma3:12b` is the only
viable substrate: clean on every investigate case and on the two traps the
Ministral base failed (`critical-narrow` 36/36, `correlated-outage` 36/36),
reasons grounded with 2 fabrications in 504 untuned, 1.2 s. It fails the two
traps the data cannot yet teach at that size — the precedent-presence shortcut
(`precedent-monitoring` 0/36, which fine-tuning fixed for Ministral from the
same rows) and severity beating the `tmp-` rule (`tmp-pod-critical` 0/36, which
the Ministral 14B base already held and the 8B fine-tunes never learned from two
`log_only` rows). A gemma3 run therefore needs synthetic noise-at-critical rows
first, the same move as the info rows. `llama3.1:8b` is out: it never chooses
investigate and invents unnamed precedents in 18 runs. Two other candidates
never trained: `gemma-3n-E4B` breaks QLoRA on its per-layer-embedding
projection (2048 → 8960, the `1×9175040` packed weight in the error), and
`Phi-4-mini` needs an older transformers than studio ships. Staying on the
Ministral 14B is the result.

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

## Analysis — what the data and the model actually are

The eval scores are real, but they are stronger than the training data on its own
would justify, and one thing regressed without the gate noticing. Read this
before building a v2.

### The targets are a four-way lookup, not reasoning

Across all 451 training rows there are exactly **four** distinct `reason` strings
and **four** `confidence` values, mapped 1:1 onto the action:

| action | n | `reason` | `confidence` |
|---|---:|---|---:|
| `investigate` | 338 | "no resolved precedent for this pattern" | 0.80 |
| `notify` | 96 | "similar past investigation resolved with little effort" | 0.85 |
| `escalate` | 16 | "critical with broad impact, operator should page in" | 0.90 |
| `log_only` | 1 | "known noise pattern (test pod or watchdog heartbeat)" | 0.95 |

So `reason` and `confidence` carry **zero information beyond `action`**, and the
model learned exactly that mapping. Two consequences:

**The `reason` field stopped explaining anything.** Both models, same eval alert
(`known-sdcard`), same production prompt:

> `gemma4:26b` — "The issue matches a known pattern of a failing SD card on this
> device for which replacement is already scheduled."
>
> `cfop-triage-ministral3:v1-q4` — "similar past investigation resolved with
> little effort."

Where that reason goes matters for how much this hurts, and the honest answer
is: one place. Traced against the consuming code (not the portable stubs):

| Surface | Reaches it? |
|---|---|
| event_runtime **activity timeline** — the `decision_made` note is `decision.reasoning` | **yes** |
| Slack — `notifications.py` renders only the `Triaged by: backend/model` attribution | no |
| The investigation record — production `HTTPInvestigateActionHandler` POSTs `alert.to_dict()` only and records `{agent_url, alert_id}`; the `"decision": reasoning` field lives in the portable `defaults.py` stub, which is not the deployed handler | no |
| `deep_context.triage_reasoning` — only on the host-shaped low-confidence reroute, which never fires (next paragraph) | effectively no |

So this is an **activity-feed / audit-log** regression. Slack never carried the
reason and the investigation record never stored it. That narrows CFOP-153 but
does not close it — the timeline is the place an operator looks to see *why* an
alert was routed the way it was. Either way `triage_eval.py` cannot see it — it
scores `action` and JSON validity only.

**`confidence` is a label alias, not a confidence.** Anything thresholding on it
is thresholding on the action. Audit result: two consumers.

The deep-investigation tier (`EscalationRoutingDecisionEngine`) reroutes an
`investigate` decision to forensics when `0 < confidence <
CFOP_DEEP_CONFIDENCE_THRESHOLD` (default 0.4) — **but only after
`_is_host_shaped()` passes**; non-host alerts return before the gate
(`test_non_host_alert_decisions_untouched` pins this: a pod `investigate` at
0.1 stays `investigate`). So the path is inert for two independent reasons:

1. Both models emit well above 0.4 — the fine-tune ≥0.80, gemma4 ≥0.9 on the
   committed 14-case JSON (`warning-correlated` is its floor).
2. Most triage traffic is pod/workload-shaped and would not reroute at any
   confidence.

That second reason matters for a v2: fixing calibration alone will not send a
CrashLoop pod to forensics. The 5-minute triage cache gates on `confidence >
0`, which both models always satisfy. Nothing is misrouted today; the
threshold just assumes a calibration neither model provides, on a subset of
alerts that mostly never reaches it.

### Two of the three input fields were noise or constant

| Field | In training | In production |
|---|---|---|
| `Alert severity` | `unknown` 405, `warning` 43, `critical` 3 | always a real value |
| `Labels` | empty in 53% of rows; ~19% of populated values are English stopwords | real Alertmanager labels |
| similar-past block | **exactly 3 entries in 100% of rows** | 0–3 |

The `Labels` corruption is a bug in `build_triage_dataset.py`: it takes the word
following "pod"/"namespace" in the summary prose, producing entries like
`{"pod": "with", "namespace": "has"}`. Node names extract correctly; pod and
namespace do not. Net effect: the model was effectively trained on the summary
line alone.

### The val split cannot support the claim usually made of it

`triage_val.jsonl` is 46/50 `investigate`, so a constant predictor scores 92% on
it, and its `label_basis` mix is nothing like train (62% `outcome-needs-action`
vs 4%). **Eval loss falling to 0.0034 mostly measures "emits well-formed JSON
with the majority label."** It is not evidence of learning the boundary.
`triage_eval.py` is the only thing that is.

### The model is nonetheless better than that data deserves

Two results carry the weight:

**The eval discriminates.** The 14 cases span all four actions — 3 strict
`log_only`, 2 `log_only|notify`, 2 `notify`, 5 `investigate`, 2 `escalate`. A
constant-`investigate` model scores **5/14 (36%)**. The fine-tune scores 14/14,
identical across all three runs, zero variance.

**It generalizes off-distribution.** Ten of the fourteen eval cases have **zero**
similar-past entries and all carry real severity — shapes occurring in 0% and 10%
of training respectively. It still gets them right. The sharpest case is
`critical-narrow` (similar=0, severity=critical, a shape absent from training),
where the base model failed 8/12 and the fine-tune passes 50/50. That is transfer,
not memorization: 451 narrow examples moved behavior on inputs never seen.

Note also that `log_only` competence is **inherited, not trained** — with one
example, the model returns confidence 0.9 on `log_only` cases rather than the 0.95
its single row teaches. The base model and the system-prompt rubric are doing that
work.

### The gap: the soak is pointed at the wrong cases

All 100 soak runs went to `precedent-monitoring` and `critical-narrow`, and
**both expect `investigate`** — the 75% majority class. The likeliest failure mode
given this training set is an over-`investigate` bias, and it is invisible there.
The only guard is the 14-case screen at ×3, where a 10%-rate regression on a
`log_only` case escapes ~73% of the time (0.9³).

Run this before trusting any retrain:

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v1-q4 --runs 50 \
  --only watchdog,smoke-test-pod,tmp-pod-critical,known-sdcard,precedent-resolved-oom
```

### What a v2 should change

1. ~~Generate varied, alert-grounded `reason` text instead of four templates~~ —
   **done (CFOP-153).** See [v2 dataset](#v2-dataset-cfop-153) below.
2. ~~Fix the `Labels` stopword-extraction bug.~~ — **done (CFOP-153).**
3. Vary the similar-past block 0–3 and carry real severity, so training shape
   matches serving shape.
4. Oversample `log_only` and `escalate`, or accept that those two labels are the
   base model's behavior and say so.
5. Build a val split that is distributionally comparable to train, or stop
   quoting val loss as evidence.


### v2, v3 and v4 datasets (CFOP-153)

Both rebuilt from the full 1,882-investigation history (`--limit 5000`; the
default of 1000 truncates silently and the builder now warns when it does).
**v2 was trained and rejected** — it fabricated precedents, see the retrain
post-mortem in `docs/triage-retrain-runbook.md` and PR #240. **v3 was trained
twice (14B and 8B) and rejected** — action-perfect on the 14B, but both
fabricate on prompts without a precedent block, a shape v3 never contained;
see *v3 results* below. **v4 was trained twice as well**: the block-less repair
worked on every case it targeted, and the one fabrication left is a shape the
history cannot supply — see *v4 results*.

| | v1 (2026-08-20) | v2 (2026-09-02) | v3 (2026-09-03) | v4 (2026-09-03) | v5 (2026-09-03) |
|---|---:|---:|---:|---:|---:|
| train rows | 451 | 522 | **262** | 310 | 324 |
| distinct `reason` strings | **4** | 383 | 246 | 275 | 289 |
| distinct `confidence` values | **4** | 39 | 38 | 39 | 39 |
| rows using a v1 canned reason | 451 | 0 | 0 | 0 | 0 |
| reasons falling back to the generic "this alert" | n/a | 0 | 0 | 0 | 0 |
| rows citing an object absent from their prompt | 0 | **176** | **0** | 0 | 0 |
| rows with no precedent block (train / val) | 0 | 0 | **0 / 0** | **48 / 5** | 62 / 5 |
| synthetic rows (train / val) | 0 | 0 | 0 | 0 | **14 / 0** |
| `escalate` rows (train / val) | 16 / — | 16 / 2 | 16 / 2 | 32 / 4 | 32 / 4 |
| investigate : escalate | 21:1 | 25:1 | **9.8:1** | 5.8:1 | 5.8:1 |

Fingerprint for checking you have the right file before training: **262 train
rows, 246 distinct reasons, 38 distinct confidences, 0 fabricated citations,
escalate 16/2.** Train is `sha256 f5533195a23c53cb…`, val `60aef4afdacbbbb3…`. v4: **310
train rows, 275 distinct reasons, 39 distinct confidences, 0 fabricated
citations, escalate 32/4, 48 train rows without a block.** Train is
`sha256 5f6968f27e6b5e3a…`, val `ec7441d1f08596eb…`; staged at
`/mnt/nas-backup/unsloth/cfoperator-v5/` (folder numbering runs one ahead of
the data generation throughout). v5: **324 train rows, 289 distinct reasons,
39 distinct confidences, 0 fabricated, 62 train rows without a block, 14
synthetic**; train `sha256 5e44b0ae1746dfa7…`, val unchanged from v4
(`ec7441d1f08596eb…`); staged at `/mnt/nas-backup/unsloth/cfoperator-v6/`.

Three things changed between v2 and v3, all in the builder:

- **The investigate frames no longer put the alert's own subject after a
  similarity cue word.** "closest earlier match to {subject}" taught the model
  `closest → <pod>`, and it reproduced that adjacency on alerts with no
  precedent by inventing one. Subject leads now; only a cosine may follow a
  cue. That reverses the cosine removal below — the objection still holds for
  the no-precedent frame, which keeps neither cue word nor number.
- **Near-duplicate frames are capped at 8 rows** (`--max-per-frame`, on by
  default). 148 investigate rows were one sentence with the pod name swapped.
  Capping per frame rather than per action is what leaves `escalate` and
  `log_only` untouched: thin classes have no big frames. Lower than 8 starts
  deleting `escalate` and the ratio gets worse, not better.
- **Two label bugs**: `api.x.ai` was read as `node=api` ("api" contains "pi"),
  and "in kube-system namespace" as `namespace=experiencing`. Both corrupted the
  trained input.

The row count is well below v1's 451. Keep 3 epochs rather than compensating:
v2's correct escalates used base-model phrasing absent from the training data,
so the fine-tune's demonstrated value is latency and JSON validity, not
classification — less deviation from the base is the point.

383 rather than the 467 an earlier build produced, and the drop is deliberate:
the similarity float was removed from the investigate near-miss frame, and it
had been supplying uniqueness to 352 rows. A cosine in every sentence teaches
"a good reason contains a float" — citable-looking, trivially faked. Uniqueness
now comes from the subject, which is the part that is actually grounded.

A "100% of reasons contain a token from the alert" figure appeared in an
earlier draft of this table and is deliberately not repeated. Review showed it
was close to vacuous: 106 rows (20.3%) said only "this alert" and satisfied the
token-overlap test via the similarity *number* shared between prompt and
reason. The generic-subject count is the number that actually moved, so that is
the one recorded.

Confidence now varies *within* an action instead of renaming it —
`investigate` spans 0.45–0.60 (a near-miss precedent that did not resolve is
less certain than a genuinely novel alert), `notify` spans 0.70–0.93 with the
cited precedent's similarity. `escalate` and `log_only` remain single-valued;
neither has enough examples for a spread to mean anything.

Label mix is essentially unchanged, which is the point — this was a
reason/confidence change, not a relabelling: `investigate` 338→402,
`notify` 96→103, `escalate` 16→16, `log_only` 1→1.

**The `Labels` trade is worth stating.** Empty-label rows went *up*, 53%→69%,
because the shape test now rejects what it used to invent. v1 had ~19% of its
populated labels as English stopwords (`{"pod": "with"}`); v2 has none. Fewer
labels, none of them lies. A bare single-word name like `prometheus` is dropped
too — deliberately conservative, same posture as `derive_severity`.

**Still open, and now more visible:** `log_only` has two examples (one alert,
with and without its block) and `escalate` thirty-two (sixteen alerts, twice).
Items 3–5 above are untouched. Real new `escalate` and `log_only` alerts are
still the largest remaining data defect; the twins change which shapes the
model has seen, not how many distinct situations.

#### v3 results (2026-09-03): action-perfect, fabricates on the shape it never saw

Two v3 models were trained on the same data and YAML — 14B on the RTX 5060
box, 8B on the 5080 — and gated at 14 cases × 36 runs
(`benchmarks/triage_eval_cfop_triage_ministral3_v3_q4.json` and
`…_8b_v3_q4.json`):

| | v2 14B | v3 8B | v3 14B |
|---|---:|---:|---:|
| action | 496/504 | 468/504 | **504/504** |
| JSON valid | 504/504 | 504/504 | 504/504 |
| latency, mean | 1.05 s | 0.76 s | 1.08 s |
| `correlated-outage` | 28/36 | 36/36 | 36/36 |
| `tmp-pod-critical` | 36/36 | **0/36** | 36/36 |
| fabricated cites, named | 36/504 | 0/504 | 27/504 |
| fabricated cites incl. unnamed precedent | — | 179/504 | 140/504 |
| final training loss | 0.0134 | 0.0415 | 0.0302 |

The 14B v3 is the best model so far on action: the v2 regression is gone (the
rebalance was the cause) and it holds the noise trap the 8B fails. Neither
ships, because both fabricate — and every fabricating run is on a prompt with
**no "Similar past investigations" block**: `novel-oom`, `critical-narrow`,
`warning-correlated`, `info-novel-cert`, `novel-imagepull` (the 8B adds
`tmp-pod-critical`). The 14B forces the near-miss frame and fills the cosine
slot with an invented sibling pod:

```
paperless-ngx-7d9c4b8f5-nq2wm: the closest earlier investigation
(paperless-ngx-76f85f4c9-2x87x) ended monitoring — no resolved precedent to lean on
```

**0 of the 262 v3 rows lack that block.** The retrospective search has a whole
history to draw on, its floor is 0.5, and the weakest best-match in the history
is 0.55, so every row got a block — while production sends none when retrieval
returns nothing (empty history, embedding call failed) and the eval sends none
on 10 of 14 cases. Probe on the 14B, `novel-oom`, six runs each: no block →
invented pod 6/6; the same alert with a synthetic block at 0.61 → quotes 0.61,
6/6; at 0.78 → quotes 0.78, 6/6. The model is not wrong about precedents; it
has never been shown a prompt without them.

Why it fabricates rather than saying so: SFT teaches a form conditioned on the
input, and the only investigate form it learned opens a parenthesis that has to
be filled. With a number in the prompt it copies the number; with none, the
most probable pod-name-shaped string wins. It signals doubt in the one channel
it has — confidence 0.45, the floor of the range — but has no sentence for
"nothing listed", because no training row ever said it.

#### What v4 changes

**Context-free twins.** Each row whose label does not depend on the block also
yields a block-less copy of the same alert: escalate and log_only (their
reasons never cite the block) and investigate rows whose best match sat below
the 0.70 near-miss floor. Not notify (the label *is* the precedent) and not
near-miss investigate (the bulk of the class; twinning it would rebuild the
pile the frame cap removes and tilt the block-less shape to "investigate").
Twins are emitted after the cap, so a capped row takes its twin with it.
+53 rows: 48 train, 5 val. escalate doubles to 32 as a side effect — the same
16 alerts seen twice, which teaches the shape, not new escalate patterns.

**The no-precedent reason describes the prompt, not the history.** "nothing
similar listed — needs a first look", the rubric's own wording, in place of
v3's "has no precedent in the investigation history". It fired on zero v3
rows, so nothing already trained changed; it is what the twins carry, and a
test pins that the eval's unnamed-precedent rule does not flag it.

**The pre-flight counts block-less rows**, and the builder warns at zero.

#### v4 results (2026-09-03): the repair worked; one shape the history cannot teach

Same YAML, same boxes (14B on the 5060, 8B on the 5080), 117 steps each, final
loss 0.0369 (14B) and 0.0409 (8B). Gated at 14 cases × 36 runs on a quiet card
(`benchmarks/triage_eval_cfop_triage_ministral3_v4_q4.json` and
`…_8b_v4_q4.json`):

| | v3 14B | v4 8B | v4 14B |
|---|---:|---:|---:|
| action | 504/504 | 465/504 | **504/504** |
| JSON valid | 504/504 | 501/504 | 504/504 |
| latency, mean | 1.08 s | 0.76 s | 1.05 s |
| `novel-oom` fabricating runs | 36 | 0 | **0** |
| `critical-narrow` / `warning-correlated` / `novel-imagepull` fabricating | 36 / 27 / 5 | 1 / 0 / 1 | **0 / 0 / 0** |
| `tmp-pod-critical` | 36/36 | **0/36** | 36/36 |
| `info-novel-cert` fabricating runs | 36 | 36 | 36 |
| fabricated cites, total | 140/504 | 38/504 | 36/504 |

The block-less cases now answer with the twin frame, verbatim:

```
paperless-ngx-7d9c4b8f5-nq2wm: nothing similar listed — needs a first look
warning: three services (immich, paperless-ngx, nextcloud) — nothing similar listed
tmp-restore-verify-9x2kd: the prefix tmp- matches the log_only rule (known noise)
```

The 8B still loses `tmp-pod-critical` outright (severity=critical beats the
noise rule at that size), returned three empty responses, and is not a
candidate. The 14B is a candidate on everything except one case.

**The residual is `info-novel-cert`, 36/36 on both sizes, and it is a
different kind of gap.** The alert is severity=info with no precedent; the
rubric allows `notify` on the severity alone; and the only notify frame in the
data is "repeats an earlier investigation that resolved (…)", because the
builder's notify rule *is* a resolved precedent. So the model says notify, and
invents the precedent that frame needs:

```
grafana.ai repeats an earlier investigation that resolved (21 days ago): …
```

No real row can fix this. The investigation history holds **zero severity=info
alerts** — checked against all 1,886 rows, not only the training set — because
info alerts are notified or logged and never investigated, so they never enter
the table the builder reads. The twins could not cover it either: a twin keeps
its row's label, and no row is labelled notify without a precedent.
(`info-severity`, the info alert *with* a precedent, is 36/36 and clean.)

Options, in the order worth taking them:

1. **A small synthetic set for v5.** Severity=info alerts drawn from this
   homelab's real alert names (certificate renewal notices, backup-completed
   notices), no precedent block, labelled notify with a frame that cites the
   severity ("severity=info — informational, no action needed"), and marked
   `meta.synthetic` so they can be filtered. It would be the first
   non-historical data in the set and should be labelled as such.
2. **Keep severity=info away from the fine-tune in production.** The rubric
   already decides those by severity alone, so `run_triage` can short-circuit
   them before the model call. That is a triage behaviour change and its own
   issue, but it takes the model off the one shape it cannot be trained on.
3. Ship the 14B v4 as it is. Not on the table: a confident false citation on
   info alerts is the CFOP-153 defect in miniature.

#### What v5 changes

**Fourteen synthetic severity=info rows, train only.** Option 1 above, taken.
Five alert families this fleet would emit at info — certificate renewals,
backup completions, ArgoCD syncs, cron successes, unattended upgrades — with
real object names, no precedent block, labelled notify with a frame that cites
the severity and nothing else: `immich.ai: severity=info — informational, no
investigation needed`. They are the first non-historical rows in the set:
every one carries `meta.synthetic: true`, validation never gets one (its file
is byte-identical to v4's), the builder runs them through the same eval
exclusion as real rows, and tests pin all of that plus that the gate's
unnamed-precedent rule does not flag the frame. `--no-synthetic-info` for an
A/B.

**How much of production this touches, checked rather than assumed:** this
homelab defines no info-level Prometheus rule (13 critical, 40 warning). What
reaches triage at `info` today is the Alertmanager Watchdog — its
`severity: none` maps to info at intake — and resolution alerts, which
`run_triage` already short-circuits. So the shape is the rubric's ("notify …
when severity=info"), not yet the fleet's. Option 2 (short-circuit non-noise
info alerts in `run_triage`) is tracked as its own issue.

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

**The restart gotcha is history since CFOP-151** (`cfoperator-deploy` #25). The
key now lives in `files/config.yaml`, which feeds a `configMapGenerator` named
`cfoperator-config` in `kustomization.yml`; the generated ConfigMap's name
carries a content hash, so a config commit changes the pod template and ArgoCD
rolls **both** `cfoperator` and `cfoperator-event-runtime` (they mount the same
file by `subPath`). No manual restart — and hand-restarting `deploy/cfoperator`
alone, as this section used to instruct, was only ever half the workloads.
Before #25 the ConfigMap was a plain manifest and a config-only commit really
did restart nothing; if the hashed name (`cfoperator-config-<hash>`) is missing
from `kubectl get cm -n apps`, you are looking at that older layout.

**This does not apply to the Helm chart.** `charts/cfoperator/templates/agent.yaml`
and `event-runtime.yaml` both annotate the pod with `checksum/config` (a sha256
of the rendered ConfigMap), so a `helm upgrade` that changes config rolls both
deployments by itself. Chart users should not cargo-cult the restart. The Helm
deployment names also differ (`{release}-cfoperator-agent` /
`{release}-cfoperator-event-runtime`), so the command above is homelab-specific
twice over.

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
ollama create cfop-triage-ministral3:v5-q4 \
  -f /path/to/cfoperator/benchmarks/Modelfile.cfop-triage-v5      # the deployed model
ollama create cfop-triage-ministral3:v1-q4 \
  -f /path/to/cfoperator/benchmarks/Modelfile.cfop-triage         # the rollback target

# Confirm it answers, then re-run the gate:
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v5-q4 --runs 3 --output /tmp/verify.json
```

Each Modelfile's `FROM` points at its generation's NAS folder
(`unsloth/cfoperator-v6/cfop-triage-v5-gguf/` for v5, `mistral-trained-01/` for
v1), so the only prerequisite is the NAS mount. Every generation's Q8_0 sits
beside its Q4 with a `-q8` Modelfile (`Modelfile.cfop-triage-v5-q8`); for v1,
change `FROM` to the `Q8_0.gguf` file and tag it `:v1`.

---

## Open follow-ups

Carried over from the v1 session plus the 2026-09-02 review; none are blocking:

- **Eval v2** — the test suite that would have caught the `reason`/`confidence`
  regression and the majority-class soak blind spot. Tiered plan, power
  calculations, counterfactual pairs, and a promotion gate in
  [docs/triage-eval-v2-plan.md](triage-eval-v2-plan.md). First step is a
  no-code soak of the `log_only`/`notify` cases.
- **Expose the triage timeout as a config/Helm knob.** A dedicated timeout
  already exists: `CFOP_TRIAGE_TIMEOUT_SECONDS` on event_runtime's HTTP client
  (since #39; code default **5s** when unset, and the homelab's
  `cfoperator-deploy` sets it to **120s**). What is missing is a
  `llm.triage_timeout` key in `config.yaml` and the env plumbing in
  `charts/cfoperator/templates/event-runtime.yaml`, which currently passes no
  `CFOP_TRIAGE_*` at all — so a chart install silently runs at 5s.
- **Helm support for `llm.triage_model`** — the key works in raw config; the
  chart does not expose it.
- **Ansible provisioning** of the ollama tag on `ubuntu-llm-01`, so a node
  rebuild does not silently drop triage back to the slow chain.
- **A triage-model health check.** The failure mode with no signal today is
  `llm.triage_model` set but the tag absent: cfoperator falls back silently and
  triage just gets slower. Triage responses already carry the serving `model`
  field, so alerting on "triage served by something other than the configured
  model" is cheap.
- **Confirm `cfoperator-finetune/` is in the NAS→iDrive backup set**, at minimum
  the 668MB `training-run-1787248524/`.
- **Archive the run directory on every future training run**, off the training
  box, before anything else. v1's survived by luck.
- **v2 retrain** once `triage_conflicts.json` accumulates enough rows to be worth
  it, ideally with a less lopsided class balance for `escalate` and `log_only`.
  It can resume from the archived adapter rather than retraining from base.
- **Tool calling stays out of scope** until ollama ships a working ministral
  tool-call parser (ollama/ollama#16934, #17550). No historical tool transcripts
  exist to train on anyway — `investigation_events` were never written.

## See also

- [`docs/triage-retrain-runbook.md`](triage-retrain-runbook.md) — the ordered
  procedure for producing a new tag. This page is the record of what v1 *is*;
  that one is what you follow with the training box in front of you.

- [`benchmarks/ministral-3-14b-baseline.md`](../benchmarks/ministral-3-14b-baseline.md) — the before/after benchmark write-up
- [`scripts/build_triage_dataset.py`](../scripts/build_triage_dataset.py) — dataset builder, and the authority on label derivation
- [`docs/config-reference.md`](config-reference.md) — the `llm.triage_model` key
- [`docs/noise-reduction.md`](noise-reduction.md) — why triage exists at all
