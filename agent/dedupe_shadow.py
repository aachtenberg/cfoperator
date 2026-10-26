"""Shadow comparison against the cfop-dedupe service (CFOP-514, phase 2 of CFOP-509).

The agent still decides every enqueue itself. This module asks cfop-dedupe
(``dedupe/``, the CFOP-510 port of the same tiers) the same question about the
same state, and counts where the two answers differ. That evidence is what
phase 3 needs before the service takes over the decision. Nothing here can
change what is enqueued: the service's answer is compared and then dropped.

One comparison, start to finish:

1. A site about to decide opens a ``scope`` with its candidate. The scope reads
   the open rows at that moment, before any tier writes an absorbed line, an
   alias or a new row. The service has to be asked about the state Python saw,
   not the state Python left behind.
2. Python decides as it always has. Each tier reports its decision through
   ``note()`` at the point where it decides:
     - ``_record_absorbed_symptom``: node fold;
     - ``_absorb_repeat_remediation``: target or identifier fold;
     - ``KnowledgeBase.queue_remediation``: suppress or new.
   A contextvar carries the scope, so none of the sites' return paths changed.
3. When the scope closes with a decision, one background thread reads the
   NotReady set, calls ``POST /v1/decide`` and compares the answers. A slow or
   dead service costs a log line and a count. It never delays or fails an
   enqueue.

A scope that closes without a decision asks nothing. That happens when the
summary dispatched an investigation instead of enqueuing, when the queue feed
is off, or when the enqueue raised.

The two answers agree under the same strictness as the shared cases: same
decision, same row and tier, and for a new row the same key (and the node, when
the service names one). ``path`` says which site decided. Only the
investigation choke point runs every tier today, so the other paths are
expected to disagree wherever the service would fold and they cannot. That gap
is the funnel's purpose, not a port bug (CFOP-509).

Stdlib only for the HTTP side, the same shape as ``tracker_client.py``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from prometheus_client import Counter

logger = logging.getLogger("cfop.dedupe_shadow")

AUTH_HEADER = "X-CFOP-Token"
SHARED_SECRET_ENV = "CFOP_DEDUPE_SHARED_SECRET"  # noqa: S105 - env var name

#: The payload fields the tiers read. Everything else on a row (rendered
#: context, diffs, decided_by) is dropped before it leaves the process.
PAYLOAD_FIELDS = ("dedupe_key", "dedupe_aliases", "recommendation", "targets", "fold_targets")
#: The candidate fields the service's contract names (dedupe/doc.go).
CANDIDATE_FIELDS = ("dedupe_key", "recommendation", "targets", "fold_targets", "host",
                    "title", "trigger", "remediation_class", "source")

#: Seconds the background call may take. Nothing waits on it.
TIMEOUT = 5.0
#: Comparisons allowed to wait for the one worker. Past this a decision is
#: counted as skipped rather than queued without bound behind a hung service.
MAX_PENDING = 32

# path: investigation | sweep-fallback | summary | chat | console.
# result: agree | disagree | unavailable | skipped. python / service: the
# decision each side reached (service is '' when it gave none). Phase 3's
# gate reads result=disagree on path=investigation.
DEDUPE_SHADOW = Counter(
    'cfoperator_dedupe_shadow_total',
    'Enqueue decisions compared with the cfop-dedupe service (CFOP-514)',
    ['path', 'result', 'python', 'service'])


class DedupeClientError(RuntimeError):
    """The service could not be reached, refused the request, or answered
    with something that is not a decision. ``status`` is the HTTP status, or 0
    for a transport or parse failure."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


