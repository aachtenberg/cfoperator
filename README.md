# CFOperator - Continuous Feedback Operator

**v1.0.8** — Autonomous infrastructure monitoring agent with proactive intelligence.

CFOperator runs continuously in the background, monitoring your fleet via an OODA loop (Observe → Orient → Decide → Act), predicting issues before they become alerts, and surfacing insights through a chat UI.

## Architecture

```
CFOperator (Docker container)
├── OODA Loop (Dual-Mode)
│   ├── Reactive: Monitor Alertmanager every 10s
│   ├── Proactive: Deep sweeps every 30min
│   └── Morning: TPS reports at 7-9 AM
│
├── Knowledge Base (ResilientKnowledgeBase)
│   ├── PostgreSQL (persistent storage)
│   └── Offline Buffer: JSON Lines fallback
│
├── Observability (Pluggable)
│   ├── Prometheus
│   ├── Loki
│   ├── Alertmanager
│   └── Docker (local socket)
│
├── LLM Fallback Chain
│   └── Ollama (local) → Groq → Gemini → Anthropic
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

## Example Fleet

| Host | Address | Role | Services |
|------|---------|------|----------|
| primary | 10.0.0.10 | primary | Prometheus, Loki, PostgreSQL |
| worker-1 | 10.0.0.11 | worker | node_exporter, promtail, Docker |
| worker-2 | 10.0.0.12 | worker (CFOperator host) | node_exporter, promtail, Docker |
| worker-3 | 10.0.0.13 | worker | node_exporter, promtail, Docker |
| gpu-host | 10.0.0.14 | gpu | Ollama, Alertmanager |

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

**Chat UI**: `http://<cfoperator-host>:8083`

```
"summary"                          → Overnight TPS report
"Why did immich restart last night?" → Targeted investigation
"Show me worker-1 container status" → Fleet query
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

## cfassist (Go CLI)

A standalone single-binary CLI assistant for SRE and systems administration. Cross-compiles to any platform — no Python or runtime dependencies needed.

```bash
# Download from GitHub Releases
gh release download cfassist-v0.3.0 --pattern 'cfassist-linux-amd64'
chmod +x cfassist-linux-amd64

# One-shot mode
./cfassist "what is my hostname?"

# Interactive TUI
./cfassist

# Pipe mode
journalctl -u nginx --since '1 hour ago' | ./cfassist "summarize errors"
```

See [cfassist-go/](cfassist-go/) for build instructions and source.

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

MIT — see [LICENSE](LICENSE).
