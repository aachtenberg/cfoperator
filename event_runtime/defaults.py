"""Portable default plugins for the event runtime."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Dict

from .models import ActionRequest, ActionResult, Alert, ContextEnvelope, Decision, ScheduledTask
from .plugins import ActionHandler, ContextProvider, DecisionEngine, Scheduler


class HostContextProvider(ContextProvider):
    """Adds lightweight host context without external dependencies."""

    name = "host-context"
    capabilities = ("host", "filesystem", "runtime")

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        envelope.context.setdefault("hostname", socket.gethostname())
        envelope.context.setdefault("pid", os.getpid())
        envelope.context.setdefault("source", alert.source)
        return envelope


class OpenReasoningDecisionEngine(DecisionEngine):
    """Portable fallback decision engine with minimal hardcoded policy."""

    name = "open-reasoning"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        details = envelope.alert.details
        action = str(details.get("requested_action") or details.get("action") or "investigate")
        requested_checks = list(details.get("requested_checks") or [])

        scheduled_tasks = []
        for item in details.get("scheduled_tasks") or []:
            if not isinstance(item, dict):
                continue
            if "name" not in item or "schedule" not in item or "rationale" not in item:
                continue
            scheduled_tasks.append(
                ScheduledTask(
                    name=str(item["name"]),
                    schedule=str(item["schedule"]),
                    rationale=str(item["rationale"]),
                    task_type=str(item.get("task_type") or "cron"),
                    target=dict(item.get("target") or {}),
                    parameters=dict(item.get("parameters") or {}),
                )
            )

        confidence = float(details.get("confidence") or 0.75)
        confidence = max(0.0, min(1.0, confidence))
        reasoning = str(details.get("reasoning") or f"Portable decision engine selected action '{action}'.")

        return Decision(
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            params=dict(details.get("action_params") or {}),
            requested_checks=requested_checks,
            scheduled_tasks=scheduled_tasks,
        )


class InvestigateActionHandler(ActionHandler):
    """Portable safe action handler for investigation workflows."""

    name = "investigate-action"
    action_name = "investigate"

    def execute(self, request: ActionRequest) -> ActionResult:
        summary = request.alert.summary
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Investigation recorded for: {summary}",
            details={
                "context": request.context.context,
                "decision": request.decision.reasoning,
            },
        )


class NotifyActionHandler(ActionHandler):
    """Portable no-op notification handler that records intent."""

    name = "notify-action"
    action_name = "notify"

    def execute(self, request: ActionRequest) -> ActionResult:
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Notification requested for: {request.alert.summary}",
            details={"params": request.decision.params},
        )


class LogOnlyActionHandler(ActionHandler):
    """Portable action handler for explicit log-only decisions."""

    name = "log-only-action"
    action_name = "log_only"

    def execute(self, request: ActionRequest) -> ActionResult:
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Logged alert: {request.alert.summary}",
            details={},
        )


class JsonFileScheduler(Scheduler):
    """Portable scheduler that stores task intents in a JSONL file."""

    name = "json-file-scheduler"

    def __init__(self, directory: str | None = None):
        if directory is None:
            directory = str(Path.home() / ".cfoperator" / "event-runtime" / "scheduled")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tasks.jsonl"
        self._lock = threading.Lock()

    def schedule(self, task: ScheduledTask) -> Dict[str, object]:
        payload = task.to_dict()
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return {
            "success": True,
            "message": f"Scheduled task stored in {self.path}",
            "task": payload,
        }


def build_default_action_handlers() -> Dict[str, ActionHandler]:
    """Return the portable default safe action handlers."""
    handlers = [InvestigateActionHandler(), NotifyActionHandler(), LogOnlyActionHandler()]
    return {handler.action_name: handler for handler in handlers}