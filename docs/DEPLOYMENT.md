# CFOperator Deployment Guide

Production is k3s + ArgoCD GitOps. **A single `git push` is the deploy path** — no rsync, no SSH, no manual `kubectl apply` for application code or manifests.

## Production Layout

| Workload | Manifest | Image | Port |
|----------|----------|-------|------|
| `cfoperator` (agent + chat UI) | [k3s/base/apps/cfoperator.yml](../../homelab-infra/k3s/base/apps/cfoperator.yml) | `ghcr.io/aachtenberg/cfoperator:main-<sha7>` | `8083` (hostNetwork) |
| `cfoperator-event-runtime` | [k3s/base/apps/cfoperator-event-runtime.yml](../../homelab-infra/k3s/base/apps/cfoperator-event-runtime.yml) | `ghcr.io/aachtenberg/cfoperator:main-<sha7>` (same image, different `command`) | `8080` (ClusterIP) |

Both pods are scheduled on `headless-gpu` (k3s name) = `ubuntu-llm-01` = 192.168.0.150. Namespace: `apps`. Control plane runs `kubectl` locally — no SSH needed.

## How a Code Change Reaches Production

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

The build workflow skips on pushes that only touch: `**.md`, `docs/**`, `benchmarks/**`, `cfassist/**`, `cfassist-go/**`, `cfshared/**`, `llm-gateway/**`, `grafana/**`, `observability/**`. Pushes that ONLY touch those paths won't produce a new image — that's intentional (they don't affect what runs in the cluster), but be aware of it if you expect a new tag and none appears.

## Common Change Types

| Change | What ships | What you do |
|--------|------------|-------------|
| Python code in `agent/`, `tools/`, `skills/`, `ui/`, `event_runtime/`, `web_server.py`, `observability/` | New image tag | Push to cfoperator/main → merge auto-bump PR on homelab-infra. |
| `requirements.txt` | New image tag | Same — push to cfoperator/main. |
| `Dockerfile` | New image tag | Same. |
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
