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
from event_runtime.http_actions import (
    COMPLETION_AUTH_HEADER,
    COMPLETION_SECRET_ENV,
    HTTPInvestigateActionHandler,
    build_http_investigate_handler,
    log_completion_endpoint_status,
    parse_completion_payload,
    verify_completion_auth,
)
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


def test_action_result_from_dict_rejects_non_object_payload():
    with pytest.raises(ValueError, match="JSON object"):
        ActionResult.from_dict([])  # type: ignore[arg-type]


def test_action_result_from_dict_rejects_string_success():
    """`bool('false') is True` — must reject so wire callers can't invert outcomes."""
    with pytest.raises(ValueError, match="success"):
        ActionResult.from_dict(
            {"action": "investigate", "success": "false", "message": "x"}
        )


def test_action_result_from_dict_requires_success_present():
    with pytest.raises(ValueError, match="success"):
        ActionResult.from_dict({"action": "investigate", "message": "x"})


def test_action_result_from_dict_rejects_non_string_action():
    with pytest.raises(ValueError, match="action"):
        ActionResult.from_dict({"action": 7, "success": True, "message": "x"})


def test_action_result_from_dict_rejects_non_dict_details():
    with pytest.raises(ValueError, match="details"):
        ActionResult.from_dict(
            {"action": "investigate", "success": True, "message": "x", "details": []}
        )


def test_action_result_from_dict_rejects_non_bool_quiet():
    with pytest.raises(ValueError, match="quiet"):
        ActionResult.from_dict(
            {"action": "investigate", "success": True, "message": "x", "quiet": "true"}
        )


def test_action_result_from_dict_accepts_zulu_timestamp():
    rebuilt = ActionResult.from_dict(
        {"action": "investigate", "success": True, "message": "x", "executed_at": "2026-05-26T12:00:00Z"}
    )
    assert rebuilt.executed_at.tzinfo is not None
    assert rebuilt.executed_at.utcoffset().total_seconds() == 0


def test_action_result_from_dict_normalizes_naive_timestamp_to_utc():
    rebuilt = ActionResult.from_dict(
        {"action": "investigate", "success": True, "message": "x", "executed_at": "2026-05-26T12:00:00"}
    )
    assert rebuilt.executed_at.tzinfo is not None
    assert rebuilt.executed_at.utcoffset().total_seconds() == 0


def test_action_result_from_dict_raises_on_garbage_timestamp():
    with pytest.raises(ValueError, match="executed_at"):
        ActionResult.from_dict(
            {"action": "investigate", "success": True, "message": "x", "executed_at": "not-a-date"}
        )


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


def _post(server, path: str, payload, *, extra_headers: dict | None = None):
    conn = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    conn.request("POST", path, body=body, headers=headers)
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


def test_completion_endpoint_400_when_alert_field_not_dict(tmp_path):
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        status, body = _post(
            server,
            "/v1/investigations/aid/complete",
            {"alert": "not-a-dict", "result": {"action": "investigate", "success": True, "message": "x"}},
        )
        assert status == 400
        assert "alert" in body["error"].lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_completion_endpoint_400_when_result_field_not_dict(tmp_path):
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        status, body = _post(
            server,
            "/v1/investigations/aid/complete",
            {"alert": _alert("aid").to_dict(), "result": "not-a-dict"},
        )
        assert status == 400
        assert "result" in body["error"].lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


# ---- parse_completion_payload (used by both transports) ------------------


def test_parse_completion_payload_happy_path():
    alert_dict = _alert("aid").to_dict()
    result_dict = {"action": "investigate", "success": True, "message": "ok"}
    alert, result = parse_completion_payload({"alert": alert_dict, "result": result_dict}, "aid")
    assert alert.alert_id == "aid"
    assert result.action == "investigate"


@pytest.mark.parametrize("bad_body,expected_msg", [
    ([], "JSON object"),
    ({}, "alert"),
    ({"alert": {}}, "result"),
    ({"alert": "x", "result": {}}, "alert"),
    ({"alert": {}, "result": "x"}, "result"),
])
def test_parse_completion_payload_rejects_bad_shapes(bad_body, expected_msg):
    with pytest.raises(ValueError, match=expected_msg):
        parse_completion_payload(bad_body, "aid")


