"""Shared runtime models for the event-driven CFOperator scaffold."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _parse_iso_timestamp(raw: Any, *, field_name: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalizing naive datetimes and ``Z`` suffix to UTC.

    Empty / missing input returns ``utc_now()``. Used by both ``Alert.from_dict``
    and ``ActionResult.from_dict`` to keep timestamp handling consistent.
    """
    if raw in (None, ""):
        return utc_now()
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw)
        # datetime.fromisoformat in 3.11+ handles trailing Z, but be explicit
        # so behavior doesn't silently change across Python versions.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid {field_name} timestamp: {raw}") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_alert_timestamp(raw: Any) -> datetime:
    """Compatibility shim: forwards to the shared parser with the legacy field name."""
    return _parse_iso_timestamp(raw, field_name="occurred_at")


class AlertSeverity(str, Enum):
    """Normalized alert severities used by the runtime."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(slots=True)
class Alert:
    """Normalized alert model shared across all event sources."""

    source: str
    severity: AlertSeverity
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)
    namespace: Optional[str] = None
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    fingerprint: Optional[str] = None
    occurred_at: datetime = field(default_factory=utc_now)
    alert_id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the alert for sinks or wire protocols."""
        payload = asdict(self)
        payload["severity"] = self.severity.value
        payload["occurred_at"] = self.occurred_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], *, require_summary: bool = True) -> "Alert":
        """Build an alert from a transport or persisted payload."""
        severity_value = str(payload.get("severity") or "info").lower()
        try:
            severity = AlertSeverity(severity_value)
        except ValueError as exc:
            raise ValueError(f"Invalid severity: {severity_value}") from exc

        summary = payload.get("summary")
        if require_summary and not summary:
            raise ValueError("Missing required field: summary")

        details = payload.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("Field details must be an object")

        return cls(
            source=str(payload.get("source") or "manual"),
            severity=severity,
            summary=str(summary or ""),
            details=dict(details),
            namespace=None if payload.get("namespace") is None else str(payload.get("namespace")),
            resource_type=None if payload.get("resource_type") is None else str(payload.get("resource_type")),
            resource_name=None if payload.get("resource_name") is None else str(payload.get("resource_name")),
            fingerprint=None if payload.get("fingerprint") is None else str(payload.get("fingerprint")),
            occurred_at=_parse_alert_timestamp(payload.get("occurred_at")),
            alert_id=str(payload.get("alert_id") or str(uuid4())),
        )

    def effective_fingerprint(self) -> str:
        """Return a stable fingerprint for duplicate suppression."""
        if self.fingerprint:
            return self.fingerprint
        stable = {
            "source": self.source,
            "severity": self.severity.value,
            "summary": self.summary,
            "namespace": self.namespace,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
        }
        payload = json.dumps(stable, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ContextEnvelope:
    """Aggregated context returned by context providers."""

    alert: Alert
    context: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass(slots=True)
class HostTarget:
    """A named bare-metal target that a host observability provider can inspect."""

    name: str
    provider: str
    address: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the target for context payloads."""
        return asdict(self)


@dataclass(slots=True)
class HostObservation:
    """A collected set of bare-metal host stats from a provider."""

    provider: str
    target: str
    stats: Dict[str, Any] = field(default_factory=dict)
    collected_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the observation for event payloads."""
        payload = asdict(self)
        payload["collected_at"] = self.collected_at.isoformat()
        return payload


@dataclass(slots=True)
class ScheduledTask:
    """A recurring or delayed check requested by the runtime or LLM."""

    name: str
    schedule: str
    rationale: str
    target: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    task_type: str = "cron"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the scheduled task for sinks or schedulers."""
        return asdict(self)


@dataclass(slots=True)
class Decision:
    """Decision output from the decision engine."""

    action: str
    confidence: float
    reasoning: str
    params: Dict[str, Any] = field(default_factory=dict)
    requested_checks: List[str] = field(default_factory=list)
    scheduled_tasks: List[ScheduledTask] = field(default_factory=list)


@dataclass(slots=True)
class ActionRequest:
    """Action invocation request dispatched to handlers."""

    alert: Alert
    decision: Decision
    context: ContextEnvelope


@dataclass(slots=True)
class ActionResult:
    """Result of an executed action."""

    action: str
    success: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    executed_at: datetime = field(default_factory=utc_now)
    quiet: bool = False  # When True, notification sinks skip this result (e.g. interim "queued" states)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the result for sinks or transport."""
        payload = asdict(self)
        payload["executed_at"] = self.executed_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ActionResult":
        """Rebuild from a wire payload (e.g. the agent's investigation post-back).

        Wire-facing — bad input must raise ``ValueError`` so callers can surface a
        400, not crash with ``AttributeError`` and bubble up as a 500.
        """
        if not isinstance(payload, dict):
            raise ValueError("ActionResult payload must be a JSON object")

        action = payload.get("action")
        if not isinstance(action, str) or not action:
            raise ValueError("Field action must be a non-empty string")

        message = payload.get("message", "")
        if not isinstance(message, str):
            raise ValueError("Field message must be a string")

        raw_success = payload.get("success")
        if isinstance(raw_success, bool):
            success = raw_success
        elif raw_success is None:
            raise ValueError("Missing required field: success")
        else:
            # Reject truthy strings like "false" that bool() would silently invert.
            raise ValueError(
                f"Field success must be a JSON boolean (got {type(raw_success).__name__})"
            )

        raw_details = payload.get("details")
        if raw_details is None:
            details = {}
        elif isinstance(raw_details, dict):
            details = dict(raw_details)
        else:
            # Falsy non-dicts (e.g. []) would silently coalesce via `or {}`; reject explicitly.
            raise ValueError("Field details must be a JSON object")

        raw_quiet = payload.get("quiet", False)
        if isinstance(raw_quiet, bool):
            quiet = raw_quiet
        else:
            raise ValueError(
                f"Field quiet must be a JSON boolean (got {type(raw_quiet).__name__})"
            )

        executed_at = _parse_iso_timestamp(payload.get("executed_at"), field_name="executed_at")

        return cls(
            action=action,
            success=success,
            message=message,
            details=details,
            executed_at=executed_at,
            quiet=quiet,
        )


@dataclass(slots=True)
class DomainEvent:
    """Append-only audit event emitted by the runtime."""

    event_type: str
    payload: Dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the event to a JSON-safe dictionary."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }


@dataclass(slots=True)
class SinkHealth:
    """Health state for a persistence sink."""

    name: str
    healthy: bool
    durable: bool
    details: Dict[str, Any] = field(default_factory=dict)