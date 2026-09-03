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

## 0.5 Choose the base model — screen it, do not assume it

**Do this before any retrain that is not a straight repeat.** It costs minutes
per candidate and no GPU-hours, and it uses the same gate that will judge the
fine-tune.

The 14B was sized when the training target was four canned strings. The v2
target is mostly *copy the right span out of the prompt*, which small models do
well — so the size that made sense for v1 is not automatically right now. Two
constraints push the same way:

- **Training** must fit 16 GB alongside the Windows compositor. The 14B at
  4-bit is ~10.4 GB of weights and spent an evening fighting for headroom
  (§5). An 8B is roughly half that.
- **Serving** co-resides with `gemma4:26b` (18 GB) on a 24 GB card, so the
  triage model's ~8.2 GB is the thing squeezing it.

`triage_eval.py` scores base models directly — the model card records
`ministral-3:14b` (base) at **37/42, hard cases 4/12 + 4/12, 0.93s**. That row
is the number to beat.

```bash
ollama pull <candidate>
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model <candidate> --runs 36 --output /tmp/base-<name>.json
```

**Pick on the traps, not the headline.** Base ministral's two failures were
both the *notify shortcut* — reading precedent **presence** as precedent
**outcome** (`precedent-monitoring`), and severity without breadth
(`critical-narrow`) — taken about two thirds of the time. The fine-tune
eliminated both.

That is the right way to read a screen: **fine-tuning reliably fixes rubric
traps and less reliably installs comprehension.** A candidate at 35/42 that
does *not* take the shortcut is a better substrate than one at 40/42 that
does.

**Prefer the same family first.** A same-family model keeps the chat template,
the `PARSER ministral` line and the ollama tool-call caveat identical, so
nothing downstream changes. A different family means re-verifying the Modelfile
against the base model's exact template — the model card is emphatic that a
paraphrased template will not transfer, and that is a silent failure, not a
loud one.

Record the screen next to the fine-tune's numbers so the choice is auditable
later. If no candidate beats the incumbent on the traps, staying on the 14B is
a result, not a non-decision.

---

## 1. Build the dataset

```bash
CFOP_TOKEN=... PYTHONPATH=agent:. .venv/bin/python scripts/build_triage_dataset.py \
  --base-url http://localhost:8083 --out-dir benchmarks/datasets --limit 5000
```

**Pass `--limit` explicitly.** It defaults to 1000, and the v3 build hit that
exactly — "fetched 1000 investigations" is not a count, it is a ceiling. The
build still succeeds and the row counts still look plausible, so the truncation
is invisible unless you compare the number against the limit. The builder now
warns when the two match. The classes that lose examples first are `escalate`
and `log_only`, which are already the thinnest.

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
# The gate's own grader, run over the TRAINING TARGETS. This is what found
# the v2 fabrication and, on v3, two label bugs -- if the builder itself
# cites something absent from its prompt, the model will learn to.
import sys; sys.path.insert(0, "benchmarks"); import triage_eval as te
fab = [o["reason"] for r, o in zip(rows, outs)
       if te.grade_reason(o["reason"], r["messages"][1]["content"])[1]]
print("fabricated citations", len(fab), "  <- must be 0", fab[:2])
PY
```

Compare against the fingerprint recorded in the model card's
[v2 and v3 datasets](triage-fine-tune.md#v2-and-v3-datasets-cfop-153) table.
A mismatch means you are about to train on a different build than the one that
was reviewed — stop and find out why. This step exists because the first
version of that table carried a stale number and would have sent someone
chasing a phantom. **`fabricated citations` must be 0**: v2 shipped with 176
and the model reproduced them.

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

**`max_seq_length`: the v2 run executed at 768**, not 1024 — cut for VRAM
headroom, see `cfop-triage-v2-gguf/AS-EXECUTED.md`. For v3 the longest row is
**648 tokens through the real tokenizer**, ~663 with the chat template, so 768
leaves ~100 of headroom. The earlier "~745 estimated" was a chars/3.5 guess
that overstates by ~18%. Re-measure *exactly* if the prompt or the frames grow
— no tokenizer install needed, ollama already has it loaded:

```bash
.venv/bin/python - <<'PY'
import json, urllib.request
rows=[json.loads(l) for l in open("benchmarks/datasets/triage_train.jsonl")]
text="".join(m["content"] for m in max(rows, key=lambda r: sum(len(m["content"]) for m in r["messages"]))["messages"])
req=urllib.request.Request("http://localhost:11434/api/generate",
    data=json.dumps({"model":"cfop-triage-ministral3:v2-q4","prompt":text,"raw":True,
                     "stream":False,"options":{"num_predict":1}}).encode(),
    headers={"Content-Type":"application/json"})
