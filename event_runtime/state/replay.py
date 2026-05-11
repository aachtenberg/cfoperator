"""Replay-capable sink wrapper that preserves local durability semantics."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .base import BaseStateSink
from .local_outbox import LocalOutboxStateSink
from ..telemetry import observe_replay_attempt


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplayCheckpointStore:
    """Persist per-remote replay cursors so remotes do not reread the full outbox forever."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state = self._load()

    def get_cursor(self, sink_name: str) -> dict | None:
        with self._lock:
            entry = dict(self._state.get("sinks", {}).get(sink_name) or {})
        cursor = entry.get("cursor")
        return dict(cursor) if isinstance(cursor, dict) else None

    def mark_replayed(self, sink_name: str, cursor: dict, event_id: str | None, count: int) -> None:
        with self._lock:
            sinks = self._state.setdefault("sinks", {})
            current = dict(sinks.get(sink_name) or {})
            total = int(current.get("replayed_events") or 0) + count
            sinks[sink_name] = {
                "cursor": dict(cursor),
                "last_event_id": event_id,
                "replayed_events": total,
                "updated_at": _utc_now(),
            }
            self._write_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "sinks": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning("Failed to read replay checkpoint state from %s: %s", self.path, exc)
            return {"version": 1, "sinks": {}}
        if not isinstance(payload, dict):
            logger.warning("Replay checkpoint state in %s was not an object", self.path)
            return {"version": 1, "sinks": {}}
        payload.setdefault("version", 1)
        payload.setdefault("sinks", {})
        return payload

    def _write_locked(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())


class ReplayingStateSink(BaseStateSink):
    """Append to a local durable sink and replay to remote sinks in the background."""

    durable = True

    def __init__(
        self,
        local_sink: LocalOutboxStateSink,
        remote_sinks: List[BaseStateSink],
        replay_interval_seconds: int = 30,
        replay_batch_size: int = 500,
        checkpoint_path: str | None = None,
    ):
        if not remote_sinks:
            raise ValueError("ReplayingStateSink requires at least one remote sink")
        super().__init__(name="replaying")
        self.local_sink = local_sink
        self.remote_sinks = remote_sinks
        self.replay_interval_seconds = replay_interval_seconds
        self.replay_batch_size = replay_batch_size
        self.checkpoints = ReplayCheckpointStore(
            checkpoint_path or str(local_sink.directory / "replay-state.json")
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self.local_sink.start()
        for sink in self.remote_sinks:
            sink.start()
        self._thread = threading.Thread(target=self._replay_loop, daemon=True, name="event-runtime-replay")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        for sink in self.remote_sinks:
            sink.stop()
        self.local_sink.stop()

    def append(self, events: List[dict]) -> bool:
        if not events:
            return True

        sink_state = {
            sink.name: {
                "cursor": self.checkpoints.get_cursor(sink.name),
            }
            for sink in self.remote_sinks
        }
        for state in sink_state.values():
            state["caught_up"] = not self.local_sink.has_events_after(state["cursor"])

        final_cursor = self.local_sink.append_with_cursor(events)
        if final_cursor is None:
            return False

        last_event_id = events[-1].get("event_id") if events else None
        for sink in self.remote_sinks:
            try:
                if sink.append(events) and sink_state[sink.name]["caught_up"]:
                    self.checkpoints.mark_replayed(
                        sink_name=sink.name,
                        cursor=final_cursor,
                        event_id=last_event_id,
                        count=len(events),
                    )
            except Exception as exc:
                logger.warning("Inline replay append to sink %s failed: %s", sink.name, exc)
                continue
        return True

    def recent(self, limit: int = 50) -> List[dict]:
        for sink in self.remote_sinks:
            events = sink.recent(limit=limit)
            if events:
                return events
        return self.local_sink.recent(limit=limit)

    def health(self) -> dict:
        local = self.local_sink.health()
        remotes = [sink.health() for sink in self.remote_sinks]
        return {
            "name": self.name,
            "healthy": local.get("healthy", False),
            "durable": True,
            "replay_interval_seconds": self.replay_interval_seconds,
            "replay_batch_size": self.replay_batch_size,
            "checkpoint_path": str(self.checkpoints.path),
            "checkpoints": self.checkpoints.snapshot().get("sinks", {}),
            "local": local,
            "remotes": remotes,
        }

    def replay_once(self) -> dict:
        replayed = 0
        any_success = False
        any_pending = False
        sink_results = []
        for sink in self.remote_sinks:
            cursor = self.checkpoints.get_cursor(sink.name)
            batch = []
            next_cursor = cursor
            last_event_id = None
            for event, candidate_cursor in self.local_sink.iter_events_with_cursors(cursor=cursor):
                batch.append(event)
                next_cursor = candidate_cursor
                last_event_id = event.get("event_id")
                if len(batch) >= self.replay_batch_size:
                    break

            if not batch:
                observe_replay_attempt(sink.name, "idle", 0)
                sink_results.append({"sink": sink.name, "success": True, "replayed": 0, "pending": False})
                continue

            any_pending = True
            try:
                if sink.append(batch):
                    self.checkpoints.mark_replayed(
                        sink_name=sink.name,
                        cursor=next_cursor or cursor or {},
                        event_id=last_event_id,
                        count=len(batch),
                    )
                    replayed += len(batch)
                    any_success = True
                    observe_replay_attempt(sink.name, "success", len(batch))
                    sink_results.append({"sink": sink.name, "success": True, "replayed": len(batch), "pending": False})
                else:
                    observe_replay_attempt(sink.name, "error", len(batch))
                    sink_results.append({"sink": sink.name, "success": False, "replayed": 0, "pending": True})
            except Exception as exc:
                logger.warning("Replay batch to sink %s failed: %s", sink.name, exc)
                observe_replay_attempt(sink.name, "error", len(batch))
                sink_results.append(
                    {"sink": sink.name, "success": False, "replayed": 0, "pending": True, "error": str(exc)}
                )
                continue
        success = any_success or not any_pending
        return {"success": success, "replayed": replayed, "sinks": sink_results}

    def _replay_loop(self) -> None:
        while not self._stop.wait(self.replay_interval_seconds):
            try:
                self.replay_once()
            except Exception:
                logger.exception("Background replay loop failed")
                continue