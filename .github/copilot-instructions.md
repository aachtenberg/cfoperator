# CFOperator Copilot Instructions

## Project Overview
CFOperator is an AI-powered SRE agent that monitors infrastructure, investigates alerts, and provides intelligent remediation suggestions. It uses an OODA loop (Observe, Orient, Decide, Act) for continuous infrastructure monitoring.

## Tech Stack
- **Language**: Python 3.11+
- **Framework**: FastAPI (web_server.py), LangGraph (agent workflows)
- **LLM**: Ollama (local) with fallback to Groq/Anthropic via llm-gateway
- **Database**: PostgreSQL with pgvector for embeddings
- **Deployment**: k3s cluster (NOT Docker in production)

## Key Directories
- `agent/` - Core agent logic (OODA loop, LLM integration, knowledge base)
- `skills/` - Investigation skills (YAML-defined runbooks)
- `tools/` - Infrastructure interaction tools
- `llm-gateway/` - LiteLLM proxy for LLM routing
- `docs/` - Documentation

## Deployment (IMPORTANT)

### Production Deployment is k3s, NOT Docker
CFOperator runs in k3s on the homelab cluster. Docker Compose is for local dev only.

### Deploy to k3s
```bash
# 1. Sync manifests to control plane
rsync -av --delete ~/repos/infrastructure-repo/k3s/ user@10.0.0.1:~/repos/infrastructure-repo/k3s/

# 2. Apply manifests
ssh user@10.0.0.1 'sudo kubectl apply -k ~/repos/infrastructure-repo/k3s/overlays/production/'

# 3. Restart cfoperator pod to pick up code changes (uses hostPath mount)
ssh user@10.0.0.1 'sudo kubectl rollout restart deployment/cfoperator -n apps'

# 4. Check status
ssh user@10.0.0.1 'sudo kubectl get pods -n apps -l app.kubernetes.io/name=cfoperator'

# 5. View logs
ssh user@10.0.0.1 'sudo kubectl logs -n apps deployment/cfoperator -f'
```

### Sync cfoperator code changes
Since cfoperator uses a hostPath volume mount from `/home/user/repos/cfoperator`, sync code to the GPU node:
```bash
rsync -av --delete --exclude='.git' --exclude='__pycache__' --exclude='.env' \
  ~/repos/cfoperator/ user@10.0.0.5:~/repos/cfoperator/
```

## Cluster Architecture
- **Control plane**: raspberrypi (10.0.0.1)
- **CFOperator node**: ubuntu-llm-01 / headless-gpu (10.0.0.5) - GPU taint, hostNetwork
  - k3s node name: `headless-gpu`
  - SSH hostname: `ubuntu-llm-01` or `10.0.0.5`
- **Namespace**: apps
- **Related manifests**: `infrastructure-repo/k3s/base/apps/cfoperator.yml`

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
