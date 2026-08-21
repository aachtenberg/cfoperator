# CFOperator Deployment Guide

Production is k3s + ArgoCD GitOps. **`git push` is the deploy path** — no rsync, no SSH, no manual `kubectl apply`.

## Production Layout

| Workload | Manifest | Image | Port |
|----------|----------|-------|------|
| `cfoperator` (agent + console) | cfoperator-deploy | `cfoperator:main-<sha7>` | `8083` (hostNetwork) |
| `cfoperator-event-runtime` | cfoperator-deploy | same image, different `command` | `8080` |
| `cfoperator-mcp` | cfoperator-deploy | same image, `-m mcp_server` | `8090` |
| `cfoperator-bridge` (Slack) | cfoperator-deploy | same image, `-m bridge`, `Recreate` | — (outbound) |
| `cfoperator-changerecord` | cfoperator-deploy | `cfoperator-changerecord` | ClusterIP |
| `cfoperator-executor` | cfoperator-deploy | `cfoperator-executor` — one Job per remediation | — |
| `cfoperator-worker` | cfoperator-deploy | `cfoperator-worker` — deep investigation | — |
| `cfoperator-cockpit` | none (agent builds the Job at spawn) | `cfoperator-cockpit` — one Job per `attach --spawn` | — |

All manifests live in the private **cfoperator-deploy** repo, which ArgoCD's standalone `cfoperator` Application syncs; the public repo holds no topology. All images are `ghcr.io/aachtenberg/…`. Namespace `apps`; both agent pods on `headless-gpu` = `ubuntu-llm-01` = 10.0.0.14. Control plane runs `kubectl` locally.

`build-cfoperator-main.yml` builds all five images per run, each pushed as floating `:main` and immutable `:main-<sha7>`. **Only the agent tag auto-bumps**; worker/executor/changerecord/cockpit track `:main`, so wait for the build job — there is nothing to merge for them either. The cockpit build `needs:` worker — it derives from it.

Per-workload config: [mcp-server.md](mcp-server.md), [slack-bridge.md](slack-bridge.md), [REMEDIATION.md](REMEDIATION.md).

## Cockpit: one-time deploy-repo changes

`cfassist attach --spawn` is inert until these land. Nothing else depends on them. The Helm chart does the same behind `cockpit.enabled` / `cockpit.ssh.secretName` — see [cockpit.md](cockpit.md).

**Tier 1 (pod)** — `cfoperator-rbac.yml`:

- `cfoperator-cockpit` ServiceAccount + read-only ClusterRole/binding, copied from `cfoperator-worker-readonly` (no exec, no write, no secrets).
- On the existing `cfoperator-jobs` Role: `create` on `secrets`. **`create` only** — `get` would make the launcher a way to read every secret in the namespace; the Job owns the Secret, so GC covers deletion.

Missing → `jobs is forbidden` / `secrets is forbidden` at spawn.

**Tiers 2/3 (non-cluster hosts)** — agent Deployment:

- Mount `cfop-forensics-ssh` at `/cockpit-ssh`, `defaultMode: 0440`. **A staging dir, not `~/.ssh`** — secret volumes are group-readable and ssh refuses such a key with a network-looking error. The agent copies to `~/.ssh` at 0600 on first use.
- `CFOP_COCKPIT_SSH_SECRET_DIR=/cockpit-ssh`
- `CFOP_COCKPIT_SSH_USER=<user>`
- `CFOP_COCKPIT_HOST_AGENT_URL=http://<agent-as-the-fleet-sees-it>:8083` — **required**. `cockpit.agent_url` is cluster DNS and a Pi cannot resolve it, so host tiers 400 until this is set.

Missing → tier 1 still works; host tiers report "could not be probed" and fall back to a cluster pod. Host inventory comes from `infrastructure.hosts` in the agent's config — no change needed.

## What the image contains

The Dockerfile COPYs named paths, not the tree: `cfshared/`, `agent/`, `tools/`, `skills/`, `ui/`, `observability/`, `event_runtime/`, `mcp_server/`, `bridge/`, `auth/`, `scripts/`, `web_server.py`, `web_auth.py`, `cockpit_spawn.py`, `cockpit_ladder.py`.

