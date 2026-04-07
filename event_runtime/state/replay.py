"""Replay-capable sink wrapper that preserves local durability semantics."""

from __future__ import annotations

import threading
from typing import List

from .base import BaseStateSink
from .local_outbox import LocalOutboxStateSink


class ReplayingStateSink(BaseStateSink):
    """Append to a local durable sink and replay to remote sinks in the background."""

    durable = True

    def __init__(
        self,
        local_sink: LocalOutboxStateSink,
        remote_sinks: List[BaseStateSink],
        replay_interval_seconds: int = 30,
    ):
        if not remote_sinks:
            raise ValueError("ReplayingStateSink requires at least one remote sink")
        super().__init__(name="replaying")
        self.local_sink = local_sink
        self.remote_sinks = remote_sinks
        self.replay_interval_seconds = replay_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
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
        if not self.local_sink.append(events):
            return False
        for sink in self.remote_sinks:
            try:
                sink.append(events)
            except Exception:
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
            "local": local,
            "remotes": remotes,
        }

    def replay_once(self) -> dict:
        events = list(self.local_sink.iter_events())
        if not events:
            return {"success": True, "replayed": 0}

        replayed = 0
        for sink in self.remote_sinks:
            try:
                if sink.append(events):
                    replayed += len(events)
            except Exception:
                continue
        return {"success": replayed > 0, "replayed": replayed, "events": len(events)}

    def _replay_loop(self) -> None:
        while not self._stop.wait(self.replay_interval_seconds):
            try:
                self.replay_once()
            except Exception:
                continue