# CFOperator - Continuous Feedback Operator

**v1.0.8** — Autonomous infrastructure monitoring agent with proactive intelligence.

CFOperator runs continuously in the background, monitoring your homelab fleet via an OODA loop (Observe → Orient → Decide → Act), predicting issues before they become alerts, and surfacing insights through a chat UI.

## Architecture

```
CFOperator (Docker container on raspberrypi3)
├── OODA Loop (Dual-Mode)
│   ├── Reactive: Monitor Alertmanager every 10s
│   ├── Proactive: Deep sweeps every 30min
│   └── Morning: TPS reports at 7-9 AM
│
├── Knowledge Base (ResilientKnowledgeBase)
│   ├── PostgreSQL: 192.168.0.167:5434 (sre_knowledge)
│   └── Offline Buffer: JSON Lines fallback
│
├── Observability (Pluggable)
│   ├── Prometheus (192.168.0.167:9090)
│   ├── Loki (192.168.0.167:3100)
│   ├── Alertmanager (192.168.0.150:9093)
│   └── Docker (local socket)
│
├── LLM Fallback Chain
│   └── Ollama (192.168.0.150) → Groq → Gemini → Anthropic
│
├── Tools (18 registered)
│   ├── Core: prometheus_query, loki_query, docker_list, docker_inspect
│   ├── SSH (9): execute, check_service, restart_service, get_logs,
│   │           list_services, docker_list, docker_restart, get_system_info, check_port
│   └── Discovery (4): ping_host, verify_ssh, verify_sudo, discover_all_hosts
│
├── Skills (3 investigation workflows)
│   ├── /investigate-container — Systematic container investigation
│   ├── /why-restart — Analyze container restart causes
│   └── /compare-hosts — Compare metrics across fleet
│
└── Web UI (Ubuntu Campbell theme)
    ├── Chat interface (WebSocket + HTTP fallback)
    ├── LLM backend/model selector
    ├── Thinking indicator
    └── Pending questions panel
```

## Fleet

| Host | Address | Role | Services |
|------|---------|------|----------|
| raspberrypi | 192.168.0.167 | primary | Prometheus, Loki, PostgreSQL |
| raspberrypi2 | 192.168.0.146 | worker | node_exporter, promtail, Docker |
| raspberrypi3 | 192.168.0.111 | worker (CFOperator host) | node_exporter, promtail, Docker |
| raspberrypi4 | 192.168.0.116 | worker | node_exporter, promtail, Docker |
| ollama-gpu | 192.168.0.150 | gpu | Ollama (6 models), Alertmanager |

## Quick Start

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your host IPs
cp .env.example .env
# Add POSTGRES_PASSWORD and API keys to .env
docker compose up -d
# Access UI: http://localhost:8083
```

## Usage

**Chat UI**: `http://192.168.0.111:8083`

```
"summary"                          → Overnight TPS report
"Why did immich restart last night?" → Targeted investigation
"Show me Pi2 container status"      → Fleet query
/investigate-container telegraf     → Skill execution
/why-restart immich-ml              → Root cause analysis
/compare-hosts                      → Fleet comparison
```

## Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `/` | Chat UI |
| `/api/health` | Health check + uptime |
| `/api/chat` | HTTP chat API |
| `/api/config/reload` | Hot-reload config (POST) |
| `/api/ollama/models` | List available Ollama models |
| `/api/ollama/models/select` | Persist model selection (POST) |
| `/api/qa` | Pending questions (GET/POST) |
| `/metrics` | Prometheus metrics |
| `/ws` | WebSocket chat |

## Key Files

| File | Purpose |
|------|---------|
| `agent.py` | Main OODA loop, chat handler, tool registry |
| `web_server.py` | Flask + Waitress, REST + WebSocket APIs |
| `ui/index.html` | Single-page chat UI (Ubuntu Campbell theme) |
| `knowledge_base.py` | ResilientKnowledgeBase wrapping PostgreSQL |
| `llm_fallback.py` | LLM provider chain with cooldown/retry |
| `config.yaml` | All URLs, host definitions, OODA timing |
| `tools/` | SSH, discovery, and core tool implementations |
| `skills/` | Investigation workflow definitions (SKILL.md) |
| `observability/` | Pluggable backends (Prometheus, Loki, Docker) |
| `grafana/` | Dashboard JSON + upload script |

## Documentation

- [DEPLOYMENT.md](DEPLOYMENT.md) — Deploy checklist and quick commands
- [METRICS.md](METRICS.md) — Prometheus metrics reference
- [grafana/README.md](grafana/README.md) — Grafana dashboard guide
- [docs/llm-observability.md](docs/llm-observability.md) — LLM metrics deep dive

## License

MIT
