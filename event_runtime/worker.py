"""Background worker queue for asynchronous alert processing."""

from __future__ import annotations

import json
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from .engine import EventRuntime
from .models import Alert, AlertSeverity


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(raw: Optional[str]) -> datetime:
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _alert_from_dict(payload: dict) -> Alert:
    severity_value = str(payload.get("severity") or "info").lower()
    severity = AlertSeverity(severity_value)
    return Alert(
        source=str(payload.get("source") or "manual"),
        severity=severity,
        summary=str(payload.get("summary") or ""),
        details=dict(payload.get("details") or {}),
        namespace=payload.get("namespace"),
        resource_type=payload.get("resource_type"),
        resource_name=payload.get("resource_name"),
        fingerprint=payload.get("fingerprint"),
        occurred_at=_parse_datetime(payload.get("occurred_at")),
        alert_id=str(payload.get("alert_id") or str(uuid4())),
    )


@dataclass(slots=True)
class WorkerJob:
    """Tracked background alert job."""

    job_id: str
    alert: Alert
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "alert": self.alert.to_dict(),
        }

    def queue_delay_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        return max(0.0, (_parse_datetime(self.started_at) - _parse_datetime(self.created_at)).total_seconds())

    def processing_duration_seconds(self) -> Optional[float]:
        if not self.started_at or not self.completed_at:
            return None
        return max(0.0, (_parse_datetime(self.completed_at) - _parse_datetime(self.started_at)).total_seconds())

    def queued_age_seconds(self) -> Optional[float]:
        if self.status != "queued":
            return None
        return max(0.0, (datetime.now(timezone.utc) - _parse_datetime(self.created_at)).total_seconds())

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkerJob":
        job = cls(
            job_id=str(payload["job_id"]),
            alert=_alert_from_dict(dict(payload.get("alert") or {})),
            status=str(payload.get("status") or "queued"),
            created_at=str(payload.get("created_at") or _utc_now()),
            started_at=payload.get("started_at"),
            completed_at=payload.get("completed_at"),
            result=payload.get("result"),
            error=payload.get("error"),
        )
        if job.status in {"completed", "failed"}:
            job._done.set()
        return job


class FileBackedWorkerState:
    """Persist worker jobs so queued work survives process restarts."""

    def __init__(self, path: str | None = None):
        if path is None:
            path = str(Path.home() / ".cfoperator" / "event-runtime" / "queue" / "jobs.json")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load_jobs(self) -> Dict[str, WorkerJob]:
        with self._lock:
            return self._read_locked()

    def save_job(self, job: WorkerJob) -> None:
        with self._lock:
            jobs = self._read_locked()
            jobs[job.job_id] = job
            self._write_locked(jobs)

    def health(self) -> dict:
        with self._lock:
            jobs = self._read_locked()
        return {
            "path": str(self.path),
            "persisted_jobs": len(jobs),
        }

    def _read_locked(self) -> Dict[str, WorkerJob]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        jobs: Dict[str, WorkerJob] = {}
        for job_id, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            try:
                jobs[str(job_id)] = WorkerJob.from_dict(raw)
            except Exception:
                continue
        return jobs

    def _write_locked(self, jobs: Dict[str, WorkerJob]) -> None:
        payload = {job_id: job.to_dict() for job_id, job in jobs.items()}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())


class BackgroundAlertWorker:
    """Process alerts asynchronously while preserving runtime semantics."""

    def __init__(
        self,
        runtime: EventRuntime,
        worker_count: int = 1,
        max_queue_size: int = 1000,
        state: FileBackedWorkerState | None = None,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        self.runtime = runtime
        self.worker_count = worker_count
        self.max_queue_size = max_queue_size
        self.state = state or FileBackedWorkerState()
        self._queue: queue.Queue[WorkerJob] = queue.Queue(maxsize=max_queue_size)
        self._jobs: Dict[str, WorkerJob] = self.state.load_jobs()
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        self._restore_pending_jobs()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._run, daemon=True, name=f"event-runtime-worker-{index}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)  # type: ignore[arg-type]
            except queue.Full:
                break
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()

    def enqueue(self, alert: Alert) -> dict:
        job = WorkerJob(job_id=str(uuid4()), alert=alert)
        with self._jobs_lock:
            self._jobs[job.job_id] = job
            self.state.save_job(job)
        self._queue.put(job)
        self.runtime.record_event("alert_queued", job=job.to_dict())
        return job.to_dict()

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        return job.to_dict() if job else None

    def wait_for_job(self, job_id: str, timeout: float | None = None) -> Optional[dict]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        job._done.wait(timeout=timeout)
        return job.to_dict()

    def health(self) -> dict:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
            queued = sum(1 for job in self._jobs.values() if job.status == "queued")
            running = sum(1 for job in self._jobs.values() if job.status == "running")
            completed = sum(1 for job in self._jobs.values() if job.status == "completed")
            failed = sum(1 for job in self._jobs.values() if job.status == "failed")
        queue_delays = [value for job in jobs if (value := job.queue_delay_seconds()) is not None]
        processing_durations = [value for job in jobs if (value := job.processing_duration_seconds()) is not None]
        queued_ages = [value for job in jobs if (value := job.queued_age_seconds()) is not None]
        return {
            "enabled": True,
            "worker_count": self.worker_count,
            "max_queue_size": self.max_queue_size,
            "queue_size": self._queue.qsize(),
            "state": self.state.health(),
            "jobs": {
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
            },
            "metrics": {
                "oldest_queued_age_seconds": max(queued_ages) if queued_ages else 0.0,
                "average_queue_delay_seconds": round(sum(queue_delays) / len(queue_delays), 6) if queue_delays else 0.0,
                "average_processing_duration_seconds": round(sum(processing_durations) / len(processing_durations), 6) if processing_durations else 0.0,
            },
        }

    def _restore_pending_jobs(self) -> None:
        with self._jobs_lock:
            pending = [job for job in self._jobs.values() if job.status in {"queued", "running"}]
            for job in pending:
                job.status = "queued"
                job.started_at = None
                job.completed_at = None
                job.error = None
                job.result = None
                job._done.clear()
                self.state.save_job(job)
        for job in pending:
            self._queue.put(job)
            self.runtime.record_event("alert_job_restored", job=job.to_dict())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if job is None:
                self._queue.task_done()
                continue

            try:
                job.status = "running"
                job.started_at = _utc_now()
                self.state.save_job(job)
                self.runtime.record_event("alert_job_started", job=job.to_dict())
                job.result = self.runtime.handle_alert(job.alert)
                job.status = "completed"
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
            finally:
                job.completed_at = _utc_now()
                self.state.save_job(job)
                if job.status == "completed":
                    self.runtime.record_event("alert_job_completed", job=job.to_dict())
                elif job.status == "failed":
                    self.runtime.record_event("alert_job_failed", job=job.to_dict(), error=job.error)
                job._done.set()
                self._queue.task_done()