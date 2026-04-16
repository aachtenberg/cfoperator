"""Background worker queue for asynchronous alert processing."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from .engine import EventRuntime
from .models import Alert
from .telemetry import (
    observe_job_finished,
    observe_job_started,
    observe_queue_enqueued,
    observe_queue_rejected,
    update_queue_state,
)


class QueueFullError(RuntimeError):
    """Raised when the background worker queue cannot accept more jobs."""


logger = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = {"completed", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(raw: Optional[str]) -> datetime:
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


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
    attempt: int = 0
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
            "attempt": self.attempt,
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
            alert=Alert.from_dict(dict(payload.get("alert") or {})),
            status=str(payload.get("status") or "queued"),
            created_at=str(payload.get("created_at") or _utc_now()),
            started_at=payload.get("started_at"),
            completed_at=payload.get("completed_at"),
            result=payload.get("result"),
            error=payload.get("error"),
            attempt=int(payload.get("attempt") or 0),
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

    def replace_jobs(self, jobs: Dict[str, WorkerJob]) -> None:
        with self._lock:
            self._write_locked(jobs)

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            jobs = self._read_locked()
            if job_id in jobs:
                del jobs[job_id]
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
        except Exception as exc:
            logger.warning("Failed to read worker state from %s: %s", self.path, exc)
            return {}
        if not isinstance(payload, dict):
            logger.warning("Worker state file %s did not contain an object payload", self.path)
            return {}
        jobs: Dict[str, WorkerJob] = {}
        for job_id, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            try:
                jobs[str(job_id)] = WorkerJob.from_dict(raw)
            except Exception as exc:
                logger.warning("Skipping corrupt worker job %s from %s: %s", job_id, self.path, exc)
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
        max_terminal_jobs: int = 1000,
        max_retries: int = 2,
        state: FileBackedWorkerState | None = None,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        if max_terminal_jobs < 0:
            raise ValueError("max_terminal_jobs must be >= 0")
        self.runtime = runtime
        self.worker_count = worker_count
        self.max_queue_size = max_queue_size
        self.max_terminal_jobs = max_terminal_jobs
        self.max_retries = max_retries
        self.state = state or FileBackedWorkerState()
        self._queue: queue.Queue[WorkerJob] = queue.Queue(maxsize=max_queue_size)
        self._jobs: Dict[str, WorkerJob] = self.state.load_jobs()
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._prune_terminal_jobs()

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._restore_pending_jobs()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._run, daemon=True, name=f"event-runtime-worker-{index}")
            thread.start()
            self._threads.append(thread)
        self._update_metrics()

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
        self._update_metrics()

    def enqueue(self, alert: Alert) -> dict:
        job = WorkerJob(job_id=str(uuid4()), alert=alert)
        with self._jobs_lock:
            self._jobs[job.job_id] = job
            self.state.save_job(job)
        try:
            self._queue.put_nowait(job)
        except queue.Full as exc:
            with self._jobs_lock:
                self._jobs.pop(job.job_id, None)
                self.state.delete_job(job.job_id)
            observe_queue_rejected()
            self._update_metrics()
            raise QueueFullError("Alert queue is full") from exc
        observe_queue_enqueued()
        self._update_metrics()
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

    def refresh_metrics(self) -> None:
        """Refresh exported queue gauges at scrape time."""
        self._update_metrics()

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
        self._update_metrics()

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
                job.attempt += 1
                job.started_at = _utc_now()
                job.error = None
                self.state.save_job(job)
                observe_job_started(job.queue_delay_seconds())
                self._update_metrics()
                self.runtime.record_event("alert_job_started", job=job.to_dict())
                job.result = self.runtime.handle_alert(job.alert)
                job.status = "completed"
            except Exception as exc:
                job.error = str(exc)
                if job.attempt < self.max_retries:
                    job.status = "queued"
                    job.started_at = None
                    job.completed_at = None
                    logger.warning(
                        "Background alert job %s failed (attempt %d/%d), requeueing: %s",
                        job.job_id, job.attempt, self.max_retries, exc,
                    )
                    self.state.save_job(job)
                    self._update_metrics()
                    self.runtime.record_event("alert_job_retrying", job=job.to_dict(), error=str(exc))
                    try:
                        self._queue.put_nowait(job)
                    except queue.Full:
                        job.status = "failed"
                        logger.error("Cannot retry job %s: queue full", job.job_id)
                    self._queue.task_done()
                    continue
                job.status = "failed"
                logger.exception("Background alert job %s failed after %d attempts", job.job_id, job.attempt)
            finally:
                if job.status in TERMINAL_JOB_STATUSES:
                    job.completed_at = _utc_now()
                    self.state.save_job(job)
                    self._prune_terminal_jobs()
                    observe_job_finished(job.status, job.processing_duration_seconds())
                    self._update_metrics()
                    if job.status == "completed":
                        self.runtime.record_event("alert_job_completed", job=job.to_dict())
                    elif job.status == "failed":
                        self.runtime.record_event("alert_job_failed", job=job.to_dict(), error=job.error)
                    job._done.set()
                    self._queue.task_done()

    def _update_metrics(self) -> None:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
            job_counts = {
                "queued": sum(1 for job in jobs if job.status == "queued"),
                "running": sum(1 for job in jobs if job.status == "running"),
                "completed": sum(1 for job in jobs if job.status == "completed"),
                "failed": sum(1 for job in jobs if job.status == "failed"),
            }
        queued_ages = [value for job in jobs if (value := job.queued_age_seconds()) is not None]
        update_queue_state(
            queue_size=self._queue.qsize(),
            max_queue_size=self.max_queue_size,
            jobs=job_counts,
            oldest_queued_age_seconds=max(queued_ages) if queued_ages else 0.0,
        )

    def _prune_terminal_jobs(self) -> None:
        with self._jobs_lock:
            terminal_jobs = [job for job in self._jobs.values() if job.status in TERMINAL_JOB_STATUSES]
            if len(terminal_jobs) <= self.max_terminal_jobs:
                return
            terminal_jobs.sort(
                key=lambda job: (_parse_datetime(job.completed_at or job.created_at), job.job_id),
                reverse=True,
            )
            keep_ids = {job.job_id for job in terminal_jobs[: self.max_terminal_jobs]}
            removed = [job_id for job_id, job in self._jobs.items() if job.status in TERMINAL_JOB_STATUSES and job_id not in keep_ids]
            for job_id in removed:
                del self._jobs[job_id]
            self.state.replace_jobs(self._jobs)
        logger.info("Pruned %d terminal worker jobs from retained state", len(removed))