"""Basic tests for the modular event runtime scaffold."""

from __future__ import annotations

from pathlib import Path

from event_runtime.engine import EventRuntime
from event_runtime.bootstrap import build_portable_runtime
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope, Decision, ScheduledTask
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, ContextProvider, DecisionEngine, Scheduler
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink
from event_runtime.state.replay import ReplayingStateSink


class StaticContext(ContextProvider):
    name = "static-context"
    capabilities = ("metrics", "logs", "kubernetes")

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        envelope.context["source"] = alert.source
        return envelope


class InvestigateDecision(DecisionEngine):
    name = "investigate-decision"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(
            action="investigate",
            confidence=1.0,
            reasoning="default",
            params={},
            requested_checks=["metrics", "logs"],
        )


class InvestigateAction(ActionHandler):
    name = "investigate-action"
    action_name = "investigate"

    def execute(self, request):
        return __import__("event_runtime.models", fromlist=["ActionResult"]).ActionResult(
            action="investigate",
            success=True,
            message=f"processed {request.alert.summary}",
            details=request.context.context,
        )


class MemoryScheduler(Scheduler):
    name = "memory-scheduler"

    def __init__(self):
        self.tasks = []

    def schedule(self, task: ScheduledTask) -> dict:
        self.tasks.append(task)
        return {
            "success": True,
            "message": f"scheduled {task.name}",
            "task": task.to_dict(),
        }


class ScheduledDecision(DecisionEngine):
    name = "scheduled-decision"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(
            action="investigate",
            confidence=0.9,
            reasoning="needs a recurring check",
            params={},
            scheduled_tasks=[
                ScheduledTask(
                    name="watch-crashloop-pod",
                    schedule="*/5 * * * *",
                    rationale="Track repeated restarts until stable",
                    target={"kind": "pod", "namespace": "apps", "name": "api"},
                    parameters={"check": "restart_rate"},
                )
            ],
        )


def test_local_outbox_persists_events(tmp_path: Path):
    sink = LocalOutboxStateSink(directory=str(tmp_path / "outbox"))
    sink.append([{"event_type": "x", "payload": {"ok": True}}])
    recent = sink.recent(limit=10)
    assert recent[0]["event_type"] == "x"


def test_runtime_processes_warning_without_database(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "runtime-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_context_provider(StaticContext())
    plugins.register_action_handler(InvestigateAction())

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.WARNING, summary="pod crashloop")
    )

    assert result["success"] is True
    events = runtime.recent_events(limit=10)
    event_types = [event["event_type"] for event in events]
    assert "action_completed" in event_types


def test_runtime_logs_info_without_action(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "info-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.INFO, summary="heartbeat")
    )

    assert result["action"] == "log_only"
    assert result["success"] is True


def test_runtime_can_schedule_follow_up_tasks(tmp_path: Path):
    scheduler = MemoryScheduler()
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "schedule-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(ScheduledDecision())
    plugins.register_action_handler(InvestigateAction())
    plugins.register_scheduler(scheduler)

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.WARNING, summary="restart storm")
    )

    assert result["success"] is True
    assert result["scheduled_tasks"]
    assert result["scheduled_tasks"][0]["success"] is True
    assert scheduler.tasks[0].name == "watch-crashloop-pod"


def test_portable_runtime_bootstrap_uses_local_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "portable"))
    runtime = build_portable_runtime()

    health = runtime.health()
    sink = health["sink"]
    assert sink["healthy"] is True
    assert sink["durable"] is True

    result = runtime.handle_alert(
        Alert(source="portable", severity=AlertSeverity.WARNING, summary="portable run")
    )
    assert result["success"] is True


def test_fastapi_adapter_module_can_be_imported_without_fastapi_installed():
    module = __import__("event_runtime.fastapi_app", fromlist=["build_app"])
    assert hasattr(module, "create_app")
    assert hasattr(module, "build_app")


class MemoryRemoteSink:
    durable = False
    name = "memory-remote"

    def __init__(self):
        self.events = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def append(self, events):
        known = {event["event_id"] for event in self.events}
        for event in events:
            if event["event_id"] not in known:
                self.events.append(event)
        return True

    def recent(self, limit: int = 50):
        return list(reversed(self.events[-limit:]))

    def health(self):
        return {"name": self.name, "healthy": True, "durable": False}


def test_replaying_sink_replays_outbox_events(tmp_path: Path):
    local_sink = LocalOutboxStateSink(directory=str(tmp_path / "replay-outbox"))
    remote_sink = MemoryRemoteSink()
    sink = ReplayingStateSink(local_sink=local_sink, remote_sinks=[remote_sink], replay_interval_seconds=3600)

    event = {
        "event_id": "evt-1",
        "created_at": "2026-04-07T00:00:00+00:00",
        "event_type": "alert_received",
        "payload": {"ok": True},
    }
    assert sink.append([event]) is True
    replay = sink.replay_once()

    assert replay["success"] is True
    assert remote_sink.events
    assert remote_sink.events[0]["event_id"] == "evt-1"


def test_postgres_sink_module_can_be_imported_without_connecting():
    module = __import__("event_runtime.state.postgres", fromlist=["PostgresStateSink"])
    sink = module.PostgresStateSink(dsn="")
    assert sink.health()["configured"] is False