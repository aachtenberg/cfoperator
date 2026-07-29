# CFOperator MCP Server Plan

Reusable MCP surface for CFOperator so any agent host (Cursor, Claude Desktop,
custom Slack bots, CLI, CI) can investigate the fleet, triage alerts, and work
remediations — without baking Cursor or Slack into the core.

_Status: phase 1 **code + deploy** live. Code shipped in PR #81 (image
`main-d5459cd`); sibling `cfoperator-mcp` Deployment running in-cluster
(cfoperator-deploy, `investigate` scope, amd64-pinned). In-cluster e2e
verified 2026-07-29 via pod exec: 401 without token, MCP initialize +
`list_investigations` tool call with token. Remaining phase-1 ops: the
`:8083` NetworkPolicy (homelab-infra) — `remediate` scope stays withheld
until it lands. Phase 2+ not started. See
[Verification](#verification-against-implementation)._

## Goals

- Expose CFOperator SRE capabilities as a **standard MCP** server (tools,
  resources, prompts).
- Stay **host-agnostic**: Cursor Cloud Agents, Cursor IDE, Claude, OpenAI-style
  hosts, and local scripts all consume the same server.
- Keep Slack → agent as an **optional bridge** with pluggable runtimes (Cursor
  SDK, Anthropic, local CFOperator chat, …).
- **Facade only** — call existing agent / event_runtime HTTP APIs; do not add a
  second OODA loop or LLM inside the MCP process.
- Ship **stdio + Streamable HTTP** transports from day one so local IDEs and
  remote/cloud agents share one tool registry.
- Reuse the unused `mcp` dependency already listed in `requirements.txt` —
  re-pin it first: `mcp>=0.9.0` is a floor from the 0.x era and resolves to
  2.0.0 today; pin a real range (e.g. `mcp>=1.10,<3`) against the FastMCP API
  actually used.

## Non-goals

- Replacing Cursor’s native Slack `@Cursor` app (that remains a valid *host*).
- Replacing event_runtime outbound Slack webhooks for alert pages.
- Exposing raw SSH/kube credentials to every MCP client (scoped tokens +
  capability tiers instead).
- Persisting host-specific IDs (Cursor `bc-*`, Slack thread IDs) inside MCP
  tool payloads.

## Architecture

```text
┌─────────────┐  ┌──────────────┐  ┌────────────────┐
│ Slack / CLI │  │ Cursor / IDE │  │ Claude / other │
│  bridge     │  │ Cloud Agent  │  │ MCP hosts      │
└──────┬──────┘  └──────┬───────┘  └───────┬────────┘
       │                │                   │
       └────────────────┼───────────────────┘
                        ▼
              ┌───────────────────┐
              │  cfoperator-mcp   │  ← reusable core
              │  (stdio + HTTP)   │
              └─────────┬─────────┘
                        ▼
         ┌──────────────┴──────────────┐
         ▼                             ▼
   agent (:8083)                 event_runtime (:8080)
   /api/chat, /v1/*              alerts, digests, sinks
```

### Package boundaries

| Package | Responsibility | Cursor-aware? |
|---------|----------------|---------------|
| `mcp_server/` | MCP tools / resources / prompts over CFOperator HTTP | No |
| `bridge/` (optional) | Slack Events → pluggable `AgentRuntime` + MCP handle | Only via a runtime plugin |
| Existing agent / event_runtime | Unchanged source of truth | No |

## Package layout

```text
mcp_server/
  __init__.py
  __main__.py          # python -m mcp_server
  server.py            # FastMCP (or mcp SDK) app + transport selection
  auth.py              # bearer / shared-secret validation
  config.py            # CFOP_AGENT_URL, CFOP_EVENT_RUNTIME_URL, scopes
  client.py            # thin httpx client to agent + event_runtime
  tools/
    __init__.py
    triage.py          # triage_alert
    investigate.py     # start_investigation, get_investigation, list_investigations
    chat.py            # ask_sre (wrap /api/chat + event poll)
    knowledge.py       # search_knowledge
    remediations.py    # list / approve / reject
    fleet.py           # optional read wrappers (metrics/logs/k8s) — phase 2
  resources/
    __init__.py
    investigations.py  # cfop://investigations/...
    remediations.py    # cfop://remediations/open
    digests.py         # cfop://digest/morning
  prompts/
    __init__.py
    playbooks.py       # map skills/*/SKILL.md → MCP prompts
  tests/
    test_tools_unit.py
    test_client_mocks.py

bridge/                      # optional second process — phase 2+
  __init__.py
  __main__.py
  slack_app.py               # Slack Events / Socket Mode
  config.yaml
  runtimes/
    __init__.py
    base.py                  # AgentRuntime Protocol
    local_cfop.py            # ask_sre / start_investigation only
    cursor.py                # Cursor SDK / Cloud Agents API
    anthropic.py             # optional
```

## MCP contract

### Tools (phase 1)

| Tool | Maps to | Scope | Impl |
|------|---------|-------|------|
| `triage_alert` | `POST /v1/triage` | `investigate` | yes |
| `start_investigation` | `POST /v1/investigate` | `investigate` | yes (forwards `idempotency_key`; upstream ignores it today) |
| `get_investigation` | `GET /api/investigations/<id>` | `read` | yes |
| `list_investigations` | `GET /api/investigations` | `read` | yes |
| `ask_sre` | `POST /api/chat` + poll `/api/chat/events/<id>` (+ stop on timeout) | `investigate` | yes |
| `list_remediations` | `GET /api/remediations` | `read` | yes |
| `get_remediation` | `GET /api/remediations/<id>` | `read` | yes (added in MVP; not in original draft table) |
| `approve_remediation` | `POST .../approve` | `remediate` | yes |
| `reject_remediation` | `POST .../reject` | `remediate` | yes |
| `search_knowledge` | new thin agent route (`GET /api/kb/search`) — phase 2 | `read` | no |

`search_knowledge` has **no upstream endpoint today** — `agent/knowledge_base.py`
is internal-only. Per design rule 1 the MCP server must not import it (that
would drag DB/embedding deps into the facade); a thin `GET /api/kb/search`
agent route is a phase-2 prerequisite.

### Tools (phase 2 — optional fleet wrappers)

Prefer calling through `ask_sre` / investigate first. Add direct wrappers only when
hosts need deterministic tool graphs without a nested LLM:

- `query_metrics`, `query_logs`, `k8s_get`, `list_hosts` (read-only)

Mutating fleet ops (`ssh_exec`, force delete) stay out of MCP unless a dedicated
`admin` scope and audit path exist.

### Resources

| URI | Content | Upstream | Impl |
|-----|---------|----------|------|
| `cfop://investigations/recent` | Recent investigation summaries (JSON) | `GET /api/investigations` | yes |
| `cfop://investigations/{id}` | Single investigation detail | `GET /api/investigations/<id>` | yes |
| `cfop://remediations/open` | Open remediation proposals / PRs | `GET /api/remediations` (filter closed) | yes |
| `cfop://digest/morning` | Latest morning / noise digest | **no stable GET today** — deferred | no |
| `cfop://alerts/{id}` | Alert + triage outcome if known | `GET /history?alert_id=` on event_runtime | no (client field unused) |

### Prompts

Map existing `skills/*/SKILL.md` into MCP prompts. Actual skills today:
`investigate-pod`, `investigate-host`, `investigate-container`,
`investigate-deployment`, `investigate-code-change`, `why-restart`,
`k3s-cluster-health`, `compare-hosts`. Prompt names mirror the skill dir names
(kebab-case preserved).

Prompts are host-neutral text templates; they must not mention Cursor or Slack.

### Errors

Structured, host-neutral:

```json
{
  "code": "upstream_unavailable" | "unauthorized" | "not_found" | "conflict" | "validation",
  "message": "human readable",
  "retryable": true
}
```

No Cursor agent IDs or Slack thread IDs in tool results.

### Idempotency

`start_investigation` accepts optional `idempotency_key` (and/or stable
`alert_id`). Retries from Slack bridges or multi-host fans must not double-enqueue
when the key matches an in-flight or completed job.

**Where dedup lives:** upstream, in `enqueue_investigation` behind
`POST /v1/investigate` — not in the MCP server. Today that route enqueues
unconditionally (`web_server.py`), so honoring this contract is an upstream
change. The MCP server stays stateless and just forwards the key; MCP-side
dedup state is explicitly rejected (it would silently break the facade and
diverge across replicas).

## Auth and capability tiers

Token (bearer / `CFOP_MCP_TOKEN`) carries a scope set:

| Scope | Allowed |
|-------|---------|
| `read` | list/get investigations, remediations, resources, search_knowledge |
| `investigate` | `read` + triage, start_investigation, ask_sre |
| `remediate` | `investigate` + approve/reject remediations |

Defaults:

- IDE / Claude Desktop demo installs → `read` or `investigate`
- Slack bridge / trusted automation → `investigate` (remediate only if explicitly enabled)
- Never ship a token with `remediate` in a public or shared desktop config

Network: MCP HTTP listener reachable only from trusted hosts / tunnel / mesh.
Secrets stay on the MCP host; cloud agent VMs never receive kube/SSH creds.

### The `:8083` bypass problem (phase 1, not phase 4)

The agent API itself is unauthenticated except the two executor-completion
endpoints (X-CFOP-Token). MCP token scopes are only real if clients cannot
reach `:8083` directly — otherwise anyone who can hit the MCP listener can
bypass it and call `POST /api/remediations/<id>/approve` straight. Before any
MCP client outside the cluster gets a token, one of:

- **NetworkPolicy** restricting agent `:8083` ingress to the MCP pod plus
  existing in-cluster callers (preferred — no agent code change), or
- shared-secret auth on the mutating agent routes.

Without this the `remediate` scope is theater.

## Transports and config

```bash
export CFOP_AGENT_URL=http://cfoperator-agent:8083
export CFOP_EVENT_RUNTIME_URL=http://cfoperator-event-runtime:8080   # optional
export CFOP_MCP_TOKEN=...
export CFOP_MCP_SCOPES=read,investigate
export CFOP_MCP_TRANSPORT=stdio   # or http
export CFOP_MCP_HOST=0.0.0.0
export CFOP_MCP_PORT=8090
```

- **stdio** — local IDE, Claude Desktop, `mcp` CLI, CI sidecars
- **Streamable HTTP** — Cursor Cloud Agents, remote bridges, team dashboards.
  Decided: Streamable HTTP, not SSE — SSE is deprecated in current MCP SDKs
  and the dep will be pinned to a 1.10+/2.x range that supports it natively.

Same tool registry for both. Transport is config only.

### Host registration examples (docs only)

**Cursor team / Cloud Agents** — register HTTP URL + auth header in
Dashboard → Integrations & MCP (repo `.cursor/mcp.json` alone is not enough for
cloud/Slack-triggered agents).

**Claude Desktop / local Cursor IDE** — stdio:

```json
{
  "mcpServers": {
    "cfoperator": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "CFOP_AGENT_URL": "http://127.0.0.1:8083",
        "CFOP_MCP_TOKEN": "...",
        "CFOP_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

## Bridge (optional): Slack without locking to Cursor

```python
class AgentRuntime(Protocol):
    async def start(self, prompt: str, *, mcp_url: str, thread_id: str) -> str: ...
    async def follow_up(self, agent_id: str, prompt: str) -> str: ...
```

| Runtime | Behavior |
|---------|----------|
| `LocalCfopRuntime` | Call `ask_sre` / `start_investigation` via MCP or HTTP; no external coding agent |
| `CursorRuntime` | Cursor SDK / Cloud Agents API; pass cfoperator MCP to the agent |
| `AnthropicRuntime` | Messages (or equivalent) + MCP connector |

Slack Events → normalize → `runtime.start(...)` → post replies on the thread.
Outbound alert paging stays on event_runtime webhooks; the bridge owns
conversational threads only.

## Implementation phases

### Phase 0 — Design lock (this doc)

- [x] Architecture, package split, tool/resource/prompt contract
- [x] Confirm HTTP transport flavor → Streamable HTTP; re-pin `mcp` dep
- [x] Confirm investigation list/get routes → `GET /api/investigations` and
      `/api/investigations/<id>` already exist; no new routes for MVP
- [x] Confirm event_runtime can back `cfop://alerts/{id}` →
      `GET /history?alert_id=` exists (phase 2 resource); `/jobs/{id}` is
      worker-job shaped, not alert-shaped — do not use it for this URI

### Phase 1 — `mcp_server` MVP (reusable core)

1. [x] Re-pin `mcp` in `requirements.txt` (`mcp>=1.27,<2`; `httpx>=0.28`;
   `uvicorn` comes transitively via `mcp`).
2. [x] Scaffold `mcp_server/` with config, auth, HTTP client.
3. [x] Implement phase-1 tools against live agent routes (`/v1/triage`,
   `/v1/investigate`, `/api/chat` + events, `/api/investigations*`,
   `/api/remediations*`).
4. [x] Add resources for recent investigations + open remediations.
5. [x] Support `stdio` and `streamable-http` transports.
6. [x] Unit tests with mocked upstream (`mcp_server/tests`, 22 passed);
   smoke script `scripts/mcp_smoke.py`.
7. [x] Sibling Deployment + Service in cfoperator-deploy (`cfoperator-mcp.yml`,
   2026-07-29): ClusterIP :8090, `investigate` scope, token Secret created
   out-of-band, amd64 nodeSelector (image is amd64-only).
8. [ ] NetworkPolicy (or shared-secret) closing the `:8083` bypass before any
   token leaves the cluster.
9. [x] Docs: [mcp-server.md](mcp-server.md) (Cursor team MCP + Claude Desktop
   + raw HTTP).

**Exit criteria (code):** A non-Cursor MCP client can triage an alert and list
remediations end-to-end — met by unit tests + smoke script shape, and by
in-cluster e2e (curl-as-MCP-client) 2026-07-29.
**Exit criteria (ops):** deploy manifests **met** (cfoperator-deploy);
NetworkPolicy **not met** — `remediate` scope withheld until it lands.

### Phase 2 — Prompts, KB, LocalCfop bridge

1. MCP prompts from `skills/*/SKILL.md`.
2. New thin agent route `GET /api/kb/search`; `search_knowledge` wired to it.
3. `bridge/` with Slack Socket Mode + `LocalCfopRuntime` only.
4. Idempotency: dedup added upstream in `enqueue_investigation`
   (`/v1/investigate` honors `idempotency_key` / `alert_id`); MCP forwards
   the key, keeps no state.
5. `cfop://digest/morning` route in event_runtime + resource wired.

**Exit criteria:** Slack message → local CFOperator answer/investigation → thread
reply, with zero Cursor dependency.

### Phase 3 — Multi-runtime + fleet wrappers

1. `CursorRuntime` (SDK or Cloud Agents API).
2. Optional `AnthropicRuntime`.
3. Read-only fleet tools if hosts need them.
4. Capability-tier enforcement hardened + audit log of tool calls.

**Exit criteria:** Same Slack app can switch runtimes via config; Cursor path uses
the same MCP as LocalCfop.

### Phase 4 — Prod hardening

1. Metrics (`mcp_tool_calls_total`, latency, upstream errors) aligned with
   `docs/METRICS.md`.
2. Rate limits per token.
3. Readiness that checks agent `/api/health`.
4. GitOps manifests in the usual deploy path.

## Design rules (do not violate)

1. **Facade only** — no nested autonomous OODA inside `mcp_server`.
2. **Host-neutral payloads** — no Cursor/Slack IDs in MCP results.
3. **Scopes on tokens** — default deny for `remediate`; scopes count for
   nothing until the `:8083` bypass is closed (NetworkPolicy or route auth).
4. **Idempotent investigate** — `alert_id` / `idempotency_key` required for bridge
   callers.
5. **Dual transport** — never ship HTTP-only or stdio-only as the only option.
6. **Outbound alert Slack ≠ bridge Slack** — event_runtime keeps paging; bridge
   keeps conversations.
7. **Inline MCP is not durable** — if a runtime resumes an agent (e.g. Cursor
   `Agent.resume`), it must re-attach MCP config itself; the server does not track
   that.

## Relationship to existing code

| Existing piece | Role in this plan |
|----------------|-------------------|
| `web_server.py` `/v1/*`, `/api/chat*`, `/api/investigations*`, `/api/remediations*` | Upstream for MCP tools |
| `agent/knowledge_base.py` | Stays internal; fronted by new `GET /api/kb/search` in phase 2 |
| `event_runtime/notifications.py` | Unchanged alert paging |
| `tools/` registry | Phase-2 optional direct wrappers; not required for MVP |
| `skills/*/SKILL.md` | Source for MCP prompts |
| `mcp` in `requirements.txt` | Finally used by `mcp_server` |
| Cursor Slack `@Cursor` | Optional external host; not required |

## Resolved questions

1. **Investigation routes** — `GET /api/investigations` and
   `/api/investigations/<id>` already exist (PR #43); MVP reuses them as-is.
2. **Transport** — Streamable HTTP (SSE deprecated in current SDKs); `mcp`
   dep re-pinned to match.
3. **`ask_sre` streaming** — final reply only in v1. Host support for MCP
   progress notifications is uneven; the `/api/chat/events/<id>` poll maps
   cleanly to a blocking tool call. Revisit if a host demands streaming.
4. **Deployment shape** — sibling Deployment, not sidecar. Different exposure
   profiles (MCP reachable via tunnel/mesh, agent cluster-internal only) and
   independent blast radius / rollout.

## Open questions

1. ~~Can event_runtime `/history` or `/jobs/<id>` back `cfop://alerts/{id}`?~~
   **Resolved:** use `GET /history?alert_id=` (event list for that alert). Drop
   `/jobs/{id}` for this resource. Still need to wire `CFOP_EVENT_RUNTIME_URL`
   into `CfopClient` (config field exists, unused today).
2. Should resources enforce `read` scope the same way tools do? Today resources
   call the client with no `require_scope` — fine for stdio (process-level
   scopes), but a future multi-token HTTP server would leak reads.
3. Pin `uvicorn` explicitly in `requirements.txt`, or keep trusting the `mcp`
   transitive dep?

## Verification against implementation

_Checked 2026-07-28 against `mcp_server/`, `web_server.py`, `event_runtime/`,
`requirements.txt`. Tests: `.venv/bin/python -m pytest mcp_server/tests` →
22 passed._

### Matches the plan

| Plan item | Evidence |
|-----------|----------|
| Facade-only HTTP client | `mcp_server/client.py` → agent `/v1/*` + `/api/*`; no LLM/KB imports |
| Dual transport | `stdio` + `streamable-http` (`config.py`, `server.py`) |
| Bearer auth on HTTP | `BearerAuthMiddleware`; refuses start without `CFOP_MCP_TOKEN` |
| Scope hierarchy | `read` ⊂ `investigate` ⊂ `remediate` via `expand_scopes` |
| Structured errors | `{"error": {code, message, retryable}}` from `guarded` / middleware |
| Phase-1 tools (9) | triage, start/get/list investigations, ask_sre, list/get/approve/reject remediations |
| Phase-1 resources (3) | `cfop://investigations/recent`, `…/{id}`, `cfop://remediations/open` |
| `ask_sre` final-reply-only | polls events until `done`; no MCP progress notifications |
| `mcp` pin | `mcp>=1.27,<2` (installed 1.27.0 in `.venv`) |
| Host docs + smoke | `docs/mcp-server.md`, `scripts/mcp_smoke.py` |
| No `bridge/` yet | correctly absent (phase 2+) |

### Gaps / mismatches

| Item | Plan expectation | Reality |
|------|------------------|---------|
| Phase 1 ops | NetworkPolicy + sibling Deployment | Deployment live (cfoperator-deploy, 2026-07-29); **NetworkPolicy still required** before any external token |
| `idempotency_key` | Upstream dedup in `enqueue_investigation` | MCP forwards key; `agent.enqueue_investigation` enqueues unconditionally |
| `search_knowledge` | Phase 2 | No tool; no `GET /api/kb/search` on agent |
| `cfop://digest/morning` | Phase 2 | No resource; morning summary is generate/notify, not a stable GET |
| `cfop://alerts/{id}` | Deferred | Upstream exists (`GET /history?alert_id=`); MCP unused `event_runtime_url` |
| MCP prompts | Phase 2 from `skills/*/SKILL.md` | No `mcp_server/prompts/`; 8 skills present on disk |
| Resource scope gating | `read` for resources | Resources skip `require_scope` |
| Metrics / rate limits / readiness | Phase 4 | None |
| ROADMAP row | Still “design” wording | Should point at shipped MVP + ops gap |

### Upstream routes the plan depends on (confirmed present)

- `POST /v1/triage`, `POST /v1/investigate` → 202 `{status, alert_id, queue_depth}`
- `GET /api/investigations`, `GET /api/investigations/<id>`
- `POST /api/chat`, `GET /api/chat/events/<id>`, `POST /api/chat/<id>/stop`
- `GET/POST /api/remediations*`, approve/reject
- `GET /api/health` (unused by MCP readiness yet)
- event_runtime `GET /history?alert_id=` (unused by MCP yet)

### Recommended next step

1. **Ops close-out for phase 1:** NetworkPolicy locking `:8083` + sibling MCP
   Deployment/Service in the deploy repo — scopes are theater until then.
2. Then **phase 2 slice:** upstream `idempotency_key` dedup in
   `enqueue_investigation`, optional `cfop://alerts/{id}` via `/history`, MCP
   prompts from skills — still defer `bridge/` until a non-Cursor host has
   exercised the HTTP transport for real.
