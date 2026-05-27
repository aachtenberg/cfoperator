"""Tests for HTTPTriageDecisionEngine and bootstrap wiring.

The engine POSTs each alert to the agent's /v1/triage. These tests mock
urllib.request.urlopen so we can pin behavior across the happy path, the
"agent unreachable" path, the cache path, and the bad-response paths.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

from event_runtime.bootstrap import build_portable_runtime
from event_runtime.defaults import OpenReasoningDecisionEngine
from event_runtime.http_actions import (
    HTTPTriageDecisionEngine,
    build_http_triage_engine,
)
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope


def _alert(alert_id: str = "aid-1", *, severity: AlertSeverity = AlertSeverity.WARNING) -> Alert:
    return Alert(
        source="alertmanager",
        severity=severity,
        summary="Pod foo not ready for 30m",
        details={"alertname": "PodNotReady"},
        alert_id=alert_id,
    )


def _envelope(alert: Alert) -> ContextEnvelope:
    return ContextEnvelope(alert=alert)


def _stub_response(payload: dict, status: int = 200):
    """Build a context-manager response object that urlopen returns."""
    class _Resp:
        def __init__(self, body, code):
            self._body = body
            self.status = code
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp(json.dumps(payload).encode("utf-8"), status)


# ---- happy path -----------------------------------------------------------


def test_decide_returns_action_from_agent_response():
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": "notify",
        "reason": "kubelet self-heals these",
        "confidence": 0.85,
    })):
        decision = engine.decide(_envelope(_alert()))
    assert decision.action == "notify"
    assert decision.confidence == 0.85
    assert "kubelet self-heals" in decision.reasoning


@pytest.mark.parametrize("action", ["log_only", "notify", "investigate", "escalate"])
def test_each_valid_action_propagates(action):
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": action, "reason": "ok", "confidence": 0.7,
    })):
        decision = engine.decide(_envelope(_alert(alert_id=f"a-{action}")))
    assert decision.action == action


# ---- safe defaults --------------------------------------------------------


def test_falls_back_to_investigate_when_agent_unreachable():
    """Agent down or network broken: do NOT lose alerts — investigate them."""
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        decision = engine.decide(_envelope(_alert()))
    assert decision.action == "invalid_or_failed_default_to_investigate".replace("_or_failed_default_to_", "_") or decision.action == "investigate"  # noqa: E501
    assert decision.action == "investigate"
    assert decision.confidence == 0.0


def test_falls_back_to_investigate_on_5xx():
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    err = urllib.error.HTTPError("http://agent:8083/v1/triage", 503, "Unavailable", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        decision = engine.decide(_envelope(_alert()))
    assert decision.action == "investigate"


def test_falls_back_to_investigate_on_bad_json():
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")

    class _BadResp:
        status = 200
        def read(self):
            return b"not json"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_BadResp()):
        decision = engine.decide(_envelope(_alert()))
    assert decision.action == "investigate"


def test_falls_back_to_investigate_on_invalid_action_label():
    """LLM returned a non-rubric action ('ponder'). Engine must still pick a valid one."""
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": "ponder", "reason": "vibes", "confidence": 0.9,
    })):
        decision = engine.decide(_envelope(_alert()))
    assert decision.action == "investigate"


# ---- cache behavior -------------------------------------------------------


def test_repeat_alert_within_ttl_hits_cache():
    """event_runtime polls Alertmanager every 30s; the same alert appears
    many times before it resolves. Triage should LLM-call once, cache the
    result, and skip the round-trip on subsequent identical alerts."""
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083", cache_ttl_seconds=300)
    alert = _alert()
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": "notify", "reason": "k", "confidence": 0.9,
    })) as mock_urlopen:
        engine.decide(_envelope(alert))
        engine.decide(_envelope(alert))
        engine.decide(_envelope(alert))
    assert mock_urlopen.call_count == 1, "subsequent calls should hit the cache"


def test_cache_does_not_serve_failed_decisions():
    """If a triage call fails (confidence=0), the next attempt should retry
    rather than serving the failed decision from cache for 5 minutes."""
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083", cache_ttl_seconds=300)
    alert = _alert()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        engine.decide(_envelope(alert))
    # Now agent comes back up
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": "notify", "reason": "ok", "confidence": 0.8,
    })) as mock_urlopen:
        decision = engine.decide(_envelope(alert))
    assert mock_urlopen.call_count == 1
    assert decision.action == "notify"


def test_different_alerts_each_call_agent_separately():
    engine = HTTPTriageDecisionEngine(agent_url="http://agent:8083")
    with patch("urllib.request.urlopen", return_value=_stub_response({
        "action": "investigate", "reason": "ok", "confidence": 0.7,
    })) as mock_urlopen:
        engine.decide(_envelope(_alert(alert_id="a")))
        engine.decide(_envelope(_alert(alert_id="b")))
        # Different summary → different fingerprint
        other = Alert(source="x", severity=AlertSeverity.WARNING,
                      summary="totally different",
                      alert_id="c")
        engine.decide(_envelope(other))
    assert mock_urlopen.call_count == 2  # a/b same fingerprint (same labels), c different


# ---- construction validation ----------------------------------------------


def test_constructor_rejects_empty_url():
    with pytest.raises(ValueError):
        HTTPTriageDecisionEngine(agent_url="")


def test_constructor_strips_trailing_slash():
    e = HTTPTriageDecisionEngine(agent_url="http://agent:8083/")
    assert e.agent_url == "http://agent:8083"


def test_build_factory_none_when_url_missing():
    assert build_http_triage_engine(None) is None
    assert build_http_triage_engine("") is None


def test_build_factory_constructs_when_url_set():
    e = build_http_triage_engine("http://agent:8083")
    assert e is not None
    assert e.agent_url == "http://agent:8083"


# ---- bootstrap wiring -----------------------------------------------------


def test_bootstrap_uses_triage_engine_when_agent_url_set(monkeypatch, tmp_path):
    """The whole point: bootstrap should register the triage engine, not
    the OpenReasoningDecisionEngine default, when an agent URL is set."""
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CFOP_AGENT_URL", "http://agent:8083")
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    runtime = build_portable_runtime()
    engine = runtime.plugins.decision_engine
    assert isinstance(engine, HTTPTriageDecisionEngine)
    assert engine.agent_url == "http://agent:8083"


def test_bootstrap_keeps_open_reasoning_when_no_agent_url(monkeypatch, tmp_path):
    """Portable deployments without an agent must still get a working engine."""
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    runtime = build_portable_runtime()
    assert isinstance(runtime.plugins.decision_engine, OpenReasoningDecisionEngine)


# ---- notify Slack format (single-line collapse) ---------------------------


def test_format_message_collapses_notify_to_single_line():
    from event_runtime.notifications import _format_message
    msg = _format_message(
        "Action completed: notify",
        severity="warning",
        details={
            "action": "notify",
            "alert_summary": "Pod monitoring/promtail-7n5mz not ready for 30m",
            "result_message": "Notification requested for: Pod monitoring/promtail-7n5mz",
        },
    )
    # Single line, matches the user-preferred format: [severity] alert_summary
    assert msg == "[warning] Pod monitoring/promtail-7n5mz not ready for 30m"
    assert "\n" not in msg


def test_format_message_keeps_long_form_for_investigate():
    """The collapsing only applies to action=notify — investigate keeps
    its current 'Action completed / Alert / Action / Result' shape."""
    from event_runtime.notifications import _format_message
    msg = _format_message(
        "Action completed: investigate",
        severity="warning",
        details={
            "action": "investigate",
            "alert_summary": "Pod X not ready",
            "result_message": "Resolved: pod restarted cleanly (12s, 4 tool calls)",
        },
    )
    assert "Alert: Pod X not ready" in msg
    assert "Result: Resolved" in msg
    assert "\n" in msg
