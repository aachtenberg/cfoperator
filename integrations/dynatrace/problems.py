"""Davis problems as event runtime alerts (CFOP-204).

Polls ``dt.davis.problems`` -- one row per problem holding its latest state;
the version history is ``dt.davis.problems.snapshots`` -- and emits one alert
the first time a problem is seen ACTIVE.

Measured on a live tenant, and each one shaped the code:

- Davis retitles a problem as it merges events into it ("Backoff event"
  became "Multiple Kubernetes problems"). Identity is ``event.id``, never the
  title, and a retitle does not alert again.
- An ACTIVE problem can go a long time without a new row: one sat 30 minutes
  unchanged while still open. So dropping out of the query window does not
  mean resolved. Resolution is only ever an observed CLOSED row; treating
  absence as a clear would send false "Resolved:" notices for escalated
  problems.
- Kubernetes context arrives as arrays (``k8s.workload.name: ["crashloop"]``).
- Asking for a field the table lacks returns null, not an error.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from event_runtime.escalation import EscalationLedger
from event_runtime.models import Alert, AlertSeverity
from event_runtime.plugins import AlertSource

from .grail import GrailClient, GrailQueryError

logger = logging.getLogger(__name__)

_FIELDS = (
    "event.id", "display_id", "event.name", "event.status", "event.category",
    "event.start", "event.description",
    "affected_entity_ids", "affected_entity_names", "affected_entity_types",
    "root_cause_entity_id", "root_cause_entity_name",
    "k8s.cluster.name", "k8s.namespace.name", "k8s.workload.kind", "k8s.workload.name",
    "k8s.pod.name", "host.name",
    "dt.davis.is_duplicate", "dt.davis.mute.status", "maintenance.is_under_maintenance",
)

# Grail's event.category. Only ERROR has been seen on a live tenant; the rest
# are Dynatrace's documented problem categories. Anything else is a warning,
# as an unknown Alertmanager severity is.
_SEVERITY = {
    "AVAILABILITY": AlertSeverity.CRITICAL,
    "ERROR": AlertSeverity.CRITICAL,
    "SLOWDOWN": AlertSeverity.WARNING,
    "PERFORMANCE": AlertSeverity.WARNING,
    "RESOURCE_CONTENTION": AlertSeverity.WARNING,
    "CUSTOM_ALERT": AlertSeverity.WARNING,
    "MONITORING_UNAVAILABLE": AlertSeverity.WARNING,
    "INFO": AlertSeverity.INFO,
}

_LOOKBACK = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
_DESCRIPTION_LIMIT = 2000
_ENTITY_LIMIT = 10


def lookback_seconds(lookback: str) -> int:
    """``7d`` -> 604800. Raises ValueError for anything but <n>m, <n>h or <n>d."""
    match = _LOOKBACK.match(lookback.strip())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"lookback must look like 30m, 12h or 7d, got {lookback!r}")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class DynatraceProblemSource(AlertSource):
    """Emit an alert per new ACTIVE Davis problem, and a resolution when it CLOSES."""

    name = "dynatrace"

    def __init__(
        self,
        client: GrailClient,
        *,
        escalation_ledger: EscalationLedger | None = None,
        poll_seconds: float = 60.0,
        lookback: str = "7d",
        max_backoff_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._escalation_ledger = escalation_ledger
        self._poll_seconds = float(poll_seconds)
        self._lookback_seconds = lookback_seconds(lookback)
        self._max_backoff = float(max_backoff_seconds)
        self._clock = clock
        self._query = (
            f"fetch dt.davis.problems, from:-{lookback.strip()}\n"
            "| sort timestamp desc\n"
            f"| fields {', '.join(_FIELDS)}"
        )
        # fingerprint -> (alert as emitted, clock time the problem was last in a result)
        self._open: Dict[str, Tuple[Alert, float]] = {}
        self._next_poll = 0.0
        self._failures = 0

    def poll(self) -> Iterable[Alert]:
        now = self._clock()
        if now < self._next_poll:
            return []
        try:
            result = self._client.query(self._query)
        except GrailQueryError as exc:
            # Keep what is open: a failed read says nothing about the problems.
            self._failures += 1
            backoff = min(self._max_backoff, self._poll_seconds * 2 ** min(self._failures - 1, 6))
            self._next_poll = now + backoff
            if self._failures <= 3 or self._failures % 10 == 0:
                logger.warning(
                    "Dynatrace problem poll failed (failure #%d, next try in %.0fs): %s",
                    self._failures, backoff, exc,
                )
            return []
        if self._failures:
            logger.info("Dynatrace problem poll recovered after %d failures", self._failures)
            self._failures = 0
        self._next_poll = now + self._poll_seconds
        for warning in result.warnings:
            # A limited result can hide a problem; say so rather than miss it quietly.
            logger.warning("Dynatrace problem poll: %s", warning)

        emitted: List[Alert] = []
        for row in result.records:
            problem_id = str(row.get("event.id") or "")
            if not problem_id:
                continue
            fingerprint = f"dynatrace:{problem_id}"
            status = str(row.get("event.status") or "").upper()
            if status == "ACTIVE":
                if fingerprint in self._open:
                    self._open[fingerprint] = (self._open[fingerprint][0], now)
                    continue
                if _is_noise(row):
                    continue
                alert = self._normalize(row, fingerprint)
                self._open[fingerprint] = (alert, now)
                emitted.append(alert)
            elif status == "CLOSED":
                opened = self._open.pop(fingerprint, None)
                if opened is not None and self._escalation_ledger is not None:
                    if self._escalation_ledger.take(fingerprint):
                        emitted.append(_resolution(opened[0]))

        # A problem out of every result for longer than the lookback can no
        # longer be matched to its CLOSED row. Forget it, without a notice.
        for fingerprint, (_alert, last_seen) in list(self._open.items()):
            if now - last_seen > self._lookback_seconds:
                logger.info("Dynatrace problem %s left the %ds window while open; forgetting it",
                            fingerprint, self._lookback_seconds)
                del self._open[fingerprint]
        return emitted

    def _normalize(self, row: Dict[str, Any], fingerprint: str) -> Alert:
        problem_id = str(row.get("event.id"))
        display_id = str(row.get("display_id") or problem_id)
        title = str(row.get("event.name") or "Dynatrace problem")
        category = str(row.get("event.category") or "").upper()

        namespace = _first(row, "k8s.namespace.name")
        workload = _first(row, "k8s.workload.name")
        pod = _first(row, "k8s.pod.name")
        host = _first(row, "host.name")
        if workload:
            resource_type, resource_name = (_first(row, "k8s.workload.kind") or "workload"), workload
        elif pod:
            resource_type, resource_name = "pod", pod
        elif host:
            resource_type, resource_name = "host", host
        else:
            resource_type = _first(row, "affected_entity_types")
            resource_name = _first(row, "affected_entity_names")

        where = f"{namespace}/{resource_name}" if namespace and resource_name else resource_name
        summary = f"Dynatrace {display_id}: {title}" + (f" on {where}" if where else "")

        entities = [
            {"id": entity_id, "name": name, "type": entity_type}
            for entity_id, name, entity_type in zip(
                _list(row, "affected_entity_ids"),
                _list(row, "affected_entity_names"),
                _list(row, "affected_entity_types"),
            )
        ]
        labels = {
            key: value
            for key, value in (
                ("cluster", _first(row, "k8s.cluster.name")),
                ("namespace", namespace),
                ("workload", workload),
                ("workload_kind", _first(row, "k8s.workload.kind")),
                ("pod", pod),
                ("host", host),
            )
            if value
        }
        dynatrace: Dict[str, Any] = {
            "display_id": display_id,
            "problem_id": problem_id,
            "category": category or None,
            "start": row.get("event.start"),
        }
        if row.get("root_cause_entity_id"):
            dynatrace["root_cause_entity"] = {
                "id": row.get("root_cause_entity_id"),
                "name": row.get("root_cause_entity_name"),
            }
        dynatrace["affected_entity_count"] = len(entities)
        dynatrace["affected_entities"] = entities[:_ENTITY_LIMIT]

        # Order is load-bearing. The agent puts json.dumps(alert)[:1000] into
        # the investigation prompt (agent.run_investigation), and details comes
        # before namespace/resource_name in Alert.to_dict(). So what an
        # investigation needs goes first and the unbounded parts go last.
        details: Dict[str, Any] = {"alertname": title, "labels": labels}
        if host:
            details["host"] = host
        details["dynatrace"] = dynatrace
        description = str(row.get("event.description") or "").strip()
        if description:
            details["description"] = description[:_DESCRIPTION_LIMIT]

        return Alert(
            source=self.name,
            severity=_SEVERITY.get(category, AlertSeverity.WARNING),
            summary=summary,
            details=details,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
            fingerprint=fingerprint,
            occurred_at=_parse_time(row.get("event.start")),
        )


def _is_noise(row: Dict[str, Any]) -> bool:
    """Problems Davis itself says not to act on. Not remembered, so an unmute alerts."""
    return (
        row.get("dt.davis.is_duplicate") is True
        or str(row.get("dt.davis.mute.status") or "").upper() == "MUTED"
        or row.get("maintenance.is_under_maintenance") is True
    )


def _resolution(original: Alert) -> Alert:
    """The one "Resolved:" alert for an escalated problem, as AlertmanagerAlertSource builds it."""
    details: Dict[str, Any] = {"resolution": True, "alertname": original.details.get("alertname")}
    if original.details.get("host"):
        details["host"] = original.details["host"]
    return Alert(
        source=original.source,
        severity=AlertSeverity.INFO,
        summary=f"Resolved: {original.summary}",
        details=details,
        namespace=original.namespace,
        resource_type=original.resource_type,
        resource_name=original.resource_name,
        fingerprint=f"{original.fingerprint}:resolved",
    )


def _list(row: Dict[str, Any], key: str) -> List[Any]:
    value = row.get(key)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _first(row: Dict[str, Any], key: str) -> Optional[str]:
    values = [v for v in _list(row, key) if v not in (None, "")]
    return str(values[0]) if values else None


def _parse_time(raw: Any) -> datetime:
    """Grail timestamps carry nanoseconds, which datetime cannot hold; keep microseconds."""
    if not raw:
        return datetime.now(timezone.utc)
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(raw)).replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