def test_parse_completion_payload_rejects_alert_id_mismatch():
    alert_dict = _alert("real").to_dict()
    with pytest.raises(ValueError, match="alert_id"):
        parse_completion_payload(
            {"alert": alert_dict, "result": {"action": "investigate", "success": True, "message": "x"}},
            "different",
        )


# ---- verify_completion_auth ----------------------------------------------


def test_verify_completion_auth_allows_when_secret_unset(monkeypatch):
    monkeypatch.delenv(COMPLETION_SECRET_ENV, raising=False)
    assert verify_completion_auth(None) is None
    assert verify_completion_auth("anything") is None


def test_verify_completion_auth_rejects_missing_header_when_secret_set(monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "supersecret")
    err = verify_completion_auth(None)
    assert err is not None and "Missing" in err


def test_verify_completion_auth_rejects_wrong_header(monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "supersecret")
    err = verify_completion_auth("wrong")
    assert err is not None and "Invalid" in err


def test_verify_completion_auth_accepts_matching_header(monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "supersecret")
    assert verify_completion_auth("supersecret") is None


# ---- log_completion_endpoint_status --------------------------------------


def test_log_completion_endpoint_status_warns_when_secret_unset(monkeypatch, caplog):
    """Operator-visible warning fires when the completion endpoint is open."""
    monkeypatch.delenv(COMPLETION_SECRET_ENV, raising=False)
    with caplog.at_level("WARNING", logger="event_runtime.http_actions"):
        log_completion_endpoint_status()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert COMPLETION_SECRET_ENV in warnings[0].getMessage()
    assert "unauthenticated" in warnings[0].getMessage()


def test_log_completion_endpoint_status_info_when_secret_set(monkeypatch, caplog):
    """When auth is enabled, an INFO line confirms it instead of warning."""
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    with caplog.at_level("INFO", logger="event_runtime.http_actions"):
        log_completion_endpoint_status()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert warnings == []
    assert len(infos) == 1
    assert "X-CFOP-Token" in infos[0].getMessage()


def test_bootstrap_logs_completion_endpoint_status_after_logging_configured(
    monkeypatch, tmp_path, caplog
):
    """The warning must fire from build_portable_runtime, not at module import.

    Module-level logging in http_actions runs before __main__ calls
    logging.basicConfig and gets swallowed. Tying the log to bootstrap
    guarantees operators see it.
    """
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv(COMPLETION_SECRET_ENV, raising=False)
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)

    with caplog.at_level("WARNING", logger="event_runtime.http_actions"):
        build_portable_runtime()
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and COMPLETION_SECRET_ENV in r.getMessage()
    ]
    assert len(warnings) == 1


# ---- completion endpoint auth integration --------------------------------


def test_completion_endpoint_401_without_token_when_secret_set(tmp_path, monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert("aid").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "x"},
        }
        status, _ = _post(server, "/v1/investigations/aid/complete", payload)
        assert status == 401
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert sink.calls == []


def test_completion_endpoint_401_with_wrong_token_when_secret_set(tmp_path, monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert("aid").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "x"},
        }
        status, _ = _post(
            server,
            "/v1/investigations/aid/complete",
            payload,
            extra_headers={COMPLETION_AUTH_HEADER: "wrong"},
        )
        assert status == 401
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert sink.calls == []


def test_completion_endpoint_200_with_matching_token(tmp_path, monkeypatch):
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert("aid").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "x"},
        }
        status, _ = _post(
            server,
            "/v1/investigations/aid/complete",
            payload,
            extra_headers={COMPLETION_AUTH_HEADER: "abc"},
        )
        assert status == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert len(sink.calls) == 1


