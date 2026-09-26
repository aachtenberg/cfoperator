"""Plugin contracts for the event-driven runtime scaffold."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, Iterable, List, Tuple

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

if TYPE_CHECKING:
    from .alert_store import AlertPage, AlertQuery


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

    def list_tasks(self, limit: int = 100) -> List[Dict[str, object]]:
        """Return scheduled tasks known to this backend."""
        return []

    def health(self) -> Dict[str, object]:
        """Return scheduler health metadata.  Overridable for richer state."""
        return {"name": self.name, "type": type(self).__name__}


class ScheduledAlertSource(AlertSource):
    """Alert source that emits alerts originating from scheduled tasks."""


class CompletionObserver(RuntimePlugin):
    """Told about every completed action, before any notification policy (CFOP-212).

    Notification sinks page people, so they sit behind the paging gates: the
    skip list and the low-severity digest, which keeps resolved and monitoring
    outcomes out of real time. An observer is for acting on a result -- writing
    it back to the system the alert came from -- so it sees those too. Interim
    results (``quiet``, such as "investigation queued") are not completions and
    are not observed.
    """

    @abstractmethod
    def observe(self, alert: Alert, result: ActionResult) -> None:
        """React to a completed action. Exceptions are logged and go no further."""


class NotificationSink(RuntimePlugin):
    """Plugin that delivers outbound notifications when actions complete."""

    @abstractmethod
    def notify(self, summary: str, *, severity: str = "info", details: Dict | None = None) -> bool:
        """Send a notification message.  Return True on success."""


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

    # The per-alert read model (CFOP-215). Not abstract: a sink that keeps no
    # such model says so, and the runtime answers /v1/alerts with a 503
    # rather than folding a window of raw events and calling it complete.

    def list_alerts(self, query: "AlertQuery") -> "AlertPage":
        """One page of alerts, by folded state. Raises AlertStoreUnavailable."""
        from .alert_store import AlertStoreUnavailable
        raise AlertStoreUnavailable(f"{type(self).__name__} keeps no per-alert read model")

    def get_alert(self, alert_id: str) -> "dict | None":
        """``{"alert": <activity>, "events": [...]}``, or None if unknown.

        Raises AlertStoreUnavailable when this sink cannot answer.
        """
        from .alert_store import AlertStoreUnavailable
        raise AlertStoreUnavailable(f"{type(self).__name__} keeps no per-alert read model")