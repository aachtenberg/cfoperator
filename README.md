# CFOperator - Continuous Feedback Operator

**v1.0.8** — Autonomous infrastructure monitoring agent with proactive intelligence.

CFOperator runs continuously in the background, monitoring your fleet via an OODA loop (Observe → Orient → Decide → Act), predicting issues before they become alerts, and surfacing insights through a chat UI.

## Architecture

```
CFOperator (Docker container)
├── OODA Loop (Dual-Mode)
│   ├── Reactive: Monitor Alertmanager every 10s
│   ├── Proactive: Deep sweeps every 30min
│   ├── LLM Judge: Verify findings before reporting
│   └── Morning: TPS reports at 7-9 AM
│
├── Knowledge Base (ResilientKnowledgeBase)
│   ├── PostgreSQL + pgvector (persistent storage + semantic search)
│   ├── Embeddings: nomic-embed-text via Ollama (768 dims, HNSW index)
│   └── Offline Buffer: JSON Lines fallback
│
├── Observability (Pluggable)
│   ├── Metrics: Prometheus / Victoria Metrics / Datadog / InfluxDB
│   ├── Logs: Loki / Elasticsearch / Splunk / CloudWatch
│   ├── Alerts: Alertmanager / PagerDuty / OpsGenie
│   ├── Containers: Kubernetes + Docker (multi-runtime)
│   └── Notifications: Slack + Discord
│
├── LLM Fallback Chain
│   └── Ollama (local) → Groq → Gemini → Anthropic
│
├── Tools
│   ├── Core: prometheus_query (auto-corrects common PromQL), loki_query
│   │         (validates LogQL), docker_list, docker_inspect, store_learning,
│   │         find_learnings, get_sweep_report, web_search, ...
│   ├── SSH (9): execute, check_service, restart_service, get_logs,
│   │           list_services, docker_list, docker_restart, get_system_info, check_port
│   ├── K8s (15): get_pods, get_pod_logs, get_deployments, rollout_restart,
│   │            get_events, get_nodes, get_node_metrics, exec_pod, describe, ...
│   └── Discovery (4): ping_host, verify_ssh, verify_sudo, discover_all_hosts
│
├── Skills (7 investigation workflows)
│   ├── /investigate-host — Systematic host/server investigation
│   ├── /investigate-container — Systematic container investigation
│   ├── /investigate-pod — Kubernetes pod investigation
│   ├── /investigate-deployment — Kubernetes deployment investigation
│   ├── /k3s-cluster-health — Full cluster health check
│   ├── /why-restart — Analyze container restart causes
│   └── /compare-hosts — Compare metrics across fleet
│
└── Web UI (Dark theme, Inter + JetBrains Mono)
    ├── Chat interface (HTTP polling with WebSocket fallback)
    ├── Collapsible sidebar (OODA config, skills, pool toggles)
    ├── LLM backend/model selector with provider fallback toggle
    ├── Sweep findings panel with severity badges
    └── Status bar (connection, uptime, last sweep)
```

## Knowledge Base & Semantic Search

CFOperator learns from every investigation. Findings, root causes, and remediation steps are stored in PostgreSQL and embedded via Ollama (`nomic-embed-text`, 768 dims) into pgvector with an HNSW index for cosine similarity search.

When a new alert fires or a sweep surfaces a finding, the agent queries the knowledge base for similar past incidents — so it can reuse proven remediation steps instead of reasoning from scratch every time.

**Components:**
- **`agent/knowledge_base.py`** — `ResilientKnowledgeBase` wrapping PostgreSQL + pgvector, with offline JSON Lines fallback when the DB is unreachable
- **`agent/embedding_service.py`** — Embedding generation via Ollama's `/api/embeddings`, with in-memory LRU cache and DB-backed cache for cross-session dedup
- **Hybrid search** — combines pgvector cosine similarity with PostgreSQL full-text search (`tsvector`) for best-of-both retrieval

## Sweep Finding Verification (LLM Judge)

Sweep models sometimes hallucinate findings — e.g., reporting "immich-ml container is missing" when `immich_machine_learning` is running fine (name mismatch). To prevent false findings from cascading into false correlations and polluting the knowledge base, a verification step runs after each sweep.

**How it works:**
1. Each sweep phase is required to include an `evidence` field — the specific tool output supporting the finding
2. After dedup, an LLM judge reviews each finding against its evidence
3. Findings where the evidence contradicts the claim, is missing, or has name mismatches are filtered out
4. Only verified findings reach report generation, notifications, storage, and correlation

