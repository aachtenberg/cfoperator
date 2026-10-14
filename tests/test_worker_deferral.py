"""The worker waits out an unavailable target, and a rerun is not deduped (CFOP-395).

Two failures, one root: the worker reran a job through ``handle_alert`` in
full, so the stateful alert policies saw the alert a second time. The default
cooldown had claimed its fingerprint on the first run and suppressed the rerun
as a duplicate of itself -- the job ended ``completed`` and nothing was sent.
On top of that, an agent that was restarting got both attempts within
milliseconds. These guard the class: any rerun must reach the handler, and a
target that says "later" is waited for, not charged.
"""

from __future__ import annotations

import time
from pathlib import Path

from event_runtime.activity import fold_alert
from event_runtime.dedupe import FileBackedCooldownPolicy
from event_runtime.engine import EventRuntime
from event_runtime.models import ActionResult, Alert, AlertSeverity, ContextEnvelope, Decision
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, DecisionEngine, TargetUnavailableError
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink
from event_runtime.worker import BackgroundAlertWorker, FileBackedWorkerState


class Investigate(DecisionEngine):
    name = "investigate"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(action="investigate", confidence=1.0, reasoning="test")


class Scripted(ActionHandler):
    """Raises each scripted exception in turn, then succeeds."""

    name = "scripted"
    action_name = "investigate"

    def __init__(self, *errors: Exception):
        self.errors = list(errors)
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ActionResult(action="investigate", success=True, message="dispatched")


def _runtime(tmp_path: Path, handler: ActionHandler) -> EventRuntime:
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "outbox"))]))
    # The production default: what made a rerun a duplicate of itself.
    plugins.register_alert_policy(FileBackedCooldownPolicy(path=str(tmp_path / "dedupe.json")))
    plugins.register_decision_engine(Investigate())
    plugins.register_action_handler(handler)
    return EventRuntime(plugins)


def _worker(tmp_path: Path, runtime: EventRuntime, **kwargs) -> BackgroundAlertWorker:
    kwargs.setdefault("defer_base_seconds", 0.01)
    kwargs.setdefault("defer_max_seconds", 0.05)
    return BackgroundAlertWorker(
        runtime=runtime,
        worker_count=1,
        max_queue_size=10,
        state=FileBackedWorkerState(path=str(tmp_path / "queue" / "jobs.json")),
        **kwargs,
    )


def _alert() -> Alert:
    return Alert(source="test", severity=AlertSeverity.WARNING, summary="agent restarting")


def _run_one(worker: BackgroundAlertWorker, timeout: float = 5.0) -> dict:
    worker.start()
    try:
        queued = worker.enqueue(_alert())
        job = worker.wait_for_job(queued["job_id"], timeout=timeout)
    finally:
        worker.stop()
    assert job is not None
    return job


def test_a_retry_reaches_the_handler_past_the_cooldown(tmp_path: Path):
    handler = Scripted(RuntimeError("agent answered 500"))
    job = _run_one(_worker(tmp_path, _runtime(tmp_path, handler)))

    assert handler.calls == 2
    assert job["status"] == "completed"
    assert job["result"]["status"] == "completed", job["result"]
    assert job["attempt"] == 2


def test_an_unavailable_target_is_waited_out_without_spending_attempts(tmp_path: Path):
    handler = Scripted(*(TargetUnavailableError("agent answered 503") for _ in range(3)))
    runtime = _runtime(tmp_path, handler)
    job = _run_one(_worker(tmp_path, runtime, max_retries=1))

    # max_retries=1 would have failed the first refusal; deferrals are not attempts.
    assert handler.calls == 4
    assert job["status"] == "completed"
    assert job["result"]["status"] == "completed", job["result"]
    assert job["attempt"] == 1
    assert job["deferrals"] == 3

    deferred = runtime.recent_events(event_type="alert_job_deferred")
    assert len(deferred) == 3
    assert all("retrying in" in event["payload"]["reason"] for event in deferred)


def test_the_delay_grows_and_is_capped(tmp_path: Path):
    handler = Scripted(*(TargetUnavailableError("down") for _ in range(4)))
    runtime = _runtime(tmp_path, handler)
    _run_one(_worker(tmp_path, runtime, defer_base_seconds=0.01, defer_max_seconds=0.03))

    delays = sorted(e["payload"]["delay_seconds"] for e in runtime.recent_events(event_type="alert_job_deferred"))
    assert delays == [0.01, 0.02, 0.03, 0.03]


def test_a_target_down_past_the_budget_fails_the_job(tmp_path: Path):
    handler = Scripted(*(TargetUnavailableError("agent unreachable") for _ in range(1000)))
    job = _run_one(_worker(tmp_path, _runtime(tmp_path, handler), defer_budget_seconds=0.2))

    assert job["status"] == "failed"
    assert "defer budget" in job["error"]
    assert job["deferrals"] >= 2


def test_a_zero_budget_makes_unavailable_an_ordinary_attempt(tmp_path: Path):
    handler = Scripted(*(TargetUnavailableError("agent answered 503") for _ in range(1000)))
    job = _run_one(_worker(tmp_path, _runtime(tmp_path, handler), defer_budget_seconds=0))

    assert handler.calls == 2  # max_retries
    assert job["status"] == "failed"
    assert job["deferrals"] == 0


def test_a_job_deferred_at_stop_is_restored_and_sent_on_the_next_start(tmp_path: Path):
    handler = Scripted(TargetUnavailableError("agent shutting down"))
    runtime = _runtime(tmp_path, handler)
    # A delay long enough that stop() lands while the job waits.
    first = _worker(tmp_path, runtime, defer_base_seconds=30, defer_max_seconds=30)
    first.start()
    try:
        job_id = first.enqueue(_alert())["job_id"]
        deadline = time.monotonic() + 5
        while handler.calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
    finally:
        first.stop()
    assert first.get_job(job_id)["status"] == "queued"

    second = _worker(tmp_path, runtime)
    second.start()
    try:
        job = second.wait_for_job(job_id, timeout=5.0)
    finally:
        second.stop()
    assert job["status"] == "completed"
    assert job["result"]["status"] == "completed", job["result"]
    assert handler.calls == 2


def test_the_fold_reads_a_deferred_alert_as_queued():
    alert = _alert().to_dict()
    job = {"job_id": "j1", "alert": alert}
    events = [
        {"event_type": "alert_queued", "created_at": "2026-10-14T10:00:00+00:00", "payload": {"job": job}},
        {"event_type": "alert_job_started", "created_at": "2026-10-14T10:00:01+00:00", "payload": {"job": job}},
        {"event_type": "alert_job_deferred", "created_at": "2026-10-14T10:00:02+00:00",
         "payload": {"job": job, "reason": "agent answered 503; retrying in 5s", "delay_seconds": 5}},
    ]
    activity = fold_alert(events)
    assert activity["status"] == "queued"
    assert activity["timeline"][-1]["note"] == "agent answered 503; retrying in 5s"
