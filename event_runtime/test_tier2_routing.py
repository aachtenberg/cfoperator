"""Tier-2 severity->channel routing: real-time only for act-now classes."""
import os
from event_runtime.engine import _is_realtime_worthy, _digest_low_severity, EventRuntime
from event_runtime.models import Alert, ActionResult


# (severity, outcome, is_resolution) -> realtime?
def test_realtime_for_act_now_classes():
    assert _is_realtime_worthy("critical", "", False)
    assert _is_realtime_worthy("warning", "needs_action", False)
    assert _is_realtime_worthy("warning", "escalated", False)
    assert _is_realtime_worthy("warning", "failed", False)
    # a plain new warning finding (no quiet outcome) still pages
    assert _is_realtime_worthy("warning", "", False)


def test_digest_for_low_signal_classes():
    assert not _is_realtime_worthy("info", "", False)          # info insight
    assert not _is_realtime_worthy("warning", "resolved", False)   # recovered
    assert not _is_realtime_worthy("warning", "monitoring", False) # watching
    assert not _is_realtime_worthy("info", "", True)           # resolution ping
    assert not _is_realtime_worthy("warning", "", True)        # cleared-finding


def test_env_toggle_default_on():
    # default on unless explicitly disabled
    old = os.environ.pop("CFOP_DIGEST_LOW_SEVERITY", None)
    try:
        assert _digest_low_severity() is True
        os.environ["CFOP_DIGEST_LOW_SEVERITY"] = "0"
        assert _digest_low_severity() is False
        os.environ["CFOP_DIGEST_LOW_SEVERITY"] = "true"
        assert _digest_low_severity() is True
    finally:
        os.environ.pop("CFOP_DIGEST_LOW_SEVERITY", None)
        if old is not None:
            os.environ["CFOP_DIGEST_LOW_SEVERITY"] = old


class _Sink:
    name = "fake"
    def __init__(self): self.sent = []
    def notify(self, summary, *, severity="info", details=None):
        self.sent.append((severity, details)); return True


def _runtime(sink):
    eng = EventRuntime.__new__(EventRuntime)
    eng.plugins = type("P", (), {"notification_sinks": [sink]})()
    return eng


def _alert(sev, summary="x", resolution=False):
    d = {"resolution": True} if resolution else {}
    return Alert.from_dict({"alert_id": "a", "summary": summary, "severity": sev, "details": d})


def test_runtime_suppresses_resolved(monkeypatch=None):
    os.environ["CFOP_DIGEST_LOW_SEVERITY"] = "true"
    sink = _Sink()
    eng = _runtime(sink)
    res = ActionResult(action="investigate", success=True, message="Resolved: x",
                       details={"outcome": "resolved"})
    eng._notify_action_completed(_alert("warning"), res, None)
    assert sink.sent == []  # routed to digest


def test_runtime_pages_needs_action():
    os.environ["CFOP_DIGEST_LOW_SEVERITY"] = "true"
    sink = _Sink()
    eng = _runtime(sink)
    res = ActionResult(action="investigate", success=True, message="Action needed: x",
                       details={"outcome": "needs_action"})
    eng._notify_action_completed(_alert("warning"), res, None)
    assert len(sink.sent) == 1  # paged real-time


def test_runtime_old_behaviour_when_disabled():
    os.environ["CFOP_DIGEST_LOW_SEVERITY"] = "0"
    sink = _Sink()
    eng = _runtime(sink)
    res = ActionResult(action="investigate", success=True, message="Resolved: x",
                       details={"outcome": "resolved"})
    eng._notify_action_completed(_alert("info"), res, None)
    assert len(sink.sent) == 1  # gate off -> sends everything
    os.environ.pop("CFOP_DIGEST_LOW_SEVERITY", None)
