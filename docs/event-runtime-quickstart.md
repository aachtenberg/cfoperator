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

        Context --> Decision["HTTPTriageDecisionEngine<br/>(POST /v1/triage to agent)"]
        Decision --> TriageCache["Triage result cache<br/>(per-alert TTL)"]
        Decision --> Route{"Triage action"}
        Route -->|log_only| LogOnly
        Route -->|notify| NotifyOnly["notify handler<br/>(no investigation)"]
        Route -->|investigate / escalate| Handler{"Action<br/>Handler?"}
        Handler -->|missing| Fail["Record action_missing"]
        Handler -->|found| Execute["Execute Action<br/>(HTTPInvestigateActionHandler<br/>or stub)"]
        Execute --> Schedule["Schedule Follow-up<br/>Tasks (optional)"]
    end

    Execute -.->|CFOP_AGENT_URL set| AgentDispatch["POST /v1/investigate<br/>to CFOperator agent<br/>(HTTPInvestigateActionHandler)"]
    AgentDispatch -.-> AgentLoop["Agent runs LLM<br/>investigation with tools"]
    AgentLoop -.->|POST /v1/investigations/<br/>{alert_id}/complete<br/>(X-CFOP-Token)| Completion["record_external_<br/>action_completion()"]
    Completion --> Audit
    Completion --> Notify["Notification Sinks<br/>(Slack, Discord)<br/><i>tagged 'triaged by &lt;backend&gt;/&lt;model&gt;'</i>"]

    LogOnly --> Audit
    Fail --> Audit
    Execute --> Audit
    NotifyOnly --> Audit
    NotifyOnly --> Notify
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
5. **Triage Decision** — `HTTPTriageDecisionEngine` POSTs the alert to the agent's `/v1/triage` so the LLM classifies it into one of `log_only` / `notify` / `investigate` / `escalate`. The agent's response includes which provider actually served the classification (`triage_backend` + `triage_model`), which is stored in `Decision.params` for downstream attribution. Results are cached per-alert with a short TTL so duplicate firings don't re-call the LLM.
6. **Action Execution** — the runtime routes by triage action:
   - `log_only` — record-only; no notification, no investigation.
   - `notify` — single-line Slack/Discord notification, no LLM investigation. The notification renders `triaged by <backend>/<model>` so operators can see which LLM made the call.
   - `investigate` / `escalate` — dispatched to the matched action handler. With `CFOP_AGENT_URL` set, `HTTPInvestigateActionHandler` POSTs the alert to the CFOperator agent's `/v1/investigate` and returns a quiet (non-notifying) `ActionResult`. Without it, the default stub handler runs (records intent only).
