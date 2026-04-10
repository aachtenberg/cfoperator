"""Prometheus telemetry for the event runtime.

The runtime stays operational even if prometheus-client is unavailable.
"""

from __future__ import annotations

import os
from typing import Dict

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest

    PROMETHEUS_AVAILABLE = True
except Exception:
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    PROMETHEUS_AVAILABLE = False


def _sanitize_label(value: object, default: str = "unknown") -> str:
    raw = str(value or default).strip().lower().replace(" ", "_")
    if not raw:
        return default
    return raw[:64]


if PROMETHEUS_AVAILABLE:
    RUNTIME_INFO = Info(
        "cfoperator_event_runtime_info",
        "Static metadata about the event runtime.",
    )
    RUNTIME_UP = Gauge(
        "cfoperator_event_runtime_up",
        "Whether the event runtime transport is currently up.",
    )
    ALERTS_RECEIVED = Counter(
        "cfoperator_event_runtime_alerts_received_total",
        "Alerts received by the runtime.",
        ["severity", "source"],
    )
    ALERT_RESULTS = Counter(
        "cfoperator_event_runtime_alert_results_total",
        "Final alert handling outcomes.",
        ["status", "action"],
    )
    ALERT_PROCESSING = Histogram(
        "cfoperator_event_runtime_alert_processing_seconds",
        "End-to-end alert handling latency.",
        ["status", "action"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    DECISIONS = Counter(
        "cfoperator_event_runtime_decisions_total",
        "Decisions emitted by the runtime decision engine.",
        ["action"],
    )
    SCHEDULED_TASKS = Counter(
        "cfoperator_event_runtime_scheduled_tasks_total",
        "Scheduled task creation attempts.",
        ["scheduler", "result"],
    )
    EVENTS_RECORDED = Counter(
        "cfoperator_event_runtime_events_recorded_total",
        "Domain events recorded by the runtime.",
        ["event_type"],
    )
    QUEUE_ENQUEUED = Counter(
        "cfoperator_event_runtime_queue_enqueued_total",
        "Jobs accepted into the background queue.",
    )
    QUEUE_REJECTED = Counter(
        "cfoperator_event_runtime_queue_rejected_total",
        "Jobs rejected because the background queue was full.",
    )
    JOB_RESULTS = Counter(
        "cfoperator_event_runtime_job_results_total",
        "Background job completion outcomes.",
        ["status"],
    )
    QUEUE_WAIT = Histogram(
        "cfoperator_event_runtime_queue_wait_seconds",
        "Time jobs spent waiting in the queue before processing started.",
        buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    QUEUE_PROCESSING = Histogram(
        "cfoperator_event_runtime_queue_processing_seconds",
        "Time spent processing queued jobs.",
        buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    QUEUE_SIZE = Gauge(
        "cfoperator_event_runtime_queue_size",
        "Current in-memory queue depth.",
    )
    QUEUE_CAPACITY = Gauge(
        "cfoperator_event_runtime_queue_capacity",
        "Configured in-memory queue capacity.",
    )
    QUEUE_OLDEST_AGE = Gauge(
        "cfoperator_event_runtime_queue_oldest_age_seconds",
        "Age of the oldest queued job.",
    )
    JOBS = Gauge(
        "cfoperator_event_runtime_jobs",
        "Current tracked jobs by state.",
        ["status"],
    )
    REPLAY_ATTEMPTS = Counter(
        "cfoperator_event_runtime_replay_attempts_total",
        "Replay attempts by sink and result.",
        ["sink", "result"],
    )
    REPLAY_EVENTS = Counter(
        "cfoperator_event_runtime_replay_events_total",
        "Events replayed or retried by sink and result.",
        ["sink", "result"],
    )
    REPLAY_BATCH = Histogram(
        "cfoperator_event_runtime_replay_batch_size",
        "Replay batch size by sink.",
        ["sink"],
        buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    )
    HOST_DISCOVERY_RUNS = Counter(
        "cfoperator_event_runtime_host_discovery_runs_total",
        "Host observability discovery attempts by provider and result.",
        ["provider", "result"],
    )
    HOST_DISCOVERED_TARGETS = Gauge(
        "cfoperator_event_runtime_host_discovered_targets",
        "Number of host targets currently discovered per provider.",
        ["provider"],
    )
    HOST_DISCOVERY_TIMESTAMP = Gauge(
        "cfoperator_event_runtime_host_discovery_timestamp_seconds",
        "Unix timestamp of the most recent successful host discovery run.",
        ["provider"],
    )
    HOST_OBSERVATION_RUNS = Counter(
        "cfoperator_event_runtime_host_observation_runs_total",
        "Host observation attempts by provider and result.",
        ["provider", "result"],
    )
else:
    RUNTIME_INFO = None
    RUNTIME_UP = None


def telemetry_available() -> bool:
    return PROMETHEUS_AVAILABLE


def initialize_runtime_info() -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    RUNTIME_INFO.info(
        {
            "service": "event_runtime",
            "telemetry": "prometheus",
            "pid": str(os.getpid()),
        }
    )


def mark_runtime_up() -> None:
    if PROMETHEUS_AVAILABLE:
        RUNTIME_UP.set(1)


def mark_runtime_down() -> None:
    if PROMETHEUS_AVAILABLE:
        RUNTIME_UP.set(0)


def observe_alert_received(alert) -> None:
    if PROMETHEUS_AVAILABLE:
        ALERTS_RECEIVED.labels(
            severity=_sanitize_label(getattr(getattr(alert, "severity", None), "value", None), "unknown"),
            source=_sanitize_label(getattr(alert, "source", None), "manual"),
        ).inc()


def observe_alert_result(status: object, action: object, duration_seconds: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    status_label = _sanitize_label(status, "unknown")
    action_label = _sanitize_label(action, "unknown")
    ALERT_RESULTS.labels(status=status_label, action=action_label).inc()
    ALERT_PROCESSING.labels(status=status_label, action=action_label).observe(max(0.0, duration_seconds))


def observe_decision(action: object) -> None:
    if PROMETHEUS_AVAILABLE:
        DECISIONS.labels(action=_sanitize_label(action, "unknown")).inc()


def observe_scheduled_task(scheduler: object, success: bool) -> None:
    if PROMETHEUS_AVAILABLE:
        SCHEDULED_TASKS.labels(
            scheduler=_sanitize_label(scheduler, "unknown"),
            result="success" if success else "error",
        ).inc()


def observe_event_recorded(event_type: object) -> None:
    if PROMETHEUS_AVAILABLE:
        EVENTS_RECORDED.labels(event_type=_sanitize_label(event_type, "unknown")).inc()


def observe_queue_enqueued() -> None:
    if PROMETHEUS_AVAILABLE:
        QUEUE_ENQUEUED.inc()


def observe_queue_rejected() -> None:
    if PROMETHEUS_AVAILABLE:
        QUEUE_REJECTED.inc()


def observe_job_started(queue_delay_seconds: float | None) -> None:
    if PROMETHEUS_AVAILABLE and queue_delay_seconds is not None:
        QUEUE_WAIT.observe(max(0.0, queue_delay_seconds))


def observe_job_finished(status: object, processing_seconds: float | None) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    JOB_RESULTS.labels(status=_sanitize_label(status, "unknown")).inc()
    if processing_seconds is not None:
        QUEUE_PROCESSING.observe(max(0.0, processing_seconds))


def update_queue_state(queue_size: int, max_queue_size: int, jobs: Dict[str, int], oldest_queued_age_seconds: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    QUEUE_SIZE.set(max(0, queue_size))
    QUEUE_CAPACITY.set(max(0, max_queue_size))
    QUEUE_OLDEST_AGE.set(max(0.0, oldest_queued_age_seconds))
    for status in ("queued", "running", "completed", "failed"):
        JOBS.labels(status=status).set(max(0, int(jobs.get(status, 0))))


def observe_replay_attempt(sink: object, result: object, events: int) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    sink_label = _sanitize_label(sink, "unknown")
    result_label = _sanitize_label(result, "unknown")
    REPLAY_ATTEMPTS.labels(sink=sink_label, result=result_label).inc()
    REPLAY_EVENTS.labels(sink=sink_label, result=result_label).inc(max(0, events))
    if events > 0:
        REPLAY_BATCH.labels(sink=sink_label).observe(events)


def observe_host_discovery(provider: object, result: object, targets: int = 0, timestamp_seconds: float | None = None) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    provider_label = _sanitize_label(provider, "unknown")
    result_label = _sanitize_label(result, "unknown")
    HOST_DISCOVERY_RUNS.labels(provider=provider_label, result=result_label).inc()
    if result_label == "success":
        HOST_DISCOVERED_TARGETS.labels(provider=provider_label).set(max(0, targets))
        if timestamp_seconds is not None:
            HOST_DISCOVERY_TIMESTAMP.labels(provider=provider_label).set(max(0.0, timestamp_seconds))


def observe_host_observation(provider: object, result: object) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    HOST_OBSERVATION_RUNS.labels(
        provider=_sanitize_label(provider, "unknown"),
        result=_sanitize_label(result, "unknown"),
    ).inc()


def render_metrics() -> tuple[bytes, str]:
    if not PROMETHEUS_AVAILABLE:
        return b"# prometheus_client not installed\n", CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST