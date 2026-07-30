# Slack bridge — talk to CFOperator from Slack

`bridge/` connects to Slack over **Socket Mode** (outbound websocket — the
homelab exposes nothing) and answers mentions/DMs with the CFOperator agent
via the `LocalCfopRuntime`. Threads keep conversational history; messages
starting with `investigate:` enqueue an asynchronous investigation instead
of a chat. Outbound alert paging stays on event_runtime webhooks — the
bridge owns conversations only.

## Slack app setup (one-time, ~15 min)

1. https://api.slack.com/apps → **Create New App** → From scratch, pick the
   workspace.
2. **Socket Mode** → enable → create an app-level token with
   `connections:write` scope → this is `SLACK_APP_TOKEN` (`xapp-...`).
3. **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`,
   `chat:write`, `im:history`, `reactions:write` → **Install to Workspace**
   → this is `SLACK_BOT_TOKEN` (`xoxb-...`).
4. **Event Subscriptions** → enable → Subscribe to bot events:
   `app_mention`, `message.im`.
5. Invite the bot to the channels you want (`/invite @cfoperator`), or DM it.

## Configuration

| Env var | Default | Notes |
|---------|---------|-------|
| `SLACK_BOT_TOKEN` | — | required, `xoxb-...` |
| `SLACK_APP_TOKEN` | — | required, `xapp-...` (Socket Mode) |
| `CFOP_AGENT_URL` | `http://127.0.0.1:8083` | agent base URL |
| `CFOP_BRIDGE_RUNTIME` | `local` | `local` (agent chat API) or `anthropic` (Claude over MCP tools) |
| `CFOP_BRIDGE_CHAT_TIMEOUT` | `300` | per-turn agent deadline (s, local runtime) |
| `CFOP_BRIDGE_MAX_HISTORY_TURNS` | `10` | thread history cap |
| `CFOP_BRIDGE_ESCALATION_MODEL` | `claude-opus-5` | model for the `claude:` per-message prefix (local runtime) |

### anthropic runtime (phase 3)

Claude answers Slack turns by driving cfoperator-mcp's tools directly — an
agentic tool loop entirely inside the bridge pod, with only the Anthropic API
call leaving the cluster. Same Slack app, one env flip.

| Env var | Default | Notes |
|---------|---------|-------|
| `ANTHROPIC_API_KEY` | — | required (already in `cfoperator-secrets`) |
| `CFOP_MCP_TOKEN` | — | required; bearer for cfoperator-mcp (from the `cfoperator-mcp` Secret) |
| `CFOP_MCP_URL` | `http://cfoperator-mcp.apps.svc:8090/mcp` | |
| `CFOP_BRIDGE_ANTHROPIC_MODEL` | `claude-opus-5` | adaptive thinking on; refusal fallbacks enabled on opus-5/fable |
| `CFOP_BRIDGE_MAX_TOOL_ITERATIONS` | `15` | cap on the tool loop per turn |

Trade-off vs `local`: Claude-quality reasoning and tool orchestration with
per-token API cost; `local` stays free on the in-cluster model. `investigate:`
prefix is a `local`-runtime feature — under `anthropic`, Claude decides itself
when to call `start_investigation`.

Run locally: `SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... python -m bridge`

## Deployment

Same image as the agent, sibling Deployment (like cfoperator-mcp):

```bash
kubectl -n apps create secret generic cfoperator-bridge \
  --from-literal=bot_token="xoxb-..." \
  --from-literal=app_token="xapp-..."
```

```yaml
containers:
  - name: cfoperator-bridge
    image: <agent image>          # needs the amd64 nodeSelector like -mcp
    command: ["python", "-m", "bridge"]
    env:
      - name: CFOP_AGENT_URL
        value: http://cfoperator.apps.svc.cluster.local:8083
      - name: SLACK_BOT_TOKEN
        valueFrom: { secretKeyRef: { name: cfoperator-bridge, key: bot_token } }
      - name: SLACK_APP_TOKEN
        valueFrom: { secretKeyRef: { name: cfoperator-bridge, key: app_token } }
```

No Service needed — Socket Mode is outbound-only.

## Usage

- `@cfoperator how is raspberrypi5?` — agent answers in the thread (it runs
  its full tool loop server-side; expect up to a few minutes for hard
  questions; the 👀 reaction means it's working).
- Reply in the thread for follow-ups — history carries over.
- `@cfoperator investigate: promtail crashlooping on pi3` — enqueues a full
  investigation (idempotent per thread+text; retries won't double-enqueue).
- `@cfoperator claude: is that etcd diagnosis on cm5 actually real?` — this
  ONE turn runs on Anthropic (`CFOP_BRIDGE_ESCALATION_MODEL`, default
  `claude-opus-5`) instead of the local model, with the thread's history
  carried over — the in-Slack second opinion for suspect local-model
  conclusions. `opus:` is an alias; the reply is tagged with 🧠 + the model
  name. Costs API tokens per use.

The same per-call selection is exposed to every MCP host via `ask_sre`'s
optional `backend` (`auto|ollama|groq|anthropic|xai`) and `model` params.

## Design notes

- Runtimes are pluggable behind `AgentRuntime` (bridge/runtimes/base.py):
  phase 3 adds Cursor / Anthropic runtimes without touching slack_app.py.
- Events are acked immediately and processed on worker threads; Slack
  event_id redeliveries are deduped (LRU).
- Replies are chunked under Slack's 4k message limit on line boundaries.
