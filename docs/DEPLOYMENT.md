# CFOperator Deployment Guide

**Version**: 1.0.8

## Production Deployment

Production deployment is k3s-based. Docker Compose is for local development only.

- Control plane: `raspberrypi` (`192.168.0.167`)
- CFOperator node: `ubuntu-llm-01` / `headless-gpu`
- Namespace: `apps`
- Infra source of truth: [k3s/base/apps/cfoperator.yml](/home/aachten/repos/homelab-infra/k3s/base/apps/cfoperator.yml)
- Event runtime manifest: [k3s/base/apps/cfoperator-event-runtime.yml](/home/aachten/repos/homelab-infra/k3s/base/apps/cfoperator-event-runtime.yml)

## Runtime Layout

Two workloads are deployed in production:

| Workload | Deployment | Service | Port |
|------|---------|---------|------|
| CFOperator API/UI | `cfoperator` | host network | `8083` |
| Event Runtime | `cfoperator-event-runtime` | `cfoperator-event-runtime` | `8080` |

The event runtime runs as a dedicated k3s deployment, not as a Docker Compose service and not as the primary production systemd path.

## How Production Works

Both workloads use a hostPath mount of the checked-out repository on the GPU node, so code changes are picked up from the node filesystem rather than a rebuilt image.

- Repo hostPath: `/home/aachten/repos/cfoperator`
- Shared config source: `cfoperator-config` ConfigMap
- Shared secret source: `cfoperator-secrets`
- Event runtime durable state: `/var/lib/cfoperator/event-runtime`

The live event runtime command is:

```bash
python3 -m event_runtime --host 0.0.0.0 --port 8080 --config /app/config.yaml --poll-interval 30
```

## Production Deploy Flow

### 1. Sync code to the GPU node

Because both deployments mount the repo from the node filesystem, application code changes need to be synced to `ubuntu-llm-01`.

```bash
rsync -av --delete --exclude='.git' --exclude='__pycache__' --exclude='.env' \
	/home/aachten/repos/cfoperator/ aachten@192.168.0.150:/home/aachten/repos/cfoperator/
```

### 1b. Rebuild the local k3s image when dependencies change

HostPath sync updates Python source, but it does not add newly required packages to the running image. If `requirements.txt` changes, rebuild the image on the GPU node and import it into k3s containerd before restarting the pod.

```bash
ssh aachten@192.168.0.150 '
	cd /home/aachten/repos/cfoperator &&
	docker build -t cfoperator-cfoperator:latest . &&
	docker save cfoperator-cfoperator:latest | sudo k3s ctr images import -
'
```

### 2. Sync and apply k3s manifests

Manifest, ConfigMap, deployment, and service changes come from `homelab-infra`.

```bash
rsync -av --delete /home/aachten/repos/homelab-infra/k3s/ \
	aachten@192.168.0.167:~/repos/homelab-infra/k3s/

ssh aachten@192.168.0.167 \
	'sudo kubectl apply -k ~/repos/homelab-infra/k3s/overlays/production/'
```

### 3. Regenerate secrets when secret inputs change

Cluster secrets are managed from `homelab-infra/secrets/.env.secrets` via Ansible.

```bash
cd /home/aachten/repos/homelab-infra/ansible
ansible-playbook deploy-k3s-secrets.yml --skip-tags ghcr
```

Use this when changing values like:

- `POSTGRES_PASSWORD`
- `GITHUB_TOKEN`
- `SLACK_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL`
- LLM API keys

### 4. Restart the workloads

Code-only changes usually require a rollout restart because the deployments mount source from hostPath.

```bash
ssh aachten@192.168.0.167 \
	'sudo kubectl rollout restart deployment/cfoperator -n apps && \
	 sudo kubectl rollout restart deployment/cfoperator-event-runtime -n apps'
```

## Event Runtime Production Details

