# Event-Driven Plugin Runtime Plan

## Goals

- Replace monolithic event handling with a modular plugin runtime.
- Keep alert intake, severity gating, and safe actions operational when PostgreSQL is down.
- Make persistence a sink, not a prerequisite for agent execution.
- Add a clean migration seam beside the existing CFOperator runtime.
- Keep the first runnable slice deployable with only Python 3 and stdlib modules.

## Package Layout

```text
event_runtime/
  __init__.py
  __main__.py
  bootstrap.py
  defaults.py
  engine.py
  models.py
  plugins.py
  plugin_manager.py
  server.py
  state/
    __init__.py
    base.py
    composite.py
    local_outbox.py
```

## Runtime Boundaries

### Core Runtime

`EventRuntime` owns only orchestration:

- persist domain events via a `StateSink`
- run severity gate
- call context providers
- call the decision engine
- dispatch actions to handlers

It does not know about PostgreSQL, FastAPI, Prometheus, Loki, or Kubernetes internals.

### Portable Bootstrap

The first deployable slice should run on a generic host with:

- Python 3.11+
- no PostgreSQL
- no Kubernetes client
- no external Python packages

The portable bootstrap uses only stdlib pieces:

- local JSONL outbox for durable event storage
- file-backed scheduler for recurring checks
- simple default safe action handlers
- threaded stdlib HTTP server exposing `/alert`, `/health`, and `/history`

Optional adapters may sit on top of the same runtime:

- FastAPI or ASGI app for uvicorn or gunicorn deployments
- future CLI or gRPC adapters if needed

Adapters are transport-only. They must not own business logic.

### Plugin Types

- `AlertSource`: emits normalized alerts into the runtime
- `ContextProvider`: enriches an alert with surrounding evidence
- `DecisionEngine`: converts alert + context into an action plan
- `ActionHandler`: executes a named action
- `Scheduler`: persists recurring or delayed checks requested by the agent
- `StateSink`: persists domain events and exposes replay/health information

### Reasoning Posture

The runtime should avoid brittle, hardcoded checklists.

- Context plugins expose capabilities, not a mandatory ordered script.
- The decision engine may choose which lines of inquiry matter for a given alert.
- Hard constraints should be limited to platform safety boundaries: severity gate, allowlisted destructive actions, execution budgets, and tenant or namespace scope.
- The LLM should be free to propose follow-up checks or recurring monitors when it believes additional coverage is needed.

This keeps the system from degenerating into spaghetti code with hidden if-else trees for every alert type.

### Database Resilience

The runtime must treat durable local storage as the write boundary.

- A local outbox sink is mandatory.
- PostgreSQL is optional at runtime.
- `CompositeStateSink` succeeds if at least one durable sink succeeds.
- Health should report degraded mode when the local outbox is growing because remote sinks are failing.

Remote persistence should be layered behind a replay-capable sink wrapper:

- append to local durable storage first
- attempt remote persistence opportunistically
- replay local outbox events in the background until the remote sink catches up
- use idempotent remote writes keyed by `event_id`

## Initial Milestones

### Milestone 1

- Define runtime models and plugin contracts
- Add `PluginManager`
- Add durable `LocalOutboxStateSink`
- Add `CompositeStateSink`
- Add minimal `EventRuntime` orchestration with severity gate
- Add first-class scheduled task models and scheduler plugin contract

### Milestone 2

- Add FastAPI webhook receiver as an `AlertSource` adapter
- Add `History` read path from local outbox plus remote sinks when available
- Add basic metrics and degraded health reporting
- Preserve the stdlib server as the zero-dependency fallback path
- Keep adapter dependencies optional and out of the runtime core package path

### Milestone 3

- Add PostgreSQL sink with async replay from the local outbox
- Add event fingerprints and duplicate suppression
- Add background worker queue
- Keep replay logic outside the runtime core so remote sinks remain pluggable

### Milestone 4

- Add Kubernetes event watcher plugin
- Add Prometheus and Loki context provider plugins
- Add safe action handlers: `log_only`, `investigate`, `notify`
- Add scheduler plugins for Kubernetes CronJobs and internal delayed jobs

### Milestone 5

- Add CronJob alert producers
- Add destructive action plugins behind explicit allowlists and feature flags
- Retire or reduce the legacy sweep loop to a slow safety net

### Milestone 6

- Allow the LLM to propose or create recurring checks through the scheduler plugin layer
- Add policy controls around where jobs may run, what images/templates may be used, and how long scheduled checks may live
- Add garbage collection for stale or superseded jobs

## Interface Rules

- Plugins communicate through runtime models, not through each other's concrete classes.
- Runtime code may depend on plugin interfaces, never on plugin implementations.
- State sinks must never block alert intake on remote availability.
- Every mutation in the runtime should emit a domain event.
- Scheduling is a separate capability, not a side effect hidden inside an action or decision engine.
- Recurring checks created by the agent should use templates and policy validation, not raw shell strings built inline by the model.

## Migration Strategy

1. Keep the current CFOperator runtime untouched.
2. Build the new runtime beside it under `event_runtime/`.
3. Route one ingress path into the new runtime first.
4. Add remote sinks and replay.
5. Move event sources one at a time.

## Acceptance Criteria For The Scaffold

- Local durable writes work with only stdlib dependencies.
- A runtime can process alerts without a database.
- Plugin registration is explicit and typed.
- Composite sinks degrade gracefully when remote sinks fail.
- The runtime can record agent-requested recurring checks without hardwiring specific checklists into the engine.
- A fresh host can run `python3 -m event_runtime --port 8080` without a pip install step.