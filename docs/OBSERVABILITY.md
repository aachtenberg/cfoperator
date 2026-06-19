# CFOperator observability & dashboard design

Design for CFOperator's observability surfaces. Goal: replace the single
70-panel "kitchen sink" board with purpose-scoped surfaces, each built on
well-known patterns so each answers a specific question.

## Two surfaces, split by job (NOT one holistic dashboard)

CFOperator observability is deliberately **not** one all-in-one board with
every component on it. Two distinct surfaces, each scoped to what it's good at:

1. **Grafana — observe & alert** (read-only, time-series): golden signals,
   rates, the funnel-over-time, LLM cost, SLOs/paging. A lean *hierarchy*
   (overview + focused drilldowns), never a kitchen-sink board.
2. **In-app operator console — operate & act** (stateful, actionable, domain
   objects): the live remediation worklist + investigations, with
   human-in-the-loop actions. Served by cfoperator's existing web UI (:8083).

Boundary rule of thumb: *a trend or a number you'd graph/alert on → Grafana;
a row you'd read or a button you'd click → console.* Neither tries to be the
other, and there is no single board that tries to be both.

## Principles (the patterns we're applying)

- **Four Golden Signals** (Google SRE) — *Latency, Traffic, Errors, Saturation*
  — the default lens for every subsystem panel group.
- **RED** (Rate, Errors, Duration) — for request/work flows: triage,
  investigations, LLM calls.
- **USE** (Utilization, Saturation, Errors) — for resources: the fleet/hosts,
  queue saturation.
- **Overview + drilldown hierarchy** — one health-at-a-glance board linking to
  focused per-subsystem boards. Explicitly avoids the mega-dashboard anti-pattern.
- **Work-queue observability / Little's Law** (`L = λW`) — for the remediation
  queue: depth, arrival rate λ, service rate μ, age-of-oldest, dead-letter.
- **Funnel / conversion** — the end-to-end agent view: where work enters, where
  it drops off, what fraction reaches resolution.

Each board is a question, not a dump. Top = health/at-a-glance; below = detail.
Drill via dashboard links (tag `cfoperator`) and panel data-links.

## Board hierarchy

