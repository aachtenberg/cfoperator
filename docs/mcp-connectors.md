# MCP connectors (console chat as an MCP client)

[mcp-server.md](mcp-server.md) is cfoperator *serving* MCP to other hosts.
This page is the other direction: the console chat *using* tools from MCP
servers an admin registers. Added in CFOP-443.

## What a connector is

A name, a URL and some request headers, saved from **Admin → Connectors**.
On save the agent lists the server's tools and adds each one to the chat as
`mcp_<name>_<tool>`. Nothing is restarted, and removing the connector takes
the tools away at once.

- **Transport.** Streamable HTTP only, which is MCP's remote transport. A
  stdio-only server needs something in front of it that speaks HTTP.
- **Auth.** Request headers, stored encrypted under `CFOP_SESSION_SECRET`
  (the same at-rest scheme as console LLM keys). Their values never come
  back out of the API; the console shows their names. Rotating the secret
  makes them unreadable, and the connector says so until they are saved
  again. OAuth-protected servers are not supported.
- **Tool filter.** Optional. Name the tools to offer, and the rest stay off.
  A large server costs every chat turn the tokens of its tool descriptions,
  and a small local model chooses worse from a long list.
- **Freshness.** Tools are listed on save, on **Refresh all**, at startup in
  the background, and again when a chat turn finds the last listing older
  than ten minutes.

## Who may use what

| Turn | Read-only tools | Other tools |
| --- | --- | --- |
| Admin chat | offered | offered; each call is logged with its arguments |
| Member chat, or a verification hand-off | offered | withheld |
| Investigation, sweep, triage, classifiers | never | never |

A tool counts as read-only only when the server marks it
`readOnlyHint: true`. Anything else is treated as a write. Plane marks a tool
read-only only when every action on it reads, so `workitem` is a write tool.

Connector tools are chat-only by design. Their output comes from a system
this agent does not control, and letting it steer an unattended run is a
prompt-injection surface nobody asked for. Like every tool result, connector
output reaches the model framed as untrusted data.

## Example: Plane

Plane's server (`plane-mcp-server`, the Python one) serves streamable HTTP
when started in `http` mode, on port 8211:

```
PLANE_BASE_URL=https://plane.example.com uvx plane-mcp-server http
```

Its header-auth endpoint is `/http/api-key/mcp`. Register it with:

| Field | Value |
| --- | --- |
| name | `plane` |
| url | `http://<host>:8211/http/api-key/mcp` |
| headers | `x-api-key: <a Plane API token>` and `x-workspace-slug: <workspace>` |
| only these tools | e.g. `workitem, workitem_comment, state` |

Deploying that server where the agent can reach it is a deployment change,
not part of this repo.

You do not need a Plane connector to close a remediation's tracker item.
Resolving or rejecting a `filed` row closes its item on the next tracker
hand-off tick (CFOP-170). The chat's resolve tool says so in its result.

## API

All under the console's session or token auth. Writes need an admin.

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/settings/mcp_connectors` | list, with header names and per-connector status |
| PUT | `/api/settings/mcp_connectors/<name>` | create or edit; omitted fields keep their value, `headers: {}` clears them |
| DELETE | `/api/settings/mcp_connectors/<name>` | remove |
| POST | `/api/settings/mcp_connectors/refresh` | list every enabled connector's tools again |

Saves and removals are audited as `mcp.connector`, with the URL, tool filter
and header names, never a header value. The list lives in `agent_settings`
under `mcp_connectors`.
