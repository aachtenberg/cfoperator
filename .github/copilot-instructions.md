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
- `web_server.py` - Flask app served by Waitress: chat REST + `POST /v1/investigate` entry point for event_runtime (WebSocket code exists but is disabled — Waitress is WSGI)
- `web_auth.py` + `auth/` - Console gate on `:8083`: DB-backed users with `admin`/`member` roles, revocable API tokens with `read` ⊂ `investigate` ⊂ `remediate` scopes (see `docs/auth.md`)
- `mcp_server/` - MCP facade over the agent API; sibling Deployment reusing the agent image (see `docs/mcp-server.md`)
- `bridge/` - Slack Socket Mode bot; sibling Deployment reusing the agent image (see `docs/slack-bridge.md`)
- `executor/` + `changerecord/` - Remediation execution Job and change-record microservice (see `docs/REMEDIATION.md`)
- `worker/` - Deep-investigation worker and forensics templates
- `scripts/` - Operator scripts shipped in the image (`create_admin.py` is the auth lockout recovery path)
- `skills/` - Investigation skills (`skills/<name>/SKILL.md`: YAML frontmatter + markdown playbook; each is also auto-registered as an MCP prompt)
- `tools/` - Infrastructure interaction tools (SSH, k8s, git, GitHub, TimescaleDB, prometheus, loki, ...)
- `observability/` - Pluggable backends (Prometheus, Loki, Kubernetes, Docker, Slack, Discord)
- `grafana/` - Dashboard JSON, a byte-identical mirror of the copies provisioned from `homelab-infra/k3s/base/monitoring/files/grafana-dashboards/` (edit there, copy here; no upload script)
- `llm-gateway/` - Standalone sibling artifact (LiteLLM proxy); NOT in the agent's runtime path
- `docs/` - Documentation (see `docs/DEPLOYMENT.md` for the deploy story)

## Deployment (IMPORTANT)

Production is **pure GitOps + image-only**. `git push` is the deploy path — no rsync, no SSH, no manual `kubectl apply` (the hostPath escape hatch was removed 2026-05-28). The full story is in [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md); the short version:

1. **Push to cfoperator/main** → [`.github/workflows/build-cfoperator-main.yml`](workflows/build-cfoperator-main.yml) builds `ghcr.io/aachtenberg/cfoperator:main-<sha7>` for linux/amd64 and pushes to ghcr.
2. **Auto-bump PR opens** on `homelab-infra` (branch `auto/bump-cfoperator-image`) updating the kustomize image override.
3. **Merge that PR.** ArgoCD reconciles `k3s/overlays/production` within ~3 min (`selfHeal: true` reverts manual `kubectl edit`s).
4. **The image-sharing pods roll** with the new image — `cfoperator`, `cfoperator-event-runtime`, `cfoperator-mcp`, and `cfoperator-bridge` all run the same image and differ only by `command:`. The same workflow also builds `cfoperator-worker`, `cfoperator-executor`, and `cfoperator-changerecord` as separate jobs; those track the floating `:main` tag and are not part of the auto-bump.

Note: adding a new top-level package means adding a `COPY` for it in the `Dockerfile` — `web_server.py` and `mcp_server/server.py` import `auth.bootstrap` at module load, so a missing copy crash-loops the pod rather than degrading it. `test_dockerfile_image.py` guards this.

**Before bumping an image tag by hand:** check whether the auto-bump PR is already open with the latest:
```bash
gh pr list --repo aachtenberg/homelab-infra --search "cfoperator in:title" --state open
```

**Workflow `paths-ignore`** (skip the build): `**.md`, `docs/**`, `benchmarks/**`, `cfassist-go/**`, `cfshared/**`, `llm-gateway/**`, `grafana/**`, `observability/**`. Pushes that ONLY touch these don't produce a new image — intentional.

**Force an ArgoCD sync** instead of waiting:
```bash
kubectl -n argocd annotate application homelab-root \
  argocd.argoproj.io/refresh=hard --overwrite
```

## Cluster Architecture
- **Control plane**: `raspberrypi` (10.0.0.10). `kubectl` runs locally against the kubeconfig — no SSH.
- **CFOperator node**: `ubuntu-llm-01` (SSH name) = `headless-gpu` (k3s node name) at 10.0.0.14. GPU taint, hostNetwork on `:8083` for the agent.
- **Namespace**: `apps`
- **Manifests**: [`homelab-infra/k3s/base/apps/cfoperator.yml`](https://github.com/aachtenberg/homelab-infra/blob/main/k3s/base/apps/cfoperator.yml) + [`cfoperator-event-runtime.yml`](https://github.com/aachtenberg/homelab-infra/blob/main/k3s/base/apps/cfoperator-event-runtime.yml)

## Configuration
- **ConfigMap**: cfoperator-config (in cfoperator.yml)
- **Secrets**: cfoperator-secrets (POSTGRES_PASSWORD, API keys)
- **Config template**: config.yaml.example

## Testing

[`.github/workflows/tests.yml`](workflows/tests.yml) runs the suites on every PR
and on pushes to `main`. It is the only automated gate before an image is built —
the build workflow does not run tests.

The suite can't run as one flat `pytest`: several trees ship a top-level module of
the same name (`nodeaction`, `entrypoint`, `server`), and most directories use bare
imports needing their own directory on `sys.path`. Run one invocation per
directory, as CI does:

```bash
for d in agent tools event_runtime executor changerecord worker mcp_server/tests bridge/tests; do
  PYTHONPATH="$PWD/$d:$PWD" python -m pytest "$d"
done

# observability and auth use absolute imports — repo root only, so their
# docker.py / tokens.py don't shadow real packages
PYTHONPATH="$PWD" python -m pytest observability auth
```

`test_tool_calling.py` needs a live LLM and is excluded from CI.

## Version
Current version is tracked in `VERSION` file. Update when releasing.
