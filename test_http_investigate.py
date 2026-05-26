"""Tests for the event_runtime HTTP investigate plumbing.

Covers PR B's surface: ActionResult.quiet, should_notify(quiet=...),
investigation_url surfacing in Slack details, HTTPInvestigateActionHandler,
bootstrap wiring, the POST /v1/investigations/{alert_id}/complete endpoint
in both the stdlib server and the FastAPI adapter, and
EventRuntime.record_external_action_completion.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from event_runtime.bootstrap import build_portable_runtime
from event_runtime.defaults import InvestigateActionHandler
from event_runtime.engine import EventRuntime
from event_runtime.http_actions import HTTPInvestigateActionHandler, build_http_investigate_handler
from event_runtime.models import ActionRequest, ActionResult, Alert, AlertSeverity, ContextEnvelope, Decision
from event_runtime.notifications import _format_message, should_notify
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, DecisionEngine
from event_runtime.server import _match_completion_path, make_handler
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink


# ---- shared helpers -------------------------------------------------------


class _TrackingSink:
    name = "tracking-notification"

    def __init__(self):
        self.calls: list[dict] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def notify(self, summary: str, *, severity: str = "info", details=None) -> bool:
        self.calls.append({"summary": summary, "severity": severity, "details": details})
        return True


class _NoopDecision(DecisionEngine):
    name = "noop-decision"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(action="investigate", confidence=1.0, reasoning="test")


def _alert(alert_id: str = "aid-1", *, severity: AlertSeverity = AlertSeverity.WARNING) -> Alert:
    return Alert(
        source="alertmanager",
        severity=severity,
        summary="Pod foo not ready for 30m",
        details={"alertname": "PodNotReady"},
        alert_id=alert_id,
    )


def _runtime(tmp_path: Path, *, sinks: list | None = None) -> tuple[EventRuntime, _TrackingSink]:
    state = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(state)
    plugins.register_decision_engine(_NoopDecision())
    sink = _TrackingSink()
    for extra in (sinks or [sink]):
        plugins.register_notification_sink(extra)
    return EventRuntime(plugins), sink


# ---- ActionResult.quiet ---------------------------------------------------


def test_action_result_quiet_defaults_to_false():
    result = ActionResult(action="investigate", success=True, message="ok")
    assert result.quiet is False


def test_action_result_quiet_round_trips_through_to_dict():
    result = ActionResult(action="investigate", success=True, message="dispatched", quiet=True)
    payload = result.to_dict()
    assert payload["quiet"] is True
    rebuilt = ActionResult.from_dict(payload)
    assert rebuilt.quiet is True
    assert rebuilt.action == "investigate"
    assert rebuilt.message == "dispatched"


def test_action_result_from_dict_tolerates_missing_quiet():
    rebuilt = ActionResult.from_dict({"action": "investigate", "success": True, "message": "x"})
    assert rebuilt.quiet is False


# ---- should_notify --------------------------------------------------------


def test_should_notify_returns_false_when_quiet_true_even_for_investigate():
    assert should_notify("investigate", True, quiet=True) is False


def test_should_notify_still_skips_log_only_actions():
    assert should_notify("log_only", True) is False


def test_should_notify_allows_investigate_by_default():
    assert should_notify("investigate", True) is True


# ---- _format_message: investigation_url surfacing -------------------------


def test_format_message_surfaces_investigation_url_and_id():
    text = _format_message(
        "Action completed: investigate",
        severity="warning",
        details={
            "alert_summary": "Pod foo not ready",
            "action": "investigate",
            "result_message": "Resolved",
            "result_details": {"investigation_url": "http://cf/123", "investigation_id": 123},
        },
    )
    assert "investigation_url: http://cf/123" in text
    assert "investigation_id: 123" in text


# ---- HTTPInvestigateActionHandler ----------------------------------------


def _action_request(alert: Alert) -> ActionRequest:
    envelope = ContextEnvelope(alert=alert)
    decision = Decision(action="investigate", confidence=1.0, reasoning="test")
    return ActionRequest(alert=alert, decision=decision, context=envelope)


def test_http_handler_requires_agent_url():
    with pytest.raises(ValueError):
        HTTPInvestigateActionHandler(agent_url="")


def test_http_handler_strips_trailing_slash():
    h = HTTPInvestigateActionHandler(agent_url="http://agent:8083/")
    assert h.agent_url == "http://agent:8083"


def test_http_handler_returns_quiet_success_after_posting_to_agent():
    h = HTTPInvestigateActionHandler(agent_url="http://agent:8083")
    captured = {}

    class _Resp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["method"] = req.get_method()
        captured["content_type"] = req.headers.get("Content-type")
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = h.execute(_action_request(_alert(alert_id="abc")))

    assert captured["url"] == "http://agent:8083/v1/investigate"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"]["alert_id"] == "abc"
    assert captured["body"]["summary"] == "Pod foo not ready for 30m"
    assert result.success is True
    assert result.quiet is True
    assert result.details["alert_id"] == "abc"


def test_http_handler_4xx_returns_failure_no_retry():
    h = HTTPInvestigateActionHandler(agent_url="http://agent:8083")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad payload", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = h.execute(_action_request(_alert()))

    assert result.success is False
    assert result.details["http_status"] == 400
    assert result.quiet is False  # operator should see the rejection


def test_http_handler_5xx_raises_so_worker_retries():
    h = HTTPInvestigateActionHandler(agent_url="http://agent:8083")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.HTTPError):
            h.execute(_action_request(_alert()))


def test_http_handler_connection_error_raises_so_worker_retries():
    h = HTTPInvestigateActionHandler(agent_url="http://agent:8083")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.URLError):
            h.execute(_action_request(_alert()))


# ---- build_http_investigate_handler / bootstrap wiring -------------------


def test_build_http_investigate_handler_none_when_url_empty():
    assert build_http_investigate_handler(None) is None
    assert build_http_investigate_handler("") is None


def test_build_http_investigate_handler_constructs_when_url_set():
    h = build_http_investigate_handler("http://agent:8083")
    assert h is not None
    assert h.agent_url == "http://agent:8083"


def test_bootstrap_registers_http_handler_when_agent_url_set(monkeypatch, tmp_path):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CFOP_AGENT_URL", "http://agent:8083")
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)

    runtime = build_portable_runtime()
    handler = runtime.plugins.action_handlers["investigate"]
    assert isinstance(handler, HTTPInvestigateActionHandler)
    assert handler.agent_url == "http://agent:8083"


def test_bootstrap_keeps_default_stub_when_agent_url_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)

    runtime = build_portable_runtime()
    assert isinstance(runtime.plugins.action_handlers["investigate"], InvestigateActionHandler)


# ---- EventRuntime.record_external_action_completion ----------------------


def test_record_external_action_completion_records_event_and_notifies(tmp_path):
    runtime, sink = _runtime(tmp_path)
    alert = _alert(alert_id="ext-1")
    result = ActionResult(action="investigate", success=True, message="Resolved: foo")

    runtime.record_external_action_completion(alert, result)

    events = runtime.plugins.state_sink.recent(limit=10)
    completions = [e for e in events if e["event_type"] == "action_completed"]
    assert len(completions) == 1
    assert completions[0]["payload"]["source"] == "external"
    assert completions[0]["payload"]["alert"]["alert_id"] == "ext-1"
    assert sink.calls == [
        {
            "summary": "Action completed: investigate",
            "severity": "warning",
            "details": {
                "alert_summary": alert.summary,
                "action": "investigate",
                "result_message": "Resolved: foo",
                "result_details": {},
            },
        }
    ]


def test_record_external_action_completion_respects_quiet(tmp_path):
    runtime, sink = _runtime(tmp_path)
    quiet_result = ActionResult(
        action="investigate",
        success=True,
        message="dispatched",
        quiet=True,
    )

    runtime.record_external_action_completion(_alert(), quiet_result)

    assert sink.calls == []
    events = runtime.plugins.state_sink.recent(limit=10)
    # Event is still recorded so the activity feed reflects it.
    assert any(e["event_type"] == "action_completed" for e in events)


# ---- _match_completion_path ----------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/v1/investigations/abc-123/complete", "abc-123"),
    ("/v1/investigations/uuid-with-dashes-and-numbers-42/complete", "uuid-with-dashes-and-numbers-42"),
    ("/v1/investigations//complete", None),
    ("/v1/investigations/abc/complete/extra", None),
    ("/v1/investigations/abc/done", None),
    ("/v1/other/abc/complete", None),
    ("/v1/investigations/path/with/slashes/complete", None),
    ("/alert", None),
])
def test_match_completion_path(path, expected):
    assert _match_completion_path(path) == expected


# ---- POST /v1/investigations/{alert_id}/complete (stdlib server) ---------


def _serve(runtime: EventRuntime):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post(server, path: str, payload):
    conn = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


def test_completion_endpoint_records_and_notifies(tmp_path):
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        alert = _alert(alert_id="end-to-end")
        payload = {
            "alert": alert.to_dict(),
            "result": {
                "action": "investigate",
                "success": True,
                "message": "Resolved: foo (3.2s, 1 tool call)",
                "details": {"investigation_id": 42, "investigation_url": "http://cf/inv/42"},
                "executed_at": "2026-05-26T12:00:00+00:00",
            },
        }
        status, body = _post(server, "/v1/investigations/end-to-end/complete", payload)
        assert status == 200
        assert body == {"status": "recorded", "alert_id": "end-to-end"}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["details"]["alert_summary"] == alert.summary
    assert call["details"]["result_details"]["investigation_url"] == "http://cf/inv/42"


def test_completion_endpoint_400_when_alert_id_mismatch(tmp_path):
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert(alert_id="real-id").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "x"},
        }
        status, body = _post(server, "/v1/investigations/different-id/complete", payload)
        assert status == 400
        assert "alert_id" in body["error"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert sink.calls == []


def test_completion_endpoint_400_when_missing_fields(tmp_path):
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        # Missing 'result'
        status, body = _post(server, "/v1/investigations/aid/complete", {"alert": _alert("aid").to_dict()})
        assert status == 400
        # Not even a dict
        status, body = _post(server, "/v1/investigations/aid/complete", [])
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_completion_endpoint_400_on_invalid_json(tmp_path):
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        status, body = _post(server, "/v1/investigations/aid/complete", "{not json")
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_completion_endpoint_quiet_result_records_but_skips_notification(tmp_path):
    """Agent could in theory post quiet=True; verify it's honored."""
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert("q").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "interim", "quiet": True},
        }
        status, _ = _post(server, "/v1/investigations/q/complete", payload)
        assert status == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert sink.calls == []
    events = runtime.plugins.state_sink.recent(limit=10)
    assert any(e["event_type"] == "action_completed" for e in events)


# ---- FastAPI adapter parity ----------------------------------------------


def test_fastapi_completion_endpoint_parity(tmp_path, monkeypatch):
    """The FastAPI adapter must expose the same endpoint as the stdlib server."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from event_runtime.fastapi_app import create_app

    runtime, sink = _runtime(tmp_path)
    app = create_app(runtime=runtime, worker=None)
    client = TestClient(app)

    alert = _alert("fapi-1")
    payload = {
        "alert": alert.to_dict(),
        "result": {"action": "investigate", "success": True, "message": "done"},
    }
    resp = client.post("/v1/investigations/fapi-1/complete", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"
    assert len(sink.calls) == 1

    bad = client.post("/v1/investigations/other/complete", json=payload)
    assert bad.status_code == 400

    missing = client.post("/v1/investigations/fapi-1/complete", json={"alert": alert.to_dict()})
    assert missing.status_code == 400
