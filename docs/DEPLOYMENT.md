# CFOperator Deployment Guide

Production is k3s + ArgoCD GitOps. **A single `git push` is the deploy path** — no rsync, no SSH, no manual `kubectl apply` for application code or manifests.

## Production Layout

| Workload | Manifest | Image | Port |
|----------|----------|-------|------|
| `cfoperator` (agent + chat UI) | [k3s/base/apps/cfoperator.yml](../../homelab-infra/k3s/base/apps/cfoperator.yml) | `ghcr.io/aachtenberg/cfoperator:main-<sha7>` | `8083` (hostNetwork) |
| `cfoperator-event-runtime` | [k3s/base/apps/cfoperator-event-runtime.yml](../../homelab-infra/k3s/base/apps/cfoperator-event-runtime.yml) | `ghcr.io/aachtenberg/cfoperator:main-<sha7>` (same image, different `command`) | `8080` (ClusterIP) |
| `cfoperator-mcp` (MCP facade) | cfoperator-deploy repo | same image, `command: ["python", "-m", "mcp_server"]` | `8090` (ClusterIP + hostPort) |
| `cfoperator-bridge` (Slack bot) | cfoperator-deploy repo | same image, `command: ["python", "-m", "bridge"]`, `Recreate` strategy | — (Socket Mode, outbound only) |
| `cfoperator-changerecord` | cfoperator-deploy repo | `ghcr.io/aachtenberg/cfoperator-changerecord` (own image, context `changerecord/`) | ClusterIP |
| `cfoperator-executor` | cfoperator-deploy repo | `ghcr.io/aachtenberg/cfoperator-executor` (own image, context `executor/`) — a disposable Job per remediation, not a Deployment | — |
| `cfoperator-worker` | cfoperator-deploy repo | `ghcr.io/aachtenberg/cfoperator-worker` (own image, context `worker/`) — deep-investigation worker | — |
| `cfoperator-cockpit` | none — the agent builds the Job manifest at spawn time | `ghcr.io/aachtenberg/cfoperator-cockpit` (own image, `FROM` the worker image, built from the repo root with `cockpit/Dockerfile`) — one interactive Job per `cfassist attach --spawn` | — |

Both agent pods are scheduled on `headless-gpu` (k3s name) = `ubuntu-llm-01` = 10.0.0.14. Namespace: `apps`. Control plane runs `kubectl` locally — no SSH needed.

The MCP and bridge Deployments reuse the agent image, so anything that produces a
new agent tag rolls them too. See [mcp-server.md](mcp-server.md),
[slack-bridge.md](slack-bridge.md), and [REMEDIATION.md](REMEDIATION.md) for each
one's own configuration and secrets.

`build-cfoperator-main.yml` builds all five images in one run — agent, worker,
executor, changerecord, cockpit — each pushed as both a floating `:main` and an
immutable `:main-<sha7>`. Only the agent tag is auto-bumped on homelab-infra;
the worker, executor, changerecord and cockpit Deployments/Jobs track `:main`,
so after changing that code you wait for its build job rather than merging a
bump PR. The cockpit build `needs:` the worker build, because it derives from
that image — and because it bakes in `cfassist-go/`, that path is no longer in
`paths-ignore`, so a CLI-only change now rebuilds all five.

### Cockpit RBAC (a manual, one-time deploy-repo change)

`cfassist attach <id> --spawn` (CFOP-35) needs two things the deploy repo does
not have yet, both in `cfoperator-rbac.yml`:

* a `cfoperator-cockpit` ServiceAccount in `apps` with a read-only ClusterRole
  and binding, copied from `cfoperator-worker-readonly` (no exec, no write, no
  secrets, no configmaps) — this is what the pod runs as;
* on the existing `cfoperator-jobs` Role, `create` on `secrets` (jobs are
  already there) — the agent writes the pod's short-lived session token to a
  Secret the Job references. `create` only: `get` would make the launcher a way
  to read every secret in the namespace, and the Secret is owned by the Job, so
  garbage collection removes it without a `delete` grant.

Until both land, the endpoint answers and `kubectl` refuses with
`jobs is forbidden` / `secrets is forbidden` — loudly, at spawn time. The Helm
chart expresses the same thing behind `cockpit.enabled` (see
[cockpit.md](cockpit.md)).

### Cockpit host tiers (a second, independent deploy-repo change)

Tiers 2/3 of the ladder (CFOP-36) put the session on a host that is not in the
cluster, which the agent reaches by **ssh** — so unlike tier 1 it needs a key in
the agent pod. That is a different grant from the RBAC above and is deliberately
separate: one lets the agent create Jobs in its own namespace, this one gives it
a credential to log in to machines with.

In `cfoperator-deploy`, on the agent Deployment:

