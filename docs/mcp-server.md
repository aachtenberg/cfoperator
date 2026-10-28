# CFOperator MCP Server — reference

`mcp_server/` exposes CFOperator's SRE capabilities as a standard MCP server
so any MCP host (Claude Desktop/Code, Cursor, the Slack bridge's
AnthropicRuntime, CLI, CI) can work the fleet. It is a **stateless facade**
over the agent HTTP API — no LLM or OODA loop runs in this process, and it
never imports the knowledge base or DB layers.

- Why it is shaped this way, what was rejected, and what was never built:
  [History and design decisions](#history-and-design-decisions)
- Slack bridge (a consumer of this server): [slack-bridge.md](slack-bridge.md)
- The other direction, the console chat using tools from someone else's MCP
  server: [mcp-connectors.md](mcp-connectors.md)
- Using this server as an incident cockpit, plus a copy-pasteable
  `.mcp.json`: [cockpit.md](cockpit.md) and
  [examples/cfoperator.mcp.json](examples/cfoperator.mcp.json)
- **Deployed since 2026-07-29**: `cfoperator-mcp` Deployment in `apps`,
  Streamable HTTP at `http://cfoperator-mcp.apps.svc.cluster.local:8090/mcp`,
  process grant `investigate`.

## Tools

Scopes form a hierarchy: `remediate` ⊃ `investigate` ⊃ `read`.

| Tool | Scope | Notes |
|------|-------|-------|
| `list_investigations(limit=20)` | read | Recent investigation summaries, newest first. Rows carry the agent's `outcome` and the operator's `triage_action` (null = untriaged) |
| `get_investigation(investigation_id)` | read | Full findings + conclusion |
| `list_remediations(status=None, limit=50)` | read | Queue rows; statuses: queued, claimed, executing, pr-open, verifying, resolved, failed, needs-human, filed (handed to the issue tracker), rejected |
| `get_remediation(remediation_id)` | read | Full row: payload, result, PR URL |
| `search_knowledge(query, limit=5)` | read | Hybrid vector+FTS over investigation learnings (FTS-only fallback; response `mode` says which) |
| `triage_alert(summary, description?, severity?, labels?, alert_id?)` | investigate | One-shot LLM triage; returns the decision, enqueues nothing |
| `start_investigation(summary, ..., idempotency_key?)` | investigate | Async enqueue; repeats of the same `idempotency_key`/`alert_id` within the agent's dedup TTL (default 1h) return `status='deduped'` |
| `ask_sre(question, timeout_seconds?, backend='auto', model=None)` | investigate | Blocking chat through the agent's own tool loop (metrics/logs/k8s/SSH, all in-cluster). `backend` selects the agent-side LLM per call: `auto` \| `ollama` \| `groq` \| `anthropic` \| `xai` \| `gemini` \| `deepseek` \| `openrouter`; `model` is provider-specific (e.g. `backend='anthropic', model='claude-opus-5'` for a second opinion on a suspect local-model conclusion). Can take minutes. |
| `approve_remediation(remediation_id)` | remediate | Hands the row to the executor (status → queued). Refused with `conflict` for manual-class rows |
| `reject_remediation(remediation_id, note?)` | remediate | Rejects with optional operator note |
| `resolve_remediation(remediation_id, note?)` | remediate | Manual close (status → resolved) for work already handled — fixed by hand or superseded; note lands in `result.resolution_note` |
| `triage_investigation(investigation_id, action, note)` | remediate | Records the operator's verdict on `triage_action`: `resolved` (handled or moot) or `ack` (seen, not claimed fixed). `note` is required, since it is the only record of the reasoning. It does not edit the agent's `outcome`, which later investigations cite as precedent |

Approving is not the change. The executor turns an approved row into a pull
request, and CFOperator never changes anything unattended: every change has a
named human behind it — the merge on a GitOps PR, the merge of a change-record
PR for a host action (node-action is refused outright when no change recorder
is configured), or an admin confirming a command in the console.

The four `remediate` tools pass two gates. The facade checks its own scope
grant, and the agent then checks the facade's outbound `CFOP_API_TOKEN`,
because approve, reject, resolve and investigation triage are admin-only
routes and a token counts as admin only when it carries `remediate`.

## Resources

| URI | Content |
|-----|---------|
| `cfop://investigations/recent` | Latest 25 investigation summaries (JSON) |
| `cfop://investigations/{id}` | Single investigation detail |
| `cfop://remediations/open` | Remediation rows not yet `resolved`, `failed` or `rejected` |
| `cfop://digest/morning` | Latest morning summary (full text since phase 2) |

Resources do not pass through the scope check or the audit line that tools do
(see [Still open](#still-open)).

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
# agent reachable e.g. via: kubectl -n apps port-forward svc/cfoperator 8083:8083
# CFOP_API_TOKEN: a console API token from /admin?tab=tokens
CFOP_AGENT_URL=http://127.0.0.1:8083 CFOP_API_TOKEN=... CFOP_MCP_SCOPES=investigate \
  python -m mcp_server
```

Smoke check against a live agent (read-only): `scripts/mcp_smoke.py`.

## Configuration

| Env var | Default | Notes |
|---------|---------|-------|
| `CFOP_AGENT_URL` | `http://127.0.0.1:8083` | Agent base URL |
| `CFOP_API_TOKEN` | — | **Required against an authenticated agent.** A console API token (minted at `/admin?tab=tokens`) that the facade presents to the agent as `Authorization: Bearer`. Without it every agent call answers 401. It needs `remediate` for the remediate tools to work |
| `CFOP_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (Streamable HTTP) |
| `CFOP_MCP_TOKEN` | — | **Required** for `http`; server refuses to start a network listener without it |
| `CFOP_MCP_SCOPES` | `read` | Comma list; each scope implies the lower ones. The scope ceiling for every tool call, on both transports |
| `CFOP_MCP_HOST` / `CFOP_MCP_PORT` | `127.0.0.1` / `8090` | HTTP listener bind |
| `CFOP_MCP_REQUEST_TIMEOUT` | `30` | Per-request timeout to the agent (s) |
| `CFOP_MCP_CHAT_TIMEOUT` | `300` | `ask_sre` overall deadline (s) |
| `CFOP_MCP_CHAT_POLL_INTERVAL` | `2.0` | How often `ask_sre` polls the agent's chat events (s) |
| `CFOP_SKILLS_DIR` | `./skills` | Source directory for MCP prompts |
| `CFOP_MCP_LOG_LEVEL` | `INFO` | Logs go to stderr (stdout belongs to stdio transport) |
| `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD`, or `CFOP_AUTH_DB_URL` | see [auth.md](auth.md) | The auth database used to verify bearers over `http`. If it is unreachable at start, only the shared `CFOP_MCP_TOKEN` is accepted until the next restart; that is logged, and the pod stays healthy |

Never ship `remediate` in a shared or public host config. For a
remediate-capable consumer, run a **separate Deployment per capability
tier** (own token Secret, own scope env) rather than widening the shared one.
That is the actual control, because the scope ceiling is per process.

### Tokens and scopes

Over `http`, the bearer is verified against the console's `auth_api_tokens`
table when one is reachable. Each caller can then hold its own credential:
it can be revoked or expire alone, it stops working when its owner is
deactivated, and its `last_used_at` advances. Mint them at
`/admin?tab=tokens`. `CFOP_MCP_SCOPES` remains the only grant for `stdio`,
where the process belongs to the local user and there is no token exchange.

The token's own scopes are **not** what tool dispatch checks. Every tool call
is checked against the process-wide `CFOP_MCP_SCOPES` (CFOP-548), so a `read`
token on an `investigate` Deployment can call `ask_sre`, and a `remediate`
token on that Deployment still cannot approve. The token identity is resolved
and parked on the request, ready for dispatch to use, but nothing reads it
yet. Until it does, one Deployment per capability tier is how a tier is kept.

`CFOP_MCP_TOKEN` still works and takes the process-wide `CFOP_MCP_SCOPES`
grant. It is checked only after the database, so a real token is never
shadowed by it, and its use is warned and audited as `token.legacy_used`.

A database error during verification returns **503** `auth_unavailable` with
`retryable: true` — it never falls through to the shared token and never
falls open.

## Audit log

Every tool call emits one structured JSON line on the `mcp_server.audit`
logger (stderr → journald → Loki):

```json
{"audit": "mcp_tool_call", "tool": "ask_sre", "scope": "investigate", "outcome": "ok", "ms": 42133.7}
```

`scope` is the scope the tool requires, not the caller's. `outcome` is `ok`,
`unauthorized` (scope miss), the upstream error code (`not_found`,
`upstream_unavailable`, ...), or `internal_error` when the call raised
something unexpected (the exception still propagates to the host). The line
does not name the token. Query in Loki alongside the rest of the fleet's logs.

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
        "CFOP_API_TOKEN": "<console API token>",
        "CFOP_MCP_SCOPES": "investigate"
      }
    }
  }
}
```

(Claude Code: `claude mcp add cfoperator -e CFOP_AGENT_URL=... -e CFOP_API_TOKEN=... -- python -m mcp_server`)

**LAN hosts (Streamable HTTP, since 2026-07-30):** the MCP endpoint is
exposed at `http://10.0.0.14:8090/mcp` via hostPort (the pod is pinned
to headless-gpu). Auth: `Authorization: Bearer <token>`, either a token
minted at `/admin?tab=tokens` or the shared one in the `cfoperator-mcp`
Secret. Defense-in-depth: the `CFOP-8090-GUARD` iptables chain (same ansible
playbook as the 8083 guard) allows cluster sources, the WireGuard backhaul and
the LAN, so the bearer token is the control. The chain is hooked into INPUT
**and** FORWARD because hostPort traffic is DNAT'd. Example workstation
config (Claude Code):

```bash
claude mcp add --transport http cfoperator http://10.0.0.14:8090/mcp \
  --header "Authorization: Bearer $CFOP_MCP_BEARER"
```

**Public internet exposure** (Cursor Cloud Agents, Claude-in-Slack
connectors) remains a deliberate, separate decision — see the CursorRuntime
entry under [Not built](#not-built).

## Deployment (live)

Deployed as `cfoperator-mcp` in `apps` (cfoperator-deploy repo): same image
as the agent with `command: ["python", "-m", "mcp_server"]`, amd64
nodeSelector (image is amd64-only), ClusterIP :8090 plus hostPort 8090,
`Recreate` (the old pod holds the hostPort, so a rolling update would never
schedule). The inbound token comes from the `cfoperator-mcp` Secret (created
out-of-band, never in git); the outbound `CFOP_API_TOKEN` is this service's
own console token (`cfoperator-secrets` key `CFOP_API_TOKEN_MCP`); and the
`POSTGRES_*` variables point at the auth database.

```bash
kubectl -n apps create secret generic cfoperator-mcp \
  --from-literal=token="$(openssl rand -hex 32)"
```

The agent's `:8083` is reachable from the LAN and gated by console auth
(sessions for browsers, bearer tokens for services, 503 on misconfiguration),
in place since 2026-08-04. A host iptables guard on headless-gpu (homelab-infra
`ansible/deploy-cfoperator-8083-guard.yml`) is defense in depth: it allows
cluster sources, the WireGuard backhaul, the LAN and docker0, and rejects the
rest. A NetworkPolicy could not do this because the agent is a hostNetwork
pod.

## Error shape

Tool failures return the host-neutral structured error:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}}
```

Codes: `upstream_unavailable` (retryable — agent down, timeout, queue full),
`unauthorized` (scope miss in-band, or the agent refused the facade's
`CFOP_API_TOKEN`), `not_found`, `conflict`, `validation`.

The HTTP layer answers before any tool runs, with the same fields unwrapped —
`{"code", "message", "retryable"}` — as `401 unauthorized` for a missing or
unknown bearer, or `503 auth_unavailable` (retryable) when the auth database
cannot be reached.

## History and design decisions

### Decisions that still hold

- **Facade only.** No OODA loop, LLM, knowledge-base or embedding import in
  this process. Search goes through a thin agent route (`GET /api/kb/search`)
  rather than importing `knowledge_base.py`, so the facade stays a facade.
- **Host-neutral payloads.** No Cursor agent ids or Slack thread ids in tool
  results or prompts; any host consumes the same server.
- **Dual transport, always.** stdio for local IDEs and CI, Streamable HTTP for
  remote hosts, one tool registry. Never ship only one.
- **Streamable HTTP, not SSE.** SSE is deprecated in current MCP SDKs.
- **`ask_sre` returns the final reply only.** Host support for MCP progress
  notifications is uneven, and the `/api/chat/events/<id>` poll maps cleanly to
  one blocking call. Revisit if a host demands streaming.
- **Sibling Deployment, not a sidecar.** The MCP listener and the agent have
  different exposure profiles, and a separate Deployment gives an independent
  blast radius and rollout.
- **One Deployment per capability tier.** A remediate-capable consumer gets its
  own Deployment, Secret and scope env rather than a wider shared grant.
- **Dedup lives upstream.** `start_investigation` forwards `idempotency_key` /
  `alert_id`; the agent's `enqueue_investigation` dedups on them for
  `ooda.investigation_dedup_ttl_seconds` (default 3600). The window is in
  memory by design: a restart clears it, which risks a duplicate investigation,
  never a lost one.
- **Outbound alert Slack is not bridge Slack.** event_runtime keeps paging; the
  bridge keeps conversations.
- **Inline MCP is not durable.** A runtime that resumes an external agent must
  re-attach the MCP config itself; this server does not track that.

### Rejected

- **Dedup state in the facade.** It would silently break the stateless facade
  and diverge across replicas.
- **A NetworkPolicy for `:8083`.** Policies do not apply to hostNetwork pods,
  so it would have been a no-op; a host iptables guard shipped instead, and
  console auth became the real control.
- **Per-request scope plumbing through the MCP session layer** was first
  rejected in favour of one Deployment per tier. Per-token *verification*
  shipped later (#105); per-token *enforcement* at dispatch has not (CFOP-548).

### Trail

- 2026-07-29 — phase 1, the facade on stdio and HTTP with scope tiers (#81);
  deployed the same day; the `:8083` host guard (homelab-infra PR #67).
- 2026-07-29 — phase 2: skill prompts, `search_knowledge`, upstream idempotent
  enqueue, the morning digest resource, and the Slack bridge (#84).
- 2026-07-29 — phase 3: the bridge's AnthropicRuntime over this server, and
  the tool-call audit log (#86).
- 2026-07-30 — per-call `backend`/`model` on `ask_sre` (#87); hostPort 8090 for
  LAN hosts behind the `CFOP-8090-GUARD` chain.
- 2026-08-01 — `resolve_remediation` (#92).
- 2026-08-04 — the console requires credentials (#93); the facade presents
  `CFOP_API_TOKEN` upstream.
- 2026-08-05 — bearers verified per token against `auth_api_tokens` (#105).
- 2026-08-09 — the shared console `CFOP_API_TOKEN` retired; each service holds
  its own.
- 2026-08-31 — `triage_investigation` (CFOP-138, #227).
- 2026-09-09 — the `filed` remediation status (CFOP-170, #248).

### Not built

- **CursorRuntime.** Cursor cloud VMs would need this server publicly reachable
  (Cloudflare Tunnel exposure) and a Cursor API key, and writing speculative
  code against a fast-moving external API with no way to test it is worse than
  the gap. The bridge's `AgentRuntime` seam is ready if the decision lands.
- **Read-only fleet wrappers** (`query_metrics`, `query_logs`, `k8s_get`,
  `list_hosts`). Only if a host needs a deterministic tool graph without a
  nested LLM; `ask_sre` has covered the need so far.
- **Mutating fleet operations** (`ssh_exec`, force delete). They stay out of MCP
  unless a dedicated admin scope and audit path exist.
- **`cfop://alerts/{id}`.** event_runtime's `GET /history?alert_id=` could back
  it; `CFOP_EVENT_RUNTIME_URL` is parsed into the settings and never used.
- **Phase 4 hardening:** no MCP metrics, no per-token rate limits, and no
  readiness check against the agent's `/api/health` (the probes are TCP on
  8090).

### Still open

- Resources skip `require_scope` and the audit line. Harmless today, since
  every scope implies `read`.
- Tool dispatch checks `CFOP_MCP_SCOPES`, not the token's scopes (CFOP-548).
- `uvicorn` is not pinned in `requirements.txt`; it arrives with `mcp`.
