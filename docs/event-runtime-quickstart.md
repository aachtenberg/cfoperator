# Event Runtime Quickstart

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
- `scheduled/` for agent-requested recurring checks

## Endpoints

- `GET /health`
- `GET /history?limit=50`
- `POST /alert`
- `GET /jobs/<job_id>` when background workers are enabled

## Optional ASGI Mode

If you want FastAPI-style deployment behind uvicorn or gunicorn, install only the adapter dependencies:

```bash
python3 -m pip install fastapi uvicorn
uvicorn event_runtime.fastapi_app:build_app --factory --host 0.0.0.0 --port 8080
```

The runtime core is the same. Only the HTTP adapter changes.

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
- `CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS`: duplicate suppression window in seconds, default `300`, set to `0` to disable
- `CFOP_EVENT_RUNTIME_WORKER_COUNT`: background worker count, default `1`, set to `0` to force synchronous processing
- `CFOP_EVENT_RUNTIME_MAX_QUEUE_SIZE`: max in-memory queued jobs, default `1000`

## Async Intake

By default, if background workers are enabled, `POST /alert` queues the alert and returns immediately.

- default mode with workers: async
- force synchronous processing: `POST /alert?mode=sync`
- inspect job status: `GET /jobs/<job_id>`

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
- duplicate replay is safe because the PostgreSQL table is keyed by `event_id`

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

## Notes

- The portable mode is intentionally minimal and safe.
- It records and schedules work locally.
- Remote sinks, richer context providers, and Kubernetes-backed schedulers can be added later without changing the runtime boundary.
- ASGI mode is optional and should be treated as an adapter, not a required dependency.
- Optional PostgreSQL persistence does not change the runtime rule that local durability comes first.
- Background workers improve intake latency but do not change the core runtime decision flow.