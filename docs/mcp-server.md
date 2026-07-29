# CFOperator MCP Server — running and connecting hosts

`mcp_server/` exposes CFOperator's SRE capabilities (triage, investigations,
chat, remediation worklist) as a standard MCP server. It is a stateless facade
over the agent HTTP API — no LLM or OODA loop runs in this process. Design and
contract: [mcp-server-plan.md](mcp-server-plan.md).

## Quick start (stdio, local)

```bash
# agent reachable e.g. via: kubectl -n cfoperator port-forward svc/cfoperator 8083:8083
CFOP_AGENT_URL=http://127.0.0.1:8083 CFOP_MCP_SCOPES=investigate \
  python -m mcp_server
```

Smoke check against a live agent (read-only):

```bash
CFOP_AGENT_URL=http://127.0.0.1:8083 python scripts/mcp_smoke.py
```

## Configuration

| Env var | Default | Notes |
|---------|---------|-------|
| `CFOP_AGENT_URL` | `http://127.0.0.1:8083` | Agent base URL |
| `CFOP_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (Streamable HTTP) |
| `CFOP_MCP_TOKEN` | — | **Required** for `http`; server refuses to start a network listener without it |
| `CFOP_MCP_SCOPES` | `read` | Comma list: `read`, `investigate`, `remediate` (each implies the previous) |
| `CFOP_MCP_HOST` / `CFOP_MCP_PORT` | `127.0.0.1` / `8090` | HTTP listener bind |
| `CFOP_MCP_REQUEST_TIMEOUT` | `30` | Per-request timeout to the agent (s) |
| `CFOP_MCP_CHAT_TIMEOUT` | `300` | `ask_sre` overall deadline (s) |
| `CFOP_MCP_LOG_LEVEL` | `INFO` | Logs go to stderr (stdout belongs to stdio transport) |

Scopes gate tools per the plan: `read` → list/get + resources; `investigate`
adds `triage_alert`, `start_investigation`, `ask_sre`; `remediate` adds
`approve_remediation` / `reject_remediation`. Never ship `remediate` in a
shared or public host config.

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

**Remote hosts (Streamable HTTP):** endpoint is `http://<host>:8090/mcp`, auth
header `Authorization: Bearer <CFOP_MCP_TOKEN>`. For Cursor Cloud
Agents/Slack-triggered agents register it in Dashboard → Integrations & MCP —
a repo `.cursor/mcp.json` alone does not reach cloud agents.

## Deployment sketch (sibling Deployment)

Same image as the agent, different command — manifests live in the private
deploy repo per the usual GitOps path:

```yaml
containers:
  - name: cfoperator-mcp
    image: <agent image>
    command: ["python", "-m", "mcp_server"]
    env:
      - name: CFOP_AGENT_URL
        value: http://cfoperator:8083
      - name: CFOP_MCP_TRANSPORT
        value: http
      - name: CFOP_MCP_HOST
        value: 0.0.0.0
      - name: CFOP_MCP_SCOPES
        value: investigate
      - name: CFOP_MCP_TOKEN
        valueFrom: { secretKeyRef: { name: cfoperator-mcp, key: token } }
    ports: [{ containerPort: 8090 }]
```

**Before any token leaves the cluster:** apply the NetworkPolicy closing the
`:8083` bypass (see plan, "The `:8083` bypass problem") — MCP scopes mean
nothing while the agent port is open to the same network.

## Error shape

All tool failures return the host-neutral structured error from the plan:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}}
```

`upstream_unavailable` + `retryable: true` covers agent-down, timeouts, and
queue-full; `unauthorized` covers both bad bearer tokens (HTTP 401) and scope
misses (in-band payload).
