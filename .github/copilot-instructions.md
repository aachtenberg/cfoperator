# CFOperator Copilot Instructions

## Project Overview
CFOperator is an AI-powered SRE agent that monitors infrastructure, investigates alerts, and provides intelligent remediation suggestions. It uses an OODA loop (Observe, Orient, Decide, Act) for continuous infrastructure monitoring.

## Tech Stack
- **Language**: Python 3.11+
- **Framework**: FastAPI (web_server.py), LangGraph (agent workflows)
- **LLM**: Ollama (local) with internal fallback chain (cooldown-tracked) + optional paid escalation (Groq / xAI / Anthropic). `llm-gateway/` is a standalone sibling artifact — the agent does NOT route through it.
- **Database**: PostgreSQL with pgvector for embeddings
- **Deployment**: k3s cluster reconciled by ArgoCD GitOps (NOT Docker in production)

## Architecture

Two processes split responsibilities along a clean HTTP boundary; they share the same `ghcr.io/aachtenberg/cfoperator` image with different `command:` args.

- **`agent`** (chat UI + OODA loop): owns the LLM-driven investigations, the knowledge base + embeddings, the proactive deep sweep, the chat UI. Exposes `POST /v1/investigate` (event_runtime calls it) and chat endpoints on `:8083`.
- **`event_runtime`** (alert ingestion + decisions): polls Alertmanager, applies dedupe/cooldown, asks the agent's LLM to triage each alert into `log_only` / `notify` / `investigate` / `escalate`, dispatches actions, owns Slack/Discord. On `investigate` / `escalate` it POSTs to the agent's `/v1/investigate`. When the investigation finishes, the agent POSTs back to `/v1/investigations/{alert_id}/complete` (authenticated via `X-CFOP-Token` from `CFOP_COMPLETION_SHARED_SECRET`), and event_runtime fires the single Slack notification with the real outcome.

```
Alertmanager → event_runtime → (triage via agent LLM) →
  investigate → POST /v1/investigate → agent runs LLM investigation →
  POST /v1/investigations/{id}/complete → event_runtime → Slack/Discord
```

## Key Directories
- `agent/` - Core agent logic (OODA loop, LLM integration, knowledge base, fallback chain)
- `event_runtime/` - Alert ingest, dedupe, decisions, action dispatch, notification sinks; runs as its own deployment
- `web_server.py` - FastAPI app: chat REST/WebSocket + `POST /v1/investigate` entry point for event_runtime
- `skills/` - Investigation skills (YAML-defined runbooks)
- `tools/` - Infrastructure interaction tools (SSH, k8s, prometheus, loki, ...)
- `observability/` - Pluggable backends (Prometheus, Loki, Kubernetes, Docker, Slack, Discord)
- `grafana/` - Source-of-truth dashboard JSON (canonical copy lives at `homelab-infra/k3s/base/monitoring/files/grafana-dashboards/`)
- `llm-gateway/` - Standalone sibling artifact (LiteLLM proxy); NOT in the agent's runtime path
- `docs/` - Documentation (see `docs/DEPLOYMENT.md` for the deploy story)

## Deployment (IMPORTANT)

Production is **pure GitOps + image-only**. `git push` is the deploy path — no rsync, no SSH, no manual `kubectl apply` (the hostPath escape hatch was removed 2026-05-28). The full story is in [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md); the short version:

1. **Push to cfoperator/main** → [`.github/workflows/build-cfoperator-main.yml`](workflows/build-cfoperator-main.yml) builds `ghcr.io/aachtenberg/cfoperator:main-<sha7>` for linux/amd64 and pushes to ghcr.
2. **Auto-bump PR opens** on `homelab-infra` (branch `auto/bump-cfoperator-image`) updating the kustomize image override.
3. **Merge that PR.** ArgoCD reconciles `k3s/overlays/production` within ~3 min (`selfHeal: true` reverts manual `kubectl edit`s).
4. **Both pods roll** with the new image (cfoperator and cfoperator-event-runtime share the image, differ by `command:`).

**Before bumping an image tag by hand:** check whether the auto-bump PR is already open with the latest:
```bash
gh pr list --repo aachtenberg/homelab-infra --search "cfoperator in:title" --state open
```

**Workflow `paths-ignore`** (skip the build): `**.md`, `docs/**`, `benchmarks/**`, `cfassist/**`, `cfassist-go/**`, `cfshared/**`, `llm-gateway/**`, `grafana/**`, `observability/**`. Pushes that ONLY touch these don't produce a new image — intentional.

**Force an ArgoCD sync** instead of waiting:
```bash
kubectl -n argocd annotate application homelab-root \
  argocd.argoproj.io/refresh=hard --overwrite
```

## Cluster Architecture
- **Control plane**: `raspberrypi` (192.168.0.167). `kubectl` runs locally against the kubeconfig — no SSH.
- **CFOperator node**: `ubuntu-llm-01` (SSH name) = `headless-gpu` (k3s node name) at 192.168.0.150. GPU taint, hostNetwork on `:8083` for the agent.
- **Namespace**: `apps`
- **Manifests**: [`homelab-infra/k3s/base/apps/cfoperator.yml`](https://github.com/aachtenberg/homelab-infra/blob/main/k3s/base/apps/cfoperator.yml) + [`cfoperator-event-runtime.yml`](https://github.com/aachtenberg/homelab-infra/blob/main/k3s/base/apps/cfoperator-event-runtime.yml)

## Configuration
- **ConfigMap**: cfoperator-config (in cfoperator.yml)
- **Secrets**: cfoperator-secrets (POSTGRES_PASSWORD, API keys)
- **Config template**: config.yaml.example

## Testing
```bash
# Local testing
python -m pytest agent/test_*.py
```

## Version
Current version is tracked in `VERSION` file. Update when releasing.
