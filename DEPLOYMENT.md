# CFOperator Deployment Guide

**Version**: 1.0.8

## Deployment Location

- **Host**: raspberrypi3 (192.168.0.111)
- **Deploy dir**: `~/repos/cfoperator/`
- **Container**: cfoperator (host network mode)
- **Web UI**: http://192.168.0.111:8083
- **Health**: http://192.168.0.111:8083/api/health
- **Metrics**: http://192.168.0.111:8083/metrics
- **Grafana**: https://aachten.grafana.net/d/cfoperator-fleet/cfoperator-fleet-monitoring

## Deploy / Rebuild

```bash
# On raspberrypi3
cd ~/repos/cfoperator
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

## Prerequisites

### Files required on deploy host (not in git)

| File | Purpose |
|------|---------|
| `.env` | POSTGRES_PASSWORD, LLM API keys |
| `config.yaml` | Host IPs, OODA timing, backend URLs |
| `secrets/.env.secrets` | Grafana Cloud creds (for dashboard upload) |
| `~/.ssh/id_rsa` | SSH keys (mounted into container) |

### Infrastructure dependencies

| Service | Host | Port |
|---------|------|------|
| PostgreSQL | 192.168.0.167 | 5434 |
| Prometheus | 192.168.0.167 | 9090 |
| Loki | 192.168.0.167 | 3100 |
| Alertmanager | 192.168.0.150 | 9093 |
| Ollama | 192.168.0.150 | 11434 |

## What's Running

| Component | Details |
|-----------|---------|
| OODA Loop | Reactive (10s) + Proactive (30min sweeps) |
| Web Server | Flask + Waitress on port 8083 (host network) |
| Tools | 18 registered (4 core + 9 SSH + 4 discovery + function) |
| Fleet | 5 hosts (raspberrypi, pi2, pi3, pi4, ollama-gpu) |
| Skills | 3 loaded (investigate-container, why-restart, compare-hosts) |
| LLM | Ollama → Groq → Gemini → Anthropic fallback chain |
| Knowledge Base | PostgreSQL (sre_knowledge) + offline buffer |
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

## Docker Compose Notes

- `network_mode: host` — no port mapping, container shares host network
- `~/.ssh:/root/.ssh:ro` — SSH keys mounted for fleet access
- `./config.yaml:/app/config.yaml:ro` — config mounted read-only
- `./skills:/app/skills:ro` — skills mounted read-only
- `/var/run/docker.sock` — local Docker access
- `restart: unless-stopped` — auto-restart on failure/reboot
