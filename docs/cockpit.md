# The incident cockpit — from an alert to a briefed agent session

CFOperator's most valuable output is not the console page. It is the
**briefing**: what it observed, what it concluded, what it queued. This document
is about getting that briefing into the thing you actually work an incident
with — an agent in a terminal, with hands on the affected machine.

The workflow it replaces is the one the operator was already doing by hand:
read the Slack alert, open a terminal, start a coding agent, and type "check
investigation 1889" so it can go query the API itself. That works, and it beats
the console, which is why it is worth productizing rather than arguing with.

Three pieces, in the order you meet them.

## 1. The alert tells you the command

Every notification that carries an investigation — Slack, Discord, or an ntfy
push on your phone — now ends with the handoff line:

```
:warning: *CFOperator Event Runtime*
Action completed: investigate
Alert: Pod immich-kiosk-0 not ready
Action: investigate
Result: needs_action after 92.4s
Recommendation: Raise the memory limit to 512Mi
Investigated by: ollama/gemma4:26b
investigation_id: 1889
Take over: cfassist attach 1889
```

It is plain text on purpose. No backticks, no deep link: the same body goes to
three sinks and only plain text copies identically out of all of them. Deep
links and protocol handlers are client-side fiddliness for a later issue.

A notification without an investigation gets no line — there would be nothing to
attach to. Triage `notify` results, which exist specifically to keep Slack
volume down, stay one line as before.

## 2. `cfassist attach <investigation-id>`

`attach` is a verb of the released single-binary cfassist — the Go build in
`cfassist-go/`, which is what `gh release download` puts on your PATH. There is
nothing else to install: if you have `cfassist`, you have `attach`.

```bash
cfassist attach 1889
```

Pulls the investigation, its operator triage notes, any remediation queue rows
linked to it, and related knowledge-base learnings; renders them into a
briefing; seeds that briefing as session context; and drops you into the usual
cfassist TUI. The session starts already knowing what happened, so the first
thing you type is "what do we do", not "what happened".

Other shapes:

```bash
cfassist attach 1889 --print              # briefing only, no session
cfassist attach 1889 "did the pod recover?"   # one-shot with the briefing seeded
cfassist attach '#1889'                   # a pasted id
cfassist attach http://cfop:8083/investigations/1889   # a pasted URL
```

`--print` exists because the briefing is the product and cfassist is only the
first vehicle for it. Pipe it into whatever agent you actually use. It makes the
API calls and nothing else — no LLM connection is needed, so it works on a box
that has no model configured.

### Two things the briefing tells you about itself

**Learning provenance depends on the search mode.** The briefing marks knowledge
base learnings produced by *this* investigation with a `*`. That is only
possible in `fts` mode: the hybrid (vector + FTS) SQL path does not select
`investigation_id`, so in `hybrid` mode nothing can be attributed and the
briefing says so instead of implying the absence of a `*` means "not from this
investigation". This is not hypothetical — attaching to investigation #2204
returned three learnings from #2204 itself that could not be marked. The header
always names the mode it got.

**An empty report is stated, not hidden.** An investigation that stored no final
response is a known local-model failure mode, so the briefing prints
`(no report recorded …)` rather than an empty section — a broken investigation
should not look like a boring one.

If the remediation queue or the knowledge base is unreachable, `attach` still
briefs you and lists what it could not fetch under **Incomplete briefing**.
Degrading visibly beats failing totally when you are mid-incident.

### Setup

`attach` talks to the console API from wherever you are, using the same
database-backed bearer token everything else does. There is no separate
credential.

0. Install the binary, if you have not:

   ```bash
   gh release download cfassist-v0.7.1 -R aachtenberg/cfoperator \
     --pattern 'cfassist-linux-arm64'
   chmod +x cfassist-linux-arm64
   sudo mv cfassist-linux-arm64 /usr/local/bin/cfassist
   ```

   (Pick your platform: `linux-amd64`, `linux-arm64`, `linux-arm`,
   `darwin-amd64`, `darwin-arm64`. Or build from source: `cd cfassist-go &&
   make build`.)

