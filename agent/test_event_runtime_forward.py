"""The agent's sweep forwards against the runtime's real bearer gate (CFOP-214).

For 34 days every sweep finding and resolution was refused with a 401: the
runtime's gate was switched on, and the agent's /alert callers had never sent
the header. Unit tests that patch ``urlopen`` could not have seen it, because
nothing on the other end checked anything. So these run the runtime's own
request handler (``event_runtime.server.make_handler``, what production
serves) with ``CFOP_RUNTIME_TOKEN`` set, and let the agent talk to it over a
socket.
"""
from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from agent.agent import CFOperator, SWEEP_FORWARD
import event_runtime.http_actions as http_actions
from event_runtime.server import make_handler


class _Runtime:
    """The slice of EventRuntime the /alert handler calls."""

    def __init__(self):
        self.alerts = []

    def handle_alert(self, alert):
        self.alerts.append(alert)
        return {"alert_id": alert.alert_id, "status": "completed"}


class _Notifier:
    channel_type = "slack"

    def __init__(self):
        self.sent = []

    def send(self, message, severity=None):
        self.sent.append(message)


@pytest.fixture
def runtime(monkeypatch):
    """A runtime on a free port with its bearer gate on, and the agent pointed at it."""
    monkeypatch.setenv("CFOP_RUNTIME_TOKEN", "shared-runtime-token")
    rt = _Runtime()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rt))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield rt
    finally:
        server.shutdown()
        server.server_close()


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.kb = SimpleNamespace(_kb=SimpleNamespace(
        get_dismissed_finding_keys=lambda days=30: set(),
        record_notification_history=lambda **kw: None))
    op.notifications = []
    return op


def _count(kind, outcome):
    return SWEEP_FORWARD.labels(kind=kind, outcome=outcome)._value.get()


FINDINGS = [
    {"finding": "vaultwarden restarted 3 times", "severity": "warning", "namespace": "apps"},
    {"finding": "disk 91% on raspberrypi3", "severity": "critical"},
]


def test_findings_pass_the_runtime_gate(runtime):
    before = _count("finding", "ok")
    assert _operator()._post_findings_to_event_runtime(FINDINGS) is True
    assert [a.summary for a in runtime.alerts] == [f["finding"] for f in FINDINGS]
    assert all(a.source == "cfoperator-sweep" for a in runtime.alerts)
    assert _count("finding", "ok") - before == 2


def test_resolutions_pass_the_runtime_gate(runtime):
    assert _operator()._post_resolutions_to_event_runtime(FINDINGS[:1]) is True
    assert len(runtime.alerts) == 1
    assert runtime.alerts[0].details["resolution"] is True


def test_a_refused_forward_is_reported_and_stops_the_batch(runtime, monkeypatch, caplog):
    # The runtime holds a token the agent was never given — the production
    # shape of the bug. Patched on the server side only; the agent's client
    # reads its own (absent) env.
    monkeypatch.delenv("CFOP_RUNTIME_TOKEN")
    monkeypatch.setattr(http_actions, "_expected_runtime_token", lambda: "runtime-only-token")
    before = _count("finding", "unauthorized")
    with caplog.at_level("WARNING"):
        assert _operator()._post_findings_to_event_runtime(FINDINGS) is False
    assert runtime.alerts == []
    # One refusal, then stop: the second finding would be refused the same way.
    assert _count("finding", "unauthorized") - before == 1
    assert "CFOP_RUNTIME_TOKEN" in caplog.text


def test_a_refused_forward_falls_back_to_the_agents_own_rollup(runtime, monkeypatch):
    """The other half of the silence: with a runtime configured, the agent
    used to skip its own roll-up unconditionally, so a refused forward meant
    no notification at all."""
    monkeypatch.delenv("CFOP_RUNTIME_TOKEN")
    monkeypatch.setattr(http_actions, "_expected_runtime_token", lambda: "runtime-only-token")
    op = _operator()
    notifier = _Notifier()
    op.notifications = [notifier]

    forwarded = op._post_findings_to_event_runtime(FINDINGS)
    op._notify_sweep_findings({"summary": "2 findings", "severity": "critical",
                               "findings": FINDINGS}, forwarded=forwarded)
    assert notifier.sent == ["2 findings"]


def test_an_accepted_forward_leaves_notification_to_the_runtime(runtime):
    op = _operator()
    notifier = _Notifier()
    op.notifications = [notifier]
    recorded = []
    op.kb._kb.record_notification_history = lambda **kw: recorded.append(kw)

    forwarded = op._post_findings_to_event_runtime(FINDINGS)
    op._notify_sweep_findings({"summary": "2 findings", "severity": "critical",
                               "findings": FINDINGS}, forwarded=forwarded)
    assert notifier.sent == []
    assert recorded and recorded[0]["channel_type"] == "event-runtime"


def test_no_runtime_configured_is_not_a_failure(monkeypatch):
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_URL", raising=False)
    assert _operator()._post_findings_to_event_runtime(FINDINGS) is None
    assert _operator()._post_resolutions_to_event_runtime(FINDINGS) is None


def test_a_refused_completion_names_the_completion_secret(runtime, monkeypatch, caplog):
    """The post-back is exempt from the bearer gate and checked against
    CFOP_COMPLETION_SHARED_SECRET instead, so its refusal must say that — a
    message blaming CFOP_RUNTIME_TOKEN sends the operator after a variable that
    is fine (review of #283)."""
    monkeypatch.delenv("CFOP_COMPLETION_SHARED_SECRET", raising=False)
    monkeypatch.setattr(http_actions, "_expected_completion_secret", lambda: "runtime-only-secret")
    with caplog.at_level("WARNING"):
        _operator()._post_action_result_to_event_runtime(
            {"alert_id": "abc-123", "summary": "x", "severity": "warning", "source": "test"},
            {"action": "investigate", "success": True, "message": "ok"})
    assert "CFOP_COMPLETION_SHARED_SECRET" in caplog.text
    assert "CFOP_RUNTIME_TOKEN" not in caplog.text
