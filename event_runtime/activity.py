"""Helpers for turning raw runtime events into readable activity records.

The fold here (``build_activity_feed`` → ``_merge_activity``) is the one
definition of what an alert's status, action and outcome are. The Postgres
read model (``state/postgres.py``) stores its output per alert rather than
restating it in SQL, and the outbox store folds on read, so both answer the
same question the same way (CFOP-215).
"""

from __future__ import annotations

from typing import Iterable, List

# Bump when a change to the fold would change what it returns for events
# already stored. The Postgres read model rebuilds every row whose version
# differs on the next start; without the bump, old alerts keep the old answer.
FOLD_VERSION = 1

# Dropped from list views: the per-event detail belongs to the one-alert read.
_DETAIL_ONLY = ("timeline", "event_types")


def alert_key(event: dict) -> str:
    """The alert an event belongs to, or "" when it belongs to none.

    The same extraction the fold groups by, so a read model keyed on this
    groups exactly as the fold does.
    """
    payload = event.get("payload") or {}
    alert = _extract_alert(payload)
    return str((alert or {}).get("alert_id") or "")


def fold_alert(events: Iterable[dict]) -> dict:
    """Fold every event of ONE alert into its activity record."""
    events = list(events)
    keys = {alert_key(event) for event in events}
    if len(keys) != 1 or "" in keys:
        raise ValueError(f"fold_alert needs the events of exactly one alert, got {sorted(keys)}")
    return build_activity_feed(events, limit=1)[0]


def summarize_activity(activity: dict) -> dict:
    """An activity record without its per-event detail, for list views."""
    return {key: value for key, value in activity.items() if key not in _DETAIL_ONLY}


def filter_events(
    events: Iterable[dict],
    *,
    event_type: str | None = None,
    alert_id: str | None = None,
    job_id: str | None = None,
) -> List[dict]:
    """Filter raw event payloads using alert or worker identifiers."""
    normalized_event_type = (event_type or "").strip()
    normalized_alert_id = (alert_id or "").strip()
    normalized_job_id = (job_id or "").strip()

    filtered: List[dict] = []
    for event in events:
        payload = event.get("payload") or {}
        alert = _extract_alert(payload)
        job = _extract_job(payload)
        if normalized_event_type and event.get("event_type") != normalized_event_type:
            continue
        if normalized_alert_id and (alert or {}).get("alert_id") != normalized_alert_id:
            continue
        if normalized_job_id and (job or {}).get("job_id") != normalized_job_id:
            continue
        filtered.append(event)
    return filtered


def build_activity_feed(events: Iterable[dict], limit: int = 50) -> List[dict]:
    """Collapse raw domain events into alert-centric activity entries."""
    grouped: dict[str, dict] = {}
    ordered = sorted(events, key=lambda event: str(event.get("created_at") or ""))
    for event in ordered:
        payload = event.get("payload") or {}
        alert = _extract_alert(payload)
        job = _extract_job(payload)
        activity_key = _activity_key(event, alert, job)
        activity = grouped.get(activity_key)
        if activity is None:
            activity = _new_activity(event, alert, job)
            grouped[activity_key] = activity
        _merge_activity(activity, event, payload, alert, job)

    activities = sorted(grouped.values(), key=lambda item: item["latest_event_at"], reverse=True)
    return activities[: max(1, limit)]


def filter_activities(
    activities: Iterable[dict],
    *,
    status: str | None = None,
    action: str | None = None,
) -> List[dict]:
    """Filter summarized activities by final status or action."""
    normalized_status = (status or "").strip()
    normalized_action = (action or "").strip()
    filtered: List[dict] = []
    for activity in activities:
        if normalized_status and activity.get("status") != normalized_status:
            continue
        if normalized_action and activity.get("action") != normalized_action:
            continue
        filtered.append(activity)
    return filtered


def _activity_key(event: dict, alert: dict | None, job: dict | None) -> str:
    alert_id = (alert or {}).get("alert_id")
    if alert_id:
        return f"alert:{alert_id}"
    job_id = (job or {}).get("job_id")
    if job_id:
        return f"job:{job_id}"
    return f"event:{event.get('event_id') or event.get('created_at') or id(event)}"


def _new_activity(event: dict, alert: dict | None, job: dict | None) -> dict:
    summary = (alert or {}).get("summary") or (job or {}).get("alert", {}).get("summary") or ""
    created_at = str(event.get("created_at") or "")
    return {
        "alert_id": (alert or {}).get("alert_id") or (job or {}).get("alert", {}).get("alert_id"),
        "job_id": (job or {}).get("job_id"),
        "source": (alert or {}).get("source") or (job or {}).get("alert", {}).get("source"),
        "severity": (alert or {}).get("severity") or (job or {}).get("alert", {}).get("severity"),
        "summary": summary,
        "namespace": (alert or {}).get("namespace") or (job or {}).get("alert", {}).get("namespace"),
        "resource_type": (alert or {}).get("resource_type") or (job or {}).get("alert", {}).get("resource_type"),
        "resource_name": (alert or {}).get("resource_name") or (job or {}).get("alert", {}).get("resource_name"),
        "status": "received",
        "action": None,
        "reason": None,
        "message": None,
        "decision": None,
        "result": None,
        "success": None,
        "first_event_at": created_at,
        "latest_event_at": created_at,
        "event_count": 0,
        "event_types": [],
        "timeline": [],
    }


