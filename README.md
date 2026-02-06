# CFOperator - Continuous Feedback Operator

**A specialized autonomous infrastructure monitoring agent with proactive intelligence.**

CFOperator is NOT a replacement for Claude Code CLI. Instead, it's a complementary tool that runs continuously in the background, learning your infrastructure patterns, predicting issues before they become alerts, and surfacing insights when you need them.

## Philosophy

**Two tools, one powerful workflow:**

- **Claude Code CLI**: Your general-purpose system assistant for day-to-day work
  - Installing packages, writing scripts, fixing permissions
  - Git operations, Docker management, debugging
  - Interactive, on-demand assistance

- **CFOperator**: Specialized infrastructure intelligence running 24/7
  - Autonomous OODA loop (Observe → Orient → Decide → Act)
  - Proactive sweeps every 30 minutes to catch trends
  - Morning summaries (TPS report style) of overnight events
  - Infrastructure-specific Q&A when you need it

## Key Features

### 1. Dual-Mode OODA Loop

**Reactive Mode (Alert-Driven)**:
- Monitors Alertmanager for firing alerts
- Triages with LLM (investigate, ignore, escalate)
- Runs autonomous investigations with 50+ tools
- Extracts learnings from resolved issues
- Asks you questions via chat when it needs input

**Proactive Mode (Continuous Intelligence)**:
- Deep sweeps every 30 minutes (configurable)
- Queries ALL metrics looking for trends
- Scans ALL logs for patterns across services
- Checks ALL containers systematically
- Compares current state to baselines
- Detects issues BEFORE they trigger alerts

### 2. Morning Summary (TPS Report Style)

Ask "summary", "report", or "status" any time to get:
- Overnight investigations resolved
- Alerts fired and auto-resolved
- Container restarts across fleet
- Patterns detected
- Metric trends (7-day comparison)
- Learnings extracted
- Recommendations

Auto-delivered 7-9 AM to chat UI + Slack.

### 3. Chat Interface

Terminal-style UI with LLM backend selector, real-time WebSocket, tool execution visibility, and pending questions panel.

### 4. Pluggable Observability Backends

Swap Prometheus→VictoriaMetrics, Loki→Elasticsearch without code changes. Just update config.yaml.

### 5. Investigation Learnings with Vector Memory

Every resolved investigation analyzed by LLM to extract patterns, solutions, root causes. Stored with embeddings for semantic search.

### 6. LLM Fallback Chain

Ollama (local) → Groq → Gemini/Claude with automatic cooldown and retry logic.

## Quick Start

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your URLs and API keys
docker-compose up -d
# Access UI: http://localhost:8083
```

## Usage

**Morning routine**:
```
# In browser: http://pi1:8083
# Type: "summary"
# Get overnight report
```

**Ask questions**:
```
"Why did immich restart last night?"
"Show me Pi2 container status"
```

**Use skills**:
```
/investigate-container telegraf
/why-restart immich-ml
/compare-hosts
```

**Answer pending questions**:
When CFOperator needs input during investigation, question appears in UI. You answer → investigation continues.

## Documentation

See full documentation in this README for:
- Architecture diagrams
- Configuration reference
- Development guide
- Roadmap

## License

MIT
