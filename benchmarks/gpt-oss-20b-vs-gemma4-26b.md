# CFOperator Suitability — gpt-oss:20b vs gemma4:26b (incumbent)

**Endpoint:** `ubuntu-llm-01:11434` (AMD RX 7900 XTX 24 GB, ROCm), ollama **v0.40.13** | **Date:** 2026-10-30 | **Issue:** CFOP-570
**Incumbent:** `gemma4:26b` (26B-A4B MoE, Q4_K_M, 17.7 GB resident)
**Candidate:** `gpt-oss:20b` (20.9B MoE, MXFP4, 12.9 GB resident, no vision — nothing in the agent sends images)

## Verdict: do not switch. gemma4:26b stays primary.

gpt-oss:20b is twice as fast and uses 6 GB less VRAM, and it ties gemma4 on the easy axes. On the
investigation loop with fault scenarios — the axis that decides what lands in the remediation
queue — it is worse on every correctness measure, and it calls tools that do not exist.

| Investigation loop, production prompt, 5 scenarios x 10 | gemma4:26b | gpt-oss:20b | gpt-oss:20b `think=low` |
|---|---|---|---|
| Status correct | **48/50** | 40/50 | 32/50 |
| Usable STATUS line (after the production nudge) | **50/50** | 43/50 | 41/50 |
| Cites the deciding evidence | **46/50** | 37/50 | 36/50 |
| FIX parses on the first pass (needs_action answers) | 27/28 | 23/25 | 22/22 |
| Trials that called a tool not in the schema | **1/50** | 8/50 | 1/50 |
| Hit the 10-round cap | 34/50 | **3/50** | 3/50 |
| Mean seconds per investigation | 29.3 | 14.4 | **6.5** |

Per scenario (status correct / evidence):

| scenario (expected) | gemma4:26b | gpt-oss:20b |
|---|---|---|
| loki-healthy (resolved) | 10/10, 10/10 | **4/10** (6 monitoring), 10/10 |
| oom-killed (needs_action) | 8/10, 10/10 | 9/10, 10/10 |
| disk-full (needs_action or escalate) | 10/10, 10/10 | 10/10, **5/10** |
| bad-rollout (needs_action) | 10/10, 10/10 | **7/10** (3 monitoring), 10/10 |
| node-flap-recovered (monitoring or resolved) | 10/10, 6/10 | 10/10, **2/10** |

What gpt-oss gets wrong is the expensive direction: a crash-looping rollout with the cause in
plain sight called `monitoring` 3 times in 10, so no remediation is queued. Its invented tools
(`docker_ps`, `k8s_get_pod_metrics`, `k8s_get_pod_stats`, `k8s_get_node_details`, `crictl_list`,
`github_search_commits`, `k8s_list_secrets`, `ssh_crictl`) each cost a round live: the registry
answers an unknown name with an error and the model has to recover. `think=low` stops the
inventing and halves latency again, but drops status accuracy to 32/50 (disk-full 4/10).

Where gpt-oss is better: it stops on its own (gemma4 runs into the cap in 34/50 and needs the
nudge), and it is far faster. Neither is worth the accuracy.

## Triage (`triage_eval.py`, 14 cases x 36 runs, `CFOP_TRIAGE_TIMEOUT_SECONDS=120`)

| | gemma4:26b | gpt-oss:20b |
|---|---|---|
| Action correct | **503/504** | 490/504 (precedent-monitoring 28/36) |
| JSON valid | **100%** | 4 unparseable |
| Reason grounded | 85.3% (FAIL < 90%) | **90.2%** |
| Fabricated citations | 5.8% (FAIL) | **0.0%** |
| Latency mean / p95 | 4.81 s / 8.61 s | **1.45 s / 2.74 s** |

Both fail the strict gate, on different criteria. Triage is not the primary model's job in
production (the ministral fine-tune on the 5060 is; the primary is its fallback), so this axis
informs but does not decide.

## Tool-calling T1-T3 and latency (2026-10-30 night round, same runtime)

Both 10/10 on all five T1-T3 runs; T1-T3 no longer separates serious candidates. Decode
~139 tok/s (gpt-oss) vs 105-110 tok/s (gemma4); latency-bench totals 9.6-10.9 s vs 12-16 s.

## Co-residence

The question was whether gpt-oss is small enough to keep the triage model resident beside it.
At the default context it is not: `cfop-triage-ministral3:v6-q4` is 7.7 GB on disk but **13.6 GB**
resident at num_ctx 32768, and 12.9 + 13.6 GB exceeds the card, so loading it evicted gpt-oss.
It would fit only with triage's context cut down — a separate question, not pursued here.

## Method notes

- Harness: `empty_response_sim.py --scenarios all --prompt production --trials 10 --nudge-retries 2`
  (PR #369). The production prompt is agent.py's current template including the FIX contract; four
  fault scenarios were added because the original healthy scenario rewards a model that always
  says "resolved" — tonight's first round could not see any of the gaps above.
- GPU kept quiet: agent moved to DeepSeek and sweep stretched for the run, then restored; no model
  load from any other client landed inside a phase (checked per phase from the ollama journal).
- Evidence was re-scored after a review fix (word-start matching; disk-full's evidence is the
  journal finding, not the word `journal`, which `journalctl` in the SSH contract matched). These
  runs predate full-answer capture, so the re-score reads each answer's first 400 and last 600
  characters; gpt-oss disk-full evidence moved 10/10 -> 5/10, nothing else changed.
- Evidence files: `empty_response_sim_cfop570_{gemma4-26b,gpt-oss-20b,gpt-oss-20b_think-low}.jsonl`,
  `triage_eval_{gemma4_26b_ollama0_40_13,gpt_oss_20b}.json`.

## Rejected in the same round (2026-10-30 night)

- **Nemotron 3.5 Lightning 30B-A3B** (UD-IQ4_NL): on the tool-less final round writes raw
  `<tool_call>` XML as its answer, which is non-empty so the production nudge never fires; 0/5 usable.
- **Flint 40B-A2.5B** (UD-Q3_K_XL, needs ollama >= 0.40.9): same failure 3/5, 2-6 parallel calls per
  round, up to 6.3k tokens on long prompts; 2/5 usable.
- **Qwen3.6-35B-A3B**: not run. Its ollama default (23-24 GB) does not fit beside the runner's
  overhead (an Unsloth UD-IQ4_XS build, 17.7 GB, would), and both Qwen models tested before failed
  multi-turn tool chaining.

## Follow-ups (not actioned here)

1. gemma4's own weak spots are now measurable: it hits the iteration cap in 34/50 and leans on the
   empty-answer nudge in 30/50; its triage reasons fabricate 5.8% of citations.
2. Invented tool names are only visible in the sim. Counting unknown-tool calls per model in
   production would make the next comparison cheaper.
