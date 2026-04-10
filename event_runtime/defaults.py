"""Portable default plugins for the event runtime."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from .host_observability import build_host_observability_plugins
from .dedupe import FileBackedCooldownPolicy
from .models import ActionRequest, ActionResult, Alert, AlertSeverity, ContextEnvelope, Decision, ScheduledTask
from .plugins import ActionHandler, AlertSource, ContextProvider, DecisionEngine, HostObservabilityProvider, Scheduler


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(raw: object) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return _utc_now()
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cron_weekday(value: datetime) -> int:
    return (value.weekday() + 1) % 7


def _expand_cron_field(field: str, minimum: int, maximum: int, *, allow_sunday_7: bool = False) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        token = part.strip()
        if not token:
            continue
        step = 1
        base = token
        if "/" in token:
            base, raw_step = token.split("/", 1)
            step = max(1, int(raw_step))

        if base in {"", "*"}:
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(base)

        if allow_sunday_7:
            if start == 7:
                start = 0
            if end == 7:
                end = 0

        if start < minimum or start > maximum or end < minimum or end > maximum:
            raise ValueError(f"cron field out of range: {field}")

        if start <= end:
            values.update(range(start, end + 1, step))
        else:
            values.update(range(start, maximum + 1, step))
            values.update(range(minimum, end + 1, step))

    return values


def _cron_matches(schedule: str, value: datetime) -> bool:
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported schedule format: {schedule}")
    minute, hour, day, month, weekday = fields
    return (
        value.minute in _expand_cron_field(minute, 0, 59)
        and value.hour in _expand_cron_field(hour, 0, 23)
        and value.day in _expand_cron_field(day, 1, 31)
        and value.month in _expand_cron_field(month, 1, 12)
        and _cron_weekday(value) in _expand_cron_field(weekday, 0, 6, allow_sunday_7=True)
    )


def _next_cron_run(schedule: str, *, after: datetime) -> datetime:
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if _cron_matches(schedule, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"unable to resolve next run for schedule: {schedule}")


def _scheduled_task_id(task_payload: Dict[str, Any]) -> str:
    stable = {
        "name": task_payload.get("name"),
        "schedule": task_payload.get("schedule"),
        "task_type": task_payload.get("task_type") or "cron",
        "target": task_payload.get("target") or {},
        "parameters": task_payload.get("parameters") or {},
    }
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _scheduled_requested_checks(parameters: Dict[str, Any]) -> list[str]:
    checks = parameters.get("requested_checks")
    if isinstance(checks, list):
        return [str(item) for item in checks if str(item).strip()]
    if parameters.get("check"):
        return [str(parameters["check"])]
    return []


def _scheduled_alert_from_task(task_payload: Dict[str, Any], *, task_id: str, scheduled_for: datetime) -> Alert:
    parameters = dict(task_payload.get("parameters") or {})
    target = dict(task_payload.get("target") or {})
    resource_type = target.get("kind")
    resource_name = target.get("name")
    namespace = target.get("namespace")
    target_parts = [str(part) for part in (resource_type, namespace, resource_name) if part]
    summary = f"Scheduled check: {task_payload.get('name') or 'follow-up'}"
    if target_parts:
        summary = f"{summary} ({'/'.join(target_parts)})"

    severity_raw = str(parameters.get("severity") or "warning").lower()
    try:
        severity = AlertSeverity(severity_raw)
    except ValueError:
        severity = AlertSeverity.WARNING

    details: Dict[str, Any] = {
        "reasoning": str(task_payload.get("rationale") or "Scheduled follow-up check triggered."),
        "requested_action": str(parameters.get("requested_action") or parameters.get("action") or "investigate"),
        "scheduled_task": dict(task_payload),
        "task_id": task_id,
        "scheduled_for": scheduled_for.isoformat(),
    }
    requested_checks = _scheduled_requested_checks(parameters)
    if requested_checks:
        details["requested_checks"] = requested_checks
    action_params = parameters.get("action_params")
    if isinstance(action_params, dict) and action_params:
        details["action_params"] = dict(action_params)

    for host_key in ("host", "hostname", "address", "instance"):
        if target.get(host_key):
            details[host_key] = str(target[host_key])
            break

    fingerprint = hashlib.sha256(f"{task_id}:{scheduled_for.isoformat()}".encode("utf-8")).hexdigest()
    return Alert(
        source="scheduler",
        severity=severity,
        summary=summary,
        details=details,
        namespace=None if namespace is None else str(namespace),
        resource_type=None if resource_type is None else str(resource_type),
        resource_name=None if resource_name is None else str(resource_name),
        fingerprint=fingerprint,
        occurred_at=scheduled_for,
    )


def _scheduled_task_view(
    task_payload: Dict[str, Any],
    *,
    scheduler: str,
    next_run_at: object = None,
    last_emitted_at: object = None,
    run_count: object = None,
    error: object = None,
) -> Dict[str, Any]:
    view = dict(task_payload)
    view.setdefault("task_type", "cron")
    view.setdefault("task_id", _scheduled_task_id(view))
    view["scheduler"] = scheduler

    if next_run_at not in {None, ""}:
        try:
            view["next_run_at"] = _parse_timestamp(next_run_at).isoformat()
        except Exception:
            view["next_run_at"] = str(next_run_at)
    if last_emitted_at not in {None, ""}:
        try:
            view["last_emitted_at"] = _parse_timestamp(last_emitted_at).isoformat()
        except Exception:
            view["last_emitted_at"] = str(last_emitted_at)
    if run_count is not None:
        view["run_count"] = int(run_count)
    if error:
        view["error"] = str(error)
    return view


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


class JsonFileScheduler(Scheduler, AlertSource):
    """Portable scheduler that stores task intents and emits due follow-up alerts."""

    name = "json-file-scheduler"

    def __init__(self, directory: str | None = None):
        if directory is None:
            directory = str(Path.home() / ".cfoperator" / "event-runtime" / "scheduled")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tasks.jsonl"
        self.state_path = self.directory / "state.json"
        self._lock = threading.Lock()

    def schedule(self, task: ScheduledTask) -> Dict[str, object]:
        payload = task.to_dict()
        payload.setdefault("task_type", "cron")
        payload["task_id"] = _scheduled_task_id(payload)
        payload["created_at"] = _utc_now().isoformat()
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

    def poll(self) -> Iterable[Alert]:
        now = _utc_now()
        with self._lock:
            tasks = self._read_tasks_locked()
            state = self._read_state_locked()
            alerts: list[Alert] = []
            next_state: Dict[str, Dict[str, object]] = {}

            for task_id, task_payload in tasks.items():
                task_type = str(task_payload.get("task_type") or "cron")
                if task_type != "cron":
                    next_state[task_id] = {
                        **dict(state.get(task_id) or {}),
                        "task_id": task_id,
                        "task_type": task_type,
                        "error": f"unsupported task_type: {task_type}",
                    }
                    continue

                try:
                    created_at = _parse_timestamp(task_payload.get("created_at"))
                    current_state = dict(state.get(task_id) or {})
                    next_run_raw = current_state.get("next_run_at")
                    next_run_at = _parse_timestamp(next_run_raw) if next_run_raw else _next_cron_run(task_payload["schedule"], after=created_at)
                    run_count = int(current_state.get("run_count") or 0)

                    if next_run_at <= now:
                        alerts.append(_scheduled_alert_from_task(task_payload, task_id=task_id, scheduled_for=next_run_at))
                        run_count += 1
                        last_emitted_at = next_run_at
                        next_run_at = _next_cron_run(task_payload["schedule"], after=next_run_at)
                    else:
                        last_emitted_at = current_state.get("last_emitted_at")

                    next_state[task_id] = {
                        "task_id": task_id,
                        "task_type": task_type,
                        "last_emitted_at": last_emitted_at if isinstance(last_emitted_at, str) else getattr(last_emitted_at, "isoformat", lambda: None)(),
                        "next_run_at": next_run_at.isoformat(),
                        "run_count": run_count,
                    }
                except Exception as exc:
                    next_state[task_id] = {
                        **dict(state.get(task_id) or {}),
                        "task_id": task_id,
                        "task_type": task_type,
                        "error": str(exc),
                    }

            self._write_state_locked(next_state)
            return alerts

    def list_tasks(self, limit: int = 100) -> list[Dict[str, object]]:
        with self._lock:
            tasks = self._read_tasks_locked()
            state = self._read_state_locked()

        entries: list[Dict[str, object]] = []
        for task_id, task_payload in tasks.items():
            current_state = dict(state.get(task_id) or {})
            task_type = str(task_payload.get("task_type") or "cron")
            next_run_at: object = current_state.get("next_run_at")
            error: object = current_state.get("error")

            if next_run_at in {None, ""} and not error and task_type == "cron":
                try:
                    created_at = _parse_timestamp(task_payload.get("created_at"))
                    next_run_at = _next_cron_run(task_payload["schedule"], after=created_at)
                except Exception as exc:
                    error = str(exc)

            entries.append(
                _scheduled_task_view(
                    task_payload,
                    scheduler=self.name,
                    next_run_at=next_run_at,
                    last_emitted_at=current_state.get("last_emitted_at"),
                    run_count=current_state.get("run_count") or 0,
                    error=error,
                )
            )

        entries.sort(key=lambda item: (item.get("next_run_at") is None, str(item.get("next_run_at") or ""), str(item.get("name") or "")))
        return entries[: max(0, limit)]

    def _read_tasks_locked(self) -> Dict[str, Dict[str, Any]]:
        tasks: Dict[str, Dict[str, Any]] = {}
        if not self.path.exists():
            return tasks
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        continue
                    payload.setdefault("task_type", "cron")
                    task_id = str(payload.get("task_id") or _scheduled_task_id(payload))
                    payload["task_id"] = task_id
                    tasks[task_id] = payload
        except Exception:
            return {}
        return tasks

    def _read_state_locked(self) -> Dict[str, Dict[str, Any]]:
        if not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}
        except Exception:
            return {}
        return {}

    def _write_state_locked(self, state: Dict[str, Dict[str, Any]]) -> None:
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())


def build_default_action_handlers() -> Dict[str, ActionHandler]:
    """Return the portable default safe action handlers."""
    handlers = [InvestigateActionHandler(), NotifyActionHandler(), LogOnlyActionHandler()]
    return {handler.action_name: handler for handler in handlers}


def build_default_alert_policies(base_dir: str | None = None) -> list[FileBackedCooldownPolicy]:
    """Return portable default alert policies."""
    if base_dir is None:
        base_dir = str(Path.home() / ".cfoperator" / "event-runtime")
    cooldown_seconds = int(os.getenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "300"))
    if cooldown_seconds <= 0:
        return []
    return [
        FileBackedCooldownPolicy(
            path=str(Path(base_dir) / "policies" / "dedupe.json"),
            cooldown_seconds=cooldown_seconds,
        )
    ]


def build_default_host_observability_plugins(
    config_path: str | None = None,
) -> tuple[list[HostObservabilityProvider], ContextProvider | None]:
    """Return portable bare-metal observability providers plus a context provider."""
    return build_host_observability_plugins(config_path=config_path)