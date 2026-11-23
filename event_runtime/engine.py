"""Minimal orchestration engine for the modular event-driven runtime."""

from __future__ import annotations

from dataclasses import asdict
import logging
import os
from time import monotonic, perf_counter
from typing import Dict, List, Optional

from cfshared import tracing

from .activity import build_activity_feed, filter_activities, filter_events
from .alert_store import AlertPage, AlertQuery, AlertStoreUnavailable
from .deadline import DeadlineExceeded, SingleFlight
from .escalation import EscalationLedger
from .models import ActionRequest, ActionResult, Alert, AlertSeverity, ContextEnvelope, Decision, DomainEvent, ScheduledTask
from .notifications import should_notify
from .plugin_manager import PluginManager


def _digest_low_severity() -> bool:
    """Whether Tier-2 routing is on (default yes). Set CFOP_DIGEST_LOW_SEVERITY=0
    to send every completion in real time (the old behaviour)."""
    return os.getenv("CFOP_DIGEST_LOW_SEVERITY", "true").strip().lower() not in ("0", "false", "no")


def _is_realtime_worthy(severity: str, outcome: str, is_resolution: bool) -> bool:
    """Tier-2 severity→channel: real-time red only for things needing attention
    now. Routine resolutions / recovered-monitoring / info go to the digest
    (the morning summary), not an instant page. Warnings and above that aren't a
    quiet outcome still page — we only divert the clearly-low-signal classes."""
    if severity == "critical":
        return True
    if outcome in ("escalated", "failed", "needs_action"):
        return True
    if is_resolution or outcome in ("resolved", "monitoring"):
        return False
    if severity == "info":
        return False
    return True
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

# How long health() waits on the schedulers, all of them together. A scheduler's
# health can be a live query (the APScheduler jobstore is), and /health is the
# readiness probe, so this has to stay under the probe's timeoutSeconds or the
# kubelet hangs up before the answer is written. tests/test_event_runtime_health.py
# holds it under the chart's.
HEALTH_CALL_TIMEOUT_SECONDS = 3.0


