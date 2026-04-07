"""Minimal orchestration engine for the modular event-driven runtime."""

from __future__ import annotations

from dataclasses import asdict
from typing import Dict, List

from .models import ActionRequest, Alert, AlertSeverity, ContextEnvelope, DomainEvent, ScheduledTask
from .plugin_manager import PluginManager


class EventRuntime:
    """Coordinate alert intake, gating, context, decisions, and actions."""

    def __init__(self, plugins: PluginManager):
        if plugins.state_sink is None:
            raise ValueError("EventRuntime requires a registered state sink")
        if plugins.decision_engine is None:
            raise ValueError("EventRuntime requires a registered decision engine")
        self.plugins = plugins

    def poll_sources(self) -> List[Dict[str, object]]:
        """Poll all registered alert sources and process emitted alerts."""
        results: List[Dict[str, object]] = []
        for source in self.plugins.alert_sources:
            for alert in source.poll():
                results.append(self.handle_alert(alert))
        return results

    def handle_alert(self, alert: Alert) -> Dict[str, object]:
        """Process a single normalized alert end-to-end."""
        self._emit("alert_received", alert=alert.to_dict())

        if alert.severity is AlertSeverity.INFO:
            self._emit("alert_skipped", alert=alert.to_dict(), reason="severity_gate")
            return {
                "alert_id": alert.alert_id,
                "status": "logged",
                "action": "log_only",
                "success": True,
            }

        envelope = ContextEnvelope(alert=alert)
        for provider in self.plugins.context_providers:
            envelope = provider.provide(alert, envelope)

        decision = self.plugins.decision_engine.decide(envelope)
        self._emit(
            "decision_made",
            alert=alert.to_dict(),
            decision=asdict(decision),
        )
        if decision.requested_checks:
            self._emit(
                "checks_requested",
                alert=alert.to_dict(),
                checks=list(decision.requested_checks),
            )

        handler = self.plugins.action_handlers.get(decision.action)
        if handler is None:
            self._emit(
                "action_missing",
                alert=alert.to_dict(),
                decision=asdict(decision),
            )
            return {
                "alert_id": alert.alert_id,
                "status": "failed",
                "action": decision.action,
                "success": False,
                "error": f"No action handler registered for {decision.action}",
            }

        request = ActionRequest(alert=alert, decision=decision, context=envelope)
        result = handler.execute(request)
        self._emit(
            "action_completed",
            alert=alert.to_dict(),
            decision=asdict(decision),
            result=result.to_dict(),
        )
        schedule_results = self._schedule_tasks(decision.scheduled_tasks)
        return {
            "alert_id": alert.alert_id,
            "status": "completed" if result.success else "failed",
            "action": result.action,
            "success": result.success,
            "message": result.message,
            "scheduled_tasks": schedule_results,
        }

    def recent_events(self, limit: int = 50) -> List[dict]:
        """Return recent persisted domain events."""
        return self.plugins.state_sink.recent(limit=limit)

    def health(self) -> dict:
        """Return runtime health summary."""
        return {
            "sources": [plugin.name for plugin in self.plugins.alert_sources],
            "context_providers": [plugin.name for plugin in self.plugins.context_providers],
            "actions": sorted(self.plugins.action_handlers.keys()),
            "schedulers": [plugin.name for plugin in self.plugins.schedulers],
            "sink": self.plugins.state_sink.health(),
        }

    def _emit(self, event_type: str, **payload: object) -> None:
        event = DomainEvent(event_type=event_type, payload=dict(payload))
        self.plugins.state_sink.append([event.to_dict()])

    def _schedule_tasks(self, tasks: List[ScheduledTask]) -> List[dict]:
        results: List[dict] = []
        if not tasks:
            return results

        for task in tasks:
            if not self.plugins.schedulers:
                result = {
                    "task": task.to_dict(),
                    "success": False,
                    "message": "No scheduler registered",
                }
                self._emit("scheduled_task_missing", scheduled_task=task.to_dict(), result=result)
                results.append(result)
                continue

            task_result: dict | None = None
            for scheduler in self.plugins.schedulers:
                task_result = dict(scheduler.schedule(task))
                task_result.setdefault("scheduler", scheduler.name)
                if task_result.get("success"):
                    break
            if task_result is None:
                task_result = {
                    "success": False,
                    "message": "Scheduler chain returned no result",
                }
            self._emit("scheduled_task_created", scheduled_task=task.to_dict(), result=task_result)
            results.append(task_result)

        return results