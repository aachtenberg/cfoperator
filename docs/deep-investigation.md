# Deep-Investigation Tier (ephemeral forensics Jobs)

The escalation tier above the agent's HTTP investigate loop. When triage
escalates a **host-shaped** alert (node/instance, no pod label) — or
investigates one with low confidence — the event runtime launches a
Kubernetes Job running headless Claude Code that SSHes into the affected
host, performs read-only forensics (journalctl, dmesg, disk health), and
posts its report back through the normal completion path. A host's
unclean-reboot alert goes there directly, without triage
([Boot forensics](#boot-forensics)).

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
       details.boot_forensics on a host alert → deep_investigate, no triage call
       host-shaped escalate, or investigate w/ 0 < confidence < threshold
       → action=deep_investigate, params.deep_context = prior-tier findings
       any other escalate → escalate_fallback_action (default notify)
  └─ DeepInvestigationActionHandler
       guards: fingerprint dedupe → concurrency cap → daily budget
       → kubectl create Job → quiet launch
            ▼
  Job pod (ghcr.io/aachtenberg/cfoperator-worker)
       worker/entrypoint.py: template → claude -p (read-only allowlist)
       ├─ POST agent /v1/deep-investigations            (row now → investigation_id;
       │                                                 KB + embeddings + PR gates after)
       └─ POST /v1/investigations/{alert_id}/complete   (loud notification,
                                                         details.investigation_id)
```

The ingest goes first so the completion can carry the investigation the agent
created: that id is what gives the Events page its link and Slack its
`cfassist attach` line (CFOP-216). It gets a short budget (2 × 10s) because it
now delays the notification, and any failure, including an agent that is down,
posts the completion without the id rather than not at all. The investigation
row stores the alert's `alert_id`, so its drawer links back to `/events#<id>`.

Key modules: `event_runtime/deep_investigation.py` (routing + handler),
`worker/` (image + entrypoint + templates),
`agent/remediation.py:open_pr_from_diff` (diff→PR through existing gates),
`web_server.py:/v1/deep-investigations` (KB ingest).

## Safety model

- **Read-only in the cluster by construction, on the host by the host**:
  dedicated SSH key (no-forwarding options, one-line revocation),
  `--permission-mode dontAsk` with an allowlist of `Bash(ssh *)` /
  `kubectl get|describe|top` / `Read`, and a worker ServiceAccount with zero
  write verbs and no secrets/configmaps access. The allowlist bounds kubectl,
  not the host: `Bash(ssh *)` lets the CLI run any command over SSH. What keeps
  the host read-only is the account `ssh_user` names, which should hold
  read-only sudo at most (this project's homelab gives it the `cfop-forensics`
  sudoers drop-in from homelab-infra), backed by the template's instructions
  (`worker/templates/host-forensics.md`). The worker runs `ssh` itself, not the
  agent's `ssh_execute` tool, so the agent's command checks and its `sudo -n`
  rewrite (CFOP-437, `tools/ssh.py`) do not apply to it.
- **Every mutation is a human-merged PR**: a ```diff block in the report
  flows through `RemediationProposer.open_pr_from_diff` — same secret-path
  refusal, branch dedupe, and shared `max_open_prs=3` cap as taint
  remediation. Gated by `remediation.deep_open_prs` (default false).
- **Nothing drops silently**: dedupe is quiet, but concurrency/budget
  deferrals and launch failures page loudly (`outcome=needs_action/failed`).
  Worker crashes still post a `success=False` completion. The only blind
  spot is a hard pod kill (OOM/activeDeadline); the Job stays visible 6h.
- **Cost caps**: `max_concurrent=2`, `daily_budget=10` launches/day
  (persisted), `activeDeadlineSeconds=900`, and the model: `claude-opus-4-8`
  unless `CFOP_DEEP_MODEL` says otherwise. A decision engine can override the
  model and template for one alert with `deep_model` / `deep_template` in the
  decision's params; the boot-forensics route sets the template, and nothing
  built in sets the model.

## Enabling

The tier is off by default in code (`enabled: false`) and is turned on per
deployment with `CFOP_DEEP_INVESTIGATION_ENABLED=true` on the event runtime, or
`event_runtime.deep_investigation.enabled`. Two more settings are required:
`completion_base_url` (`CFOP_DEEP_COMPLETION_BASE_URL`), without which the tier
stays off and a warning is logged at startup, and `ssh_user`
(`CFOP_DEEP_SSH_USER`), which has no default (CFOP-137) and without which every
launch fails loudly, naming the setting.

The Job also needs, in its namespace: the read-only `cfoperator-worker`
ServiceAccount and its RBAC, the worker image (CI builds `cfoperator-worker:main`
on push to main), the secret named by `secrets_name` (the Anthropic key and the
completion secret), and the SSH key secret `cfop-forensics-ssh`, whose public
half is installed for `ssh_user` on every target host.

1. **Distribute the forensics pubkey.** This project's homelab does it once,
   from homelab-infra:
   `ansible-playbook -i inventory.yml ansible/deploy-cfop-forensics-key.yml`
2. **Turn the tier on** with the settings above and restart the event runtime.
3. **Smoke test** — post a synthetic host-shaped alert:
   ```bash
   curl -s -X POST http://cfoperator-event-runtime.apps.svc.cluster.local:8080/alert \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $CFOP_RUNTIME_TOKEN" \
     -d '{"summary":"smoke: deep investigation of raspberrypi5","severity":"critical",
          "source":"manual","resource_type":"node","resource_name":"raspberrypi5",
          "details":{"labels":{"node":"raspberrypi5"},"host":"raspberrypi5"}}'
   ```
   Then verify, in order: `decision_made` audit event shows
   `action=deep_investigate` + `deep_context`; `kubectl get jobs -n apps
   -l cfop.dev/role=deep-investigation` shows the Job; ntfy/Slack carries
   the report + recommendation on completion; the agent KB has a
   `[deep] ...` investigation row with an embedding.
4. **Burn-in**: `remediation.deep_open_prs` defaults to false, so on a new
   install proposed diffs appear inside notifications only. Leave it off for
   several runs. When the diffs look trustworthy, set it in the agent's config
   and verify one PR lands on a `cfop/remediate-deep-*` branch through the
   gates. It is a remediation flag, so it takes effect only under
   `profile: remediate` or with no `profile:` key; `profile: investigate`
   forces it off ([config-reference.md](config-reference.md#profiles)).

## Tuning

All knobs live in `event_runtime.deep_investigation` (config.yaml) with
`CFOP_DEEP_*` env overrides; every key and its override is in
[config-reference.md](config-reference.md). Notables:
`routing.confidence_threshold` (default 0.4; below this, an `investigate`
verdict on a host alert goes deep), `default_model` (`claude-opus-4-8` by
default — drop to a Sonnet or Haiku model to trade depth for cost),
`routing.escalate_fallback_action` (default `notify`, for an `escalate` verdict
on anything not host-shaped), `default_template`
(templates bake into the worker image under `worker/templates/`),
`allowed_tools` (optional subset of the worker's read-only ceiling; omit
to keep ssh + kubectl get/describe/top + Read). `CFOP_DEEP_ALLOWED_TOOLS`
wins over the YAML key — present even empty refuses every tool; omit the
env var to honour YAML / the ceiling.

## Boot forensics

An unclean reboot needs no triage: it has already happened, and the useful
response is always prior-boot forensics, so an LLM classification would add
latency and no signal. A host-shaped alert carrying
`details.boot_forensics: true` skips the triage call and goes straight to
`deep_investigate` with confidence 1.0 and the `boot-forensics` template
(`worker/templates/boot-forensics.md`: `journalctl -b -1`, pstore, and the
rest). The alert's `previous_boot_id`, `detected_at` and `marker_state` ride
along as the prior-tier findings. `routing.route_boot_forensics`
(`CFOP_DEEP_ROUTE_BOOT_FORENSICS`, default true) turns the route off. Shipped
2026-06-11 (#42), the same day as the tier.

The alert comes from the hosts, outside this repo. In this project's homelab,
homelab-infra's `ansible/deploy-boot-forensics.yml` installs a systemd pair: a
clean shutdown leaves a marker, and a boot without one POSTs the synthetic
alert with the runtime's bearer token, retrying while the cluster comes back.

## Watching the runtime itself

The tiers above all run in the cluster, so none of them can report the
cluster's monitoring being down. Two signals cover the runtime (CFOP-152).
`cfoperator_event_runtime_last_poll_timestamp_seconds` advances only on a
successful poll, and `CFOperatorEventRuntimePollStalled`
(`observability/event-runtime-alert-rules.yml`) fires when it goes stale. For
the failure that takes Prometheus and Alertmanager down with the runtime,
`event_runtime.heartbeat` (`event_runtime/heartbeat.py`) pushes to a URL after
every clean poll cycle, and the endpoint alerts on silence. It is off until
`url` is set, and the receiver is the operator's to run (healthchecks.io, an
Uptime Kuma push monitor, a cron on a box outside the cluster); the keys are in
[config-reference.md](config-reference.md).