* mount the existing `cfop-forensics-ssh` secret (the keypair the
  deep-investigation worker and the node-action executor already use) at
  `/cockpit-ssh`, `defaultMode: 0440` — **a staging directory, not `~/.ssh`**: a
  secret volume is root-owned and group-readable, and ssh refuses a private key
  like that with an error that reads like a network problem. The agent copies it
  to `~/.ssh` at 0600 on first use, exactly as the executor does;
* `CFOP_COCKPIT_SSH_SECRET_DIR=/cockpit-ssh` and `CFOP_COCKPIT_SSH_USER=<user>`;
* `CFOP_COCKPIT_HOST_AGENT_URL=http://<agent-as-the-fleet-sees-it>:8083`.
  **Not optional in practice.** `cockpit.agent_url` is what the *pod* calls and
  is cluster DNS; a Pi cannot resolve it, so a host-tier session set up with it
  would attach and then fail to fetch its own briefing. The spawn refuses
  rather than allowing that, so without this variable tiers 2/3 answer with a
  400 naming it.

Nothing else changes: the host inventory is already `infrastructure.hosts` in
the config the agent reads. Until the mount lands, tier 1 keeps working and the
host tiers fail at the probe with `Permission denied (publickey)` — which the
ladder reports as "the affected host could not be probed" and degrades to a pod
in the cluster rather than failing silently.

The chart does the same behind `cockpit.ssh.secretName` (empty by default).

### What the image contains