def decide(base_url: str, request: Dict[str, Any], *, timeout: float = TIMEOUT) -> Dict[str, Any]:
    """POST /v1/decide. Returns the service's decision, or raises."""
    secret = (os.getenv(SHARED_SECRET_ENV) or "").strip()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               **({AUTH_HEADER: secret} if secret else {})}
    req = urllib.request.Request(f"{base_url.rstrip('/')}/v1/decide",
                                 data=json.dumps(request, default=str).encode("utf-8"),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator URL
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise DedupeClientError(
            f"decide failed ({e.code}): {e.read().decode('utf-8', errors='replace')[:200]}",
            status=e.code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DedupeClientError(f"decide failed: {e}") from e
    if not isinstance(body, dict) or body.get("decision") not in ("new", "fold", "suppress"):
        raise DedupeClientError(f"decide answered without a decision: {str(body)[:200]}")
    return body


def candidate_from(details: Dict[str, Any]) -> Dict[str, Any]:
    """The contract's candidate fields, copied now. A site's tiers rewrite
    ``details`` in place (the node collapse renames the host and the key), so
    a reference taken here would describe the candidate after the decision."""
    return {k: details[k] for k in CANDIDATE_FIELDS if details.get(k) is not None}


def trim_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Open rows as the contract carries them: id, status, and only the payload
    fields the tiers read."""
    out = []
    for r in rows or []:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        out.append({"id": r.get("id"), "status": r.get("status"),
                    "payload": {k: payload[k] for k in PAYLOAD_FIELDS if payload.get(k) is not None}})
    return out


class _Scope:
    def __init__(self, path: str, candidate: Dict[str, Any], open_rows: List[Dict[str, Any]]):
        self.path = path
        self.candidate = candidate
        self.open_rows = open_rows
        self.outcome: Optional[Dict[str, Any]] = None


_SCOPE: contextvars.ContextVar[Optional[_Scope]] = contextvars.ContextVar("dedupe_shadow_scope", default=None)


def note(decision: str, row_id: Optional[int] = None, *, tier: str = "",
         dedupe_key: Optional[str] = None, host: Optional[str] = None) -> None:
    """Record the decision Python just made, on the open scope.

    A no-op outside a scope, so the tiers call it unconditionally. The first
    note wins: one scope is one decision, and a later note could only come
    from something the decision set off.
    """
    s = _SCOPE.get()
    if s is None or s.outcome is not None:
        return
    s.outcome = {"decision": decision, "row_id": row_id, "tier": tier,
                 "dedupe_key": str(dedupe_key or ""), "host": str(host or "")}


def matches(python: Dict[str, Any], service: Dict[str, Any]) -> bool:
    """Whether the two decisions are the same decision (the shared cases' rule)."""
    if python.get("decision") != service.get("decision"):
        return False
    if python["decision"] in ("fold", "suppress"):
        return (python.get("row_id") == service.get("row_id")
                and python.get("tier") == (service.get("tier") or ""))
    if python.get("dedupe_key") != (service.get("dedupe_key") or ""):
        return False
    # A new row names the node only when the node tier rewrote it; otherwise
    # the host is whatever the caller passed and the service says nothing.
    return not service.get("host") or python.get("host") == service["host"]


_lock = threading.Lock()
_pending = 0
_executor: Optional[ThreadPoolExecutor] = None


def _submit(fn: Callable[..., Any], *args: Any) -> None:
    """Run one comparison on the shadow thread, created on first use."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dedupe-shadow")
        executor = _executor
    executor.submit(fn, *args)


def compare(url: str, path: str, candidate: Dict[str, Any], open_rows: List[Dict[str, Any]],
            notready: Callable[[], Iterable[str]], python: Dict[str, Any]) -> str:
    """Ask the service and record how its answer compares. Returns the result
    label. Never raises: it runs where nothing would see an exception."""
    global _pending
    try:
        try:
            nodes = sorted({str(n).lower() for n in (notready() or ()) if n})
        except Exception as e:
            logger.debug(f"Dedupe shadow: NotReady set unreadable ({e}); sending none")
            nodes = []
        request = {"candidate": candidate, "open_rows": open_rows, "closed_rows": [],
                   "vocabulary": {"notready_nodes": nodes}}
        try:
            service = decide(url, request)
        except DedupeClientError as e:
            DEDUPE_SHADOW.labels(path=path, result="unavailable",
                                 python=python["decision"], service="").inc()
            logger.warning(f"Dedupe shadow ({path}): service unavailable, nothing compared: {e}")
            return "unavailable"
        result = "agree" if matches(python, service) else "disagree"
        DEDUPE_SHADOW.labels(path=path, result=result, python=python["decision"],
                             service=str(service.get("decision") or "")).inc()
        if result == "disagree":
            logger.warning(
                f"Dedupe shadow ({path}) disagrees on {candidate.get('dedupe_key')!r}: "
                f"python {python['decision']} #{python.get('row_id')} tier={python.get('tier')!r} "
                f"key={python.get('dedupe_key')!r}; service {service.get('decision')} "
                f"#{service.get('row_id')} tier={service.get('tier')!r} "
                f"key={service.get('dedupe_key')!r} ({service.get('reason')})")
        else:
            logger.debug(f"Dedupe shadow ({path}) agrees: {python['decision']} "
                         f"#{python.get('row_id')} on {candidate.get('dedupe_key')!r}")
        return result
    except Exception as e:
        logger.warning(f"Dedupe shadow ({path}) comparison failed: {e}", exc_info=True)
        return "unavailable"
    finally:
        with _lock:
            _pending -= 1


@contextmanager
def scope(path: str, candidate: Dict[str, Any], *, url: str,
          open_rows: Callable[[], Iterable[Dict[str, Any]]],
          notready: Callable[[], Iterable[str]]) -> Iterator[Optional[_Scope]]:
    """One shadow comparison around one enqueue decision.

    ``url`` empty means the service is not configured, and the scope does
    nothing at all, not even the snapshot. A snapshot that fails does the
    same: the comparison is lost and the enqueue is not.
    """
    if not url:
        yield None
        return
    try:
        s = _Scope(path, candidate_from(candidate), trim_rows(open_rows()))
    except Exception as e:
        logger.debug(f"Dedupe shadow ({path}): open rows unreadable ({e}); not comparing")
        yield None
        return
    token = _SCOPE.set(s)
    try:
        yield s
    finally:
        _SCOPE.reset(token)
    # Reached only when the body returned normally: a decision that raised
    # was not made, so there is nothing to compare. Python has already acted
    # by now, so nothing below may raise into the site.
    if s.outcome is None:
        return
    global _pending
    with _lock:
        if _pending >= MAX_PENDING:
            DEDUPE_SHADOW.labels(path=path, result="skipped",
                                 python=s.outcome["decision"], service="").inc()
            return
        _pending += 1
    try:
        _submit(compare, url, path, s.candidate, s.open_rows, notready, s.outcome)
    except Exception as e:
        with _lock:
            _pending -= 1
        logger.debug(f"Dedupe shadow ({path}): could not schedule the comparison: {e}")
