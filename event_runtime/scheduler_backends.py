"""Scheduler backend implementations for the event runtime."""

from __future__ import annotations

import json
import os
import threading
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from .defaults import _parse_timestamp, _scheduled_alert_from_task, _scheduled_task_id, _scheduled_task_view, _utc_now
from .models import Alert, ScheduledTask
from .plugins import AlertSource, Scheduler

_SPOOL_LOCKS: dict[str, threading.Lock] = {}
_SPOOL_LOCKS_GUARD = threading.Lock()


def _spool_lock_for(path: Path) -> threading.Lock:
    key = str(path.expanduser())
    with _SPOOL_LOCKS_GUARD:
        lock = _SPOOL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SPOOL_LOCKS[key] = lock
        return lock


def _append_json_line(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _spool_lock_for(path)
    with lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _emit_scheduled_task(spool_path: str, task_payload: Dict[str, Any]) -> None:
    payload = dict(task_payload or {})
    task_id = str(payload.get("task_id") or _scheduled_task_id(payload))
    payload["task_id"] = task_id
    _append_json_line(
        Path(spool_path),
        {
            "task_id": task_id,
            "scheduled_for": _utc_now().isoformat(),
            "task": payload,
        },
    )


class APSchedulerScheduler(Scheduler, AlertSource):
    """Scheduler backend backed by APScheduler with durable job storage."""

    name = "apscheduler-scheduler"

    def __init__(
        self,
        *,
        jobstore_url: str,
        spool_path: str,
        misfire_grace_time_seconds: int = 300,
    ):
        if not str(jobstore_url or "").strip():
            raise ValueError("jobstore_url is required for APSchedulerScheduler")

        try:
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:
            raise RuntimeError(
                "APScheduler backend requested but apscheduler with SQLAlchemy support is not installed"
            ) from exc

        self.jobstore_url = str(jobstore_url).strip()
        self.spool_path = Path(spool_path)
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.misfire_grace_time_seconds = max(1, int(misfire_grace_time_seconds))
        self._BackgroundScheduler = BackgroundScheduler
        self._CronTrigger = CronTrigger
        self._scheduler = BackgroundScheduler(
            timezone=timezone.utc,
            jobstores={"default": SQLAlchemyJobStore(url=self.jobstore_url)},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": self.misfire_grace_time_seconds,
            },
        )
        self._started = False
        self._lock = _spool_lock_for(self.spool_path)
        self._ensure_jobstore_parent_dir()

    def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False

    def schedule(self, task: ScheduledTask) -> Dict[str, object]:
        payload = task.to_dict()
        payload.setdefault("task_type", "cron")
        payload["task_id"] = _scheduled_task_id(payload)
        payload["created_at"] = _utc_now().isoformat()
        task_type = str(payload.get("task_type") or "cron")
        if task_type != "cron":
            return {
                "success": False,
                "message": f"unsupported task_type: {task_type}",
                "task": payload,
            }

        try:
            trigger = self._CronTrigger.from_crontab(payload["schedule"], timezone=timezone.utc)
            self._scheduler.add_job(
                _emit_scheduled_task,
                trigger=trigger,
                id=str(payload["task_id"]),
                replace_existing=True,
                kwargs={
                    "spool_path": str(self.spool_path),
                    "task_payload": payload,
                },
                coalesce=True,
                max_instances=1,
                misfire_grace_time=self.misfire_grace_time_seconds,
            )
        except Exception as exc:
            return {
                "success": False,
                "message": f"Failed to schedule task: {exc}",
                "task": payload,
            }

        return {
            "success": True,
            "message": "Scheduled task stored in APScheduler job store",
            "task": payload,
        }

    def list_tasks(self, limit: int = 100) -> list[Dict[str, object]]:
        entries: list[Dict[str, object]] = []
        for job in self._scheduler.get_jobs():
            payload = dict(job.kwargs.get("task_payload") or {})
            if not payload:
                continue
            entry = _scheduled_task_view(
                payload,
                scheduler=self.name,
                next_run_at=getattr(job, "next_run_time", None),
                run_count=payload.get("run_count") or 0,
            )
            entry["job_id"] = job.id
            entries.append(entry)
        entries.sort(key=lambda item: (item.get("next_run_at") is None, str(item.get("next_run_at") or ""), str(item.get("name") or "")))
        return entries[: max(0, limit)]

    def poll(self) -> Iterable[Alert]:
        events = self._drain_spool()
        alerts: list[Alert] = []
        for event in events:
            task_payload = dict(event.get("task") or {})
            if not task_payload:
                continue
            task_id = str(event.get("task_id") or task_payload.get("task_id") or _scheduled_task_id(task_payload))
            scheduled_for = _parse_timestamp(event.get("scheduled_for"))
            alerts.append(_scheduled_alert_from_task(task_payload, task_id=task_id, scheduled_for=scheduled_for))
        return alerts

    def _drain_spool(self) -> list[Dict[str, Any]]:
        if not self.spool_path.exists():
            return []
        with self._lock:
            raw_lines = self.spool_path.read_text(encoding="utf-8").splitlines()
            if not raw_lines:
                return []
            self.spool_path.write_text("", encoding="utf-8")

        events: list[Dict[str, Any]] = []
        for line in raw_lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def _ensure_jobstore_parent_dir(self) -> None:
        if not self.jobstore_url.startswith("sqlite:///"):
            return
        sqlite_path = Path(self.jobstore_url.partition("sqlite:///")[2]).expanduser()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)