class EventRuntime:
    """Coordinate alert intake, gating, context, decisions, and actions."""

    def __init__(self, plugins: PluginManager, escalation_ledger: EscalationLedger | None = None):
        if plugins.state_sink is None:
            raise ValueError("EventRuntime requires a registered state sink")
        if plugins.decision_engine is None:
            raise ValueError("EventRuntime requires a registered decision engine")
        self.plugins = plugins
        # Shared with the Alertmanager source so it can emit a one-time
        # "Resolved:" notification when an escalated alert clears (None = off).
        self._escalation_ledger = escalation_ledger
        self._started = False
        self.health_call_timeout_seconds = HEALTH_CALL_TIMEOUT_SECONDS
        # One per scheduler, keyed by identity: see _scheduler_health.
        self._scheduler_health_calls: Dict[int, SingleFlight] = {}

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

    def handle_alert(self, alert: Alert, *, admitted: bool = False) -> Dict[str, object]:
        """Process a single normalized alert end-to-end.

        ``admitted`` skips the alert policies, for a rerun of an alert they
        already let through. They are stateful: the cooldown claimed the
        fingerprint on the first run, so evaluating them again suppresses the
        alert as a duplicate of itself (CFOP-395).

        One alert is one trace (CFOP-317): this ``alert`` root is what the
        agent's ``triage`` and ``investigation`` spans hang under, over the
        traceparent the outbound calls carry. Shapes only: the alert's
        attributes are the redacted, clipped set ``tracing.alert_attributes``
        allows, plus the status and action the pass ended with.
        """
        with tracing.span('alert', tracing.alert_attributes(alert.to_dict())):
            result = self._handle_alert(alert, admitted=admitted)
            tracing.annotate({'cfop.alert.status': result.get('status'),
                              'cfop.alert.action': result.get('action')})
            if result.get('success') is False:
                tracing.mark_error('AlertHandlingFailed')
            return result

    def _handle_alert(self, alert: Alert, *, admitted: bool = False) -> Dict[str, object]:
        started = perf_counter()
        result: Dict[str, object] | None = None
        observe_alert_received(alert)
        self.record_event("alert_received", alert=alert.to_dict())
        try:
            for policy in ([] if admitted else self.plugins.alert_policies):
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

            # INFO-severity alerts normally bypass the decision flow as noise
            # control. Resolution alerts are an exception: they are synthesized
            # by sweep (info by definition — a resolution is not a firing alert)
            # but operators still want to see them in Slack with a clear
            # "Resolved:" prefix. The triage short-circuit on the agent side
            # makes this cheap (no LLM call).
            if alert.severity is AlertSeverity.INFO and not alert.details.get("resolution"):
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
            self._maybe_mark_escalation(alert, action_result)
            self.record_event(
                "action_completed",
                alert=alert.to_dict(),
                decision=asdict(decision),
                result=action_result.to_dict(),
            )
            self._observe_completion(alert, action_result)
            self._notify_action_completed(alert, action_result, decision=decision)
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
        """Return a human-readable activity feed, newest alert first.

        Served from the per-alert read model, so ``status`` and ``action``
        filter every alert rather than a window of recent events (CFOP-215).
        ``event_limit`` only matters on the fallback below, for a sink that
        keeps no read model: there the feed is still a window, as before.
        """
        try:
            return self.list_alerts(AlertQuery(limit=limit, status=status, action=action, full=True)).alerts
        except AlertStoreUnavailable:
            pass
        fetch_limit = max(limit * 12, 100)
        if event_limit is not None:
            fetch_limit = max(limit, event_limit)
        activities = build_activity_feed(self.plugins.state_sink.recent(limit=fetch_limit), limit=fetch_limit)
        return filter_activities(activities, status=status, action=action)[:limit]

    def list_alerts(self, query: AlertQuery) -> AlertPage:
        """One page of alerts by folded state. Raises AlertStoreUnavailable."""
        return self.plugins.state_sink.list_alerts(query)

    def get_alert(self, alert_id: str) -> Optional[dict]:
        """An alert's activity and every event behind it, or None."""
        return self.plugins.state_sink.get_alert(alert_id)

    def scheduled_tasks(self, limit: int = 100, *, scheduler_name: str | None = None) -> List[dict]:
        """Return scheduled tasks known to registered schedulers."""
        results: List[dict] = []
        remaining = max(0, limit)
        if remaining == 0:
            return results

        for scheduler in self.plugins.schedulers:
            if scheduler_name and scheduler.name != scheduler_name:
                continue
            entries = scheduler.list_tasks(limit=remaining)
            for entry in entries:
                payload = dict(entry)
                payload.setdefault("scheduler", scheduler.name)
                results.append(payload)
            remaining = max(0, limit - len(results))
            if remaining == 0:
                break
        return results

    def health(self) -> dict:
        """Return runtime health summary."""
        host_discovery_state = None
        for provider in self.plugins.context_providers:
            if hasattr(provider, "health") and callable(provider.health):
                state = provider.health()
                if isinstance(state, dict) and "discovered_targets_count" in state:
                    host_discovery_state = state
                    break

        scheduler_states = []
        # One deadline for the lot, so two slow schedulers cannot add up to
        # more than a probe will wait.
        deadline = monotonic() + self.health_call_timeout_seconds
        for scheduler in self.plugins.schedulers:
            scheduler_states.append(self._scheduler_health(scheduler, deadline))

        return {
            "sources": [plugin.name for plugin in self.plugins.alert_sources],
            "policies": [plugin.name for plugin in self.plugins.alert_policies],
            "host_observability_providers": [plugin.name for plugin in self.plugins.host_observability_providers],
            "context_providers": [plugin.name for plugin in self.plugins.context_providers],
            "actions": sorted(self.plugins.action_handlers.keys()),
            "notification_sinks": [plugin.name for plugin in self.plugins.notification_sinks],
            "completion_observers": [plugin.name for plugin in self.plugins.completion_observers],
            "schedulers": [plugin.name for plugin in self.plugins.schedulers],
            "scheduler": scheduler_states,
            "host_discovery": host_discovery_state,
            "sink": self.plugins.state_sink.health(),
        }

    def _scheduler_health(self, scheduler, deadline: float) -> dict:
        """One scheduler's health entry, answered by ``deadline`` whatever it does.

        A scheduler that raises was always reported in the payload and never
        failed the request. One that hangs was not: its query sat on a dead
        connection, the probe that asked gave up, and the next probe started
        another (CFOP-768). So the call is shared and bounded, and a hang reads
        like any other failed check.
        """
        call = self._scheduler_health_calls.get(id(scheduler))
        if call is None:
            # setdefault, so two probes racing here end up sharing one.
            call = self._scheduler_health_calls.setdefault(
                id(scheduler),
                SingleFlight(scheduler.health, name=f"health-{scheduler.name}"),
            )
        try:
            return call.call(deadline - monotonic())
        except DeadlineExceeded:
            if call.first_report():
                logger.warning(
                    "scheduler %s did not answer its health check within %.1fs; "
                    "reporting it as timed out until that call returns",
                    scheduler.name, self.health_call_timeout_seconds,
                )
            return {"name": scheduler.name, "error": "health check timed out"}
        except Exception:
            return {"name": scheduler.name, "error": "health check failed"}

    def record_event(self, event_type: str, **payload: object) -> None:
        """Append an explicit domain event to the configured state sink."""
        event = DomainEvent(event_type=event_type, payload=dict(payload))
        self.plugins.state_sink.append([event.to_dict()])
        observe_event_recorded(event_type)

    def record_external_action_completion(self, alert: Alert, action_result: ActionResult) -> None:
        """Record + notify for an action completed by an external executor.

        Used by the HTTP completion endpoint when the agent posts back the
        result of an investigation it ran asynchronously. Mirrors the
        in-process action_completed flow so the activity feed and
        notification path are identical regardless of executor.
        """
        self._maybe_mark_escalation(alert, action_result)
        self.record_event(
            "action_completed",
            alert=alert.to_dict(),
            result=action_result.to_dict(),
            source="external",
        )
        self._observe_completion(alert, action_result)
        self._notify_action_completed(alert, action_result)

    def _observe_completion(self, alert: Alert, action_result: ActionResult) -> None:
        """Tell completion observers, before any notification policy applies (CFOP-212)."""
        if action_result.quiet:
            return
        for observer in self.plugins.completion_observers:
            try:
                observer.observe(alert, action_result)
            except Exception:
                logger.warning("Completion observer %s failed", observer.name, exc_info=True)

    def _maybe_mark_escalation(self, alert: Alert, action_result: ActionResult) -> None:
        """Record an escalated alert's fingerprint so its later resolution can
        be announced. Keyed on the same ``outcome`` signal the digest router
        uses (``action_result.details["outcome"] == "escalated"``). No-op unless
        an escalation ledger is wired and the alert carries a fingerprint."""
        if self._escalation_ledger is None or not alert.fingerprint:
            return
        details = action_result.details if isinstance(action_result.details, dict) else {}
        if str(details.get("outcome") or "") == "escalated":
            self._escalation_ledger.mark(alert.fingerprint)

    def _notify_action_completed(
        self,
        alert: Alert,
        action_result: ActionResult,
        decision: "Optional[Decision]" = None,
    ) -> None:
        """Best-effort notification dispatch after an action completes.

        ``decision`` is optional so the external-completion path (where the
        original Decision is no longer in scope) can still call us without
        triage attribution. When provided, the Decision's params surface
        in the notification details so the Slack formatter can show
        "triaged by <backend>/<model>".
        """
        if not self.plugins.notification_sinks:
            return
        if not should_notify(action_result.action, action_result.success, quiet=action_result.quiet):
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
        if decision is not None and decision.params:
            details["decision_params"] = dict(decision.params)
        # Hoist operator-facing hints so the notification formatter can render
        # them. ``remediation`` becomes the "Recommendation:" line; ``resolution``
        # flips the prefix from "[severity]"/"Alert:" to
        # ":white_check_mark: Resolved:". A sweep finding carries its remediation
        # on the original Alert; a direct /investigate carries it on the
        # returned ActionResult details, so check both (Alert wins).
        result_details = action_result.details if isinstance(action_result.details, dict) else {}
        remediation = (
            alert.details.get("remediation")
            or result_details.get("remediation")
            or result_details.get("recommendation")
        )
        if remediation:
            details["recommendation"] = str(remediation)
        if alert.details.get("resolution"):
            details["resolution"] = True

        # Tier-2 noise routing: divert low-signal completions (resolutions,
        # recovered-monitoring, info) to the digest instead of paging real time.
        # The item is still recorded in activity/history; the morning summary
        # surfaces it. Critical/escalated/needs_action/failed/warnings still page.
        outcome = str(result_details.get("outcome") or "")
        if _digest_low_severity() and not _is_realtime_worthy(
            severity, outcome, bool(alert.details.get("resolution"))
        ):
            observe_notification("digest", "suppressed")
            logger.info(
                "Tier-2 digest (suppressed real-time): severity=%s outcome=%s :: %s",
                severity, outcome or "-", (alert.summary or "")[:80],
            )
            return

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