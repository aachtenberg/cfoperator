"""Domain events shaped like the ones EventRuntime.handle_alert records.

Shared by the alert-store suites (CFOP-215) so the in-memory and Postgres
stores are exercised on identical input.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def alert(i, *, source="alertmanager", severity="warning", summary=None,
          namespace="apps", resource_name=None):
    return {
        "alert_id": f"alert-{i:05d}",
        "source": source,
        "severity": severity,
        "summary": summary or f"alert number {i}",
        "details": {},
        "namespace": namespace,
        "resource_type": "pod",
        "resource_name": resource_name or f"pod-{i}",
        "fingerprint": None,
        "occurred_at": BASE.isoformat(),
    }


def event(event_type, at, **payload):
    return {"event_id": str(uuid4()), "event_type": event_type,
            "created_at": at.isoformat(), "payload": payload}


def lifecycle(a, start, outcome, *, action="investigate"):
    """The events one alert produces for a given outcome, oldest first.

    outcome: completed | failed | suppressed | logged | received
    """
    events = [event("alert_received", start, alert=a)]
    t = start + timedelta(seconds=1)
    if outcome == "received":
        return events
    if outcome == "suppressed":
        return events + [event("alert_suppressed", t, alert=a, policy="dedupe", reason="duplicate")]
    if outcome == "logged":
        return events + [event("alert_skipped", t, alert=a, reason="severity_gate")]
    decision = {"action": action, "confidence": 0.8, "reasoning": f"why {a['alert_id']}"}
    result = {"action": action, "success": outcome == "completed",
              "message": f"{outcome}: {a['summary']}", "details": {"investigation_id": 7}}
    return events + [
        event("decision_made", t, alert=a, decision=decision),
        event("action_completed", t + timedelta(seconds=1), alert=a, decision=decision, result=result),
    ]


def fleet(n=40):
    """n alerts across outcomes, sources and severities, some sharing a
    latest_event_at so paging has ties to get right."""
    outcomes = ("completed", "failed", "suppressed", "logged", "received")
    sources = ("alertmanager", "cfoperator-sweep", "boot-forensics")
    severities = ("info", "warning", "critical")
    events = []
    for i in range(n):
        # Every third alert is a twin of the one before it: same start, same
        # outcome, so the same latest_event_at, and only alert_id breaks the tie.
        slot = i - 1 if i % 3 == 2 else i
        start = BASE + timedelta(minutes=slot)
        a = alert(i, source=sources[i % 3], severity=severities[i % 3],
                  summary=f"{'Disk pressure' if i % 4 == 0 else 'Pod restarting'} on node-{i % 5}")
        events += lifecycle(a, start, outcomes[slot % len(outcomes)])
    return events
