"""Basic tests for the modular event runtime scaffold."""

from __future__ import annotations

from pathlib import Path

from event_runtime.engine import EventRuntime
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope, Decision, ScheduledTask
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, ContextProvider, DecisionEngine, Scheduler
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink


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