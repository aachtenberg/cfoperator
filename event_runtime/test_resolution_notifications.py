"""Resolution notifications: a one-time ":white_check_mark: Resolved:" alert is
emitted when an *escalated* Alertmanager alert clears. Non-escalated clears stay
silent (scoped by design) and the resolution fires at most once."""

from event_runtime.engine import EventRuntime
from event_runtime.escalation import EscalationLedger
from event_runtime.models import ActionResult, Alert, AlertSeverity
from event_runtime.sources import AlertmanagerAlertSource


def _raw(fingerprint, summary="Node down", node="raspberrypi4", severity="critical"):
    return {
        "fingerprint": fingerprint,
        "labels": {"alertname": "NodeNotReady", "node": node, "severity": severity},
        "annotations": {"summary": summary},
        "status": {"state": "active"},
        "startsAt": "2026-06-08T16:14:00Z",
    }


class _StubSource(AlertmanagerAlertSource):
    """AlertmanagerAlertSource with _fetch_alerts driven by a scripted queue."""

    def __init__(self, ledger):
        super().__init__(url="http://am.test", escalation_ledger=ledger)
        self._cycles = []

    def feed(self, raw_alerts):
        self._cycles.append(raw_alerts)

    def _fetch_alerts(self):
        return self._cycles.pop(0) if self._cycles else []


# --- EscalationLedger ---------------------------------------------------------

def test_ledger_take_is_once():
    ledger = EscalationLedger()
    assert ledger.take("fp1") is False          # never marked
    ledger.mark("fp1")
    assert len(ledger) == 1
    assert ledger.take("fp1") is True            # present -> removed
    assert ledger.take("fp1") is False           # gone after first take
    assert len(ledger) == 0


def test_ledger_ignores_empty_fingerprint():
    ledger = EscalationLedger()
    ledger.mark("")
    assert len(ledger) == 0
    assert ledger.take("") is False


# --- Source resolution synthesis ---------------------------------------------

def test_resolution_emitted_only_for_escalated():
    ledger = EscalationLedger()
    src = _StubSource(ledger)

    # Cycle 1: two alerts fire.
    src.feed([_raw("escal", "Node raspberrypi4 not ready"), _raw("quiet", "Disk filling")])
    fired = src.poll()
    assert {a.fingerprint for a in fired} == {"escal", "quiet"}

    # The engine escalates only the first one.
    ledger.mark("escal")

    # Cycle 2: both alerts clear (empty active set).
    src.feed([])
    resolved = src.poll()

    assert len(resolved) == 1
    note = resolved[0]
    assert note.fingerprint == "escal:resolved"          # distinct -> dodges dedupe
    assert note.severity is AlertSeverity.INFO
    assert note.details.get("resolution") is True
    assert note.summary == "Resolved: Node raspberrypi4 not ready"
    assert note.details.get("host") == "raspberrypi4"


def test_resolution_fires_at_most_once():
    ledger = EscalationLedger()
    src = _StubSource(ledger)
    src.feed([_raw("escal")])
    src.poll()
    ledger.mark("escal")

    src.feed([])           # clears -> one resolution
    assert len(src.poll()) == 1
    src.feed([])           # still gone -> no repeat
    assert src.poll() == []


def test_recurring_alert_resolves_again_after_refiring():
    ledger = EscalationLedger()
    src = _StubSource(ledger)
    # fire -> escalate -> clear (1 resolution)
    src.feed([_raw("flap")]); src.poll(); ledger.mark("flap")
    src.feed([]); assert len(src.poll()) == 1
    # same fingerprint fires again -> escalate -> clear (resolution again)
    src.feed([_raw("flap")]); src.poll(); ledger.mark("flap")
    src.feed([]); assert len(src.poll()) == 1


def test_disabled_when_no_ledger():
    src = AlertmanagerAlertSource.__new__(AlertmanagerAlertSource)
    AlertmanagerAlertSource.__init__(src, url="http://am.test")  # ledger defaults to None
    src._cycles = [[_raw("escal")], []]
    src._fetch_alerts = lambda: src._cycles.pop(0) if src._cycles else []
    assert {a.fingerprint for a in src.poll()} == {"escal"}
    assert src.poll() == []          # no ledger -> no resolution synthesis


# --- Engine escalation marking ------------------------------------------------

def _engine(ledger):
    eng = EventRuntime.__new__(EventRuntime)
    eng._escalation_ledger = ledger
    return eng


def test_engine_marks_on_escalated_outcome():
    ledger = EscalationLedger()
    eng = _engine(ledger)
    alert = Alert.from_dict({
        "alert_id": "a", "summary": "x", "severity": "critical", "fingerprint": "escal",
    })
    eng._maybe_mark_escalation(alert, ActionResult(
        action="investigate", success=True, message="Escalated: ...",
        details={"outcome": "escalated"},
    ))
    assert ledger.take("escal") is True


def test_engine_does_not_mark_non_escalated():
    ledger = EscalationLedger()
    eng = _engine(ledger)
    alert = Alert.from_dict({
        "alert_id": "a", "summary": "x", "severity": "warning", "fingerprint": "quiet",
    })
    eng._maybe_mark_escalation(alert, ActionResult(
        action="investigate", success=True, message="ok",
        details={"outcome": "monitoring"},
    ))
    assert len(ledger) == 0


def test_engine_mark_is_noop_without_ledger_or_fingerprint():
    # no ledger wired
    _engine(None)._maybe_mark_escalation(
        Alert.from_dict({"alert_id": "a", "summary": "x", "severity": "critical", "fingerprint": "fp"}),
        ActionResult(action="investigate", success=True, message="", details={"outcome": "escalated"}),
    )
    # ledger present but alert has no fingerprint -> nothing to key on
    ledger = EscalationLedger()
    _engine(ledger)._maybe_mark_escalation(
        Alert.from_dict({"alert_id": "a", "summary": "x", "severity": "critical"}),
        ActionResult(action="investigate", success=True, message="", details={"outcome": "escalated"}),
    )
    assert len(ledger) == 0
