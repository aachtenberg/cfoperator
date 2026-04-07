"""Background worker queue for asynchronous alert processing."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from .engine import EventRuntime
from .models import Alert


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class BackgroundAlertWorker:
    """Process alerts asynchronously while preserving runtime semantics."""

    def __init__(self, runtime: EventRuntime, worker_count: int = 1, max_queue_size: int = 1000):
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        self.runtime = runtime
        self.worker_count = worker_count
        self.max_queue_size = max_queue_size
        self._queue: queue.Queue[WorkerJob] = queue.Queue(maxsize=max_queue_size)
        self._jobs: Dict[str, WorkerJob] = {}
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
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
            queued = sum(1 for job in self._jobs.values() if job.status == "queued")
            running = sum(1 for job in self._jobs.values() if job.status == "running")
            completed = sum(1 for job in self._jobs.values() if job.status == "completed")
            failed = sum(1 for job in self._jobs.values() if job.status == "failed")
        return {
            "enabled": True,
            "worker_count": self.worker_count,
            "max_queue_size": self.max_queue_size,
            "queue_size": self._queue.qsize(),
            "jobs": {
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
            },
        }

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
                self.runtime.record_event("alert_job_started", job=job.to_dict())
                job.result = self.runtime.handle_alert(job.alert)
                job.status = "completed"
                self.runtime.record_event("alert_job_completed", job=job.to_dict())
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                self.runtime.record_event("alert_job_failed", job=job.to_dict(), error=str(exc))
            finally:
                job.completed_at = _utc_now()
                job._done.set()
                self._queue.task_done()