def _merge_activity(activity: dict, event: dict, payload: dict, alert: dict | None, job: dict | None) -> None:
    event_type = str(event.get("event_type") or "unknown")
    created_at = str(event.get("created_at") or "")
    if alert:
        activity["alert_id"] = activity.get("alert_id") or alert.get("alert_id")
        activity["source"] = activity.get("source") or alert.get("source")
        activity["severity"] = activity.get("severity") or alert.get("severity")
        activity["summary"] = activity.get("summary") or alert.get("summary")
        activity["namespace"] = activity.get("namespace") or alert.get("namespace")
        activity["resource_type"] = activity.get("resource_type") or alert.get("resource_type")
        activity["resource_name"] = activity.get("resource_name") or alert.get("resource_name")
    if job:
        activity["job_id"] = activity.get("job_id") or job.get("job_id")

    activity["latest_event_at"] = max(activity.get("latest_event_at") or created_at, created_at)
    activity["first_event_at"] = min(activity.get("first_event_at") or created_at, created_at)
    activity["event_count"] = int(activity.get("event_count") or 0) + 1
    activity.setdefault("event_types", []).append(event_type)
    activity.setdefault("timeline", []).append(_timeline_entry(event_type, created_at, payload, job))

    if event_type == "alert_received":
        activity["status"] = "received"
        return
    if event_type == "alert_queued":
        activity["status"] = "queued"
        return
    if event_type in {"alert_job_started", "alert_job_restored"}:
        activity["status"] = "running" if event_type == "alert_job_started" else "queued"
        return
    if event_type == "alert_suppressed":
        activity["status"] = "suppressed"
        activity["action"] = "suppressed"
        activity["success"] = True
        activity["reason"] = payload.get("reason")
        return
    if event_type == "alert_skipped":
        activity["status"] = "logged"
        activity["action"] = "log_only"
        activity["success"] = True
        activity["reason"] = payload.get("reason")
        return
    if event_type == "decision_made":
        decision = dict(payload.get("decision") or {})
        activity["decision"] = decision or None
        activity["action"] = decision.get("action") or activity.get("action")
        return
    if event_type == "checks_requested":
        return
    if event_type == "action_missing":
        decision = dict(payload.get("decision") or {})
        activity["decision"] = decision or activity.get("decision")
        activity["status"] = "failed"
        activity["action"] = decision.get("action") or activity.get("action")
        activity["success"] = False
        activity["message"] = f"No action handler registered for {activity.get('action') or 'unknown'}"
        return
    if event_type == "action_completed":
        result = dict(payload.get("result") or {})
        activity["status"] = "completed" if result.get("success") else "failed"
        activity["action"] = result.get("action") or activity.get("action")
        activity["success"] = bool(result.get("success"))
        activity["message"] = result.get("message") or activity.get("message")
        activity["result"] = result or None
        return
    if event_type == "alert_job_completed":
        result = dict((job or {}).get("result") or {})
        if result:
            activity["action"] = result.get("action") or activity.get("action")
            activity["message"] = result.get("message") or activity.get("message")
            activity["result"] = result
            activity["success"] = result.get("success") if result.get("success") is not None else activity.get("success")
            activity["status"] = result.get("status") or ("completed" if result.get("success") else activity.get("status"))
        else:
            activity["status"] = "completed"
        return
    if event_type == "alert_job_failed":
        activity["status"] = "failed"
        activity["success"] = False
        activity["message"] = payload.get("error") or (job or {}).get("error") or activity.get("message")
        return


def _timeline_entry(event_type: str, created_at: str, payload: dict, job: dict | None) -> dict:
    note = payload.get("reason")
    if note is None and event_type == "decision_made":
        decision = payload.get("decision") or {}
        note = decision.get("reasoning")
    if note is None and event_type == "action_completed":
        result = payload.get("result") or {}
        note = result.get("message")
    if note is None and event_type in {"alert_job_failed", "action_missing"}:
        note = payload.get("error")
    if note is None and job:
        note = (job.get("result") or {}).get("message")
    return {
        "created_at": created_at,
        "event_type": event_type,
        "note": note,
    }


def _extract_alert(payload: dict) -> dict | None:
    alert = payload.get("alert")
    if isinstance(alert, dict):
        return alert
    job = payload.get("job")
    if isinstance(job, dict):
        candidate = job.get("alert")
        if isinstance(candidate, dict):
            return candidate
    return None


def _extract_job(payload: dict) -> dict | None:
    job = payload.get("job")
    return job if isinstance(job, dict) else None