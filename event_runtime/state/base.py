"""Base helpers for runtime state sinks."""

from __future__ import annotations

from typing import List

from ..plugins import StateSink


class BaseStateSink(StateSink):
    """Common sink base with a default no-op lifecycle."""

    durable: bool = False

    def __init__(self, name: str):
        self.name = name

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def append(self, events: List[dict]) -> bool:
        raise NotImplementedError

    def recent(self, limit: int = 50) -> List[dict]:
        raise NotImplementedError

    def health(self) -> dict:
        raise NotImplementedError