"""Composite state sink with durable-local-first semantics."""

from __future__ import annotations

from typing import List

from ..alert_store import AlertPage, AlertQuery, AlertStoreUnavailable
from .base import BaseStateSink


class CompositeStateSink(BaseStateSink):
    """Write to multiple sinks but require at least one durable sink to succeed."""

    def __init__(self, sinks: List[BaseStateSink]):
        if not sinks:
            raise ValueError("CompositeStateSink requires at least one sink")
        super().__init__(name="composite")
        self.sinks = sinks
        if not any(sink.durable for sink in sinks):
            raise ValueError("CompositeStateSink requires at least one durable sink")

    def start(self) -> None:
        for sink in self.sinks:
            sink.start()

    def stop(self) -> None:
        for sink in self.sinks:
            sink.stop()

    def append(self, events: List[dict]) -> bool:
        durable_success = False
        attempted_durable = False
        for sink in self.sinks:
            try:
                success = sink.append(events)
            except Exception:
                success = False

            if sink.durable:
                attempted_durable = True
                durable_success = durable_success or success

        return durable_success if attempted_durable else False

    def recent(self, limit: int = 50) -> List[dict]:
        for sink in self.sinks:
            events = sink.recent(limit=limit)
            if events:
                return events
        return []

    def list_alerts(self, query: AlertQuery) -> AlertPage:
        return self._first_answer(lambda sink: sink.list_alerts(query))

    def get_alert(self, alert_id: str) -> dict | None:
        return self._first_answer(lambda sink: sink.get_alert(alert_id))

    def _first_answer(self, ask):
        reasons = []
        for sink in self.sinks:
            try:
                return ask(sink)
            except AlertStoreUnavailable as exc:
                reasons.append(f"{sink.name}: {exc}")
        raise AlertStoreUnavailable("; ".join(reasons) or "no sinks")

    def health(self) -> dict:
        statuses = [sink.health() for sink in self.sinks]
        return {
            "name": self.name,
            "healthy": any(status.get("healthy", False) for status in statuses if status.get("durable", False)),
            "durable": True,
            "sinks": statuses,
        }