"""Minimal orchestration engine for the modular event-driven runtime."""

from __future__ import annotations

from dataclasses import asdict
import logging
from time import perf_counter
from typing import Dict, List

from .activity import build_activity_feed, filter_activities, filter_events
from .models import ActionRequest, ActionResult, Alert, AlertSeverity, ContextEnvelope, DomainEvent, ScheduledTask
from .notifications import should_notify
from .plugin_manager import PluginManager
from .telemetry import (
    initialize_runtime_info,
    mark_runtime_down,
    mark_runtime_up,
    observe_alert_received,
    observe_alert_result,
    observe_decision,
    observe_event_recorded,
    observe_notification,
    observe_scheduled_task,
)


logger = logging.getLogger(__name__)


class EventRuntime:
    """Coordinate alert intake, gating, context, decisions, and actions."""

    def __init__(self, plugins: PluginManager):
        if plugins.state_sink is None:
            raise ValueError("EventRuntime requires a registered state sink")
        if plugins.decision_engine is None:
            raise ValueError("EventRuntime requires a registered decision engine")
        self.plugins = plugins
        self._started = False

    def start(self) -> None:
        """Start registered plugins once for transports that need runtime lifecycle hooks."""
        if self._started:
            return
        initialize_runtime_info()
        self.plugins.start_all()
        self._started = True
        mark_runtime_up()

    def stop(self) -> None:
        """Stop registered plugins when the runtime transport shuts down."""
        if not self._started:
            return
        self.plugins.stop_all()
        self._started = False
        mark_runtime_down()

    def poll_sources(self) -> List[Dict[str, object]]:
        """Poll all registered alert sources and process emitted alerts."""
        results: List[Dict[str, object]] = []
        for source in self.plugins.alert_sources:
            for alert in source.poll():
                results.append(self.handle_alert(alert))
        return results

    def handle_alert(self, alert: Alert) -> Dict[str, object]:
        """Process a single normalized alert end-to-end."""
        started = perf_counter()
        result: Dict[str, object] | None = None
        observe_alert_received(alert)
        self.record_event("alert_received", alert=alert.to_dict())
        try:
            for policy in self.plugins.alert_policies:
                allowed, reason = policy.evaluate(alert)
                if not allowed:
                    self.record_event(
                        "alert_suppressed",
                        alert=alert.to_dict(),
                        policy=policy.name,
                        reason=reason or "suppressed",
                    )
                    result = {
                        "alert_id": alert.alert_id,
                        "status": "suppressed",
                        "action": "suppressed",
                        "success": True,
                        "reason": reason or "suppressed",
                    }
                    return result

            if alert.severity is AlertSeverity.INFO:
                self.record_event("alert_skipped", alert=alert.to_dict(), reason="severity_gate")
                result = {
                    "alert_id": alert.alert_id,
                    "status": "logged",
                    "action": "log_only",
                    "success": True,
                }
                return result

            envelope = ContextEnvelope(alert=alert)
            for provider in self.plugins.context_providers:
                envelope = provider.provide(alert, envelope)

            decision = self.plugins.decision_engine.decide(envelope)
            observe_decision(decision.action)
            self.record_event(
                "decision_made",
                alert=alert.to_dict(),
                decision=asdict(decision),
            )
            if decision.requested_checks:
                self.record_event(
                    "checks_requested",
                    alert=alert.to_dict(),
                    checks=list(decision.requested_checks),
                )

            handler = self.plugins.action_handlers.get(decision.action)
            if handler is None:
                self.record_event(
                    "action_missing",
                    alert=alert.to_dict(),
                    decision=asdict(decision),
                )
                result = {
                    "alert_id": alert.alert_id,
                    "status": "failed",
                    "action": decision.action,
                    "success": False,
                    "error": f"No action handler registered for {decision.action}",
                }
                return result

            request = ActionRequest(alert=alert, decision=decision, context=envelope)
            action_result = handler.execute(request)
            self.record_event(
                "action_completed",
                alert=alert.to_dict(),
                decision=asdict(decision),
                result=action_result.to_dict(),
            )
            self._notify_action_completed(alert, action_result)
            schedule_results = self._schedule_tasks(decision.scheduled_tasks)
            result = {
                "alert_id": alert.alert_id,
                "status": "completed" if action_result.success else "failed",
                "action": action_result.action,
                "success": action_result.success,
                "message": action_result.message,
                "scheduled_tasks": schedule_results,
            }
            return result
        except Exception:
            logger.exception("Failed to handle alert %s", alert.alert_id)
            observe_alert_result(status="error", action="error", duration_seconds=perf_counter() - started)
            raise
        finally:
            if result is not None:
                observe_alert_result(
                    status=result.get("status", "unknown"),
                    action=result.get("action", "unknown"),
                    duration_seconds=perf_counter() - started,
                )

    def recent_events(
        self,
        limit: int = 50,
        *,
        event_type: str | None = None,
        alert_id: str | None = None,
        job_id: str | None = None,
    ) -> List[dict]:
        """Return recent persisted domain events, optionally filtered by type or alert."""
        fetch_limit = max(limit, limit * 10) if any([event_type, alert_id, job_id]) else limit
        events = self.plugins.state_sink.recent(limit=fetch_limit)
        return filter_events(events, event_type=event_type, alert_id=alert_id, job_id=job_id)[:limit]

    def recent_activity(
        self,
        limit: int = 25,
        *,
        status: str | None = None,
        action: str | None = None,
        event_limit: int | None = None,
    ) -> List[dict]:
        """Return a human-readable activity feed derived from recent events."""
        fetch_limit = max(limit * 12, 100)
        if event_limit is not None:
            fetch_limit = max(limit, event_limit)
        activities = build_activity_feed(self.plugins.state_sink.recent(limit=fetch_limit), limit=fetch_limit)
        return filter_activities(activities, status=status, action=action)[:limit]

    def health(self) -> dict:
        """Return runtime health summary."""
        return {
            "sources": [plugin.name for plugin in self.plugins.alert_sources],
            "policies": [plugin.name for plugin in self.plugins.alert_policies],
            "host_observability_providers": [plugin.name for plugin in self.plugins.host_observability_providers],
            "context_providers": [plugin.name for plugin in self.plugins.context_providers],
            "actions": sorted(self.plugins.action_handlers.keys()),
            "schedulers": [plugin.name for plugin in self.plugins.schedulers],
            "sink": self.plugins.state_sink.health(),
        }

    def record_event(self, event_type: str, **payload: object) -> None:
        """Append an explicit domain event to the configured state sink."""
        event = DomainEvent(event_type=event_type, payload=dict(payload))
        self.plugins.state_sink.append([event.to_dict()])
        observe_event_recorded(event_type)

    def _notify_action_completed(self, alert: Alert, action_result: ActionResult) -> None:
        """Best-effort notification dispatch after an action completes."""
        if not self.plugins.notification_sinks:
            return
        if not should_notify(action_result.action, action_result.success):
            return

        severity = alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity)
        status = "completed" if action_result.success else "failed"
        summary = f"Action {status}: {action_result.action}"
        details = {
            "alert_summary": alert.summary,
            "action": action_result.action,
            "result_message": action_result.message,
            "result_details": action_result.details,
        }

        for sink in self.plugins.notification_sinks:
            try:
                ok = sink.notify(summary, severity=severity, details=details)
                observe_notification(sink.name, "success" if ok else "error")
            except Exception:
                logger.warning("Notification sink %s failed", sink.name, exc_info=True)
                observe_notification(sink.name, "error")

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
                self.record_event("scheduled_task_missing", scheduled_task=task.to_dict(), result=result)
                observe_scheduled_task("missing", False)
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
            self.record_event("scheduled_task_created", scheduled_task=task.to_dict(), result=task_result)
            observe_scheduled_task(task_result.get("scheduler", "unknown"), bool(task_result.get("success")))
            results.append(task_result)

        return results