7. **Scheduling** — follow-up checks are persisted to the scheduler store and polled back into the runtime as synthetic alerts when they become due
8. **Audit** — every step emits append-only domain events to the local JSONL outbox, with background replay to PostgreSQL when configured
9. **Completion Post-Back (delegated investigations only)** — when the agent finishes its LLM investigation, it POSTs the completed `ActionResult` to `/v1/investigations/{alert_id}/complete` with the `X-CFOP-Token` header. The runtime records an `action_completed` event tagged `source=external` and fires the single Slack/Discord notification with the real outcome, including the `triaged by …` attribution line carried through from the original triage decision.

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
- `GET /activity?limit=25&status=&action=` — the newest alerts with their full timelines (the legacy feed; served from the read model below)
- `GET /v1/alerts` — alerts by their folded state, newest first. Parameters: `limit` (1–200, default 50), `cursor` (the previous page's `next_cursor`), `status`, `action`, `source`, `severity`, `since`/`until` (ISO 8601, bounds on the latest event), `q` (substring of summary, resource, namespace or alert id). An unknown parameter is a 400. Returns `{alerts, next_cursor, store, lagging}`: `store` is `postgres` or `outbox`, and `lagging` means Postgres answered while the outbox still holds events it has not replayed.
- `GET /v1/alerts/<alert_id>` — one alert's folded state plus every event behind it, oldest first; 404 if unknown
- `GET /scheduled?limit=100`
- `GET /metrics`
- `POST /alert`
- `GET /jobs/<job_id>` when background workers are enabled
- `POST /v1/investigations/<alert_id>/complete` — receive a completed `ActionResult` from an external executor (the agent). Body shape: `{"alert": <Alert>, "result": <ActionResult>}`. Requires `X-CFOP-Token` header when `CFOP_COMPLETION_SHARED_SECRET` is set.

When `CFOP_RUNTIME_TOKEN` is set, every route above except `/livez`, `/health`, `/metrics` and the completion endpoint requires `Authorization: Bearer $CFOP_RUNTIME_TOKEN`.

### The alert read model (CFOP-215)

`/v1/alerts` filters on an alert's *folded* state (status, action, and so on), which exists only after all of the alert's events are combined. With Postgres configured, `event_runtime_alerts` keeps one row per alert. Each row is recomputed from all of that alert's events, in the same transaction that inserts a new one, using the same fold as the in-memory path (`activity.fold_alert`). There is no second definition of status in SQL, and replaying an event rewrites the same row. `activity.FOLD_VERSION` is stored on every row. On start, a background rebuild folds every alert that is missing or at another version, which is also how an existing `event_runtime_events` table gets its read model.

Until that rebuild finishes, or whenever Postgres cannot answer, the runtime answers from the local outbox. The answer is complete but slower, and the response says `store: "outbox"`. Without Postgres configured, the outbox is the only store. `/health` reports the rebuild under `sink.remotes[].read_model`.

If you change the fold (`_merge_activity` and friends), bump `FOLD_VERSION`, or rows written before the change keep the old answer.

The agent is a caller too. It forwards every sweep finding and "Resolved" notice to `POST /alert`, so it needs the **same** `CFOP_RUNTIME_TOKEN` in its own environment. The runtime cannot tell a caller that forgot the token from one that was never meant to call, and turning the gate on without giving the agent the token stopped every sweep notification for 34 days (CFOP-214). A refused forward is logged at WARNING and counted in `cfoperator_sweep_forward_total{outcome="unauthorized"}`.

Alertmanager, if you push to the runtime rather than letting it poll, sends the token with a `http_config.authorization` block:

```yaml
receivers:
  - name: cfoperator
    webhook_configs:
      - url: http://cfoperator-event-runtime:8080/alert
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/cfop-runtime-token
```

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
- `CFOP_EVENT_RUNTIME_MAX_TERMINAL_JOBS`: how many completed/failed jobs to retain in the persisted state file for `GET /jobs/<id>` lookups, default `100`; older terminal jobs are pruned. Increase for richer post-mortem debugging at the cost of a larger state file
- `CFOP_EVENT_RUNTIME_QUEUE_STATE_PATH`: persisted worker job state path, default `~/.cfoperator/event-runtime/queue/jobs.json`
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_ENABLED`: enable bare-metal host observability plugins, default `1`
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_JSON`: inline JSON config for bare-metal observability providers
- `CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH`: path to a JSON config file for bare-metal observability providers
- `CFOP_AGENT_URL`: base URL of the CFOperator agent (e.g. `http://cfoperator.apps.svc.cluster.local:8083`). When set, the runtime registers `HTTPInvestigateActionHandler` in place of the default stub for the `investigate` action, so every `investigate` decision is dispatched to the agent over HTTP. Unsetting it falls back to the stub.
- `CFOP_RUNTIME_TOKEN`: bearer token enforced on the HTTP surface — `POST /alert` (which enqueues LLM investigations) plus the `/history`, `/activity`, `/scheduled` and `/jobs` read endpoints that replay the fleet's incident history. `/livez`, `/health`, `/metrics` and the completion endpoint (separately authenticated) stay open. When unset, those routes accept unauthenticated requests and a warning is logged at startup. Compared via `secrets.compare_digest`. Set it on the agent as well: the agent sends it on its `/alert` forwards (`event_runtime/client.py`).
- `CFOP_EVENT_RUNTIME_PLUGINS`: plugins to load after the built-ins, as comma-separated `module` or `module:callable` entries (callable defaults to `register`). A named plugin that cannot be loaded stops the runtime at startup. See [External Plugins](#external-plugins).
- `CFOP_COMPLETION_SHARED_SECRET`: shared secret enforced on `POST /v1/investigations/{alert_id}/complete`. Callers must send a matching `X-CFOP-Token` header. When unset, the endpoint accepts unauthenticated posts (portable deployments without an agent stay runnable) and logs a warning at startup. Compared via `secrets.compare_digest` to avoid timing attacks.

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

Portable mode does not require PostgreSQL. If you want remote event persistence in addition to the local outbox, you can enable it via env var or via `config.yaml`.

The runtime resolves persistence settings in this order:

1. **Env var DSN (highest priority):**
   ```bash
   export CFOP_EVENT_RUNTIME_PG_DSN='postgresql://cfoperator:pass@db:5432/cfoperator'
   ```
2. **Explicit DSN in `config.yaml`:**
   ```yaml
   event_runtime:
     persistence:
       postgres:
         enabled: true
         dsn: postgresql://cfoperator:pass@db:5432/cfoperator
         table_name: event_runtime_events   # optional, defaults to event_runtime_events
   ```
3. **Built from the top-level `database:` block when `enabled: true` and no explicit DSN is set.** This is the path the in-cluster Kubernetes deployment uses, where credentials are injected via env vars into the shared `database:` config:
   ```yaml
   database:
     host: ${POSTGRES_HOST}
     port: ${POSTGRES_PORT}
     database: ${POSTGRES_DB}
     user: ${POSTGRES_USER}
     password: ${POSTGRES_PASSWORD}

   event_runtime:
     persistence:
       postgres:
         enabled: true
         table_name: event_runtime_events
   ```

Persistence is implicitly enabled if any of `CFOP_EVENT_RUNTIME_PG_DSN`, `event_runtime.persistence.postgres.dsn`, or `event_runtime.persistence.postgres.enabled: true` is set. Override with `CFOP_EVENT_RUNTIME_PG_ENABLED=0` to force-disable.

Behavior once enabled:

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
- `cfoperator_event_runtime_completion_requests_total{outcome}` — outcome label: `recorded`, `auth_missing`, `auth_invalid`, `bad_request`, `error`. Inbound to `POST /v1/investigations/{alert_id}/complete`. Auth-related labels surfacing > 0 indicates someone is trying to hit the endpoint without (or with the wrong) `X-CFOP-Token`.

Import [grafana/event-runtime-dashboard.json](../grafana/event-runtime-dashboard.json) into Grafana to observe alert throughput, queue health, replay behavior, scheduled follow-up tasks, and end-to-end latency.

Prometheus alert rules for runtime health, queue stalls, replay failures, and bare-metal host observability failures are provided in [observability/event-runtime-alert-rules.yml](../observability/event-runtime-alert-rules.yml).

Prometheus scrape configuration for the runtime is provided in [observability/prometheus-event-runtime-scrape.yml](../observability/prometheus-event-runtime-scrape.yml).

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

You can also start from the repository unit template at [deploy/systemd/cfoperator-event-runtime.service](../deploy/systemd/cfoperator-event-runtime.service).

## External Plugins

The runtime's plugin roles (alert sources, context providers, notification
sinks, action handlers and the rest, in `event_runtime/plugins.py`) are not
limited to the ones it registers itself. Name a module in
`CFOP_EVENT_RUNTIME_PLUGINS` and the runtime imports it and calls its
`register` function once the built-ins are in place:

```python
# my_plugin.py -- importable from the runtime's PYTHONPATH
from event_runtime.plugins import AlertSource

class MySource(AlertSource):
    name = "my-source"

    def poll(self):
        return []  # normalized event_runtime.models.Alert objects

def register(plugins, context):
    # plugins: the runtime's PluginManager; use its register_* methods.
    # context.config: the merged root config; context.config_path: its path.
    # context.escalation_ledger: pass it to an alert source that reports
    #   clears, so escalated alerts still get their one "Resolved:" notice.
    plugins.register_alert_source(MySource())
```

```bash
export CFOP_EVENT_RUNTIME_PLUGINS=my_plugin            # calls my_plugin.register
export CFOP_EVENT_RUNTIME_PLUGINS=my_plugin:setup      # calls my_plugin.setup instead
```

Plugins load last, in the order named, and a module named twice loads once.
Because action handlers are keyed by action name, a plugin can deliberately
replace a built-in one, the same way `CFOP_AGENT_URL` replaces the
`investigate` stub. A plugin that fails to import or raises while registering
stops the runtime at startup with the entry in the message: silently running
without a source the operator asked for is the worse failure.

### Evidence for the investigation

Context providers write into the envelope for the runtime's own use, and the
investigate request to the agent carries only the alert. One key is the
exception: text a provider puts under `envelope.context["evidence"][<name>]` is
sent with the request, and the agent shows it to the model as its own section
of the investigation prompt, labelled as data rather than instructions. Each
block is capped at 4000 characters and the total at 8000, by the runtime before
sending and again by the agent. Nothing else in the envelope crosses, so
existing providers are unaffected. The contract lives in `cfshared/evidence.py`.

```python
def provide(self, alert, envelope):
    envelope.context.setdefault("evidence", {})["my-source"] = "Recent errors:\n- ..."
    return envelope
```

### Acting on results

A plugin that needs to act on what an investigation concluded (write it back to
the system the alert came from, say) registers a `CompletionObserver` with
`plugins.register_completion_observer(...)`. Its `observe(alert, result)` is
called for every completed action, from the agent's post-back as well as
in-process ones, **before** any notification policy. So it also sees the
resolved and monitoring outcomes that the low-severity digest keeps out of real
time. Interim results (`quiet`, such as "investigation queued") are not
completions and are not observed. An observer that raises is logged and
changes nothing else. Use a notification sink to tell people and an observer
to act on results: a sink's return value is recorded as delivery success or
failure, and a sink sits behind the paging gates.

### Dynatrace problems (`integrations.dynatrace`)

An optional plugin that ships in the image and stays inert until it is named.
It turns Davis problems into alerts, so a Dynatrace environment can start
investigations the way Alertmanager does. It is not a shipped backend:
[infrastructure-config.md](infrastructure-config.md#what-actually-ships) keeps
Dynatrace "not planned" for that.

```bash
export CFOP_EVENT_RUNTIME_PLUGINS=integrations.dynatrace
export DT_ENVIRONMENT_URL=https://<env>.apps.dynatrace.com   # the platform URL, not <env>.live
export DT_PLATFORM_TOKEN=dt0s16....                          # needs storage:events:read
# optional
export CFOP_DYNATRACE_POLL_SECONDS=60    # least time between queries (>= 10)
export CFOP_DYNATRACE_LOOKBACK=7d        # how far back the problem query reaches
export CFOP_DYNATRACE_PROBLEM_FILTER='in("my-cluster", k8s.cluster.name)'   # DQL scope; unset = every problem
export CFOP_DYNATRACE_EVIDENCE=1         # 0/false/off: problems only, no evidence queries
# optional: write each investigation's conclusion back onto its problem
export DT_PROBLEMS_TOKEN=dt0c01....        # classic access token with problems.write
export DT_API_URL=https://<env>.live.dynatrace.com   # derived from DT_ENVIRONMENT_URL on SaaS
```

- One alert per problem, the first time it is seen ACTIVE. The fingerprint is
  `dynatrace:<event.id>`, so Davis retitling the problem as it merges events
  does not alert again.
- A problem is resolved only when a CLOSED row for it is seen. Dropping out of
  the query window does not count: an open problem can go a long time without
  a new row. An escalated problem gets the usual single `Resolved:` notice.
- Duplicates, muted problems and problems under maintenance are skipped until
  that stops being true.
- `CFOP_DYNATRACE_PROBLEM_FILTER` scopes which problems count, as a DQL
  condition. The evidence and write-back act only on the source's alerts, so
  they follow it. Select on where a problem is (cluster, host, entity), never
  on its state: the CLOSED row that resolves a problem has to pass the filter
  too. If Dynatrace rejects the query, it is logged as an error naming the
  filter, since until it is fixed no problems arrive.
- Each problem's investigation also gets Dynatrace's own view of it as
  [evidence](#evidence-for-the-investigation): the Davis events in the problem,
  the last hour of the workload's logs grouped into distinct lines with counts,
  and its container restarts over two hours (for a host problem, its logs above
  INFO and its CPU instead). The queries are fixed and run under a 20 s budget
  before triage; the model reads the results and never writes DQL. A failed
  query is reported in the evidence rather than left out.
- With `DT_PROBLEMS_TOKEN` set, each finished investigation is written back as
  a comment on its problem: outcome, recommendation, summary and model. That
  includes resolved and monitoring outcomes, which the low-severity digest
  keeps out of real-time notifications (it is a
  [completion observer](#acting-on-results), not a sink). The token must be a
  classic access token with `problems.write`; a platform token is refused at
  startup. A failed post is logged and not retried, because a comment is not
  idempotent. Each investigation is written once.
- Kubernetes problems carry `namespace`, workload kind and name; host problems
  carry `host`. The Davis description and affected entities ride in
  `details.dynatrace`.
- A missing or malformed setting stops the runtime at startup. Dynatrace being
  unreachable does not: the poll logs, backs off, and the other sources carry on.
- Like the Alertmanager source, it re-emits the problems still open after a
  restart. Whether those count as repeats is up to the runtime's file-backed
  policies (see Duplicate Suppression): by default the recurrence window
  suppresses a critical alert for 30 minutes after it first fired and anything
  else for 6 hours. Davis `ERROR` and `AVAILABILITY` problems map to critical.

## Notes

- The portable mode is intentionally minimal and safe.
- It records and schedules work locally.
- Remote sinks, richer context providers, and Kubernetes-backed schedulers can be added later without changing the runtime boundary.
- Bare-metal observability is pluggable and can be purely local, explicitly configured, or discovery-driven depending on the providers you enable.
- ASGI mode is optional and should be treated as an adapter, not a required dependency.
- Optional PostgreSQL persistence does not change the runtime rule that local durability comes first.
- Background workers improve intake latency but do not change the core runtime decision flow.
- Worker job state is persisted locally so queued alerts survive restart by default.