# Discovery pass

One-shot fleet characterization (CFOP-47): fixes the cold-start problem where
the KB is empty on a fresh install and the first investigations run knowing
nothing about the fleet.

Two strictly-ordered layers:

1. **Deterministic enumeration** (`discovery/inventory.py`) — a fixed, bounded
   set of read-only queries against Prometheus and (optionally) the Kubernetes
   API. Facts come from APIs, never from the model.
2. **LLM interpretation** (`discovery/entrypoint.py`) — the model infers roles,
   relationships, naming conventions, and a first sketch of "normal", and
   proposes KB learnings. It may request a bounded number of follow-up
   Prometheus instant queries, then must produce its final characterization.

## Running

Report-only against a bare Prometheus (nothing else installed). No published
image yet — build locally:

```bash
docker build -t cfoperator-discovery -f discovery/Dockerfile discovery/
docker run --rm \
  -e PROMETHEUS_URL=http://prom:9090 \
  -e CFOP_DISCOVERY_LLM_BASE_URL=http://ollama:11434/v1 \
  -e CFOP_DISCOVERY_LLM_MODEL=gemma4:26b \
  cfoperator-discovery
```

Add `CFOP_AGENT_URL` + `CFOP_API_TOKEN` (a token with the `investigate` scope)
and validated learnings are also POSTed to the agent's `POST /api/learnings`,
tagged `source:discovery`, `inferred`, `confidence:0.NN`. Without them, the run
is report-only and writes nothing anywhere.

## Environment

| Variable | Meaning |
|---|---|
| `PROMETHEUS_URL` | Required. Prometheus base URL. |
| `CFOP_DISCOVERY_K8S_URL` / `_K8S_TOKEN` | Optional read-only k8s API access; in-cluster SA is auto-detected when running as a pod. `CFOP_DISCOVERY_K8S_INSECURE=true` skips TLS verify (trials only). |
| `CFOP_DISCOVERY_LLM_BACKEND` | `openai` (default — covers Ollama/vLLM/llm-gateway) or `anthropic`. |
| `CFOP_DISCOVERY_LLM_BASE_URL` / `_MODEL` / `_API_KEY` / `_MAX_TOKENS` / `_TIMEOUT` | Backend config, same shape as the executor's `CFOP_EXEC_LLM_*`. |
| `CFOP_DISCOVERY_MAX_ROUNDS` | Extra query rounds the model may use (default 2). |
| `CFOP_DISCOVERY_REPORT_DIR` | Also write `report.md` / `report.json` here. |
| `CFOP_AGENT_URL` / `CFOP_API_TOKEN` | Optional push mode (see above). |

## Bounds (visibly tame)

- Read-only: Prometheus + k8s GETs only. **No SSH** — the image doesn't even
  ship a client, and the test suite fails if any process/SSH mechanism appears
  in the component.
- One bounded pass: a fixed enumeration set, capped follow-up queries, capped
  result sizes. Not a crawler.
- The report ends with a **"What was queried"** appendix listing every request
  the pass made, in order, so the tameness claim is verifiable.
- No DB access, no `agent/` imports: the only write path is the agent's HTTP
  API under token scopes.

## Reviewing the output

Wrong inferred context is worse than none — it feeds every future
investigation's orient phase. Every seeded learning carries `applies_when`
(required; the KB auto-deprecates trigger-less learnings), a confidence stamp,
and is individually removable via `DELETE /api/learnings/<id>` (admin).
