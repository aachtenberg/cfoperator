"""Querying alerts by their folded state (CFOP-215).

``/activity`` used to read the newest N raw events, fold them per alert, and
only then filter. So ``status=failed`` meant "failed among whatever fitted in
the window", an alert straddling the window edge came back half-folded, and
nothing could be paged. Filtering has to happen on state that is already
folded, over every alert.

Two stores answer this, and the query semantics are defined once, here:

- the Postgres read model (``state/postgres.py``), one row per alert, kept up
  to date on every append;
- the local outbox, folded in memory by ``apply_query`` below, which is the
  answer when Postgres is down or not configured.

``test_alert_store_pg.py`` runs the same queries against both over the same
events and requires the same answer.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Mapping, Optional, Tuple

from .activity import summarize_activity
from .models import AlertSeverity

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_MAX_FILTER_LEN = 64
_MAX_Q_LEN = 200
_PARAMS = frozenset({"limit", "cursor", "status", "action", "source", "severity", "since", "until", "q"})


class AlertStoreUnavailable(Exception):
    """This store cannot answer right now, or keeps no per-alert read model."""


@dataclass(frozen=True)
class AlertQuery:
    limit: int = DEFAULT_LIMIT
    # (latest_event_at, alert_id) of the last row of the previous page.
    after: Optional[Tuple[datetime, str]] = None
    status: Optional[str] = None
    action: Optional[str] = None
    source: Optional[str] = None
    severity: Optional[str] = None
    # Bounds on the alert's latest event: since <= latest_event_at < until.
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    # Case-insensitive substring of summary, resource, namespace or alert id.
    q: Optional[str] = None
    # Keep the timeline and event types in each row. Internal (the legacy
    # /activity feed returns them); the HTTP list view does not.
    full: bool = False


@dataclass
class AlertPage:
    alerts: List[dict]
    next_cursor: Optional[str]
    # Which store answered: "postgres" or "outbox".
    store: str
    # True when Postgres answered but the outbox holds events it has not
    # replayed yet, so the newest activity may be missing.
    lagging: bool = False

    def to_dict(self) -> dict:
        return {"alerts": self.alerts, "next_cursor": self.next_cursor,
                "store": self.store, "lagging": self.lagging}


def parse_alert_query(params: Mapping[str, str]) -> AlertQuery:
    """Build a query from HTTP parameters. Raises ValueError for a 400.

    Unknown parameters are refused rather than ignored: ``statu=failed``
    silently returning everything is the failure mode this replaces.
    """
    unknown = sorted(set(params) - _PARAMS)
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(unknown)}")

    raw_limit = params.get("limit")
    limit = DEFAULT_LIMIT
    if raw_limit not in (None, ""):
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ValueError("limit must be an integer") from None
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")

    severity = _short(params, "severity")
    if severity is not None and severity not in {s.value for s in AlertSeverity}:
        raise ValueError(f"severity must be one of {', '.join(s.value for s in AlertSeverity)}")

    q = params.get("q") or None
    if q is not None:
        q = q.strip() or None
        if q is not None and len(q) > _MAX_Q_LEN:
            raise ValueError(f"q is limited to {_MAX_Q_LEN} characters")

    since = _timestamp(params, "since")
    until = _timestamp(params, "until")
    if since and until and since >= until:
        raise ValueError("since must be earlier than until")

    cursor = params.get("cursor") or None
    return AlertQuery(
        limit=limit,
        after=decode_cursor(cursor) if cursor else None,
        status=_short(params, "status"),
        action=_short(params, "action"),
        source=_short(params, "source"),
        severity=severity,
        since=since,
        until=until,
        q=q,
    )


def encode_cursor(latest_event_at: datetime, alert_id: str) -> str:
    raw = json.dumps([_utc(latest_event_at).isoformat(), alert_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, alert_id = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return parse_timestamp(stamp), str(alert_id)
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise ValueError("cursor is not one this server issued") from None


def parse_timestamp(value: str) -> datetime:
    """ISO 8601, with a trailing Z accepted; naive values are taken as UTC."""
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return _utc(parsed)


def apply_query(activities: Iterable[dict], query: AlertQuery, *, store: str) -> AlertPage:
    """Answer ``query`` over folded activities in memory.

    The outbox store's implementation, and the reference the SQL in
    ``state/postgres.py`` is held to. Only alert activities count: the fold
    also emits records keyed by a bare job or event id, and the read model
    has no row for those.
    """
    needle = query.q.casefold() if query.q else None
    rows = []
    for activity in activities:
        alert_id = activity.get("alert_id")
        if not alert_id:
            continue
        latest = parse_timestamp(activity["latest_event_at"])
        if query.status and activity.get("status") != query.status:
            continue
        if query.action and activity.get("action") != query.action:
            continue
        if query.source and activity.get("source") != query.source:
            continue
        if query.severity and activity.get("severity") != query.severity:
            continue
        if query.since and latest < query.since:
            continue
        if query.until and latest >= query.until:
            continue
        if needle and not any(needle in str(activity.get(key) or "").casefold()
                              for key in ("summary", "resource_name", "namespace", "alert_id")):
            continue
        if query.after and (latest, alert_id) >= query.after:
            continue
        rows.append((latest, alert_id, activity))

    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    page = rows[: query.limit]
    next_cursor = None
    if len(rows) > query.limit:
        latest, alert_id, _ = page[-1]
        next_cursor = encode_cursor(latest, alert_id)
    alerts = [row[2] if query.full else summarize_activity(row[2]) for row in page]
    return AlertPage(alerts=alerts, next_cursor=next_cursor, store=store)


def _short(params: Mapping[str, str], name: str) -> Optional[str]:
    value = (params.get(name) or "").strip()
    if not value:
        return None
    if len(value) > _MAX_FILTER_LEN:
        raise ValueError(f"{name} is limited to {_MAX_FILTER_LEN} characters")
    return value


def _timestamp(params: Mapping[str, str], name: str) -> Optional[datetime]:
    value = (params.get(name) or "").strip()
    if not value:
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
