"""Durable local outbox sink used when remote persistence is unavailable."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import List

from .base import BaseStateSink


class LocalOutboxStateSink(BaseStateSink):
    """Append-only JSONL sink with fsync-backed durability."""

    durable = True

    def __init__(
        self,
        directory: str | None = None,
        file_prefix: str = "events",
        max_file_size_bytes: int = 5 * 1024 * 1024,
    ):
        super().__init__(name="local_outbox")
        if directory is None:
            directory = str(Path.home() / ".cfoperator" / "event-runtime" / "outbox")
        self.directory = Path(directory)
        self.file_prefix = file_prefix
        self.max_file_size_bytes = max_file_size_bytes
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_path = self._next_path()
        self._handle = open(self._current_path, "a", encoding="utf-8")

    def append(self, events: List[dict]) -> bool:
        with self._lock:
            for event in events:
                self._handle.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            if self._current_path.stat().st_size >= self.max_file_size_bytes:
                self._rotate()
        return True

    def recent(self, limit: int = 50) -> List[dict]:
        events: List[dict] = []
        files = sorted(self.directory.glob(f"{self.file_prefix}_*.jsonl"))
        for path in reversed(files):
            with open(path, "r", encoding="utf-8") as handle:
                for line in reversed(handle.readlines()):
                    line = line.strip()
                    if not line:
                        continue
                    events.append(json.loads(line))
                    if len(events) >= limit:
                        return events
        return events

    def health(self) -> dict:
        files = sorted(self.directory.glob(f"{self.file_prefix}_*.jsonl"))
        total_size = sum(path.stat().st_size for path in files)
        return {
            "name": self.name,
            "healthy": True,
            "durable": True,
            "directory": str(self.directory),
            "files": len(files),
            "bytes": total_size,
        }

    def stop(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()

    def _rotate(self) -> None:
        self._handle.close()
        self._current_path = self._next_path()
        self._handle = open(self._current_path, "a", encoding="utf-8")

    def _next_path(self) -> Path:
        counter = len(list(self.directory.glob(f"{self.file_prefix}_*.jsonl"))) + 1
        return self.directory / f"{self.file_prefix}_{counter:06d}.jsonl"