# Empty-final-response simulation — investigation tool loop

**Date:** 2026-07-10 · **Harness:** `empty_response_sim.py` — replays the
`_chat_with_tools_inner` Ollama path with the real 50-tool schema payload
(`investigation_tool_schemas.json`, dumped from the deployed pod), the real
investigation system prompt (STATUS/RECOMMENDATION contract), temperature 0.7,
10-iteration cap with final-round tool withholding, and canned healthy-Loki
tool results — so every trial is identical and variance is the model's.

**Motivation:** investigations #1880/#1884/#1885/#1889 completed with
`findings.response == ""` on gemma4:26b; `_extract_status("")` silently
defaulted the outcome to `monitoring`. #1885 was a node-not-ready alert.

## Baseline — how often does a model end the loop with an empty message?

10 trials/model against ubuntu-llm-01 (`empty_response_sim_results.jsonl`):

| model | empty finals | missing STATUS line | hit iteration cap | avg tool calls |
|---|---|---|---|---|
| gemma4:26b | **10/10** | 10/10 | 10/10 | 9.0 |
| qwen3.6:27b | 1/10 | 1/10 | 3/10 | 10.5 |
| glm-4.7-flash | 4/10 | 4/10 | 7/10 | 17.4 |

gemma4's shape: a tool call every round with zero accompanying text, then an
empty message when tools are withheld on the final round. "Cluster is healthy,
wrap up" is precisely the case it cannot verbalize unprompted. The failure
mode is not gemma-exclusive — retry-on-empty is a general safeguard, not a
model workaround.

## Nudge retry — does an explicit "answer NOW" user message recover it?

gemma4:26b, `--nudge-retries 2`:

| variant | empties | recovered by nudge | with STATUS line |
|---|---|---|---|
| tools withheld (`_nudge_notools.jsonl`) | 9/10 | **9/9** | 9/9 |
| tools re-offered (`_nudge_withtools.jsonl`) | 10/10 | **10/10** | 10/10 |
| production wording (`_nudge_prodwording.jsonl`) | 4/5 | **4/4** | 4/4 |

Every recovery succeeded on the **first** nudge attempt. Combined: 23/23.

## Resulting fix (agent/agent.py)

On an empty no-tool-calls final: append `EMPTY_RESPONSE_NUDGE` once and grant
one bonus round past `max_iterations`; if still empty, raise
`EmptyLLMResponseError` so `_chat_with_tools_with_fallback` rotates to the
next provider. An empty response is never again stored as findings.

## Repro

```bash
python benchmarks/empty_response_sim.py --trials 10
python benchmarks/empty_response_sim.py --models gemma4:26b --trials 10 --nudge-retries 2
```
