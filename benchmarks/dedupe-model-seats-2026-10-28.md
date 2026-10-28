# Dedupe model seats, 2026-10-28 (CFOP-545)

Which model can answer the dedupe model step's question: is this new FIX the
same change as one of the open rows? Scored by `dedupe_model_eval.py`
through the production prompt and parser (`dedupe_model.ask`), on the queue
as it stood that morning (405 rows). Raw results, without the models'
reasons: `dedupe_model_eval_2026-10-28.json`. Hand verdicts:
`dedupe_model_adjudication.json`.

## The cases

- **28 duplicates**: rows the operator closed as a duplicate (or that were
  cancelled as one), each shown with the rows open when it arrived.
- **60 distinct**: unmarked rows, the 60 that look most like an open row
  (shared host, shared target, overlapping words). They are the hard
  negatives: the same subject, maybe a different fix.

The first run showed the labels are incomplete. Of 60 pairs the models
folded that no note marks, 46 are the same fix: the twelve raspberrypi4 rows
of one dead node, the immich-kiosk nodeSelector, the timescaledb selector,
Cloudflare ingress, the nginx zone. The operator's notes cover roughly half
the queue's duplicates. Every such fold was read and given a verdict: S (same
fix, an unmarked duplicate), B (borderline, one covers more than the other),
D (a different fix on the same subject). **Only D counts as a wrong fold.**

## Results

One run per case. Wrong = D verdicts only. Latency is the median and p95 of
a whole call. Ollama ran alone, one request at a time, on the shared card.

| seat | caught /28 | unmarked dups found | borderline | **wrong** | p50 s | p95 s |
|---|---|---|---|---|---|---|
| openrouter:openai/gpt-5.5 | 25 | 27 | 1 | **0** | 3.7 | 7.1 |
| openrouter:openai/gpt-5.4-mini | 25 | 22 | 3 | **0** | 2.3 | 3.9 |
| deepseek:deepseek-flash | 25 | 26 | 2 | **0** | 2.3 | 5.0 |
| xai:grok-4.5 | 24 | 25 | 4 | **0** | 8.0 | 21.5 |
| groq:openai/gpt-oss-20b | 24 | 23 | 3 | **0** | 1.1 | 3.0 |
| groq:openai/gpt-oss-120b | 24 | 23 | 5 | **0** | 1.6 | 3.2 |
| openrouter:anthropic/claude-haiku-5.5 | 21 | 25 | 1 | **0** | 3.1 | 4.6 |
| ollama:gemma4:26b | 24 | 26 | 3 | 1 | 12.7 | 31.2 |
| deepseek:deepseek-v4-pro | 23 | 24 | 3 | 1 | 10.0 | 42.1 |
| openrouter:openai/gpt-5.4-nano | 22 | 23 | 2 | 1 | 4.0 | 6.0 |
| gemini:gemini-3-flash-preview | 25 | 28 | 6 | 2 | 1.1 | 1.4 |
| gemini:gemini-3.1-flash-lite | 25 | 24 | 5 | 2 | 1.2 | 1.5 |
| openrouter:anthropic/claude-sonnet-5.5 | 24 | 26 | 2 | 2 | 1.7 | 3.0 |
| openrouter:mistralai/ministral-14b-2512 | 22 | 22 | 7 | 5 | 1.9 | 6.0 |
| xai:grok-4.20-0309-non-reasoning | 25 | 27 | 7 | 7 | 1.3 | 1.9 |
| openrouter:mistralai/ministral-8b-2512 | 26 | 24 | 7 | **15** | 2.0 | 4.4 |

## Reading it

- **Most seats can do this.** Seven made no wrong fold on the 60 hardest
  negatives and caught 21 to 25 of 28 labelled duplicates. Zero in 60 bounds
  the wrong-fold rate at about 5% (the rule of three), on the hardest cases
  in the queue.
- **Size is not what matters, reasoning is.** gpt-oss-20b (about 3.6B active
  parameters) made no wrong fold. Ministral 8B and grok-4.20 non-reasoning
  catch as many duplicates but fold different fixes on the same host
  (Ministral 8B merged a fan-control fix with an ollama memory limit, and an
  IPv6 firewall change with a pod-log check). They are not safe seats.
- **The misses are mostly defensible.** Every seat misses #222 against #220.
  Those are two fixes for one problem (a DaemonSet dnsPolicy, and
  NetworkManager on the hosts), closed against one issue: the label is
  loose, not the model. The first run also missed #337 (a zone of 256k
  against the earlier rows' 1m) as "a different value"; the prompt now says
  the same setting at a different value is the same fix, and catches went up
  on most seats.
- **#400–#402**, the case that started CFOP-545: every seat caught #401. Four
  missed #402 (grok-4.5, deepseek-v4-pro, claude-haiku-5.5, gpt-5.4-nano),
  which also bundles a second change (a probe-role audit) and reads as more
  than the first two. With one of the pair folded, the operator would have
  had two rows instead of three.

## Caveats

- One run per case. A seat that folds wrongly a few percent of the time can
  score 0 here.
- On an OpenAI-compatible seat the agent's one-shot also offers the
  unattended tool schemas; the harness does not. Shadow mode is the check
  for a model that would rather call a tool than answer.
- The direct Anthropic seat did not run (that key had no credit), so Claude
  was scored through OpenRouter.
- The verdicts are one reader's. They are in the adjudication file to be
  disputed; `--replay` re-grades the saved run after any edit.
