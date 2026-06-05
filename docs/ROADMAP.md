# CFOperator roadmap & TODO tracker

Single-pane status across the in-flight workstreams. Detail lives in the linked
design docs; this file is the index + open-TODO list.

_Last updated: 2026-06-05._

## Currently: all planned work shipped — watch live behavior
The full noise-reduction stack (Tiers 1–3), the correctness fixes, and the
remediation pipeline (now **open_prs LIVE**) are deployed. Nothing queued.

Watch for:
- **Remediation PRs** — when a needs_action unschedulable-pod alert fires, the
  agent opens a PR (`cfop/remediate-*`) on homelab-infra. Review/merge or close.
  If it ever proposes something wrong, set `remediation.open_prs: false` to pause.
- Slack stays quiet for low-signal/recurring/dismissed findings; real
  needs_action / critical still pages.
- anything *over*-suppressed (a real issue gone quiet) → raise
  `ooda.noise.recovered_restart_threshold`, or a finding wrongly PR'd → pause open_prs.

## Shipped (live in prod)

### Investigation quality
- Recommendation surfaced on completed investigations (was bare "Resolved").
- `STATUS:`-based outcome classifier + truthful `needs_action` outcome (replaced
  keyword-sniffing that mislabeled healthy pods as resolved).
- **B1** — verify a `resolved` verdict against live pod state; downgrade if still
  broken.

### Noise reduction → [docs/noise-reduction.md](noise-reduction.md)
- **Tier 1** (`ooda.noise`, default-on): early-exit + downgrade for
  recovered-and-healthy runtime alerts (alert path). Doubles as the
  over-investigation guard. _(live: main-8ee2ea4)_
- **Job/CronJob churn no longer read as failure** — ephemeral pods filtered from
  the container baseline + correlation. _(live: main-0b633cc)_
- **Gap-fixes (#16)** — Tier 1 extended to the **sweep path** (suppress recovered
  restart findings) + **persisted-correlation purge** (clean false rows #15
  couldn't reach). _(live: main-c6712af — verified: purged 15 false rows, sweep clean)_
- **Tier 2c (severity→channel)** — real-time Slack only for act-now classes;
  resolutions/monitoring/resolved/info + correlation insights → digest. _(live)_
- **Tier 2d (recurrence suppression)** — recurring identical finding notifies
  once per window (6h; 30m critical); escalations bypass. _(live)_
- **node-Ready false-positive suppressor** — metric-misread "all nodes NotReady"
  killed by the ground-truth filter. _(live: main-b1c2938)_
- **Tier 3 (learn dismissals)** — `acknowledged`/`false_positive` skip re-notify
  (#17), generalized count-insensitively (#14). _(live)_

### Remediation pipeline → [docs/remediation-pipeline.md](remediation-pipeline.md)
- **B2** proposer: unschedulable pod → patch candidate or precise decline
  (conservative; declines the adguard-shape host-port traps).
- **B2-live** PR path: locate manifest → branch/commit → open PR. Write path
  smoke-tested end-to-end (homelab-infra PR #40, cleaned up).
- **open_prs LIVE** (`enabled: true, open_prs: true, max_open_prs: 3`) — agent
  autonomously opens PRs on qualifying needs_action unschedulable alerts;
  **human-merge gated**. Pause = set open_prs:false + restart.

### Infra (homelab, one-offs — done)
- svclb-traefik sysctl churn fixed (+ baked into ansible bootstrap).
- ansible `--disable=servicelb` / topology-comment cleanup.
- adguardhome unconfigured leftover removed.
- LB decision: stay on k3s servicelb (Cloudflare Tunnel makes LB choice moot).

## Open TODOs

All planned items (#11–#17, Tiers 1–3) are shipped. Remaining are optional
future enhancements, none scheduled:

| Item | Notes | Priority |
|------|-------|----------|
| Remediation: more fix classes | Resource-limit bumps, image pins (Phase C). Currently only add-toleration. | later |
| Remediation: wire `open_pr` into event-runtime decision vocabulary | `_TRIAGE_VALID_ACTIONS` is frozen to 4; not needed while the agent drives off the investigation result. | optional |
| Noise: semantic/cross-resource dismissal learning | Current Tier 3 is deterministic (count-insensitive, per-resource). Embedding-based cross-resource matching would generalize further but risks over-suppression. | optional |
| Noise: richer suppressed-item digest | Today's "digest" = morning summary + queryable history; a dedicated "what I quieted" roll-up could come later. | optional |

## Recommended next
Nothing required — **observe live behavior** (esp. the first real remediation
PRs and overall Slack volume). Pick up an optional item only if a need emerges.

## Deploy reminders
- cfoperator code: push to `main` → CI builds `:main-<sha>` → bumps private
  `cfoperator-deploy` → ArgoCD rolls. (`docs/*` is in `paths-ignore` → no rebuild.)
- Agent config (`remediation`, `ooda.noise`) lives in `cfoperator-deploy`'s
  `cfoperator-config` ConfigMap; a change needs a pod restart to reload.
