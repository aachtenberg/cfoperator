# Event Runtime Quickstart

## Architecture

```mermaid
flowchart TD
    subgraph Intake
        HTTP["POST /alert"]
        Sources["Alert Sources<br/>(plugins)"]
    end

    HTTP --> Worker
    Sources -->|poll| Engine

    subgraph Worker["Background Worker Queue"]
        direction LR
        Enqueue["enqueue()"] --> Q["Job Queue<br/>(file-backed)"]
        Q --> Threads["Worker Threads"]
    end

    Threads --> Engine

    subgraph Engine["Event Runtime Engine"]
        direction TB
        Receive["Receive Alert"] --> Policies
        Policies["Alert Policies<br/>(dedupe, cooldown)"]
        Policies -->|suppressed| Audit
        Policies -->|allowed| Gate
        Gate{"Severity<br/>Gate"}
        Gate -->|INFO| LogOnly["Log Only"]
        Gate -->|WARNING/CRITICAL| Context

        subgraph Context["Context Enrichment"]
            direction LR
            HostCtx["Host Context<br/>(hostname, pid)"]
            BareMetalCtx["Bare-Metal Stats<br/>(local, SSH, Prometheus)"]
            K3sCtx["K3s Cluster Stats<br/>(kube-state-metrics,<br/>cAdvisor)"]
        end

        Context --> Decision["Decision Engine<br/>(open-reasoning)"]
        Decision --> Handler{"Action<br/>Handler?"}
        Handler -->|missing| Fail["Record action_missing"]
        Handler -->|found| Execute["Execute Action<br/>(investigate, notify,<br/>log_only)"]
        Execute --> Schedule["Schedule Follow-up<br/>Tasks (optional)"]
    end

    LogOnly --> Audit
    Fail --> Audit
    Execute --> Audit
    Schedule --> Scheduler["Scheduler<br/>(JSON fallback or APScheduler backend)"]

    subgraph Audit["State Sink (audit trail)"]
        direction LR
        Local["Local Outbox<br/>(JSONL, fsync)"]
        Local -->|replay| Remote["PostgreSQL<br/>(optional)"]
    end

    subgraph Observe["Observability"]
        direction LR
        Metrics["/metrics<br/>(Prometheus)"]
        Health["/health"]
        History["/history"]
        Grafana["Grafana<br/>Dashboard"]
        AlertRules["Alert Rules<br/>(Prometheus)"]
    end

    Audit --> Metrics
    Audit --> History
    Engine --> Metrics
```

### Alert Processing Flow

1. **Intake** — alerts arrive via `POST /alert` (async to worker queue) or from polled alert sources
2. **Policy Gating** — pluggable policies evaluate the alert (e.g., fingerprint-based duplicate suppression with configurable cooldown)
3. **Severity Gate** — INFO alerts are logged without invoking the decision engine
4. **Context Enrichment** — pluggable providers attach investigation context:
   - Host context (hostname, PID)
   - Bare-metal OS stats (local `/proc`, SSH, Prometheus node-exporter)
   - K3s cluster stats (node conditions, pod counts, CPU/memory usage, restart counts via kube-state-metrics + cAdvisor)
5. **Decision** — the decision engine selects an action, confidence score, reasoning, and optional scheduled follow-up tasks
6. **Action Execution** — the matched action handler runs (investigate, notify, log_only)
7. **Scheduling** — follow-up checks are persisted to the scheduler store and polled back into the runtime as synthetic alerts when they become due
8. **Audit** — every step emits append-only domain events to the local JSONL outbox, with background replay to PostgreSQL when configured

## Minimal Setup

The portable event runtime is designed to run on any host with Python 3.11+ and no extra services.

Requirements:

- Python 3.11+

No PostgreSQL, no Prometheus, no Loki, and no pip install are required for the first slice.

## Start

From the repository root:

```bash
python3 -m event_runtime --host 0.0.0.0 --port 8080
```

This is the default zero-dependency mode.

By default the runtime stores data under:

```text
~/.cfoperator/event-runtime/
```

This includes:

- `outbox/` for durable domain events
- `scheduled/` for agent-requested recurring checks and scheduler state

Portable mode also supports pluggable bare-metal host observability so alerts can be enriched with OS stats from local hosts, configured SSH targets, and discovered Prometheus node exporters.

Scheduled follow-up checks are executed by scheduler-backed synthetic alerts. The default fallback scheduler stores task intents in `scheduled/tasks.jsonl` and tracks execution state in `scheduled/state.json`.

For production, the recommended backend is APScheduler. It persists cron jobs in a durable job store and spools fired runs back into the runtime as alerts.

## Scheduler Backends

- `json-file`: zero-dependency fallback; stores tasks in `scheduled/tasks.jsonl` and next-run state in `scheduled/state.json`
- `apscheduler`: recommended production backend; stores cron jobs in a SQLAlchemy-backed job store, defaults to the event runtime PostgreSQL DSN when available, and spools fired runs to `scheduled/apscheduler-fired.jsonl`

YAML example:

```yaml
event_runtime:
  scheduler:
    backend: apscheduler
    # jobstore_url: postgresql://user:password@host:5432/dbname
    misfire_grace_time_seconds: 300
```

