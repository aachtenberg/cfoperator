# LLM Gateway

Lightweight OpenAI-compatible LLM proxy with routing, fallback, and Prometheus observability.

## Features

- **OpenAI-compatible API** - Drop-in replacement for `/v1/chat/completions`
- **Multi-backend support** - Ollama, Groq, Anthropic (auto-translates formats)
- **Automatic fallback** - Falls through backends on failures
- **Health-based routing** - Skips unhealthy backends
- **Prometheus metrics** - Request counts, latency histograms, token usage
- **Async job queue** - Submit long-running requests, poll for results
- **K8s-ready** - Health probes, minimal image (~12MB)

## Quick Start

```bash
# Configure backends
cp config.yaml.example config.yaml
export GROQ_API_KEY=gsk_xxx
export ANTHROPIC_API_KEY=sk-ant-xxx

# Build and run
make build
./llm-gateway
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | OpenAI-compatible chat (routes to backends) |
| `GET /v1/models` | List available models |
| `POST /v1/jobs` | Submit async request |
| `GET /v1/jobs/{id}` | Get job status/result |
| `GET /metrics` | Prometheus metrics |
| `GET /health` | Liveness probe |
| `GET /ready` | Readiness probe |
| `GET /backends` | Backend status |

## Configuration

```yaml
listen: ":4000"

backends:
  - name: ollama-local
    provider: ollama
    url: http://localhost:11434
    model: llama3.1:8b
    enabled: true

  - name: groq
    provider: openai  # Groq uses OpenAI-compatible API
    url: https://api.groq.com
    model: llama-3.3-70b-versatile
    api_key: ${GROQ_API_KEY}
    enabled: true

# Fallback order
fallback:
  - ollama-local
  - groq
```

## Metrics

| Metric | Labels | Description |
|--------|--------|-------------|
| `llm_gateway_requests_total` | backend, model, status | Request count |
| `llm_gateway_request_duration_seconds` | backend, model | Latency histogram |
| `llm_gateway_tokens_total` | backend, model, type | Token usage |
| `llm_gateway_backend_healthy` | backend | Health status (0/1) |
| `llm_gateway_fallbacks_total` | from, to | Fallback events |
| `llm_gateway_jobs_total` | status | Async job counts |
| `llm_gateway_job_queue_size` | - | Current queue depth |

## Kubernetes Deployment

```bash
# Create secrets
kubectl create secret generic llm-gateway-secrets \
  --from-literal=groq-api-key=$GROQ_API_KEY \
  --from-literal=anthropic-api-key=$ANTHROPIC_API_KEY

# Deploy
make docker
make deploy
```

## Usage Example

```bash
# Direct request
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Async job
JOB=$(curl -s -X POST http://localhost:4000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Explain quantum computing"}]}' \
  | jq -r .id)

# Poll for result
curl http://localhost:4000/v1/jobs/$JOB
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     LLM Gateway                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │  /v1/chat/  │  │  /metrics   │  │  Health Checker │  │
│  │ completions │  │  Prometheus │  │  (30s interval) │  │
│  └──────┬──────┘  └─────────────┘  └─────────────────┘  │
│         │                                               │
│  ┌──────▼──────────────────────────────────────────┐    │
│  │              Backend Router                      │    │
│  │  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐ │    │
│  │  │ Ollama │→ │ Ollama │→ │  Groq  │→ │Anthropic│ │    │
│  │  │  LLM01 │  │  RPI4  │  │ Cloud  │  │ Cloud  │ │    │
│  │  └────────┘  └────────┘  └────────┘  └────────┘ │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```