The Dockerfile copies `cfshared/`, `agent/`, `tools/`, `skills/`, `ui/`,
`observability/`, `event_runtime/`, `mcp_server/`, `bridge/`, `auth/`,
`scripts/`, `web_server.py`, `web_auth.py`, `cockpit_spawn.py`, and
`cockpit_ladder.py`. `cfshared/` holds the config
loader that `agent/` and `event_runtime/` both import at module load, so it has
the same all-or-nothing character as the four below. The last four matter
operationally: `web_server.py` and
`mcp_server/server.py` both import `auth.bootstrap` at module load, so omitting
`auth/` crash-loops the agent and the MCP pod rather than degrading them, and
`scripts/create_admin.py` is the lockout recovery path in
[auth.md](auth.md#locked-out--no-usable-admin) — it has to be in the image to be
usable. `test_dockerfile_image.py` asserts these copies stay present.

## How a Code Change Reaches Production

Before any of this: [`.github/workflows/tests.yml`](../.github/workflows/tests.yml)
runs the pytest suites on every pull request and on pushes to `main`. It is the
only automated gate between a branch and an image — the build workflow does not
run tests. See the README's "Tests & CI" section for how to run the same
per-directory invocations locally.

1. **Push to `main` on cfoperator.** This triggers [`.github/workflows/build-cfoperator-main.yml`](../.github/workflows/build-cfoperator-main.yml), which builds `ghcr.io/aachtenberg/cfoperator:main-<sha7>` for `linux/amd64` and pushes to ghcr.
2. **Auto-bump PR opens** on homelab-infra (branch `auto/bump-cfoperator-image`), editing `k3s/overlays/production/kustomization.yml`'s image override to the new immutable tag. Same PR is updated in place if it's still open from a previous push.
3. **Merge that PR.** ArgoCD picks it up within ~3 min (`selfHeal: true` reverts manual `kubectl edit`s).
4. **Both pods roll** with the new image, since the production overlay's image transformer rewrites `ghcr.io/aachtenberg/cfoperator` for any container that uses that name.

```
git push cfoperator → build workflow → ghcr image → auto-bump PR on homelab-infra
  → merge → ArgoCD sync → both pods restart with new code + deps
```

Force a sync instead of waiting for the 3-min poll:

```bash
kubectl -n argocd annotate application homelab-root \
  argocd.argoproj.io/refresh=hard --overwrite
kubectl -n argocd get application homelab-root  # check Sync + Health
```

### Workflow `paths-ignore`

The build workflow skips on pushes that only touch: `**.md`, `docs/**`, `benchmarks/**`, `cfassist-go/**`, `llm-gateway/**`, `grafana/**`. Pushes that ONLY touch those paths won't produce a new image — that's intentional (they don't affect what runs in the cluster), but be aware of it if you expect a new tag and none appears.

**The rule for this list: a path may only be ignored if it is not baked into the
image.** Ignoring a path the Dockerfile COPYs means a change to it produces no
image, no tag bump and no rollout — the fix appears to deploy and does not, then
lands later attributed to whatever unrelated commit finally triggered a build.

Two entries broke that rule and were removed: `cfshared/**` (added before the
package existed, then COPYed and imported at module load by both the agent and
`event_runtime`) and `observability/**` (COPYed, imported by `agent/agent.py`).
Both were found by review rather than by the pipeline, which is why the rule is
now **enforced** — `test_dockerfile_image.py` cross-checks this list against the
Dockerfile's COPY set and fails if they contradict each other. Add a path here
only if that test still passes.

## Common Change Types

| Change | What ships | What you do |
|--------|------------|-------------|
| Python code in `agent/`, `tools/`, `skills/`, `ui/`, `event_runtime/`, `web_server.py`, `observability/` | New image tag | Push to cfoperator/main → merge auto-bump PR on homelab-infra. |
| `auth/`, `web_auth.py`, `scripts/` | New image tag | Same. Console auth and the `create_admin.py` recovery path ship in the agent image. |
| `mcp_server/`, `bridge/` | New image tag | Same — both Deployments reuse the agent image, so they roll on the same bump. |
| `worker/`, `executor/`, `changerecord/` | New sibling image tag | Same workflow, separate build job. These track the floating `:main` tag — wait for the build job to finish instead of merging a bump PR (a Job re-queued too early pulls the prior `:main`). |
| `requirements.txt` | New image tag | Same — push to cfoperator/main. |
| `Dockerfile` | New image tag | Same. Add a `COPY` for any new top-level package, or it will be missing at runtime. |
| YAML manifest in homelab-infra | ArgoCD apply | Push to homelab-infra/main. Force-sync if impatient. |
| Grafana dashboard JSON | Provisioned ConfigMap | Edit `homelab-infra/k3s/base/monitoring/files/grafana-dashboards/cfoperator-dashboard.json`, push to homelab-infra/main. |
| Secret value | Re-sealed SealedSecret | Edit `homelab-infra/secrets/.env.secrets`, run `./scripts/seal-secrets.sh`, push. |

## Before Manually Bumping an Image Tag

**Check first whether the auto-bump PR is already open:**

```bash
gh pr list --repo aachtenberg/homelab-infra --search "cfoperator in:title" --state open
```

If a `chore(deploy): bump cfoperator to main-<sha>` PR is open, it's the latest available image. Merge it instead of writing manifest edits by hand.

## Verification

```bash
# Pods
kubectl get pods -n apps -l 'app.kubernetes.io/name in (cfoperator,cfoperator-event-runtime)'

# What image is actually running
kubectl get deploy -n apps cfoperator cfoperator-event-runtime \
  -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.template.spec.containers[*].image}{"\n"}{end}'

# Logs
kubectl logs -n apps deploy/cfoperator -f
kubectl logs -n apps deploy/cfoperator-event-runtime -f

# event-runtime endpoints (cluster DNS; or via pod IP if hostNetwork)
pod_ip=$(kubectl get pod -n apps -l app.kubernetes.io/name=cfoperator-event-runtime \
  -o jsonpath='{.items[0].status.podIP}')
curl -fsS "http://${pod_ip}:8080/health"
curl -fsS "http://${pod_ip}:8080/metrics" | grep cfoperator_event_runtime
```

## Console Auth

The `:8083` console authenticates against `auth_users` / `auth_api_tokens` in
the knowledge base database: real accounts with `admin` / `member` roles, and
individually revocable API tokens carrying `read` ⊂ `investigate` ⊂ `remediate`
scopes. Manage both at `/users` and `/tokens`.

The tables are created on start, and the first admin is seeded from the existing
`CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH` sealed secret — so shipping this
does not lock anyone out and does not require a coordinated secret change.

The shared `CFOP_API_TOKEN` was **retired 2026-08-09**. `event_runtime`, `mcp`
and `bridge` each hold their own database token, mounted *as* the env var
`CFOP_API_TOKEN`, so callers are unchanged and revoking one breaks only that
service. There is no plain `CFOP_API_TOKEN` key in `cfoperator-secrets` any
more, and the agent no longer mounts one — with it unset, `web_auth.py` skips
the legacy branch entirely. Do not re-add it.

With no auth database reachable, the console falls back to the legacy
environment credentials. A database that is configured but *unreachable* returns
503 on every non-exempt route — never 401, never open.

See [docs/auth.md](auth.md) for the roles table, the rollout order, and the
lockout / token-rotation runbooks.

## Local / Non-Production Modes

```bash
# Docker Compose for local agent development
docker compose up -d

# Direct event_runtime launch
python3 -m event_runtime --host 0.0.0.0 --port 8080
```

See [docs/event-runtime-quickstart.md](event-runtime-quickstart.md) for local runtime usage.

## Prerequisites

| File | Purpose |
|------|---------|
| `config.yaml` | local dev config |
| `.env` | local dev secrets |
| `homelab-infra/secrets/.env.secrets` | source of truth for cluster secrets |
| `~/.ssh/id_rsa` | SSH access for fleet operations |

| Infra service | Default port | Required? |
|---------------|--------------|-----------|
| PostgreSQL | 5432 | Yes |
| Prometheus | 9090 | Yes |
| Loki | 3100 | Yes |
| Alertmanager | 9093 | Optional |
| Ollama | 11434 | Yes (or configure cloud LLM) |