1. Mint a token at `<console>/admin?tab=tokens`. **`read` scope is enough** —
   attach never writes (see below).
2. Point cfassist at the agent, either by environment:

   ```bash
   export CFOP_AGENT_URL=http://127.0.0.1:8083
   export CFOP_API_TOKEN=cfop_…
   ```

   …or per-invocation with `--agent-url`:

   ```bash
   cfassist attach --agent-url http://127.0.0.1:8083 1889
   ```

   > **Not `--url`.** The global `--url` flag is the *LLM* endpoint. Pointing it
   > at the agent — natural enough against a port-forward — sends prompts to
   > CFOperator as if it were a model server, and fails confusingly rather than
   > loudly. Precedence is `--agent-url` → config file → `CFOP_AGENT_URL`.

   …or in `~/.cfassist/config.yaml`:

   ```yaml
   cfoperator:
     url: http://127.0.0.1:8083
     token: ${CFOP_API_TOKEN}
     timeout: 30
   ```

   Config values win; the environment fills in whatever the file leaves blank.
   These are the same variable names `mcp_server` reads, so a workstation
   already set up for MCP needs nothing new.

3. If the agent is in-cluster, `:8083` is firewalled to cluster sources (see
   [mcp-server.md](mcp-server.md)); from a workstation use
   `kubectl -n apps port-forward svc/cfoperator 8083:8083`.

### attach is read-only, and structurally so

The client refuses any HTTP method that is not GET, in the transport itself
(`cfassist-go/internal/cfoperator/client.go`, `allowedMethods`) rather than by
convention. Approving, rejecting, resolving or reclassifying a remediation is a
deliberate human action in the console — an attached session must not be able to
reach for it even by accident. The briefing tells the model this too, so it
recommends those actions rather than claiming to have taken them.

A contributor who adds a mutating helper fails a test before they can ship it,
from both sides: the Go transport test asserts a non-GET never reaches the
socket, and `test_cockpit_attach_contract.py` asserts the allowlist itself has
not grown.

Short-lived per-investigation tokens (so an attach can be scoped and expire) are
CFOP-32; today `attach` presents your own console token.

## 3. Any MCP client as the cockpit

cfassist is the first-class vehicle, not the only one. CFOperator briefs *any*
agent, and MCP is the neutral contract: the live MCP server exposes the same
investigations, remediations and knowledge base as tools, resources and skill
prompts.

Copy [`examples/cfoperator.mcp.json`](examples/cfoperator.mcp.json) to `.mcp.json`
in your working directory (or merge the `cfoperator` entry into your host's
config), keep the stdio *or* the HTTP entry, and delete the other. Then:

```
> read cfop://investigations/1889 and tell me what the agent already checked
```

For Claude Code specifically, the equivalent one-liners:

```bash
# stdio, against a port-forwarded agent
claude mcp add cfoperator -e CFOP_AGENT_URL=http://127.0.0.1:8083 -- python -m mcp_server

# HTTP, against the deployed MCP server
claude mcp add --transport http cfoperator http://10.0.0.14:8090/mcp \
  --header "Authorization: Bearer $(kubectl -n apps get secret cfoperator-mcp -o jsonpath='{.data.token}' | base64 -d)"
```

Tools, resources, prompts, the scope ladder and the per-token scope semantics
are all documented in [mcp-server.md](mcp-server.md) — this page does not
restate them.

**Keep `remediate` out of a shared host config.** `read` works an incident;
`investigate` adds triage and `ask_sre`; `remediate` hands an agent the approve
button. If you need a remediate-capable consumer, give it its own deployment,
its own token and its own scope rather than widening the shared one.

## What v0 deliberately is not

- **No spawn.** `attach` runs wherever you run it. It does not start a pod, a
  container or a session on the affected host — that is CFOP-35 / CFOP-36.
- **No write-back.** What you and the agent work out in the session does not
  land back on the investigation (CFOP-37). Today, if it matters, put it in the
  console.
- **No scoped tokens.** CFOP-32; see above.
- **No deep links.** Copy-paste is the interface.