- Deployment manifest: [k3s/base/apps/cfoperator-event-runtime.yml](/home/aachten/repos/homelab-infra/k3s/base/apps/cfoperator-event-runtime.yml)
- Service name: `cfoperator-event-runtime`
- Health endpoint: `GET /health`
- Metrics endpoint: `GET /metrics`
- History endpoint: `GET /history`
- Scheduled tasks endpoint: `GET /scheduled`
- Activity endpoint: `GET /activity`

The event runtime currently uses:

- local durable outbox on `/var/lib/cfoperator/event-runtime`
- PostgreSQL replay/persistence via `cfoperator-config` + `cfoperator-secrets`
- GitHub integration via `GITHUB_TOKEN` from `cfoperator-secrets`
- APScheduler as the production scheduler backend, defaulting to the runtime PostgreSQL DSN for durable job storage
- spool-backed scheduled alert delivery under `/var/lib/cfoperator/event-runtime/scheduled`

When Python dependencies change, hostPath sync is not enough. Rebuild the `cfoperator-cfoperator` image on the GPU node and import it into k3s containerd before the rollout restart so the running pods can import the updated packages.

## Verify Production

```bash
# Pods
ssh aachten@192.168.0.167 \
	'sudo kubectl get pods -n apps -l app.kubernetes.io/name=cfoperator && \
	 sudo kubectl get pods -n apps -l app.kubernetes.io/name=cfoperator-event-runtime'

# CFOperator logs
ssh aachten@192.168.0.167 \
	'sudo kubectl logs -n apps deployment/cfoperator -f'

# Event runtime logs
ssh aachten@192.168.0.167 \
	'sudo kubectl logs -n apps deployment/cfoperator-event-runtime -f'
```

For runtime endpoint checks from the control plane:

```bash
ssh aachten@192.168.0.167 '
	pod_ip=$(sudo kubectl get pod -n apps -l app.kubernetes.io/name=cfoperator-event-runtime -o jsonpath="{.items[0].status.podIP}") &&
	curl -fsS "http://${pod_ip}:8080/health" && echo &&
	curl -fsS "http://${pod_ip}:8080/metrics" | grep cfoperator_event_runtime
'
```

## Local / Non-Production Modes

### Docker Compose

Docker Compose is still available for local development of the legacy CFOperator service.

```bash
cd /path/to/cfoperator
docker compose down && docker compose build && docker compose up -d
```

### Direct Event Runtime Launch

For local runtime development:

```bash
python3 -m event_runtime --host 0.0.0.0 --port 8080
```

See [docs/event-runtime-quickstart.md](/home/aachten/repos/cfoperator/docs/event-runtime-quickstart.md) for local runtime usage.

### Systemd

There is also a non-k8s systemd unit template for host installs:

- [deploy/systemd/cfoperator-event-runtime.service](/home/aachten/repos/cfoperator/deploy/systemd/cfoperator-event-runtime.service)

That unit is useful for standalone or portable host deployment, but it is not the current production deployment path.

## Prerequisites

### Files and inputs

| File | Purpose |
|------|---------|
| `config.yaml` | local/development config |
| `.env` | local/development secrets |
| `homelab-infra/secrets/.env.secrets` | source of truth for cluster secrets |
| `~/.ssh/id_rsa` | SSH access for fleet operations |

### Infrastructure dependencies

| Service | Default Port | Required |
|---------|-------------|----------|
| PostgreSQL | 5432 | Yes |
| Prometheus | 9090 | Yes |
| Loki | 3100 | Yes |
| Alertmanager | 9093 | Optional |
| Ollama | 11434 | Yes (or configure cloud LLM) |

## Quick Commands

```bash
# Reload manifests
ssh aachten@192.168.0.167 \
	'sudo kubectl apply -k ~/repos/homelab-infra/k3s/overlays/production/'

# Restart only event runtime
ssh aachten@192.168.0.167 \
	'sudo kubectl rollout restart deployment/cfoperator-event-runtime -n apps'

# Restart only CFOperator
ssh aachten@192.168.0.167 \
	'sudo kubectl rollout restart deployment/cfoperator -n apps'

# Upload Grafana dashboard
/home/aachten/repos/cfoperator/grafana/upload-dashboard.sh
```
