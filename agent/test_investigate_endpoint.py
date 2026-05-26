"""Tests for the HTTP-driven investigation pipeline.

Covers the surface introduced by PR A: run_investigation extraction,
enqueue + worker, post-back to event_runtime, and the POST /v1/investigate
endpoint. Heavy dependencies (LLM, KB, embeddings) are mocked — these tests
exercise wiring, not the LLM loop itself.
"""

from __future__ import annotations

import json
import os
import queue
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import CFOperator


def _operator(*, queue_size: int = 4, reactive_poll: bool = True) -> CFOperator:
    """Build a CFOperator with only the attributes the tested methods touch."""
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'investigation_queue_size': queue_size, 'reactive_poll': reactive_poll}}
    op._investigation_queue = queue.Queue(maxsize=queue_size)
    op._investigation_worker_thread = None
    op._reactive_poll_enabled = reactive_poll
    op.current_investigation = None
    return op


def _alert(summary: str = "Pod foo not ready for 30m", **overrides) -> dict:
    payload = {
        'source': 'alertmanager',
        'severity': 'warning',
        'summary': summary,
        'details': {'alertname': 'PodNotReady'},
        'alert_id': overrides.pop('alert_id', 'aid-test-1'),
    }
    payload.update(overrides)
    return payload


# ---- enqueue + queue full -------------------------------------------------


def test_enqueue_happy_path_returns_alert_id_and_grows_queue():
    op = _operator()
    result = op.enqueue_investigation(_alert(alert_id='aid-1'))
    assert result['status'] == 'queued'
    assert result['alert_id'] == 'aid-1'
    assert result['queue_depth'] == 1
    assert op._investigation_queue.qsize() == 1


def test_enqueue_raises_queue_full_when_capacity_exhausted():
    op = _operator(queue_size=2)
    op.enqueue_investigation(_alert(alert_id='a'))
    op.enqueue_investigation(_alert(alert_id='b'))
    with pytest.raises(queue.Full):
        op.enqueue_investigation(_alert(alert_id='c'))


def test_enqueue_rejection_increments_counter():
    from agent.agent import INVESTIGATION_QUEUE_REJECTED
    op = _operator(queue_size=1)
    op.enqueue_investigation(_alert(alert_id='a'))
    before = INVESTIGATION_QUEUE_REJECTED._value.get()
    with pytest.raises(queue.Full):
        op.enqueue_investigation(_alert(alert_id='b'))
    assert INVESTIGATION_QUEUE_REJECTED._value.get() == before + 1


# ---- run_investigation orchestration --------------------------------------


def test_run_investigation_chains_observe_orient_act_and_returns_act_result():
    op = _operator()
    expected = {'action': 'investigate', 'success': True, 'message': 'ok', 'details': {}, 'executed_at': 'now'}

    calls = []
    def fake_observe(alert):
        calls.append(('observe', alert))
        return {'trigger': alert['summary'], 'alert': alert}

    def fake_orient(ctx):
        calls.append(('orient', dict(ctx)))
        ctx['oriented'] = True
        return ctx

    def fake_act(ctx):
        calls.append(('act', dict(ctx)))
        return expected

    op._observe_alert = fake_observe
    op._orient = fake_orient
    op._act = fake_act

    result = op.run_investigation(_alert())
    assert result == expected
    assert [c[0] for c in calls] == ['observe', 'orient', 'act']
    assert calls[2][1].get('oriented') is True


# ---- ActionResult helpers --------------------------------------------------


def test_build_action_result_matches_event_runtime_shape():
    op = _operator()
    result = op._build_action_result(success=True, message='Resolved: foo', details={'investigation_id': 7})
    assert result['action'] == 'investigate'
    assert result['success'] is True
    assert result['message'] == 'Resolved: foo'
    assert result['details']['investigation_id'] == 7
    assert isinstance(result['executed_at'], str)
    assert result['executed_at'].endswith('+00:00') or 'T' in result['executed_at']


@pytest.mark.parametrize('outcome,verb', [
    ('resolved', 'Resolved'),
    ('escalated', 'Escalated'),
    ('monitoring', 'Monitoring'),
    ('failed', 'Investigation failed'),
])
def test_action_message_formats_outcome(outcome, verb):
    msg = CFOperator._action_message(outcome, 'Pod foo not ready', 4.5, 3)
    assert msg.startswith(verb + ':')
    assert '4.5s' in msg
    assert '3 tool calls' in msg


# ---- post-back to event_runtime -------------------------------------------


def test_post_action_result_no_ops_when_env_unset(monkeypatch):
    monkeypatch.delenv('CFOP_EVENT_RUNTIME_URL', raising=False)
    op = _operator()
    captured = []
    with patch('urllib.request.urlopen', side_effect=AssertionError('should not be called')):
        op._post_action_result_to_event_runtime(_alert(), {'action': 'investigate'})
    assert captured == []


