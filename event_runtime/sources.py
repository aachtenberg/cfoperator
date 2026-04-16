"""Built-in alert source plugins for the event runtime."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Alert, AlertSeverity
from .plugins import AlertSource

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "critical": AlertSeverity.CRITICAL,
    "error": AlertSeverity.CRITICAL,
    "warning": AlertSeverity.WARNING,
    "info": AlertSeverity.INFO,
    "none": AlertSeverity.INFO,
}


def _parse_iso(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


class AlertmanagerAlertSource(AlertSource):
    """Poll the Alertmanager v2 API for active alerts.

    Normalizes each firing Alertmanager alert into the runtime Alert model.
    Tracks fingerprints already seen so the same firing alert is only emitted
    once per firing window (Alertmanager keeps the alert active until resolved).

    Includes exponential backoff on consecutive failures to avoid log spam
    and unnecessary load when Alertmanager is unreachable.
    """

    name = "alertmanager"

    def __init__(
        self,
        url: str,
        poll_filter: str = "active=true&silenced=false&inhibited=false",
        timeout_seconds: int = 10,
        include_labels: bool = True,
        max_backoff_seconds: float = 300.0,
    ):
        self.url = url.rstrip("/")
        self.poll_filter = poll_filter
        self.timeout_seconds = timeout_seconds
        self.include_labels = include_labels
        self.max_backoff_seconds = max_backoff_seconds
        self._seen_fingerprints: Set[str] = set()
        self._consecutive_failures: int = 0
        self._backoff_until: float = 0.0

    def poll(self) -> Iterable[Alert]:
        if time.monotonic() < self._backoff_until:
            return []

        raw_alerts = self._fetch_alerts()
        if raw_alerts is None:
            return []

        current_fingerprints: Set[str] = set()
        new_alerts: List[Alert] = []

        for raw in raw_alerts:
            fingerprint = str(raw.get("fingerprint") or "")
            if not fingerprint:
                continue
            current_fingerprints.add(fingerprint)
            if fingerprint in self._seen_fingerprints:
                continue
            alert = self._normalize(raw)
            if alert is not None:
                new_alerts.append(alert)

        # Forget resolved alerts so they can fire again later
        self._seen_fingerprints = current_fingerprints
        return new_alerts

    def _fetch_alerts(self) -> List[Dict[str, Any]] | None:
        """Fetch alerts from Alertmanager. Returns None on failure."""
        url = f"{self.url}/api/v2/alerts"
        if self.poll_filter:
            url = f"{url}?{self.poll_filter}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            if self._consecutive_failures > 0:
                logger.info("Alertmanager connection restored after %d failures", self._consecutive_failures)
            self._consecutive_failures = 0
            self._backoff_until = 0.0
            return result
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._consecutive_failures += 1
            backoff = min(self.max_backoff_seconds, 5.0 * (2 ** min(self._consecutive_failures - 1, 6)))
            self._backoff_until = time.monotonic() + backoff
            if self._consecutive_failures <= 3 or self._consecutive_failures % 10 == 0:
                logger.warning(
                    "Failed to poll Alertmanager at %s (failure #%d, backoff %.0fs): %s",
                    self.url, self._consecutive_failures, backoff, exc,
                )
            return None

    def _normalize(self, raw: Dict[str, Any]) -> Alert | None:
        labels = raw.get("labels") or {}
        annotations = raw.get("annotations") or {}

        alertname = str(labels.get("alertname") or "")
        if not alertname:
            return None

        severity_raw = str(labels.get("severity") or "warning").lower()
        severity = _SEVERITY_MAP.get(severity_raw, AlertSeverity.WARNING)

        summary = str(
            annotations.get("summary")
            or annotations.get("description")
            or alertname
        )

        details: Dict[str, Any] = {}
        if self.include_labels:
            details["labels"] = dict(labels)
        if annotations:
            details["annotations"] = dict(annotations)
        details["alertname"] = alertname
        status = raw.get("status") or {}
        if isinstance(status, dict) and status.get("state"):
            details["alertmanager_state"] = status["state"]

        namespace = labels.get("namespace")
        resource_name = labels.get("pod") or labels.get("node") or labels.get("instance")
        resource_type = None
        if labels.get("pod"):
            resource_type = "pod"
        elif labels.get("node"):
            resource_type = "node"
        elif labels.get("instance"):
            resource_type = "instance"

        # Pass host hints so the host observability context provider can match
        host = labels.get("node") or labels.get("instance")
        if host:
            details["host"] = host

        return Alert(
            source="alertmanager",
            severity=severity,
            summary=summary,
            details=details,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
            fingerprint=str(raw.get("fingerprint") or ""),
            occurred_at=_parse_iso(raw.get("startsAt")),
        )