## Endpoints

- `GET /health`
- `GET /history?limit=50`
- `GET /scheduled?limit=100`
- `GET /metrics`
- `POST /alert`
- `GET /jobs/<job_id>` when background workers are enabled

## Optional ASGI Mode

If you want FastAPI-style deployment behind uvicorn or gunicorn, install only the adapter dependencies:

```bash
python3 -m pip install fastapi uvicorn prometheus-client
uvicorn event_runtime.fastapi_app:build_app --factory --host 0.0.0.0 --port 8080
```

The runtime core is the same. Only the HTTP adapter changes.

If `prometheus-client` is missing, `GET /metrics` falls back to a placeholder response instead of exporting runtime series.

## Example Alert

```bash
curl -X POST http://127.0.0.1:8080/alert \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "manual",
    "severity": "warning",
    "summary": "pod restart storm",
    "details": {
      "reasoning": "Track this condition and schedule a follow-up monitor.",
      "requested_action": "investigate",
      "requested_checks": ["logs", "metrics"],
      "scheduled_tasks": [
        {
          "name": "watch-pod-restarts",
          "schedule": "*/5 * * * *",
          "rationale": "Repeated restarts need follow-up visibility",
          "target": {"kind": "pod", "namespace": "apps", "name": "api"},
          "parameters": {"check": "restart_rate"}
        }
      ]
    }
  }'
```

## Environment Variables

- `CFOP_EVENT_RUNTIME_DIR`: base directory for all runtime files
- `CFOP_EVENT_RUNTIME_OUTBOX_DIR`: override outbox storage path
- `CFOP_EVENT_RUNTIME_SCHEDULE_DIR`: override scheduled task storage path
- `CFOP_EVENT_RUNTIME_PG_DSN`: optional PostgreSQL DSN for remote event persistence
- `CFOP_EVENT_RUNTIME_REPLAY_INTERVAL_SECONDS`: optional replay interval for syncing outbox events to PostgreSQL
- `CFOP_EVENT_RUNTIME_SCHEDULER_BACKEND`: scheduler backend selection, `json-file` or `apscheduler`
- `CFOP_EVENT_RUNTIME_APSCHEDULER_JOBSTORE_URL`: optional explicit APScheduler SQLAlchemy job store URL
- `CFOP_EVENT_RUNTIME_APSCHEDULER_SPOOL_PATH`: optional path for fired APScheduler runs waiting to be polled into alerts
- `CFOP_EVENT_RUNTIME_APSCHEDULER_MISFIRE_GRACE_SECONDS`: grace window for delayed APScheduler runs, default `300`
- `CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS`: duplicate suppression window in seconds, default `300`, set to `0` to disable
- `CFOP_EVENT_RUNTIME_WORKER_COUNT`: background worker count, default `1`, set to `0` to force synchronous processing
- `CFOP_EVENT_RUNTIME_MAX_QUEUE_SIZE`: max in-memory queued jobs, default `1000`
- `CFOP_EVENT_RUNTIME_QUEUE_STATE_PATH`: persisted worker job state path, default `~/.cfoperator/event-runtime/queue/jobs.json`
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_ENABLED`: enable bare-metal host observability plugins, default `1`
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_JSON`: inline JSON config for bare-metal observability providers
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH`: path to a JSON config file for bare-metal observability providers

The runtime also reads bare-metal host observability config from `config.yaml` when PyYAML is available. It looks for `event_runtime.host_observability` first, then `observability.host_observability`. You can point the runtime at a specific file with `CONFIG_PATH=/path/to/config.yaml` or `python3 -m event_runtime --config /path/to/config.yaml`.

## Bare-Metal Host Observability

The portable runtime does not assume every target is Kubernetes or Docker-backed. You can attach host observability providers that collect OS stats from bare-metal hosts.

Supported provider types:

- `local`: zero-dependency stats from the current host via stdlib and `/proc`
- `ssh`: configured remote hosts over SSH
- `prometheus`: discovered or configured node-exporter targets from Prometheus

Discovery model:

- `local` always discovers the runtime host
- `ssh` discovers the configured host list
- `prometheus` can auto-discover targets with `up{job=~"node-exporter|node_exporter"} == 1`
- discovery is refreshed periodically at `refresh_interval_seconds`, default `300`; set it to `0` to refresh on every alert

Example config file:

```json
{
  "default_to_local": true,
  "include_discovered_targets": true,
  "providers": [
    {"type": "local"},
    {
      "type": "ssh",
      "hosts": {
        "edge-01": {
          "address": "10.0.0.10",
          "ssh": {"user": "cfoperator", "key_path": "~/.ssh/id_ed25519"}
        }
      }
    },
    {
      "type": "prometheus",
      "url": "http://prometheus:9090",
      "discover": true,
      "job_pattern": "node-exporter|node_exporter"
    }
  ]
}
```

Enable it with:

```bash
export CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH="$HOME/.cfoperator/event-runtime/host-observability.json"
python3 -m event_runtime --host 0.0.0.0 --port 8080
```

If an alert includes `details.host`, `details.hostname`, `details.address`, `details.instance`, or `resource_type=host`, the runtime will try to match that host against discovered targets and attach collected OS stats to the alert context.

Repo-native YAML example:

```yaml
event_runtime:
  host_observability:
    refresh_interval_seconds: 300
    default_to_local: true
    include_discovered_targets: true
    providers:
      - type: local
      - type: ssh
        hosts:
          edge-01:
            address: 10.0.0.10
            ssh:
              user: cfoperator
              key_path: ${HOME}/.ssh/id_ed25519
      - type: prometheus
        url: http://prometheus:9090
        discover: true
        job_pattern: node-exporter|node_exporter