**Graceful degradation:** If the judge LLM call fails, original findings pass through unmodified.

**Logs:** Look for `"Finding verification: N → M (K filtered)"` to see the judge in action.

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
/investigate-host raspberrypi2      → Host-level investigation
/investigate-container telegraf     → Container investigation
/why-restart immich-ml              → Root cause analysis
/compare-hosts                      → Fleet comparison
```

## Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `/` | Chat UI |
| `/api/health` | Health check + uptime |
| `/api/chat` | HTTP chat API |
| `/api/sweep` | Trigger deep system sweep (POST) |
| `/api/config/reload` | Hot-reload config (POST) |
| `/api/ollama/models` | List available Ollama models |
| `/api/ollama/models/select` | Persist model selection (POST) |
| `/api/qa` | Pending questions (GET/POST) |
| `/metrics` | Prometheus metrics |
| `/ws` | WebSocket chat |

## cfassist (Go CLI)

A standalone single-binary CLI assistant for SRE and systems administration. Cross-compiles to any platform — no Python or runtime dependencies needed.

### Install

```bash
# Download the latest release (pick your platform)
gh release download cfassist-v0.7.1 -R aachtenberg/cfoperator --pattern 'cfassist-linux-arm64'
chmod +x cfassist-linux-arm64
sudo mv cfassist-linux-arm64 /usr/local/bin/cfassist

# Available binaries: linux-amd64, linux-arm64, linux-arm, darwin-amd64, darwin-arm64
```

### Configure

cfassist reads `~/.cfassist/config.yaml` on startup:

```yaml
llm:
  provider: ollama
  url: http://localhost:11434
  model: llama3:8b
  temperature: 0.7
  context_window: 8192

tools:
  bash:
    enabled: true
    timeout: 30
  read_file:
    enabled: true
    max_lines: 500
```

### Usage

```bash
# Interactive TUI
cfassist

# One-shot mode
cfassist "what is my hostname?"

# Pipe mode
journalctl -u nginx --since '1 hour ago' | cfassist "summarize errors"
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--config` | Path to config file (default `~/.cfassist/config.yaml`) |
| `--model` | Override LLM model |
| `--provider` | Select LLM provider by name |
| `--url` | Override LLM endpoint URL |
| `--version` | Show version |

### Build from Source

```bash
cd cfassist-go
make build          # native binary
make linux-arm64    # cross-compile for Pi
make all            # all platforms
```

## Key Files

| File | Purpose |
|------|---------|
| `agent/agent.py` | Main OODA loop, chat handler, tool registry |
| `web_server.py` | Flask + Waitress, REST + WebSocket APIs |
| `ui/index.html` | Single-page chat UI (dark theme, sidebar layout) |
| `agent/knowledge_base.py` | ResilientKnowledgeBase wrapping PostgreSQL + pgvector |
| `agent/embedding_service.py` | Embedding generation via Ollama with LRU + DB cache |
| `agent/llm_fallback.py` | LLM provider chain with cooldown/retry |
| `config.yaml.example` | All URLs, host definitions, OODA timing |
| `tools/` | SSH, K8s, discovery, and core tool implementations |
| `skills/` | Investigation workflow definitions (SKILL.md) |
| `observability/` | Pluggable backends (Prometheus, Loki, Kubernetes, Docker, Slack, Discord) |
| `llm-gateway/` | Go proxy with health-based routing + fallback |
| `benchmarks/` | Inference latency benchmarks (TTFT, tokens/sec) |
| `grafana/` | Dashboard JSON + upload script |

## Documentation

### Getting Started
- [README.md](README.md) — This file (architecture, quick start)
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Deploy checklist and quick commands

### Operations & Monitoring
- [docs/METRICS.md](docs/METRICS.md) — Prometheus metrics reference
- [grafana/README.md](grafana/README.md) — Grafana dashboard guide
- [docs/llm-observability.md](docs/llm-observability.md) — LLM metrics deep dive
- [docs/infrastructure-config.md](docs/infrastructure-config.md) — Fleet configuration

### Benchmarks
- [benchmarks/results.md](benchmarks/results.md) — Ollama inference latency benchmark (TTFT, tokens/sec, GPU stats)
- [docs/ollama-tool-calling-benchmark.md](docs/ollama-tool-calling-benchmark.md) — Multi-host tool calling benchmark

## License

MIT — see [LICENSE](LICENSE).
