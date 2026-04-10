"""Durable local outbox sink used when remote persistence is unavailable."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Iterator, List

from .base import BaseStateSink


logger = logging.getLogger(__name__)


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
        self._handle = None

    def append(self, events: List[dict]) -> bool:
        self.append_with_cursor(events)
        return True

    def append_with_cursor(self, events: List[dict]) -> dict | None:
        if not events:
            return None
        with self._lock:
            self._ensure_handle_locked()
            return self._append_locked(events)

    def recent(self, limit: int = 50) -> List[dict]:
        events: List[dict] = []
        files = self._snapshot_files()
        for path in reversed(files):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in reversed(handle.readlines()):
                        line = line.strip()
                        if not line:
                            continue
                        events.append(json.loads(line))
                        if len(events) >= limit:
                            return events
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as exc:
                logger.warning("Skipping invalid outbox line while reading %s: %s", path, exc)
        return events

    def iter_events(self) -> Iterator[dict]:
        """Yield events in append order across all outbox files."""
        for path in self._snapshot_files():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        yield json.loads(line)
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as exc:
                logger.warning("Skipping invalid outbox line while iterating %s: %s", path, exc)

    def iter_events_with_cursors(self, cursor: dict | None = None) -> Iterator[tuple[dict, dict]]:
        """Yield events in append order together with the next replay cursor."""
        current_file = str((cursor or {}).get("file") or "")
        current_offset = int((cursor or {}).get("offset") or 0)

        for path in self._snapshot_files():
            if current_file and path.name < current_file:
                continue

            try:
                with open(path, "r", encoding="utf-8") as handle:
                    if current_file and path.name == current_file:
                        handle.seek(current_offset)

                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        next_cursor = {"file": path.name, "offset": handle.tell()}
                        line = line.strip()
                        if not line:
                            continue
                        yield json.loads(line), next_cursor
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as exc:
                logger.warning("Skipping invalid outbox line while replaying %s: %s", path, exc)

    def has_events_after(self, cursor: dict | None = None) -> bool:
        """Return whether any events exist after the provided replay cursor."""
        for _event, _next_cursor in self.iter_events_with_cursors(cursor=cursor):
            return True
        return False

    def health(self) -> dict:
        files = self._snapshot_files()
        total_size = sum(path.stat().st_size for path in files)
        return {
            "name": self.name,
            "healthy": True,
            "durable": True,
            "directory": str(self.directory),
            "files": len(files),
            "bytes": total_size,
        }

    def start(self) -> None:
        with self._lock:
            self._ensure_handle_locked()

    def stop(self) -> None:
        with self._lock:
            if self._handle is not None and not self._handle.closed:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()

    def _rotate(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._current_path = self._next_path()
        self._handle = open(self._current_path, "a", encoding="utf-8")

    def _append_locked(self, events: List[dict]) -> dict | None:
        final_cursor = None
        for event in events:
            self._handle.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")
            final_cursor = {"file": self._current_path.name, "offset": self._handle.tell()}
        self._handle.flush()
        os.fsync(self._handle.fileno())
        if self._current_path.stat().st_size >= self.max_file_size_bytes:
            self._rotate()
        return final_cursor

    def _next_path(self) -> Path:
        prefix = f"{self.file_prefix}_"
        pattern = re.compile(r"^(?P<prefix>.+)_(?P<counter>\d{6})\.jsonl$")
        counter = 0
        for path in self._list_files():
            match = pattern.match(path.name)
            if not match or match.group("prefix") != self.file_prefix:
                continue
            counter = max(counter, int(match.group("counter")))
        return self.directory / f"{prefix}{counter + 1:06d}.jsonl"

    def _list_files(self) -> List[Path]:
        return sorted(self.directory.glob(f"{self.file_prefix}_*.jsonl"))

    def _snapshot_files(self) -> List[Path]:
        with self._lock:
            if self._handle is not None and not self._handle.closed:
                self._handle.flush()
            return list(self._list_files())

    def _ensure_handle_locked(self) -> None:
        if self._handle is not None and not self._handle.closed:
            return
        if self._current_path.exists() and self._current_path.stat().st_size < self.max_file_size_bytes:
            self._handle = open(self._current_path, "a", encoding="utf-8")
            return
        self._current_path = self._next_path()
        self._handle = open(self._current_path, "a", encoding="utf-8")