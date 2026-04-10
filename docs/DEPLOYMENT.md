# CFOperator Deployment Guide

**Version**: 1.0.8

## Deployment Location

- **Host**: Your Docker host (any Linux machine)
- **Deploy dir**: Clone the repo or copy files
- **Container**: cfoperator (host network mode)
- **Web UI**: http://localhost:8083
- **Health**: http://localhost:8083/api/health
- **Metrics**: http://localhost:8083/metrics

## Event Runtime Host Deployment

The modular event runtime is deployed differently from the legacy CFOperator container.

- **Mode**: bare-metal host process
- **Bind**: `python3 -m event_runtime --host 0.0.0.0 --port 8080`
- **Health**: `http://<host>:8080/health`
- **Metrics**: `http://<host>:8080/metrics`
- **Systemd unit template**: [deploy/systemd/cfoperator-event-runtime.service](/home/aachten/repos/cfoperator/deploy/systemd/cfoperator-event-runtime.service)
- **Prometheus scrape sample**: [observability/prometheus-event-runtime-scrape.yml](/home/aachten/repos/cfoperator/observability/prometheus-event-runtime-scrape.yml)
- **Alert rules**: [observability/event-runtime-alert-rules.yml](/home/aachten/repos/cfoperator/observability/event-runtime-alert-rules.yml)

The validated rollout path for the event runtime is host-mode first. The legacy Flask application remains the existing containerized/k3s-oriented service.

## Deploy / Rebuild

```bash
cd /path/to/cfoperator
git pull
docker compose down && docker compose build && docker compose up -d
```

## Verify

```bash
# Health check
curl http://localhost:8083/api/health

# Ollama models available
curl http://localhost:8083/api/ollama/models

# Prometheus metrics
curl http://localhost:8083/metrics | grep cfoperator

# Container running
docker ps | grep cfoperator

# Live logs
docker logs -f cfoperator
```

For the event runtime:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics | grep cfoperator_event_runtime
curl -X POST 'http://localhost:8080/alert?mode=sync' \
	-H 'Content-Type: application/json' \
	-d '{"source":"manual","severity":"warning","summary":"deployment smoke"}'
systemctl status cfoperator-event-runtime
journalctl -u cfoperator-event-runtime -f
```

## Prerequisites

### Files required on deploy host (not in git)

| File | Purpose |
|------|---------|
| `.env` | POSTGRES_PASSWORD, LLM API keys |
| `config.yaml` | Host IPs, OODA timing, backend URLs |
| `secrets/.env.secrets` | Grafana Cloud creds (for dashboard upload, optional) |
| `~/.ssh/id_rsa` | SSH keys (mounted into container for fleet access) |

For the event runtime host deployment:

| File | Purpose |
|------|---------|
| `/etc/cfoperator/config.yaml` | Runtime config including `event_runtime.host_observability` |
| `/var/lib/cfoperator/event-runtime/` | Local runtime durability, queue, replay, scheduler, and policy state |

### Infrastructure dependencies

| Service | Default Port | Required |
|---------|-------------|----------|
| PostgreSQL | 5432 | Yes |
| Prometheus | 9090 | Yes |
| Loki | 3100 | Yes |
| Alertmanager | 9093 | Optional |
| Ollama | 11434 | Yes (or configure cloud LLM) |

## What's Running

| Component | Details |
|-----------|---------|
| OODA Loop | Reactive (10s) + Proactive (30min sweeps) |
| Web Server | Flask + Waitress on port 8083 (host network) |
| Tools | Core + 9 SSH + 15 K8s + 4 discovery + function tools |
| Skills | 7 loaded (investigate-host, investigate-container, investigate-pod, investigate-deployment, k3s-cluster-health, why-restart, compare-hosts) |
| LLM | Ollama → Groq → Gemini → Anthropic fallback chain |
| Knowledge Base | PostgreSQL + offline buffer |
| Metrics | /metrics endpoint with Prometheus counters/gauges/histograms |

## Quick Commands

```bash
# Restart
docker compose restart

# Rebuild
docker compose down && docker compose build && docker compose up -d

# Hot-reload config (no restart needed)
curl -X POST http://localhost:8083/api/config/reload

# View sweep activity
docker logs cfoperator 2>&1 | grep -i sweep

# View errors
docker logs cfoperator 2>&1 | grep ERROR

# Upload Grafana dashboard
./grafana/upload-dashboard.sh
```

Event runtime host-mode commands:

```bash
sudo cp deploy/systemd/cfoperator-event-runtime.service /etc/systemd/system/
sudo mkdir -p /etc/cfoperator /var/lib/cfoperator/event-runtime
sudo systemctl daemon-reload
sudo systemctl enable --now cfoperator-event-runtime
sudo systemctl status cfoperator-event-runtime
```

## Docker Compose Notes

- `network_mode: host` — no port mapping, container shares host network
- `~/.ssh:/root/.ssh:ro` — SSH keys mounted for fleet access
- `./config.yaml:/app/config.yaml:ro` — config mounted read-only
- `./skills:/app/skills:ro` — skills mounted read-only
- `/var/run/docker.sock` — local Docker access
- `restart: unless-stopped` — auto-restart on failure/reboot