```

## Async Intake

By default, if background workers are enabled, `POST /alert` queues the alert and returns immediately.

- default mode with workers: async
- force synchronous processing: `POST /alert?mode=sync`
- inspect job status: `GET /jobs/<job_id>`
- queued jobs are persisted locally and restored on process restart

`GET /health` also exposes worker metrics including:

- `oldest_queued_age_seconds`
- `average_queue_delay_seconds`
- `average_processing_duration_seconds`

Example:

```bash
curl -X POST 'http://127.0.0.1:8080/alert?mode=async' \
  -H 'Content-Type: application/json' \
  -d '{"source":"manual","severity":"warning","summary":"async test"}'
```

## Optional PostgreSQL Persistence

Portable mode does not require PostgreSQL. If you want remote event persistence in addition to the local outbox, set:

```bash
export CFOP_EVENT_RUNTIME_PG_DSN='postgresql://cfoperator:pass@db:5432/cfoperator'
python3 -m event_runtime --host 0.0.0.0 --port 8080
```

Behavior:

- the local outbox remains the success boundary for writes
- PostgreSQL is best-effort at ingest time
- a background replay loop retries syncing outbox events to PostgreSQL
- replay progress is checkpointed locally so successful remotes resume from the last acknowledged outbox cursor instead of replaying the full history every cycle
- duplicate replay is safe because the PostgreSQL table is keyed by `event_id`

## Telemetry

The event runtime exposes Prometheus metrics at:

```text
GET /metrics
```

Key metric families include:

- `cfoperator_event_runtime_alerts_received_total`
- `cfoperator_event_runtime_alert_results_total`
- `cfoperator_event_runtime_alert_processing_seconds`
- `cfoperator_event_runtime_queue_size`
- `cfoperator_event_runtime_queue_rejected_total`
- `cfoperator_event_runtime_queue_wait_seconds`
- `cfoperator_event_runtime_queue_processing_seconds`
- `cfoperator_event_runtime_replay_attempts_total`
- `cfoperator_event_runtime_replay_events_total`
- `cfoperator_event_runtime_host_discovery_runs_total`
- `cfoperator_event_runtime_host_discovered_targets`
- `cfoperator_event_runtime_host_observation_runs_total`

Import [grafana/event-runtime-dashboard.json](/home/aachten/repos/cfoperator/grafana/event-runtime-dashboard.json) into Grafana to observe alert throughput, queue health, replay behavior, scheduled follow-up tasks, and end-to-end latency.

Prometheus alert rules for runtime health, queue stalls, replay failures, and bare-metal host observability failures are provided in [observability/event-runtime-alert-rules.yml](/home/aachten/repos/cfoperator/observability/event-runtime-alert-rules.yml).

Prometheus scrape configuration for the runtime is provided in [observability/prometheus-event-runtime-scrape.yml](/home/aachten/repos/cfoperator/observability/prometheus-event-runtime-scrape.yml).

## Duplicate Suppression

Portable mode enables file-backed duplicate suppression by default.

- alerts with the same fingerprint are suppressed during the cooldown window
- fingerprints are derived from source, severity, summary, namespace, and resource identity unless one is supplied explicitly
- suppression state is stored under `~/.cfoperator/event-runtime/policies/`

Disable it if you want every repeated alert to be processed:

```bash
export CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS=0
```

## Systemd Example

```ini
[Unit]
Description=CFOperator Event Runtime
After=network.target

[Service]
WorkingDirectory=/opt/cfoperator
ExecStart=/usr/bin/python3 -m event_runtime --host 0.0.0.0 --port 8080
Restart=on-failure
User=cfoperator

[Install]
WantedBy=multi-user.target
```

You can also start from the repository unit template at [deploy/systemd/cfoperator-event-runtime.service](/home/aachten/repos/cfoperator/deploy/systemd/cfoperator-event-runtime.service).

## Notes

- The portable mode is intentionally minimal and safe.
- It records and schedules work locally.
- Remote sinks, richer context providers, and Kubernetes-backed schedulers can be added later without changing the runtime boundary.
- Bare-metal observability is pluggable and can be purely local, explicitly configured, or discovery-driven depending on the providers you enable.
- ASGI mode is optional and should be treated as an adapter, not a required dependency.
- Optional PostgreSQL persistence does not change the runtime rule that local durability comes first.
- Background workers improve intake latency but do not change the core runtime decision flow.
- Worker job state is persisted locally so queued alerts survive restart by default.