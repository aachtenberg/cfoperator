"""Portable duplicate suppression policies for the event runtime."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Tuple

from .models import Alert
from .plugins import AlertPolicy

logger = logging.getLogger(__name__)


class FileBackedCooldownPolicy(AlertPolicy):
    """Suppress duplicate alerts with the same fingerprint during a cooldown window."""

    name = "file-backed-cooldown"

    def __init__(self, path: str | None = None, cooldown_seconds: int = 300):
        if path is None:
            path = str(Path.home() / ".cfoperator" / "event-runtime" / "policies" / "dedupe.json")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._state = self._load_state()

    def evaluate(self, alert: Alert) -> Tuple[bool, str | None]:
        fingerprint = alert.effective_fingerprint()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.cooldown_seconds)

        with self._lock:
            self._prune(now)
            current = self._state.get(fingerprint)
            if current:
                return False, f"duplicate suppressed until {current}"
            self._state[fingerprint] = expires_at.isoformat()
            self._persist()
        return True, None

    def health(self) -> dict:
        return {
            "name": self.name,
            "healthy": True,
            "cooldown_seconds": self.cooldown_seconds,
            "entries": len(self._state),
            "path": str(self.path),
        }

    def _load_state(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return {str(key): str(value) for key, value in data.items()}
        except Exception as exc:
            logger.warning("Failed to load dedupe state from %s: %s", self.path, exc)
            return {}
        return {}

    def _persist(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    def _prune(self, now: datetime) -> None:
        stale = []
        for fingerprint, expires in self._state.items():
            try:
                if datetime.fromisoformat(expires) <= now:
                    stale.append(fingerprint)
            except ValueError:
                stale.append(fingerprint)
        for fingerprint in stale:
            self._state.pop(fingerprint, None)


class RecurrenceSuppressionPolicy(AlertPolicy):
    """Notify a recurring finding once, then stay quiet for a long window.

    The 5-minute cooldown only catches alert storms; proactive sweep findings
    recur every cycle (~90-150 min) and would re-notify each time (svclb,
    faster-whisper, ...). This suppresses an *identical* recurrence (same
    fingerprint) for a long window — shorter for critical, which should keep
    reminding. Escalation passes through automatically: severity is part of the
    fingerprint, so warning→critical is a new fingerprint and notifies.

    Separate state file from the cooldown so the two policies don't clobber.
    """

    name = "recurrence-suppression"

    def __init__(self, path: str | None = None, window_seconds: int = 21600,
                 critical_window_seconds: int = 1800):
        if path is None:
            path = str(Path.home() / ".cfoperator" / "event-runtime" / "policies" / "recurrence.json")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.window_seconds = window_seconds
        self.critical_window_seconds = critical_window_seconds
        self._lock = threading.Lock()
        self._state = self._load_state()

    def evaluate(self, alert: Alert) -> Tuple[bool, str | None]:
        fingerprint = alert.effective_fingerprint()
        severity = alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity)
        window = self.critical_window_seconds if severity == "critical" else self.window_seconds
        now = datetime.now(timezone.utc)

        with self._lock:
            self._prune(now)
            current = self._state.get(fingerprint)
            if current:
                return False, f"recurring finding suppressed until {current}"
            self._state[fingerprint] = (now + timedelta(seconds=window)).isoformat()
            self._persist()
        return True, None

    def health(self) -> dict:
        return {
            "name": self.name,
            "healthy": True,
            "window_seconds": self.window_seconds,
            "critical_window_seconds": self.critical_window_seconds,
            "entries": len(self._state),
            "path": str(self.path),
        }

    def _load_state(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return {str(key): str(value) for key, value in data.items()}
        except Exception as exc:
            logger.warning("Failed to load recurrence state from %s: %s", self.path, exc)
            return {}
        return {}

    def _persist(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    def _prune(self, now: datetime) -> None:
        stale = []
        for fingerprint, expires in self._state.items():
            try:
                if datetime.fromisoformat(expires) <= now:
                    stale.append(fingerprint)
            except ValueError:
                stale.append(fingerprint)
        for fingerprint in stale:
            self._state.pop(fingerprint, None)