print("longest row, exact tokens:", json.load(urllib.request.urlopen(req))["prompt_eval_count"], "(+~15 for the chat template)")
PY
```

**For v3: use the v2 settings unchanged.** `cfoperator-v4/cfoperator-v3-recommended.yaml`
on the NAS is that file with only the header rewritten; its settings diff
empty against the v2 one. The row count is half of v2's (262 vs 522, from the
frame cap) — **keep `num_epochs` at 3 and do not compensate.** v2's correct
escalates used base-model phrasing absent from the training data, so less
deviation from the base is the goal, not more steps. Note that three settings
in the YAML never reach the trainer (`batch_size` runs 1, `max_grad_norm` runs
None, `finetune_vision_layers` runs True); the same YAML reproduces the same
*executed* config, which is what a clean A/B on the data needs.

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
ollama create cfop-triage-ministral3:v2-q4 -f benchmarks/Modelfile.cfop-triage-v2
```

**Write a NEW Modelfile per generation; do not repoint the old one.**
`Modelfile.cfop-triage` is the documented NAS recovery path for v1 ("if
ubuntu-llm-01 is rebuilt... no retraining required"), so changing its `FROM`
breaks that silently and leaves the rollback target unbuildable at exactly the
moment it is wanted. Copy it and change only `FROM`.

Keep the `TEMPLATE` block **byte-identical** — same base model, and a
paraphrased template does not transfer and fails silently rather than loudly.
Verify rather than eyeball it:

```bash
.venv/bin/python - <<'EOF'
def block(p):
    s = open(p).read()
    # anchor on the directive, not the word -- "TEMPLATE" also appears in
    # the header comment, and slicing from there reports a false mismatch
    return s[s.index('TEMPLATE \"\"\"'):]
a = block("benchmarks/Modelfile.cfop-triage")
b = block("benchmarks/Modelfile.cfop-triage-v2")
print("identical:", a == b)
EOF
```

Use a **new tag**. Do not reuse `:v1-q4`: the incumbent must stay intact and
servable for rollback.

## 8. Gate

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
  --model cfop-triage-ministral3:v3-q4 --runs 36 \
  --output benchmarks/triage_eval_cfop_triage_ministral3_v3_q4.json
```

`--runs 36` rather than 3: 3 runs detect an 8%-rate fault only 22% of the time,
36 detect it 95%. The reasoning is in
[`triage-eval-v2-plan.md`](triage-eval-v2-plan.md).

Minimum bar, matching what v1 cleared: **42/42 on the 14-case screen, 24/24 on
the hard cases, 100/100 soak, 100% JSON valid**, and Q4/Q8 agreeing on every
case.

**The gate now grades the reason too.** Two lines were added after v2 passed
it at 98.4% while fabricating:

```
Reason grounded  504/504 (100.0%)
Fabricated cites   0/504 (0.0%)
```

**`Fabricated cites` must be 0, regardless of action accuracy.** On v2 it read
41.7% — `novel-oom` scored 36/36 on the action while every reason cited a pod
that does not exist. Note the first line passed v2 too: a reason can name the
real pod *and* invent a precedent. Grounding is not the absence of fabrication.
Every fabricating run is also printed inline as it happens (`FABRICATED <name>`),
even when the action was right. Reasons are persisted in the JSON now, so an
audit can be retroactive.

Then read a few reasons with your own eyes — the checks are narrow by design:

```bash
PYTHONPATH=agent:. .venv/bin/python benchmarks/reason_compare.py \
  --case novel-oom --runs 4 --models cfop-triage-ministral3:v3-q4 cfop-triage-ministral3:v2-q4
```

`novel-oom` and `correlated-outage` have no precedents, so any "nearest was…"
is an invention. `known-sdcard` has one, so the reason should quote it. And
`correlated-outage` is the v2 regression: 28/36; **≥ 34/36** says the rebalance
worked, *unchanged* says the imbalance was not the cause and the escalate class
needs real new examples rather than a better ratio.

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
