# Triage fine-tune — retrain runbook

**Ordered procedure for producing a new `cfop-triage-ministral3` tag.**
[`triage-fine-tune.md`](triage-fine-tune.md) is the model card: what v1 *is*,
why, and every archived value. This is the thing you follow with the training
box in front of you. Where the two disagree about a v1 fact, the model card
wins — it was read back from the archived run.

Written for the **v2 (CFOP-153) retrain**, which is the first use. Steps 1–3
happen on the cfoperator host; 4–6 on the Windows training box; 7–9 on
`ubuntu-llm-01`.

---

## 0. Before you start

| | |
|---|---|
| Training box | Windows, RTX 5080 16GB, unsloth studio (LAN API on `:8888`) |
| Base model | `unsloth/Ministral-3-14B-Instruct-2512-unsloth-bnb-4bit` |
| Serving host | ollama on `ubuntu-llm-01` / k3s node `headless-gpu` |
| Incumbent | `cfop-triage-ministral3:v1-q4` — **leave it serving throughout** |
| Rollback | one config line; see [model card](triage-fine-tune.md#deployment-rollback-and-the-restart-gotcha) |

The incumbent stays deployed until the candidate clears the gate. Nothing in
this runbook touches production until step 9.

---

## 1. Build the dataset

```bash
PYTHONPATH=agent:. .venv/bin/python scripts/build_triage_dataset.py \
  --base-url http://localhost:8083 --out-dir benchmarks/datasets
```

Offline variant, if you already have an investigation dump:

```bash
PYTHONPATH=agent:. .venv/bin/python scripts/build_triage_dataset.py \
  --from-file dump.json --out-dir benchmarks/datasets \
  --embed-url http://192.168.0.150:11434
```

The builder's docstring is the authority on label derivation and leakage
controls. Two things it guarantees and you should not weaken: the system prompt
is extracted from `agent/agent.py` at runtime (a paraphrase would not transfer),
and anything resembling a `triage_eval.py` case is excluded so the eval stays
held-out.

**The output is gitignored** — real homelab data, public repo. It is
hand-carried to the training box and never committed.

## 2. Pre-flight: check you have the file you think you have

```bash
.venv/bin/python - <<'PY'
import json, collections
rows=[json.loads(l) for l in open("benchmarks/datasets/triage_train.jsonl")]
outs=[json.loads(r["messages"][-1]["content"]) for r in rows]
print("rows                ", len(rows))
print("distinct reasons    ", len({o["reason"] for o in outs}))
print("distinct confidences", len({o["confidence"] for o in outs}))
print("generic subjects    ", sum(1 for o in outs if "this alert" in o["reason"]))
print("label mix           ", dict(collections.Counter(o["action"] for o in outs)))
PY
```

Compare against the fingerprint recorded in the model card's
[v2 dataset](triage-fine-tune.md#v2-dataset-cfop-153) table. A mismatch means
you are about to train on a different build than the one that was reviewed —
stop and find out why. This step exists because the first version of that table
carried a stale number and would have sent someone chasing a phantom.

## 3. Copy to the training box

`triage_train.jsonl` and `triage_val.jsonl`. Keep the previous generation
alongside (`*.v1.jsonl`) so a comparison run is possible without a rebuild.

---

## 4. Configure the run

Start from the v1 values — they are in the model card's
[Hyperparameters](triage-fine-tune.md#hyperparameters) block, read back from
the archived run, not from notes. Change only what the data change argues for.

**Leave alone:** `r=16`, `lora_alpha=16`, `lr=1e-4`, `linear`, `warmup_steps=10`,
`per_device_train_batch_size=1`, `gradient_accumulation_steps=8` (effective
batch 8), `adamw_8bit`, `bf16`, `gradient_checkpointing`, `max_seq_length=1024`,
**`seed=3407`**. Keeping the seed is what makes v1-vs-v2 a clean comparison.

**`max_seq_length=1024` is verified sufficient** for the v2 set: longest row is
~745 estimated tokens, nothing truncates. Re-measure if the prompt or the
reason frames grow.

**Change, with the reason:**

| Setting | v1 | v2 | Why |
|---|---|---|---|
| `lora_dropout` | 0.0 | **0.05** | v1's targets were four fixed strings — nothing to overfit to. v2 has hundreds of reasons built from five frames, and the failure mode is memorising the frames and emitting them regardless of alert. The model card notes 0.05 was recommended for v1 and never actually applied. |
| `num_train_epochs` | 2 | **3** | Completion length roughly doubled (p50 29→47 tokens, max 32→80) and the task changed from recalling a fixed string to copying the right span out of the prompt. Watch the eval curve; stop at 2 if it turns. |
| `save_steps` | 30 | **30, keep every checkpoint** | With three epochs and a harder target you want the option to fall back. v1's monotonic curve will not necessarily repeat. |

**Target modules — decide deliberately.** v1 attached adapters to
`q/k/v/o` only, *not* because seven were unselected but because unsloth's
attention filter silently overrode the UI. Setting seven in the UI is **not
sufficient**; you must also clear the filter. Attention-only is defensible for
v2 — copying a span from prompt to output is an attention operation — but make
it a choice, not an inheritance.

**Save the studio YAML this time.** v1's settings were recoverable only because
the output directory happened to survive twelve days on the box. That was luck.

## 5. Train

### Before you press start: the two environment variables

```
set UNSLOTH_CE_LOSS_TARGET_GB=1
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

**Launch studio from that shell.** These are a launch requirement, not a
one-time fix — a reboot removes them, and studio started from a shortcut or
auto-start will not have them.

Without them the run does not crash in an obvious way. It runs at **70–90
s/step instead of ~7**, showing 99% GPU utilisation at 71–93 W of a 360 W
board, and dies at random steps. That reads like "the card is too small" and
it is not. **High utilisation at low wattage means PCIe waiting, not
compute** — check watts before touching any other setting.

~17 minutes for v1 at 2 epochs; budget proportionally. If it is dramatically
slower, read the model card's
[Windows/5080 gotchas](triage-fine-tune.md#the-windows5080-gotchas--a-30x-speed-story)
before changing anything — that section is a 30x speed story with a specific
cause.

### Reading the loss — the trap

**Do not expect v1's eval loss of 0.0034, and do not chase it.**

That number measured "emit well-formed JSON with the majority label and one of
four fixed strings", against a val split that is 46/50 `investigate`. v2 targets
carry real entropy, so loss will settle **substantially higher** — and that is
the run working. A v2 loss approaching v1's would be the warning sign: it would
mean a shortcut was found.

The val split is still temporal and still not distributionally matched to train.
It is a divergence check, not a quality measure. `triage_eval.py` is the gate.

## 6. Export

Per-quantization, over the studio LAN API — the exact calls are in the model
card's [Export and import](triage-fine-tune.md#export-and-import). Export is
**merged**, not adapter-only, and de-quantizes the 14B on the fly, so it is the
heaviest step on the box.

Do `Q8_0` first as the archival reference, then `Q4_K_M` as the deployment
candidate. **Both clear the gate before either ships.** Copy the GGUFs to the
NAS beside the v1 artifacts; do not overwrite them.

---

## 7. Import on `ubuntu-llm-01`

```bash
ollama create cfop-triage-ministral3:v2-q4 -f benchmarks/Modelfile.cfop-triage
```

Point the Modelfile's `FROM` at the new GGUF. It must keep the **base model's
exact chat template** — the fine-tune was trained against it and a paraphrase
will not transfer.

Use a **new tag**. Do not reuse `:v1-q4`: the incumbent must stay intact and
servable for rollback.

## 8. Gate

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v2-q4 --runs 36 \
  --output /tmp/v2-eval.json
```

`--runs 36` rather than 3: 3 runs detect an 8%-rate fault only 22% of the time,
36 detect it 95%. The reasoning is in
[`triage-eval-v2-plan.md`](triage-eval-v2-plan.md).

Minimum bar, matching what v1 cleared: **42/42 on the 14-case screen, 24/24 on
the hard cases, 100/100 soak, 100% JSON valid**, and Q4/Q8 agreeing on every
case.

**The gate scores `action` only.** It will *not* tell you whether the reasons
improved — which is the entire point of the v2 dataset. Check that by hand:

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v2-q4 --runs 1 --only known-sdcard
```

and read the reason next to gemma4's on the same case. Looking for: does it name
the alert, or recite a frame? A canned string is the v1 regression returning.

## 9. Deploy

One line in `cfoperator-deploy` (`llm.triage_model`), then watch both workloads
roll — `cfoperator-config` is a `configMapGenerator`, so the content hash moves
the pod template and ArgoCD rolls it; no manual restart. Details and the
rollback path are in the model card's
[Deployment section](triage-fine-tune.md#deployment-rollback-and-the-restart-gotcha).

**Rollback is the same line pointed back at `:v1-q4`**, which is why step 7
insists on a new tag.

---

## If you are here because something is wrong

| Symptom | Look at |
|---|---|
| Loss much higher than v1 | Expected. See §5. |
| Loss as low as v1 | Suspicious — look for a shortcut, not a success |
| Eval passes but reasons are canned | The dataset, not the model. Re-run §2. |
| Training far slower than ~17 min | **Check watts first.** High util + low watts = the two env vars are missing (§5). Not VRAM, not `gradient_checkpointing`, not sequence length — all three were investigated and none was the cause. |
| Run dies at a random early step | Same cause as above, most likely |
| Model lost, no retrain wanted | [Rebuilding from the NAS](triage-fine-tune.md#rebuilding-from-the-nas) — two commands |
| `/v1/triage` returns the primary's name | Tag missing on the host; nothing asserts this today (eval plan Tier 7) |