def test_completion_endpoint_allows_when_secret_unset(tmp_path, monkeypatch):
    """Portable deployments without an agent must remain runnable."""
    monkeypatch.delenv(COMPLETION_SECRET_ENV, raising=False)
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        payload = {
            "alert": _alert("aid").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "x"},
        }
        status, _ = _post(server, "/v1/investigations/aid/complete", payload)
        assert status == 200
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

    # alert / result must be dicts — parity with the stdlib server.
    not_a_dict = client.post(
        "/v1/investigations/fapi-1/complete",
        json={"alert": "x", "result": {"action": "investigate", "success": True, "message": "x"}},
    )
    assert not_a_dict.status_code == 400


def test_fastapi_completion_endpoint_enforces_auth(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    from fastapi.testclient import TestClient
    from event_runtime.fastapi_app import create_app

    runtime, sink = _runtime(tmp_path)
    app = create_app(runtime=runtime, worker=None)
    client = TestClient(app)
    alert = _alert("fapi-a")
    payload = {
        "alert": alert.to_dict(),
        "result": {"action": "investigate", "success": True, "message": "done"},
    }

    no_token = client.post("/v1/investigations/fapi-a/complete", json=payload)
    assert no_token.status_code == 401

    wrong = client.post(
        "/v1/investigations/fapi-a/complete",
        json=payload,
        headers={COMPLETION_AUTH_HEADER: "nope"},
    )
    assert wrong.status_code == 401

    right = client.post(
        "/v1/investigations/fapi-a/complete",
        json=payload,
        headers={COMPLETION_AUTH_HEADER: "abc"},
    )
    assert right.status_code == 200
    assert len(sink.calls) == 1


# ---- completion_requests metric ------------------------------------------


def _counter_value(counter, **labels) -> float:
    """Read a labeled Counter value via the public collect() API."""
    for metric in counter.collect():
        for sample in metric.samples:
            if not sample.name.endswith("_total"):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


def test_completion_endpoint_increments_recorded_metric(tmp_path):
    from event_runtime.telemetry import COMPLETION_REQUESTS
    runtime, sink = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        before = _counter_value(COMPLETION_REQUESTS, outcome="recorded")
        payload = {
            "alert": _alert("mp1").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "ok"},
        }
        status, _ = _post(server, "/v1/investigations/mp1/complete", payload)
        assert status == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert _counter_value(COMPLETION_REQUESTS, outcome="recorded") == before + 1


def test_completion_endpoint_increments_auth_missing_metric(tmp_path, monkeypatch):
    from event_runtime.telemetry import COMPLETION_REQUESTS
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        before = _counter_value(COMPLETION_REQUESTS, outcome="auth_missing")
        payload = {
            "alert": _alert("mp2").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "ok"},
        }
        status, _ = _post(server, "/v1/investigations/mp2/complete", payload)
        assert status == 401
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert _counter_value(COMPLETION_REQUESTS, outcome="auth_missing") == before + 1


def test_completion_endpoint_increments_auth_invalid_metric(tmp_path, monkeypatch):
    from event_runtime.telemetry import COMPLETION_REQUESTS
    monkeypatch.setenv(COMPLETION_SECRET_ENV, "abc")
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        before = _counter_value(COMPLETION_REQUESTS, outcome="auth_invalid")
        payload = {
            "alert": _alert("mp3").to_dict(),
            "result": {"action": "investigate", "success": True, "message": "ok"},
        }
        status, _ = _post(
            server,
            "/v1/investigations/mp3/complete",
            payload,
            extra_headers={COMPLETION_AUTH_HEADER: "wrong"},
        )
        assert status == 401
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert _counter_value(COMPLETION_REQUESTS, outcome="auth_invalid") == before + 1


def test_completion_endpoint_increments_bad_request_metric(tmp_path):
    from event_runtime.telemetry import COMPLETION_REQUESTS
    runtime, _ = _runtime(tmp_path)
    server, thread = _serve(runtime)
    try:
        before = _counter_value(COMPLETION_REQUESTS, outcome="bad_request")
        # Malformed JSON triggers the JSONDecodeError branch.
        status, _ = _post(server, "/v1/investigations/mp4/complete", "{not json")
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert _counter_value(COMPLETION_REQUESTS, outcome="bad_request") == before + 1
