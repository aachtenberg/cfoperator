# CFOperator MCP Server — reference

`mcp_server/` exposes CFOperator's SRE capabilities as a standard MCP server
so any MCP host (Claude Desktop/Code, Cursor, the Slack bridge's
AnthropicRuntime, CLI, CI) can work the fleet. It is a **stateless facade**
over the agent HTTP API — no LLM or OODA loop runs in this process, and it
never imports the knowledge base or DB layers.

- Design/contract/history: [mcp-server-plan.md](mcp-server-plan.md)
- Slack bridge (a consumer of this server): [slack-bridge.md](slack-bridge.md)
- **Deployed since 2026-07-29**: `cfoperator-mcp` Deployment in `apps`,
  Streamable HTTP at `http://cfoperator-mcp.apps.svc.cluster.local:8090/mcp`,
  bearer token in the `cfoperator-mcp` Secret, `investigate` scope.

## Tools

Scopes form a hierarchy: `remediate` ⊃ `investigate` ⊃ `read`.

| Tool | Scope | Notes |
|------|-------|-------|
| `list_investigations(limit=20)` | read | Recent investigation summaries, newest first |
| `get_investigation(investigation_id)` | read | Full findings + conclusion |
| `list_remediations(status=None, limit=50)` | read | Queue rows; statuses: queued, claimed, executing, pr-open, verifying, resolved, failed, needs-human, rejected |
| `get_remediation(remediation_id)` | read | Full row: payload, result, PR URL |
| `search_knowledge(query, limit=5)` | read | Hybrid vector+FTS over investigation learnings (FTS-only fallback; response `mode` says which) |
| `triage_alert(summary, description?, severity?, labels?, alert_id?)` | investigate | One-shot LLM triage; returns the decision, enqueues nothing |
| `start_investigation(summary, ..., idempotency_key?)` | investigate | Async enqueue; repeats of the same `idempotency_key`/`alert_id` within the agent's dedup TTL (default 1h) return `status='deduped'` |
| `ask_sre(question, timeout_seconds?, backend='auto', model=None)` | investigate | Blocking chat through the agent's own tool loop (metrics/logs/k8s/SSH, all in-cluster). `backend` selects the agent-side LLM per call: `auto` \| `ollama` \| `groq` \| `anthropic` \| `xai`; `model` is provider-specific (e.g. `backend='anthropic', model='claude-opus-5'` for a second opinion on a suspect local-model conclusion). Can take minutes. |
| `approve_remediation(remediation_id)` | remediate | Hands the row to the executor (status → queued) |
| `reject_remediation(remediation_id, note?)` | remediate | Rejects with optional operator note |
| `resolve_remediation(remediation_id, note?)` | remediate | Manual close (status → resolved) for work already handled — fixed by hand or superseded; note lands in `result.resolution_note` |

## Resources

| URI | Content |
|-----|---------|
| `cfop://investigations/recent` | Latest 25 investigation summaries (JSON) |
| `cfop://investigations/{id}` | Single investigation detail |
| `cfop://remediations/open` | In-flight remediation rows (terminal statuses filtered out) |
| `cfop://digest/morning` | Latest morning summary (full text since phase 2) |

## Prompts

Every `skills/*/SKILL.md` is auto-registered as an MCP prompt (kebab-case
names preserved), so the prompt list is whatever `skills/` contains at
start — currently 9: `investigate-pod`, `investigate-host`,
`investigate-container`, `investigate-deployment`,
`investigate-code-change`, `why-restart`, `k3s-cluster-health`,
`compare-hosts`, `mqtt-top-talkers`. Each takes an optional `target`
argument that appends a "Target: ..." section to the playbook.

`mqtt-top-talkers` drives the agent's `timescale_query` tool, which is only
registered when the agent has `TIMESCALE_PASSWORD` set. The prompt is
registered by this process regardless — it is text, and the tool call happens
agent-side.

## Quick start (stdio, local)

```bash
# agent reachable e.g. via: kubectl -n cfoperator port-forward svc/cfoperator 8083:8083
CFOP_AGENT_URL=http://127.0.0.1:8083 CFOP_MCP_SCOPES=investigate \
  python -m mcp_server
```

Smoke check against a live agent (read-only): `scripts/mcp_smoke.py`.

## Configuration