Most are imported at module load, so a missing COPY crash-loops a pod rather than degrading it — `cfshared/` (agent + event-runtime), `auth/` (agent + MCP), `web_auth.py`, `cockpit_*.py`. `scripts/create_admin.py` is the lockout recovery path ([auth.md](auth.md#locked-out--no-usable-admin)).

`test_dockerfile_image.py` enforces this. **Add a `COPY` for any new top-level package.**

## How a Code Change Reaches Production

[`tests.yml`](../.github/workflows/tests.yml) runs on every PR and push to `main` — the only automated gate; the build workflow runs no tests.

**There is nothing to merge.** Push to `main` and it deploys:

1. [`build-cfoperator-main.yml`](../.github/workflows/build-cfoperator-main.yml) builds and pushes `linux/amd64` images.
2. The `bump-deploy-repo` job commits the new `:main-<sha7>` straight to **cfoperator-deploy**'s `main` (`kustomize edit set image`).
3. ArgoCD syncs within ~3 min (`selfHeal: true` reverts manual `kubectl edit`) and both agent pods roll.

Total: ~5 min from push. A `v*` tag build publishes versioned images but **does not** bump the deploy repo — pointing ArgoCD at a release tag would freeze the fleet on it.

Force a sync instead of waiting:

```bash
kubectl -n argocd annotate application cfoperator \
  argocd.argoproj.io/refresh=hard --overwrite
kubectl -n argocd get application cfoperator
```

`cfoperator` is its own Application (`k3s/base/argocd/applications/cfoperator.yml` in homelab-infra), pointed at cfoperator-deploy — not part of `homelab-root`.

### Workflow `paths-ignore`

Skipped: `**.md`, `docs/**`, `benchmarks/**`, `llm-gateway/**`, `grafana/**`. A push touching only these produces no image — intentional, but worth knowing when you expect a tag and none appears.

**A path may only be ignored if the Dockerfile does not COPY it.** Otherwise the change produces no image and no rollout: the fix appears to deploy, doesn't, and lands later attributed to an unrelated commit. `cfshared/**` and `observability/**` broke this and were removed; `cfassist-go/**` left the list when the cockpit image began baking it in, so a CLI-only change now rebuilds all five. `test_dockerfile_image.py` cross-checks the list against the COPY set.

## Common Change Types

| Change | What ships | What you do |
|--------|------------|-------------|
| `agent/`, `tools/`, `skills/`, `ui/`, `event_runtime/`, `observability/`, `web_server.py` | New agent tag | Push to main. Deploys itself. |
| `auth/`, `web_auth.py`, `scripts/` | New agent tag | Same. |
| `mcp_server/`, `bridge/` | New agent tag | Same — both reuse the agent image. |
| `worker/`, `executor/`, `changerecord/`, `cockpit/`, `cfassist-go/` | New sibling tags | Same push, separate build jobs. These track `:main` — **wait for the build**, don't merge a bump PR (a Job re-queued too early pulls the prior `:main`). |
| `requirements.txt`, `Dockerfile` | New agent tag | Same. A new top-level package needs a `COPY`. |
| cfoperator-deploy YAML (manifests, RBAC) | ArgoCD apply | Push to cfoperator-deploy/main. |
| homelab-infra YAML (everything else in the cluster) | ArgoCD apply | Push to homelab-infra/main. |
| Grafana dashboard | Provisioned ConfigMap | Edit `k3s/base/monitoring/files/grafana-dashboards/`, push. |
| Secret value | Re-sealed SealedSecret | Edit `homelab-infra/secrets/.env.secrets`, run `./scripts/seal-secrets.sh`, push. |

## Verification

```bash
kubectl get pods -n apps -l 'app.kubernetes.io/name in (cfoperator,cfoperator-event-runtime)'

# What image is actually running
kubectl get deploy -n apps cfoperator cfoperator-event-runtime \
  -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.template.spec.containers[*].image}{"\n"}{end}'

kubectl logs -n apps deploy/cfoperator -f

# event-runtime (via pod IP; the agent is hostNetwork)
pod_ip=$(kubectl get pod -n apps -l app.kubernetes.io/name=cfoperator-event-runtime \
  -o jsonpath='{.items[0].status.podIP}')
curl -fsS "http://${pod_ip}:8080/health"
curl -fsS "http://${pod_ip}:8080/metrics" | grep cfoperator_event_runtime
```

## Console Auth

`:8083` authenticates against `auth_users` / `auth_api_tokens` in the KB database — `admin` / `member` roles, revocable tokens with `read` ⊂ `investigate` ⊂ `remediate`. Managed at `/admin` and `/account`.

Tables are created on start; the first admin seeds from the existing `CFOP_UI_USERNAME` / `CFOP_UI_PASSWORD_HASH` sealed secret, so shipping it locks nobody out.

**The shared `CFOP_API_TOKEN` was retired 2026-08-09. Do not re-add it.** event-runtime, MCP and bridge each hold their own database token, mounted *as* that env var, so callers are unchanged and revoking one breaks only that service.

No auth database → legacy env credentials. Configured but unreachable → 503 on every non-exempt route; never 401, never open.

Roles, rollout order and lockout runbooks: [auth.md](auth.md).

## Local / Non-Production

```bash
docker compose up -d                                    # local agent
python3 -m event_runtime --host 0.0.0.0 --port 8080     # event runtime only
```

See [event-runtime-quickstart.md](event-runtime-quickstart.md).

## Prerequisites

| File | Purpose |
|------|---------|
| `config.yaml` | local dev config |
| `.env` | local dev secrets |
| `homelab-infra/secrets/.env.secrets` | source of truth for cluster secrets |
| `~/.ssh/id_rsa` | SSH access for fleet operations |

| Infra service | Port | Required? |
|---------------|------|-----------|
| PostgreSQL | 5432 | Yes |
| Prometheus | 9090 | Yes |
| Loki | 3100 | Yes |
| Ollama | 11434 | Yes (or a cloud LLM) |
| Alertmanager | 9093 | Optional |