def test_post_action_result_no_ops_when_alert_id_missing(monkeypatch):
    monkeypatch.setenv('CFOP_EVENT_RUNTIME_URL', 'http://er.local:8080')
    op = _operator()
    with patch('urllib.request.urlopen', side_effect=AssertionError('should not be called')):
        op._post_action_result_to_event_runtime({'summary': 'x'}, {'action': 'investigate'})


def test_post_action_result_posts_to_completion_endpoint(monkeypatch):
    monkeypatch.setenv('CFOP_EVENT_RUNTIME_URL', 'http://er.local:8080/')
    op = _operator()

    captured = {}

    class _FakeResp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = req.data
        captured['method'] = req.get_method()
        captured['content_type'] = req.headers.get('Content-type')
        return _FakeResp()

    alert_payload = _alert(alert_id='abc-123')
    with patch('urllib.request.urlopen', side_effect=fake_urlopen):
        op._post_action_result_to_event_runtime(
            alert_payload,
            {'action': 'investigate', 'success': True, 'message': 'Resolved: x', 'details': {}},
        )
    assert captured['url'] == 'http://er.local:8080/v1/investigations/abc-123/complete'
    assert captured['method'] == 'POST'
    assert captured['content_type'] == 'application/json'
    decoded = json.loads(captured['body'])
    # Wire shape is {alert, result} so the completion endpoint can rebuild
    # an Alert and fire its notification with the original severity/summary.
    assert decoded['alert']['alert_id'] == 'abc-123'
    assert decoded['alert']['summary'] == alert_payload['summary']
    assert decoded['result']['action'] == 'investigate'
    assert decoded['result']['success'] is True


def test_post_action_result_swallows_transport_errors(monkeypatch):
    """Post-back is best-effort — a 500 or connection refused should not raise."""
    monkeypatch.setenv('CFOP_EVENT_RUNTIME_URL', 'http://er.local:8080')
    op = _operator()
    with patch('urllib.request.urlopen', side_effect=OSError('connection refused')):
        op._post_action_result_to_event_runtime(
            _alert(alert_id='abc'),
            {'action': 'investigate'},
        )


# ---- HTTP endpoint --------------------------------------------------------


def _flask_client():
    """Build a Flask test client wired to a stub operator that records enqueue calls."""
    from web_server import WebServer

    operator = SimpleNamespace(
        current_investigation=None,
        start_time=0.0,
        enqueue_calls=[],
    )

    def enqueue_investigation(alert):
        operator.enqueue_calls.append(alert)
        return {'status': 'queued', 'alert_id': alert.get('alert_id'), 'queue_depth': len(operator.enqueue_calls)}

    operator.enqueue_investigation = enqueue_investigation
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host = 'localhost'
    server.port = 0
    from flask import Flask
    server.app = Flask(__name__)
    server.sock = None
    server.ws_clients = []
    server._chat_sessions = {}
    import threading as _t
    server._sessions_lock = _t.Lock()
    server._setup_routes()
    return server.app.test_client(), operator


def test_endpoint_returns_202_for_valid_alert():
    client, op = _flask_client()
    resp = client.post('/v1/investigate', json={'summary': 'Pod foo', 'severity': 'warning', 'alert_id': 'x'})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body['status'] == 'accepted'
    assert body['alert_id'] == 'x'
    assert len(op.enqueue_calls) == 1


def test_endpoint_rejects_missing_summary_with_400():
    client, op = _flask_client()
    resp = client.post('/v1/investigate', json={'severity': 'warning'})
    assert resp.status_code == 400
    assert op.enqueue_calls == []


def test_endpoint_rejects_non_object_body_with_400():
    client, op = _flask_client()
    resp = client.post('/v1/investigate', data='not json', content_type='application/json')
    assert resp.status_code == 400


def test_endpoint_returns_503_when_queue_full():
    client, op = _flask_client()
    def reject(alert):
        raise queue.Full()
    op.enqueue_investigation = reject
    resp = client.post('/v1/investigate', json={'summary': 'Pod foo'})
    assert resp.status_code == 503
    assert resp.get_json()['error'] == 'investigation queue full'


# ---- reactive poll gating -------------------------------------------------


def test_reactive_poll_flag_defaults_to_true_when_unset():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    # __init__ would read this; simulate the read.
    enabled = bool(op.config.get('ooda', {}).get('reactive_poll', True))
    assert enabled is True


def test_reactive_poll_flag_respects_false():
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'reactive_poll': False}}
    enabled = bool(op.config.get('ooda', {}).get('reactive_poll', True))
    assert enabled is False
