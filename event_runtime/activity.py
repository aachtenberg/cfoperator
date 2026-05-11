"""Helpers for turning raw runtime events into readable activity records."""

from __future__ import annotations

from html import escape
from typing import Iterable, List


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


def render_activity_html(activities: Iterable[dict]) -> bytes:
    """Render a small HTML timeline for operators who need a quick audit view."""
    cards: List[str] = []
    for activity in activities:
        summary = escape(str(activity.get("summary") or "(no summary)"))
        source = escape(str(activity.get("source") or "unknown"))
        severity = escape(str(activity.get("severity") or "unknown"))
        status = escape(str(activity.get("status") or "unknown"))
        action = escape(str(activity.get("action") or "unknown"))
        latest_event_at = escape(str(activity.get("latest_event_at") or ""))
        alert_id = escape(str(activity.get("alert_id") or ""))
        job_id = escape(str(activity.get("job_id") or ""))
        message = escape(str(activity.get("message") or activity.get("reason") or ""))
        cards.append(
            """
            <article class=\"activity-card\">
              <header>
                <div>
                  <h2>{summary}</h2>
                  <p>{source} / {severity}</p>
                </div>
                <div class=\"status-pill\">{status}</div>
              </header>
              <dl>
                <div><dt>Action</dt><dd>{action}</dd></div>
                <div><dt>Latest Event</dt><dd>{latest_event_at}</dd></div>
                <div><dt>Alert ID</dt><dd>{alert_id}</dd></div>
                <div><dt>Job ID</dt><dd>{job_id}</dd></div>
              </dl>
              <p class=\"message\">{message}</p>
            </article>
            """.format(
                summary=summary,
                source=source,
                severity=severity,
                status=status,
                action=action,
                latest_event_at=latest_event_at,
                alert_id=alert_id,
                job_id=job_id,
                message=message,
            )
        )

    body = "".join(cards) or "<p class=\"empty-state\">No runtime activity recorded yet.</p>"
    html = (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
        "    <title>CFOperator Event Runtime Activity</title>\n"
        "    <style>\n"
        "      :root {\n"
        "        color-scheme: light;\n"
        "        --bg: #f3efe6;\n"
        "        --panel: rgba(255, 252, 246, 0.92);\n"
        "        --ink: #17222f;\n"
        "        --muted: #58636f;\n"
        "        --line: rgba(23, 34, 47, 0.14);\n"
        "        --accent: #0d6b5f;\n"
        "      }\n"
        "      * { box-sizing: border-box; }\n"
        "      body {\n"
        "        margin: 0;\n"
        "        font-family: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;\n"
        "        background:\n"
        "          radial-gradient(circle at top left, rgba(13, 107, 95, 0.12), transparent 32%),\n"
        "          linear-gradient(180deg, #f7f2e8 0%, var(--bg) 100%);\n"
        "        color: var(--ink);\n"
        "      }\n"
        "      main {\n"
        "        max-width: 1100px;\n"
        "        margin: 0 auto;\n"
        "        padding: 32px 20px 48px;\n"
        "      }\n"
        "      h1 {\n"
        "        margin: 0 0 8px;\n"
        "        font-family: \"IBM Plex Serif\", Georgia, serif;\n"
        "        font-size: clamp(2rem, 4vw, 3rem);\n"
        "      }\n"
        "      .lede {\n"
        "        margin: 0 0 24px;\n"
        "        color: var(--muted);\n"
        "        max-width: 60ch;\n"
        "      }\n"
        "      .activity-grid {\n"
        "        display: grid;\n"
        "        gap: 16px;\n"
        "      }\n"
        "      .activity-card {\n"
        "        border: 1px solid var(--line);\n"
        "        border-radius: 18px;\n"
        "        background: var(--panel);\n"
        "        padding: 18px 20px;\n"
        "        box-shadow: 0 18px 40px rgba(23, 34, 47, 0.06);\n"
        "      }\n"
        "      .activity-card header {\n"
        "        display: flex;\n"
        "        justify-content: space-between;\n"
        "        gap: 12px;\n"
        "        align-items: start;\n"
        "      }\n"
        "      .activity-card h2 {\n"
        "        margin: 0 0 4px;\n"
        "        font-size: 1.1rem;\n"
        "      }\n"
        "      .activity-card p {\n"
        "        margin: 0;\n"
        "        color: var(--muted);\n"
        "      }\n"
        "      .status-pill {\n"
        "        padding: 6px 12px;\n"
        "        border-radius: 999px;\n"
        "        background: rgba(13, 107, 95, 0.12);\n"
        "        color: var(--accent);\n"
        "        font-weight: 600;\n"
        "        text-transform: capitalize;\n"
        "        white-space: nowrap;\n"
        "      }\n"
        "      dl {\n"
        "        display: grid;\n"
        "        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));\n"
        "        gap: 12px;\n"
        "        margin: 16px 0 0;\n"
        "      }\n"
        "      dt {\n"
        "        font-size: 0.75rem;\n"
        "        text-transform: uppercase;\n"
        "        letter-spacing: 0.08em;\n"
        "        color: var(--muted);\n"
        "      }\n"
        "      dd {\n"
        "        margin: 6px 0 0;\n"
        "        font-family: \"IBM Plex Mono\", monospace;\n"
        "        word-break: break-word;\n"
        "      }\n"
        "      .message {\n"
        "        margin-top: 16px;\n"
        "        color: var(--ink);\n"
        "      }\n"
        "      .empty-state {\n"
        "        margin: 0;\n"
        "        padding: 32px;\n"
        "        border-radius: 18px;\n"
        "        background: var(--panel);\n"
        "        border: 1px solid var(--line);\n"
        "      }\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        "    <main>\n"
        "      <h1>Event Runtime Activity</h1>\n"
        "      <p class=\"lede\">A readable audit trail of what the runtime received, decided, skipped, queued, and completed.</p>\n"
        f"      <section class=\"activity-grid\">{body}</section>\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )
    return html.encode("utf-8")


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