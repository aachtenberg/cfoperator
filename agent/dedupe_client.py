"""The agent's half of cfop-dedupe, which decides every remediation enqueue
(CFOP-538, phase 3 of CFOP-509).

The service (``dedupe/``) is stateless and only decides. The agent sends it
everything a decision needs and applies the answer itself, holding
``CFOperator._remediation_lock`` from the snapshot of open rows to the insert,
so two enqueues racing each other cannot both be told "new".

This module is the part that talks to the service:

- ``resolve()`` asks ``POST /v1/decide``. When the service cannot answer
  (unreachable, timed out, refused, or answering with something that is not a
  decision), the knowledge base's exact-key check decides instead, over the
  same snapshot. CFOP-509 chose that fallback on purpose: an outage can let a
  duplicate through (a reworded repeat, a node symptom), never lose a row, and
  no enqueue waits for the service to come back.
- ``count()`` records the decision a site acted on.
- ``candidate_from()`` / ``trim_rows()`` give the request its shape.

Stdlib only for the HTTP side, the same shape as ``tracker_client.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

from prometheus_client import Counter, Histogram

from cfshared import tracing

logger = logging.getLogger("cfop.dedupe")

AUTH_HEADER = "X-CFOP-Token"
SHARED_SECRET_ENV = "CFOP_DEDUPE_SHARED_SECRET"  # noqa: S105 - env var name

#: The payload fields the tiers read. Everything else on a row (rendered
#: context, diffs, decided_by) is dropped before it leaves the process.
PAYLOAD_FIELDS = ("dedupe_key", "dedupe_aliases", "recommendation", "targets", "fold_targets")
#: The candidate fields the service's contract names (dedupe/doc.go).
CANDIDATE_FIELDS = ("dedupe_key", "recommendation", "targets", "fold_targets", "host",
                    "title", "trigger", "remediation_class", "source")

#: Seconds an enqueue waits for the service. It decides in memory over a
#: handful of rows, so this only bounds a hung service; the fallback then
#: decides and the enqueue goes on.
TIMEOUT = 2.0

#: Statuses that end a row: its key stops matching, a fold cannot land on it.
TERMINAL = ("resolved", "rejected")

# path: investigation | sweep-fallback | summary | chat | console.
# decision: new | fold | suppress. tier: node | target | identifier | key |
# model (CFOP-545), or '' for a new row. source: service | fallback (the
# service could not answer and the exact key decided) | unconfigured (no
# service URL is set) | model (the dedupe model step folded it).
DEDUPE_DECISIONS = Counter(
    'cfoperator_dedupe_decisions_total',
    'Remediation enqueue decisions, by who made them (CFOP-538)',
    ['path', 'decision', 'tier', 'source'])
# outcome: ok | error. Timed on the agent's side, so it sees the timeouts and
# refused connections the service never does.
DEDUPE_REQUEST_SECONDS = Histogram(
    'cfoperator_dedupe_request_seconds',
    'Agent calls to cfop-dedupe POST /v1/decide (CFOP-538)',
    ['outcome'], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0))
# Both children exist from startup at zero, so the first failed call reads as
# an increase (homelab-infra's CFOperatorDedupeFallback alerts on it) instead
# of as a new series, which increase() cannot see.
for _outcome in ("ok", "error"):
    DEDUPE_REQUEST_SECONDS.labels(outcome=_outcome)


class DedupeClientError(RuntimeError):
    """The service could not be reached, refused the request, or answered
    with something that is not a decision. ``status`` is the HTTP status, or 0
    for a transport or parse failure."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


def decide(base_url: str, request: Dict[str, Any], *, timeout: float = TIMEOUT) -> Dict[str, Any]:
    """POST /v1/decide. Returns the service's decision, or raises.

    Carries the current span's ``traceparent``, so the service's decision log
    line names the trace it belongs to."""
    secret = (os.getenv(SHARED_SECRET_ENV) or "").strip()
    headers = tracing.inject({"Content-Type": "application/json", "Accept": "application/json",
                              **({AUTH_HEADER: secret} if secret else {})})
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
    if body["decision"] != "new" and not body.get("row_id"):
        raise DedupeClientError(f"decide answered {body['decision']} without a row: {str(body)[:200]}")
    return body


def candidate_from(details: Dict[str, Any]) -> Dict[str, Any]:
    """The contract's candidate fields, copied now: applying a decision
    rewrites ``details`` in place (a node incident renames the host and the
    key), and the request has to describe the candidate as it was asked."""
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


def fallback(candidate: Dict[str, Any], open_rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The knowledge base's exact-key check, over the snapshot the service
    would have been asked about: suppressed if an open row's own key is the
    candidate's, otherwise new under the candidate's key. With no snapshot,
    new, and ``queue_remediation``'s own check at insert time decides."""
    key = str(candidate.get("dedupe_key") or "")
    if key:
        for row in open_rows or []:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if row.get("status") not in TERMINAL and payload.get("dedupe_key") == key:
                return {"decision": "suppress", "row_id": row.get("id"), "tier": "key",
                        "dedupe_key": key, "reason": f"open row #{row.get('id')} already carries {key}"}
    return {"decision": "new", "dedupe_key": key, "reason": "exact key only: no open row carries it"}


def resolve(url: str, path: str, candidate: Dict[str, Any],
            open_rows: Optional[List[Dict[str, Any]]], notready_nodes: List[str]) -> Dict[str, Any]:
    """The decision for one enqueue, and who made it (``source``).

    ``open_rows`` None means the snapshot could not be read; the service is
    not asked about a state nobody saw. Never raises: every failure is the
    fallback's to decide."""
    with tracing.span('dedupe.decide', {'cfop.dedupe.path': path}):
        if not url:
            out = {**fallback(candidate, open_rows), "source": "unconfigured"}
        elif open_rows is None:
            out = {**fallback(candidate, None), "source": "fallback", "error": "snapshot"}
            tracing.mark_error("DedupeSnapshotError")
        else:
            request = {"candidate": candidate, "open_rows": open_rows, "closed_rows": [],
                       "vocabulary": {"notready_nodes": notready_nodes}}
            started = time.monotonic()
            try:
                out = {**decide(url, request), "source": "service"}
                DEDUPE_REQUEST_SECONDS.labels(outcome="ok").observe(time.monotonic() - started)
            except Exception as e:
                DEDUPE_REQUEST_SECONDS.labels(outcome="error").observe(time.monotonic() - started)
                status = getattr(e, "status", 0) or None
                logger.warning(f"cfop-dedupe could not decide ({path}); the exact key decides: {e}")
                out = {**fallback(candidate, open_rows), "source": "fallback",
                       "error": f"http {status}" if status else type(e).__name__}
                tracing.annotate({'cfop.dedupe.http_status': status})
                tracing.mark_error(type(e).__name__)
        tracing.annotate({'cfop.dedupe.decision': out["decision"],
                          'cfop.dedupe.tier': out.get("tier") or "",
                          'cfop.dedupe.source': out["source"],
                          'cfop.dedupe.row_id': out.get("row_id")})
        return out


def count(path: str, decision: Dict[str, Any]) -> None:
    """Record a decision the site acted on."""
    DEDUPE_DECISIONS.labels(path=path, decision=decision["decision"],
                            tier=decision.get("tier") or "", source=decision["source"]).inc()