| uid | title | pattern | answers |
|---|---|---|---|
| `cfop-overview` | CFOperator — Overview | Golden Signals + funnel | "Is the agent healthy, and is work flowing end to end?" |
| `cfop-remediation` | CFOperator — Remediation | work-queue | "What's in the remediation queue and how is it moving?" |
| `cfop-investigations` | CFOperator — Investigations & Triage | RED + funnel | "What's being triaged/investigated and with what outcomes?" |
| `cfop-llm` | CFOperator — LLM & Cost | RED | "LLM throughput, latency, fallbacks, token cost?" |
| `cfop-fleet` | CFOperator — Fleet | USE | "Host/container resource health" (today's panels, regrouped) |

Common: datasource Prometheus `cf6z7j8gxto1sc` + Loki; tag `cfoperator`;
template vars `$host`, `$namespace`, `$provider`, `$level` where relevant;
cross-board links + Overview row → drilldown data-links.

## Overview board

One row per subsystem, each 3–4 golden-signal stat tiles + a sparkline,
data-linked to its drilldown. Plus the headline **end-to-end funnel**.

End-to-end funnel (stages → backing metric):

| stage | metric | status |
|---|---|---|
| Alerts received | `cfoperator_alerts_received_total` | **add** (or confirm event_runtime) |
| Triaged (by action) | `cfoperator_triage_action_total{action}` | **add** (log_only/notify/investigate/escalate) |
| Investigated | `cfoperator_investigations_total` | have |
| Needs action | `cfoperator_investigations_total{outcome="needs_action"}` | have |
| Remediation queued | `cfoperator_remediation_enqueued_total` | **add** |
| Auto-eligible | `cfoperator_remediation_enqueued_total{eligible="true"}` | **add** |
| PR opened | `cfoperator_remediation_outcome_total{outcome="pr_open"}` | **add** |
| PR merged | `cfoperator_remediation_outcome_total{outcome="resolved"}` | **add** |

Funnel rendered as a bar gauge / stat row showing count + conversion % per stage
(drop-off is the signal).

## Remediation board (work-queue pattern)

- **Tiles:** Queued (auto-eligible) · Awaiting PR merge · Needs-human backlog · Failed (24h)
- **Depth (L):** `cfoperator_remediation_queue{status}` stacked over time
- **Arrival (λ):** `rate(cfoperator_remediation_enqueued_total[…])` by `source` (deep-investigation vs morning-summary) and `class`
- **Service (μ) / outcomes:** `rate(cfoperator_remediation_outcome_total[…])` by `outcome`
- **Auto vs needs-human:** ratio of `enqueued{eligible}`
- **Executor activity:** `cfoperator_remediation_executor_spawned_total{result}`
- **Dead-lease recovery:** `cfoperator_remediation_reaped_total`
- **Age-of-oldest queued** (saturation signal) — needs a gauge or recording rule
- **Logs:** Loki `{namespace="apps",container="cfoperator"} |= "remediation"`
- *(full tier)* **latency:** time-in-queue + time-to-merge histograms (p50/p95)

## Operator console (in-app, :8083) — operate & act

The human-in-the-loop control plane Grafana can't be: render domain objects and
act on them. Extends the existing web UI; not a new service. (This is also the
fix for "I didn't see those in the queue" — there is no remediation read-API
today, so the queue is currently invisible except as Grafana counts.)

Backend (Flask, reuse existing patterns + the chat UI's auth):
- `GET /api/remediations?status=&limit=` — list queue rows **(NEW read API)**
- `GET /api/remediations/<id>` — one row: payload, PR, attempts, history **(NEW)**
- `GET /api/investigations[/<id>]` — exists
- Actions: `POST /v1/remediations/<id>/{approve,reject,reclassify}` **(NEW)**;
  `…/feed-summary`, `…/feed-sweeps` (exist); pipeline flag toggles via
  `kb.set_setting` (queue_feed/drain/verify) **(NEW UI over existing settings)**

Views:
- **Worklist** — queue rows grouped by status (queued / needs-human / pr-open /
  resolved / failed); columns: class, risk, confidence, host, recommendation,
  PR link, age. Filter/sort; row → detail.
- **Detail** — full recommendation + source investigation (drill-in), proposed
  diff, PR status; action buttons (approve / reject / reclassify).
- **Pipeline controls** — feed/drain/verify toggles + "run feed-summary now"
  with current state, replacing the curl + port-forward dance.

Constraints: read/act on domain state only — **no time-series here** (that's
Grafana's job). All mutating actions audited to the KB.

## Metric inventory

**Already emitted** (reuse): `cfoperator_uptime_seconds`,
`cfoperator_ooda_cycles_total`, `cfoperator_sweeps_total{mode}`,
`cfoperator_tool_calls_total{tool_name,result}`,
`cfoperator_investigations_total{outcome}`,
`cfoperator_investigation_queue_depth`, `cfoperator_investigation_postback_total{status}`,
`cfoperator_errors_total`, `cfoperator_monitored_hosts`,
`cfoperator_running_containers`, `cfoperator_llm_requests_total{provider,model,result}`,
`cfoperator_llm_tokens_total{provider,model,type}`, `cfoperator_llm_latency` (histogram),
`cfoperator_llm_errors_total`, `cfoperator_llm_fallbacks_total`,
`cfoperator_embedding_*`, `cfoperator_remediation_queue{status}` (live).

**To add** (agent layer, at existing hook points):

| metric | type | labels | hook |
|---|---|---|---|
| `cfoperator_remediation_enqueued_total` | counter | `source,class,eligible` | feed methods (after `queue_remediation`) |
| `cfoperator_remediation_executor_spawned_total` | counter | `result` | `_drain_remediation_queue` |
| `cfoperator_remediation_outcome_total` | counter | `outcome` | `_reconcile_remediation_prs`, `fail_remediation` path |
| `cfoperator_remediation_reaped_total` | counter | — | `_reap_remediations` |
| `cfoperator_triage_action_total` | counter | `action` | triage decision (for funnel) |
| `cfoperator_alerts_received_total` | counter | — | alert ingest (confirm event_runtime first) |
| *(full)* `cfoperator_remediation_age_seconds` | histogram | — | on terminal transition |
| *(full)* `cfoperator_remediation_time_to_merge_seconds` | histogram | — | on PR merge |

Counters live in `agent/agent.py` (prometheus_client); kept at the agent layer
so `knowledge_base.py` stays metrics-free (no layering violation).

## Provisioning

Dashboards are GitOps'd in `homelab-infra/k3s/base/monitoring/files/grafana-dashboards/`
with a generator entry in that dir's `kustomization.yml`; Grafana's sidecar
reloads on ConfigMap change. New boards = new JSON files + kustomization lines.
The existing `cfoperator-dashboard.json` becomes `cfop-fleet` (regrouped); its
bolted-on remediation tiles move to `cfop-remediation`.

## Rollout phases

Two parallel tracks (Grafana + console); the console track delivers the most
operator value soonest (it's what closes the human-in-the-loop).

**Grafana (observe):**
1. **Instrument** — add the counters above (one PR, cfoperator).
2. **Overview + Remediation** — build both boards; funnel on overview.
3. **Split drilldowns** — carve Investigations/Triage and LLM/Cost out of the
   old board; rename the remainder to Fleet.
4. **Full tier** — latency histograms + age-of-oldest recording rule.

**Console (operate):**
A. **Read API** — `GET /api/remediations[/<id>]` + a Worklist view (makes the
   queue visible). 
B. **Actions** — approve/reject/reclassify + pipeline flag toggles + run-feed.
C. **Drill** — investigation detail + proposed diff + PR status.

## Open questions

- Is there already an `alerts_received` / triage-action counter in event_runtime
  we can reuse for the funnel, or do we add both?
- Age-of-oldest-queued: gauge updated in the loop vs a Prometheus recording rule?
- Keep one combined board with rows, or truly separate dashboards? (Recommend
  separate + tag-linked — cleaner, faster to load, independent iteration.)
