# CFOperator roadmap & TODO tracker

Single-pane status across the in-flight workstreams. Detail lives in the linked
design docs; this file is the index + open-TODO list.

_Last updated: 2026-06-04._

## Currently: observation window
Three behaviors went live this cycle (noise filter, Job-awareness, dry-run
remediation). **We're observing them against real traffic before building more.**
Watch for:
- faster-whisper / "Unknown restart" class going quiet (Tier-1 working).
- freshet-alerter ↔ ingest "co-failure" insight stops recurring (Job-fix working).
- remediation dry-run proposals are sensible (good declines, plausible patches) —
  this is the trust data for the `open_prs` decision.
- anything *over*-suppressed (a real issue downgraded to monitoring) → tune the
  restart threshold.

## Shipped (live in prod)

### Investigation quality
- Recommendation surfaced on completed investigations (was bare "Resolved").
- `STATUS:`-based outcome classifier + truthful `needs_action` outcome (replaced
  keyword-sniffing that mislabeled healthy pods as resolved).
- **B1** — verify a `resolved` verdict against live pod state; downgrade if still
  broken.

### Noise reduction → [docs/noise-reduction.md](noise-reduction.md)
- **Tier 1** (`ooda.noise`, default-on): early-exit + downgrade for
  recovered-and-healthy runtime alerts. Doubles as the over-investigation guard.
- **Job/CronJob churn no longer read as failure** — kills false "stopped" findings
  and false "co-failure" correlations. _(live: main-0b633cc)_

### Remediation pipeline → [docs/remediation-pipeline.md](remediation-pipeline.md)
- **B2 dry-run** proposer: unschedulable pod → patch candidate or precise decline
  (conservative; declines the adguard-shape host-port traps).
- **B2-live** PR path: locate manifest → branch/commit → open PR (mock-tested +
  read-path smoke-tested against real homelab-infra).
- **Enabled in prod, dry-run** (`remediation.enabled: true, open_prs: false`).

### Infra (homelab, one-offs — done)
- svclb-traefik sysctl churn fixed (+ baked into ansible bootstrap).
- ansible `--disable=servicelb` / topology-comment cleanup.
- adguardhome unconfigured leftover removed.
- LB decision: stay on k3s servicelb (Cloudflare Tunnel makes LB choice moot).

## Open TODOs

| ID | Item | Notes | Priority |
|----|------|-------|----------|
| #11 | Flip remediation `open_prs` | After observation. Needs: one throwaway-PR write smoke test, a global cap on open remediation PRs. Token already capable. | after observe |
| #13 | **Tier 2 noise** — severity→channel + recurrence suppression | Real-time red only for `escalate`/broken-now; route `monitoring`/info/resolved → morning digest. Recurring identical findings notify once until state-change/escalation; honor `acknowledged`/`false_positive`. **This is what stops the real-time pings.** | next build |
| #14 | **Tier 3 noise** — learn endemic noise | Feed `acknowledged`/`false_positive` findings into the KB so the agent pre-classifies known-benign patterns. | later |
| — | Remediation: more fix classes | Resource-limit bumps, image pins (Phase C). Currently only add-toleration. | later |
| — | Remediation: wire `open_pr` into event-runtime decision vocabulary | `_TRIAGE_VALID_ACTIONS` is frozen to 4; not needed while the agent drives off the investigation result. | optional |

## Recommended next (when resuming)
**#13 Tier 2.** Tier 1 + the Job-fix made severity *correct*; Tier 2 makes the
*channel* match it, which is what actually quiets the real-time Slack pings. Then
#11 once the dry-run proposals have proven sensible.

## Deploy reminders
- cfoperator code: push to `main` → CI builds `:main-<sha>` → bumps private
  `cfoperator-deploy` → ArgoCD rolls. (`docs/*` is in `paths-ignore` → no rebuild.)
- Agent config (`remediation`, `ooda.noise`) lives in `cfoperator-deploy`'s
  `cfoperator-config` ConfigMap; a change needs a pod restart to reload.
