"""Plugin contracts for the event-driven runtime scaffold."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Tuple

from .models import (
    ActionRequest,
    ActionResult,
    Alert,
    ContextEnvelope,
    Decision,
    HostObservation,
    HostTarget,
    ScheduledTask,
)


class RuntimePlugin(ABC):
    """Base class for all runtime plugins."""

    name: str

    def start(self) -> None:
        """Optional lifecycle hook for startup."""

    def stop(self) -> None:
        """Optional lifecycle hook for shutdown."""


class AlertSource(RuntimePlugin):
    """Plugin that yields normalized alerts into the runtime."""

    @abstractmethod
    def poll(self) -> Iterable[Alert]:
        """Return zero or more alerts ready for processing."""


class AlertPolicy(RuntimePlugin):
    """Plugin that can suppress or modify alert handling decisions."""

    @abstractmethod
    def evaluate(self, alert: Alert) -> Tuple[bool, str | None]:
        """Return whether processing should continue and an optional reason."""


class ContextProvider(RuntimePlugin):
    """Plugin that enriches alerts with investigation context."""

    capabilities: tuple[str, ...] = ()

    @abstractmethod
    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        """Extend the provided context envelope."""


class HostObservabilityProvider(RuntimePlugin):
    """Plugin that discovers and collects bare-metal host OS stats."""

    def discover_targets(self) -> List[HostTarget]:
        """Return the host targets this provider can inspect."""
        return []

    @abstractmethod
    def collect(self, target: HostTarget) -> HostObservation | None:
        """Collect an observation for the provided host target."""


class DecisionEngine(RuntimePlugin):
    """Plugin that turns an alert and context into an action decision."""

    @abstractmethod
    def decide(self, envelope: ContextEnvelope) -> Decision:
        """Return the action decision for the provided context."""


class ActionHandler(RuntimePlugin):
    """Plugin that executes a named action."""

    action_name: str

    @abstractmethod
    def execute(self, request: ActionRequest) -> ActionResult:
        """Execute the action request."""


class Scheduler(RuntimePlugin):
    """Plugin that stores or applies scheduled follow-up checks."""

    @abstractmethod
    def schedule(self, task: ScheduledTask) -> Dict[str, object]:
        """Create or update a scheduled task."""


class StateSink(RuntimePlugin):
    """Plugin that persists domain events and exposes health."""

    durable: bool = False

    @abstractmethod
    def append(self, events: List[dict]) -> bool:
        """Persist a batch of already serialized events."""

    @abstractmethod
    def recent(self, limit: int = 50) -> List[dict]:
        """Read the most recent persisted events known to this sink."""

    @abstractmethod
    def health(self) -> dict:
        """Return sink health metadata."""