"""Shared, thread-safe ledger of escalated alert fingerprints.

The poll thread (``AlertmanagerAlertSource``) and the worker threads
(``EventRuntime.handle_alert``) run concurrently, so the escalation set is
guarded by a lock. The engine ``mark``\\s a fingerprint when an action escalates
to a human; the source ``take``\\s it (atomic check-and-remove) when the
corresponding alert clears, so a one-time "Resolved:" notification is emitted
only for alerts a human was actually paged about.
"""

from __future__ import annotations

import threading


class EscalationLedger:
    """Track which firing-alert fingerprints escalated to a human.

    A plain set behind a lock: the engine adds, the source atomically removes.
    Bounded by the number of *concurrently firing* escalated alerts — entries
    are removed when the alert resolves, so it does not grow without bound.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprints: set[str] = set()

    def mark(self, fingerprint: str) -> None:
        """Record that ``fingerprint`` escalated. No-op for empty fingerprints."""
        if not fingerprint:
            return
        with self._lock:
            self._fingerprints.add(fingerprint)

    def take(self, fingerprint: str) -> bool:
        """Atomically remove ``fingerprint`` and report whether it was present.

        Returns ``True`` exactly once per escalation, so the caller emits a
        single resolution notification even if ``take`` is retried.
        """
        if not fingerprint:
            return False
        with self._lock:
            if fingerprint in self._fingerprints:
                self._fingerprints.discard(fingerprint)
                return True
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._fingerprints)
