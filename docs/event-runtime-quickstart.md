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