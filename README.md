# CFOperator - Continuous Feedback Operator

An intelligent homelab monitoring agent with dual-mode operation:
- **Reactive**: Responds to alerts with LLM-driven investigations
- **Proactive**: Periodic deep sweeps to catch issues before they alert

Built with learnings from SRE Sentinel, inspired by OODA loop principles.

## Key Features

### Dual-Mode OODA Loop
- **Reactive Mode**: Handles firing alerts immediately with intelligent triage
- **Proactive Mode**: Every 30 minutes, sweeps ALL metrics/logs/containers looking for trends

### Pluggable Observability Backends
- **Metrics**: Prometheus (default), VictoriaMetrics, Datadog, InfluxDB
- **Logs**: Loki (default), Elasticsearch, Splunk, CloudWatch
- **Containers**: Docker (default), Kubernetes, Podman
- **Alerts**: Alertmanager (default), PagerDuty, Opsgenie
- **Notifications**: Slack (default), Discord, Email

Simply implement the backend interface - no code changes required!

### Offline Resilience
**CRITICAL**: Agent continues operating when database is unavailable.
- Uses `ResilientKnowledgeBase` with local JSON Lines buffering
- Automatically syncs buffered events when PostgreSQL comes back
- No data loss during outages

### Intelligence
- **LLM Fallback Chain**: Ollama → Groq → Gemini/Claude (proven from SRE Sentinel)
- **Vector Memory**: pgvector + Ollama embeddings for semantic search
- **Learning Extraction**: Automatically extracts patterns from resolved investigations
- **Learning Consolidation**: Merges duplicate learnings to build institutional knowledge

## Architecture

```
┌─────────────────────┐
│  CFOperator Agent   │
│  (Single Process)   │
└──────────┬──────────┘
           │
           ├─► Pluggable Backends
           │   ├─ Prometheus/VictoriaMetrics/Datadog (metrics)
           │   ├─ Loki/Elasticsearch (logs)
           │   ├─ Docker/Kubernetes (containers)
           │   └─ Alertmanager/PagerDuty (alerts)
           │
           ├─► LLM Fallback Chain
           │   ├─ Ollama (local, primary)
           │   ├─ Groq (fast fallback)
           │   └─ Gemini/Claude (backup)
           │
           └─► PostgreSQL + pgvector
               (with offline buffering)
```

## Quick Start

### 1. Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your backend URLs and API keys
```

### 2. Deploy

```bash
# Using Docker Compose
docker-compose up -d

# Or build and run manually
docker build -t cfoperator .
docker run -v ./config.yaml:/app/config.yaml cfoperator
```

### 3. Monitor

```bash
# Check logs
docker logs -f cfoperator

# View investigations in PostgreSQL
docker exec cfoperator-postgres psql -U cfoperator -d cfoperator \
  -c "SELECT id, trigger, outcome, started_at FROM investigations ORDER BY id DESC LIMIT 10;"
```

## Configuration

See [config.yaml.example](config.yaml.example) for full configuration options.

### Swap Backends

Want to use VictoriaMetrics instead of Prometheus?

1. Implement the `MetricsBackend` interface:
```python
# observability/victoriametrics.py
from observability.base import MetricsBackend

class VictoriaMetricsBackend(MetricsBackend):
    def query(self, query: str, time=None):
        # VictoriaMetrics-specific implementation
        ...
```

2. Update config.yaml:
```yaml
observability:
  metrics:
    backend: victoriametrics
    url: http://victoriametrics:8428
```

That's it! No code changes needed in the agent.

## Success Criteria

### Reactive Mode (Alert-Driven)
- ✅ Alert fires → Agent triages → Investigates → Resolves → Extracts learning
- ✅ LLM fallback works (Ollama fails → Groq takes over seamlessly)
- ✅ Learnings extracted and searchable via vector DB

### Proactive Mode (Continuous Intelligence)
- ✅ Every 30 min: Deep sweep queries all metrics, logs, containers
- ✅ Sweep identifies trends before they become alerts (disk filling, memory creep)
- ✅ Sweep report generated with severity (info/warning/critical)
- ✅ Pattern detection: "Pi2 always struggles after Pi3 restarts"

### Offline Resilience
- ✅ Agent continues operating when PostgreSQL is down
- ✅ Events buffered locally to JSON Lines files
- ✅ Automatic sync when database comes back online
- ✅ No data loss during outages

## Proactive Sweep Examples

The agent catches issues before they alert:

- **Slow disk fill**: Sweep detects "/var disk 70% → 75% → 82% over 3 weeks", warns before 90% alert fires
- **Memory creep**: Sweep notices "immich-ml steady 95% memory for 4 days", suggests increasing limit
- **Cross-service patterns**: Sweep finds "redis connection errors always spike when postgres restarts"

## License

MIT

## Credits

- Built with proven components from [SRE Sentinel](https://github.com/aachtenberg/sre-sentinel)
- Inspired by OODA loop principles
- Uses [OpenClaw](https://github.com/wandb/openclaw) memory patterns
