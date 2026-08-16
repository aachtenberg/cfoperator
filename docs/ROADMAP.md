# CFOperator roadmap & TODO tracker

Single-pane status across the workstreams. Detail lives in the linked design
docs; this file is the index + open-TODO list.

_Last updated: 2026-08-16._

## Currently

The agent runs the full loop in production: **alert → triage → investigate →
propose a fix → open a GitOps PR → verify after merge**. A human still presses
every merge button; nothing here mutates a running cluster.

Watch for:

- **Remediation PRs** — a qualifying `needs_action` alert produces a PR
  (`cfop/remediate-*`) on the manifest repo. Review/merge or close. If a
  proposal is ever wrong, set `remediation.open_prs: false` to pause.
- **Deep-investigation PRs** — the L3 tier proposes diffs too, under the same
  gates and the same shared cap (`deep_open_prs`).
- Slack stays quiet for low-signal/recurring/dismissed findings; real
  `needs_action` / critical still pages.
- Anything *over*-suppressed (a real issue gone quiet) → raise
  `ooda.noise.recovered_restart_threshold`.

## Shipped (live in prod)

### The investigation loop
- Recommendation surfaced on completed investigations (was bare "Resolved").
- `STATUS:`-based outcome classifier + truthful `needs_action` outcome.
- Verify a `resolved` verdict against live pod state; downgrade if still broken.
- **Deep-investigation tier (L3)** → [docs/deep-investigation.md](deep-investigation.md)
  — a longer, tool-richer pass for findings the standard investigation cannot
  close. Proposes diffs under `deep_open_prs`.

### Noise reduction → [docs/noise-reduction.md](noise-reduction.md)
- **Tier 1** (`ooda.noise`, default-on): early-exit + downgrade for
  recovered-and-healthy runtime alerts, on both the alert and sweep paths.
  Doubles as the over-investigation guard.
- **Job/CronJob churn no longer read as failure** — ephemeral pods filtered from
  the container baseline + correlation.
- **Tier 2c (severity→channel)** — real-time Slack only for act-now classes;
  everything else to the digest.
- **Tier 2d (recurrence suppression)** — recurring identical finding notifies
  once per window (6h; 30m critical); escalations bypass.
- **Tier 3 (learn dismissals)** — `acknowledged`/`false_positive` skip re-notify,
  generalized count-insensitively.
- **node-Ready false-positive suppressor** — metric-misread "all nodes NotReady"
  killed by the ground-truth filter.

### Remediation pipeline → [docs/remediation-pipeline.md](remediation-pipeline.md)
- **Proposer**: unschedulable pod → patch candidate or a precise decline
  (conservative by design; declines the host-port traps that look like a missing
  toleration but are not).
- **Live PR path**: locate manifest → branch/commit → open PR, **human-merge
  gated**, bounded by `max_open_prs`.
- **Remediation queue + executor** — a work queue with feed/reap/drain/verify
  daemons, an operator console at `/remediations`, per-item target repo, and
  counters driving a Grafana funnel. The executor regenerates its own diff via a
  two-pass (file-select → diff) LLM call and is deliberately portable: env in,
  HTTP out, its own LLM backend.
- **Node-action lane (gated SSH)** — shipped in the image, dormant until
  `node_action.enabled`. Pins a higher model floor for host-touching actions.
- **changerecord microservice** ([#80](https://github.com/aachtenberg/cfoperator/pull/90))
  — merged; dormant until the deploy repo wires its URL + secret.

### Console & auth
- **Multi-user console auth** with roles and revocable API tokens; credentials
  required on `:8083`.
- **Admin panel + Account page**; LLM runtime controls with an indicator and
  quick model switcher.
- Shared console header (`ui/nav.js`) across all pages — identity, logout,
  active-section state.
- Investigations and remediations read-APIs + views; an operator can resolve a
  remediation with a note.

### Integrations
- **MCP server** (`mcp_server/`) → [docs/mcp-server.md](mcp-server.md) — phases
  1–3 live: the server facade, prompts + KB search + idempotent enqueue, and an
  `AnthropicRuntime` over MCP with a tool-call audit log. Per-call model
  selection (`ask_sre` backend/model, `claude:` prefix). Local runtime is the
  default; the Anthropic path is flip-ready.
- **Slack bridge** (`bridge/`) — Socket Mode.
- **ntfy sink** for the triage `notify` action.
- **`timescale_query` tool** — read-only SQL over the telemetry TimescaleDB.

### Model selection → [benchmarks/](../benchmarks/)
- `gemma4:26b` is `llm.primary.model` (26B-A4B MoE), chosen on a three-axis
  benchmark: tool-calling, triage classification against the production prompt,
  and latency. `qwen3.8:27b` is the local standby.
- `benchmarks/triage_eval.py` scores a candidate on the *live* triage prompt,
  extracted from `agent/agent.py` at runtime so the harness cannot drift from
  what actually runs.

## Open TODOs

None scheduled. Remaining items are optional:

| Item | Notes | Priority |
|------|-------|----------|
| Remediation: more fix classes | Resource-limit bumps, image pins (Phase C). Currently add-toleration plus what the executor's diff pass can derive. | later |
| Remediation: wire `open_pr` into the event-runtime decision vocabulary | `_TRIAGE_VALID_ACTIONS` is frozen to 4; not needed while the agent drives off the investigation result. | optional |
| Deep tier: surface declines | `deep_open_prs` declines are log-only; they should reach the console. | optional |
| Noise: semantic/cross-resource dismissal learning | Current Tier 3 is deterministic (count-insensitive, per-resource). Embedding-based matching would generalize further but risks over-suppression. | optional |
| Noise: richer suppressed-item digest | Today's "digest" = morning summary + queryable history; a dedicated "what I quieted" roll-up could come later. | optional |
| changerecord: wire up | Merged but dormant — needs URL + secret in the deploy repo. | optional |

Commercialization work is tracked separately in Plane (CFOP-23…40), not here.

## Deploy reminders
- cfoperator code: push to `main` → CI builds `:main-<sha>` → bumps private
  `cfoperator-deploy` → ArgoCD rolls. (`docs/`, `benchmarks/` and friends are in
  `paths-ignore` → no rebuild.)
- Agent config (`remediation`, `ooda.noise`, `llm`) lives in `cfoperator-deploy`'s
  `cfoperator-config` ConfigMap; a change needs a pod restart to reload. Runtime
  flags that exist in the DB are togglable live from the console instead.
