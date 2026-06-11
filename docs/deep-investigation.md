# Deep-Investigation Tier (ephemeral forensics Jobs)

The escalation tier above the agent's HTTP investigate loop. When triage
escalates a **host-shaped** alert (node/instance, no pod label) — or
investigates one with low confidence — the event runtime launches a
Kubernetes Job running headless Claude Code that SSHes into the affected
host, performs read-only forensics (journalctl, dmesg, disk health), and
posts its report back through the normal completion path.

This is the first explicit tier handoff in the escalation ladder:

| Tier | What | Cost |
|------|------|------|
| L0 | dedupe, noise policies, dismissal learning | free |
| L1 | one-shot LLM triage (`/v1/triage`) | cheap |
| L2 | agent investigate loop (`/v1/investigate`) | moderate |
| **L3** | **deep-investigation Job (this)** | budgeted |
| L4 | human via ntfy/Slack | you |

## Architecture

```
Alertmanager → event_runtime engine
  └─ EscalationRoutingDecisionEngine (wraps the triage engine)
       host-shaped escalate, or investigate w/ 0 < confidence < threshold
       → action=deep_investigate, params.deep_context = prior-tier findings
  └─ DeepInvestigationActionHandler
       guards: fingerprint dedupe → concurrency cap → daily budget
       → kubectl create Job → quiet launch
            ▼
  Job pod (ghcr.io/aachtenberg/cfoperator-worker)
       worker/entrypoint.py: template → claude -p (read-only allowlist)
       ├─ POST /v1/investigations/{alert_id}/complete   (loud notification)
       └─ POST agent /v1/deep-investigations            (KB + embeddings + PR gates)
```

Key modules: `event_runtime/deep_investigation.py` (routing + handler),
`worker/` (image + entrypoint + templates),
`agent/remediation.py:open_pr_from_diff` (diff→PR through existing gates),
`web_server.py:/v1/deep-investigations` (KB ingest).

## Safety model

- **Read-only by construction**: dedicated SSH key (no-forwarding options,
  one-line revocation), `--permission-mode dontAsk` with an allowlist of
  `Bash(ssh *)` / `kubectl get|describe|top` / `Read`, and a worker
  ServiceAccount with zero write verbs and no secrets/configmaps access.
- **Every mutation is a human-merged PR**: a ```diff block in the report
  flows through `RemediationProposer.open_pr_from_diff` — same secret-path
  refusal, branch dedupe, and shared `max_open_prs=3` cap as taint
  remediation. Gated by `remediation.deep_open_prs` (default false).
- **Nothing drops silently**: dedupe is quiet, but concurrency/budget
  deferrals and launch failures page loudly (`outcome=needs_action/failed`).
  Worker crashes still post a `success=False` completion. The only blind
  spot is a hard pod kill (OOM/activeDeadline); the Job stays visible 6h.
- **Cost caps**: `max_concurrent=2`, `daily_budget=10` launches/day
  (persisted), `activeDeadlineSeconds=900`, model knob `CFOP_DEEP_MODEL`.

## Rollout / enable (current state: SHIPPED but DISABLED)

Prerequisites already in place after the three repos' changes merge:
RBAC + SA + env (cfoperator-deploy), worker image (CI builds
`cfoperator-worker:main` on push to main), `cfop-forensics-ssh`
SealedSecret (homelab-infra).

1. **Distribute the forensics pubkey** (one-time, from homelab-infra):
   `ansible-playbook -i inventory.yml ansible/deploy-cfop-forensics-key.yml`
2. **Flip the flag** in cfoperator-deploy `cfoperator-event-runtime.yml`:
   `CFOP_DEEP_INVESTIGATION_ENABLED: "true"` → push → ArgoCD rolls the pod.
3. **Smoke test** — post a synthetic host-shaped alert:
   ```bash
   curl -s -X POST http://cfoperator-event-runtime.apps.svc.cluster.local:8080/alert \
     -H 'Content-Type: application/json' \
     -d '{"summary":"smoke: deep investigation of raspberrypi5","severity":"critical",
          "source":"manual","resource_type":"node","resource_name":"raspberrypi5",
          "details":{"labels":{"node":"raspberrypi5"},"host":"raspberrypi5"}}'
   ```
   Then verify, in order: `decision_made` audit event shows
   `action=deep_investigate` + `deep_context`; `kubectl get jobs -n apps
   -l cfop.dev/role=deep-investigation` shows the Job; ntfy/Slack carries
   the report + recommendation on completion; the agent KB has a
   `[deep] ...` investigation row with an embedding.
4. **Burn-in**: leave `remediation.deep_open_prs: false` for several runs;
   proposed diffs appear inside notifications only. When the diffs look
   trustworthy, flip it in the cfoperator-config ConfigMap and verify one
   PR lands on a `cfop/remediate-deep-*` branch through the gates.

## Tuning

All knobs live in `event_runtime.deep_investigation` (config.yaml) with
`CFOP_DEEP_*` env overrides — see `config.yaml.example`. Notables:
`routing.confidence_threshold` (default 0.4; below this, an `investigate`
verdict on a host alert goes deep), `default_model` (opus by default —
drop to sonnet/haiku to trade depth for cost), `default_template`
(templates bake into the worker image under `worker/templates/`).

## Future phases (designed, not built)

- **Phase 2 — boot forensics**: systemd oneshot on each host detects an
  unclean reboot at boot and POSTs a synthetic alert; routing sends it to
  this same worker with a `boot-forensics` template. No new machinery.
- **Phase 3 — dead-man's switch**: out-of-band watchdog on ubuntu-itx-01
  (the only non-cluster box) that pages ntfy directly when the event
  runtime's heartbeat goes stale — covers "the monitoring stack itself is
  down", which no in-cluster tier can.
