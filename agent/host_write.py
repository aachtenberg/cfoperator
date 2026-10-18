"""Console-confirmed host writes (CFOP-442).

A write-shaped SSH command on an admin's console chat turn is neither run as
the read identity (whose sudoers refuses it) nor refused. The turn pauses,
the console shows the admin the host, the exact command and why it counts as
a write, and the admin answers Run or Decline. On Run the command goes
through the node-action lane once — a claimed row, a pre-approved change
record, an executor Job as the write identity — and its result comes back to
the model in the same turn.

The product does not gatekeep what an accountable admin can do; it makes it
auditable (feedback recorded on CFOP-442). Unattended runs and member turns
never reach this module: the registry only routes here on a turn that may
change things and carries a confirmer, and only the console route that knows
a browser session builds one.

Two pieces:

  * ``PendingConfirms`` — the process-wide table of questions waiting for an
    answer. The chat runs in a worker thread; the answer arrives on a
    request thread (``POST /api/chat/<chat_id>/confirm``). One agent serves
    the console (the single-agent lock), so an in-process table is the whole
    rendezvous; a restart drops pending questions, and a question nobody
    answers ran nothing.
  * ``HostWriteConfirmer`` — one turn's confirmer, handed to the registry as
    ``ToolPolicy.confirm_host_write``. It asks, waits, and on Run calls the
    agent's ``run_confirmed`` with who approved and when.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("cfoperator.host_write")

# How long a question waits for the admin. Long enough to read a command and
# look something up; short enough that a walked-away tab does not hold a
# chat worker forever. A question that times out ran nothing.
CONFIRM_TIMEOUT_S = 300


class _Pending:
    __slots__ = ("event", "approved", "approver", "decided_at", "why")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.approved = False
        self.approver = ""
        self.decided_at: Optional[str] = None
        self.why = ""


class PendingConfirms:
    """Questions waiting for an admin's answer, by confirm id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, _Pending] = {}

    def open(self) -> tuple:
        cid = uuid.uuid4().hex[:12]
        entry = _Pending()
        with self._lock:
            self._items[cid] = entry
        return cid, entry

    def close(self, cid: str) -> None:
        with self._lock:
            self._items.pop(cid, None)

    def is_open(self, cid: str) -> bool:
        with self._lock:
            entry = self._items.get(cid)
            return entry is not None and not entry.event.is_set()

    def resolve(self, cid: str, approved: bool, approver: str, why: str = "") -> str:
        """Answer one question. Returns 'ok', 'unknown' or 'answered'.

        The first answer wins: a second click, or a Stop racing a Run, finds
        the question already answered and changes nothing.
        """
        with self._lock:
            entry = self._items.get(cid)
            if entry is None:
                return "unknown"
            if entry.event.is_set():
                return "answered"
            entry.approved = bool(approved)
            entry.approver = str(approver or "").strip() or "unknown"
            entry.decided_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            entry.why = why
            entry.event.set()
            return "ok"


PENDING = PendingConfirms()


class HostWriteConfirmer:
    """One chat turn's way of asking an admin before a host write runs.

    ``emit(event_type, data)`` is the turn's event callback (the console
    renders ``confirm`` as a card and ``confirm_result`` as its outcome).
    ``run_confirmed(**kw)`` runs an approved command and returns the tool
    result. ``busy`` is true while this confirmer is waiting on a person or
    on the Job, so the chat stream knows a silence is not a hang.
    """

    def __init__(self, emit: Callable[[str, Dict[str, Any]], None],
                 run_confirmed: Callable[..., Dict[str, Any]], *,
                 timeout_s: float = CONFIRM_TIMEOUT_S,
                 pending: Optional[PendingConfirms] = None) -> None:
        self._emit = emit
        self._run_confirmed = run_confirmed
        self._timeout_s = timeout_s
        self._pending = pending or PENDING
        self._busy = 0
        self._busy_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        with self._busy_lock:
            return self._busy > 0

    def _enter(self) -> None:
        with self._busy_lock:
            self._busy += 1

    def _leave(self) -> None:
        with self._busy_lock:
            self._busy -= 1

    def __call__(self, *, tool: str, host: str, command: str, reason: str) -> Dict[str, Any]:
        self._enter()
        try:
            return self._ask(tool=tool, host=host, command=command, reason=reason)
        finally:
            self._leave()

    def _ask(self, *, tool: str, host: str, command: str, reason: str) -> Dict[str, Any]:
        cid, entry = self._pending.open()
        try:
            self._emit('confirm', {
                'id': cid, 'tool': tool, 'host': host, 'command': command,
                'reason': reason, 'timeout_s': int(self._timeout_s),
            })
            answered = entry.event.wait(self._timeout_s)
        finally:
            self._pending.close(cid)
        if not answered:
            self._emit('confirm_result', {'id': cid, 'decision': 'timeout'})
            logger.info("Host write on %s not confirmed within %ss; nothing ran", host,
                        int(self._timeout_s))
            return {'error': (f"Nobody confirmed `{command}` on {host} within "
                              f"{int(self._timeout_s // 60)} minutes, so nothing ran. Ask the "
                              "operator whether they still want it."),
                    'refused': True, 'declined': True, 'tool': tool}
        if not entry.approved:
            stopped = entry.why == 'stopped'
            self._emit('confirm_result', {'id': cid, 'decision': 'stopped' if stopped else 'declined',
                                          'by': entry.approver})
            logger.info("Host write on %s declined by %s; nothing ran", host, entry.approver)
            return {'error': (f"{entry.approver} declined `{command}` on {host}; nothing ran. "
                              "Do not run it another way. Ask what they want instead."),
                    'refused': True, 'declined': True, 'tool': tool}
        self._emit('confirm_result', {'id': cid, 'decision': 'approved', 'by': entry.approver})
        logger.info("Host write on %s confirmed by %s: %s", host, entry.approver, command)
        return self._run_confirmed(tool=tool, host=host, command=command, reason=reason,
                                   approver=entry.approver,
                                   decided_at=entry.decided_at or _now())


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def wait_for_row(get_row: Callable[[int], Optional[Dict[str, Any]]], rid: int, *,
                 timeout_s: float, poll_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Optional[Dict[str, Any]]:
    """Poll a remediation row until it reaches an outcome, or None on timeout.

    The executor posts its completion to the agent, which writes the row; the
    chat turn reads it back here. A read that fails is retried until the
    deadline, not treated as an outcome.
    """
    deadline = clock() + timeout_s
    while True:
        try:
            row = get_row(rid)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read remediation #%s while waiting: %s", rid, e)
            row = None
        if row and row.get('status') in ('resolved', 'failed', 'needs-human', 'rejected'):
            return row
        if clock() >= deadline:
            return None
        sleep(poll_s)