| Env var | Default | Notes |
|---------|---------|-------|
| `CFOP_AGENT_URL` | `http://127.0.0.1:8083` | Agent base URL |
| `CFOP_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (Streamable HTTP) |
| `CFOP_MCP_TOKEN` | — | **Required** for `http`; server refuses to start a network listener without it |
| `CFOP_MCP_SCOPES` | `read` | Comma list; each scope implies the lower ones |
| `CFOP_MCP_HOST` / `CFOP_MCP_PORT` | `127.0.0.1` / `8090` | HTTP listener bind |
| `CFOP_MCP_REQUEST_TIMEOUT` | `30` | Per-request timeout to the agent (s) |
| `CFOP_MCP_CHAT_TIMEOUT` | `300` | `ask_sre` overall deadline (s) |
| `CFOP_SKILLS_DIR` | `./skills` | Source directory for MCP prompts |
| `CFOP_MCP_LOG_LEVEL` | `INFO` | Logs go to stderr (stdout belongs to stdio transport) |

Never ship `remediate` in a shared or public host config. For a
remediate-capable consumer, prefer a **separate Deployment per capability
tier** (own token Secret, own scope env) over widening the shared one.

### Per-token scopes

Over `http`, the bearer is resolved against the console's `auth_api_tokens`
table when one is reachable, and the token's *own* scopes apply — so one HTTP
listener can serve a `read` dashboard and an `investigate` bot without either
inheriting the other's authority, and either can be revoked alone. Mint them at
the console's `/tokens` page. `CFOP_MCP_SCOPES` remains the grant for `stdio`,
where the process belongs to the local user and there is no token exchange.

`CFOP_MCP_TOKEN` still works and still takes the process-wide `CFOP_MCP_SCOPES`
grant, deliberately: promoting the shared token to a full-scope identity would
defeat the point of moving off it. It is checked only after the database, so a
real token is never shadowed by it, and its use is warned and audited.

A database error during verification returns **503** with `retryable: true` — it
never falls through to the shared token and never falls open.

## Audit log

Every tool call emits one structured JSON line on the `mcp_server.audit`
logger (stderr → journald → Loki):

```json
{"audit": "mcp_tool_call", "tool": "ask_sre", "scope": "investigate", "outcome": "ok", "ms": 42133.7}
```

`outcome` is `ok`, `unauthorized` (scope miss), or the upstream error code
(`not_found`, `upstream_unavailable`, ...). Query in Loki alongside the rest
of the fleet's logs.

## Host setup

**Claude Desktop / Claude Code / local Cursor IDE (stdio):**

```json
{
  "mcpServers": {
    "cfoperator": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "CFOP_AGENT_URL": "http://127.0.0.1:8083",
        "CFOP_MCP_SCOPES": "investigate"
      }
    }
  }
}
```

(Claude Code: `claude mcp add cfoperator -e CFOP_AGENT_URL=... -- python -m mcp_server`)

**LAN hosts (Streamable HTTP, since 2026-07-30):** the MCP endpoint is
exposed at `http://10.0.0.14:8090/mcp` via hostPort (the pod is pinned
to headless-gpu). Auth: `Authorization: Bearer <token from the
cfoperator-mcp Secret>`. Defense-in-depth: the `CFOP-8090-GUARD` iptables
chain (same ansible playbook as the 8083 guard) allows only cluster
sources, the WireGuard backhaul, and the operator workstation
(10.0.0.20) — hooked into INPUT **and** FORWARD because hostPort
traffic is DNAT'd. Example workstation config (Claude Code):

```bash
claude mcp add --transport http cfoperator http://10.0.0.14:8090/mcp \
  --header "Authorization: Bearer $(kubectl -n apps get secret cfoperator-mcp -o jsonpath='{.data.token}' | base64 -d)"
```

**Public internet exposure** (Cursor Cloud Agents, Claude-in-Slack
connectors) remains a deliberate, separate decision — see the plan doc's
phase-3 CursorRuntime note.

## Deployment (live)

Deployed as `cfoperator-mcp` in `apps` (cfoperator-deploy repo): same image
as the agent with `command: ["python", "-m", "mcp_server"]`, amd64
nodeSelector (image is amd64-only), ClusterIP :8090, token from the
`cfoperator-mcp` Secret (created out-of-band, never in git):

```bash
kubectl -n apps create secret generic cfoperator-mcp \
  --from-literal=token="$(openssl rand -hex 32)"
```

**The `:8083` bypass is closed** (2026-07-29): a host iptables guard on
headless-gpu (homelab-infra `ansible/deploy-cfoperator-8083-guard.yml`)
restricts the agent port to cluster sources — a NetworkPolicy could not do
this because the agent is a hostNetwork pod. Direct LAN browsing to
`10.0.0.14:8083` requires being in `guard_allowed_cidrs` or using
`kubectl port-forward svc/cfoperator 8083:8083`.

## Error shape

All tool failures return the host-neutral structured error:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}}
```

Codes: `upstream_unavailable` (retryable — agent down, timeout, queue full),
`unauthorized` (bad bearer at HTTP layer, or scope miss in-band),
`not_found`, `conflict`, `validation`.
