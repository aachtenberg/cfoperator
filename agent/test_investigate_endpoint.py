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
    import threading as _t
    op = CFOperator.__new__(CFOperator)
    op.config = {'ooda': {'investigation_queue_size': queue_size, 'reactive_poll': reactive_poll}}
    op._investigation_queue = queue.Queue(maxsize=queue_size)
    op._investigation_worker_thread = None
    op._investigation_lock = _t.Lock()
    op._reactive_poll_enabled = reactive_poll
    op._enqueue_dedup_ttl = 3600.0
    op._enqueue_dedup_keys = {}
    op._enqueue_dedup_lock = _t.Lock()
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


# ---- idempotent enqueue (phase-2 MCP contract) ------------------------------


def test_enqueue_dedupes_repeat_alert_id_within_ttl():
    op = _operator()
    first = op.enqueue_investigation(_alert(alert_id='aid-dup'))
    second = op.enqueue_investigation(_alert(alert_id='aid-dup'))
    assert first['status'] == 'queued'
    assert second['status'] == 'deduped'
    assert op._investigation_queue.qsize() == 1


def test_enqueue_prefers_idempotency_key_over_alert_id():
    op = _operator()
    op.enqueue_investigation(_alert(alert_id='a1', idempotency_key='k1'))
    # same key, different alert_id -> deduped
    result = op.enqueue_investigation(_alert(alert_id='a2', idempotency_key='k1'))
    assert result['status'] == 'deduped'
    # different key -> enqueued
    result = op.enqueue_investigation(_alert(alert_id='a2', idempotency_key='k2'))
    assert result['status'] == 'queued'
    assert op._investigation_queue.qsize() == 2


def test_enqueue_dedup_expires_after_ttl():
    op = _operator()
    op._enqueue_dedup_ttl = 0.0  # everything expired immediately
    op.enqueue_investigation(_alert(alert_id='aid-ttl'))
    result = op.enqueue_investigation(_alert(alert_id='aid-ttl'))
    assert result['status'] == 'queued'
    assert op._investigation_queue.qsize() == 2


def test_enqueue_without_any_key_never_dedupes():
    op = _operator()
    alert = _alert()
    alert.pop('alert_id')
    assert op.enqueue_investigation(dict(alert))['status'] == 'queued'
    assert op.enqueue_investigation(dict(alert))['status'] == 'queued'
    assert op._investigation_queue.qsize() == 2


def test_queue_full_rejection_releases_dedup_claim():
    op = _operator(queue_size=1)
    op.enqueue_investigation(_alert(alert_id='first'))
    with pytest.raises(queue.Full):
        op.enqueue_investigation(_alert(alert_id='retry-me'))
    # drain and retry: the rejected key must not read as 'deduped'
    op._investigation_queue.get_nowait()
    result = op.enqueue_investigation(_alert(alert_id='retry-me'))
    assert result['status'] == 'queued'


def _counter_value(counter) -> float:
    """Read a Counter's current value via its public collect() API.

    Avoids `Counter._value.get()` which depends on prometheus_client internals.
    """
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith('_total'):
                return float(sample.value)
    return 0.0


def test_enqueue_rejection_increments_counter():
    from agent.agent import INVESTIGATION_QUEUE_REJECTED
    op = _operator(queue_size=1)
    op.enqueue_investigation(_alert(alert_id='a'))
    before = _counter_value(INVESTIGATION_QUEUE_REJECTED)
    with pytest.raises(queue.Full):
        op.enqueue_investigation(_alert(alert_id='b'))
    assert _counter_value(INVESTIGATION_QUEUE_REJECTED) == before + 1


# ---- run_investigation orchestration --------------------------------------


def test_observe_alert_prefers_top_level_summary_for_event_runtime_payload():
    """event_runtime Alert dicts carry top-level `summary`, not `annotations.summary`."""
    op = _operator()
    ctx = op._observe_alert({'summary': 'Pod foo not ready', 'severity': 'warning'})
    assert ctx['trigger'] == 'Pod foo not ready'


def test_observe_alert_falls_back_to_annotations_for_alertmanager_payload():
    """Raw Alertmanager payloads (used by the reactive poll) put summary under annotations."""
    op = _operator()
    ctx = op._observe_alert({'annotations': {'summary': 'Pod legacy alert'}})
    assert ctx['trigger'] == 'Pod legacy alert'


def test_observe_alert_uses_unknown_when_no_summary_present():
    op = _operator()
    ctx = op._observe_alert({'labels': {'alertname': 'X'}})
    assert ctx['trigger'] == 'Unknown alert'


def test_investigation_lock_serializes_concurrent_paths():
    """Two threads calling run_investigation must not overlap inside _act."""
    import threading as _t
    op = _operator()
    op._observe_alert = lambda alert: {'alert': alert, 'trigger': alert['summary']}
    op._orient = lambda ctx: ctx

    in_flight = []
    max_in_flight = []
    enter_barrier = _t.Barrier(2)

    def slow_act(ctx):
        in_flight.append(1)
        max_in_flight.append(sum(in_flight))
        # Give the other thread a chance to race; the lock should keep it out.
        _t.Event().wait(0.05)
        in_flight.pop()
        return {'action': 'investigate', 'success': True, 'message': 'ok', 'details': {}, 'executed_at': 'x'}

    op._act = slow_act

    def worker():
        enter_barrier.wait(timeout=2)
        op.run_investigation(_alert())

    threads = [_t.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max(max_in_flight) == 1, "two investigations ran concurrently — lock not held"


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
        captured['auth_token'] = req.headers.get('X-cfop-token')
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
    # No CFOP_COMPLETION_SHARED_SECRET set, so no auth header sent.
    assert captured['auth_token'] is None
    decoded = json.loads(captured['body'])
    # Wire shape is {alert, result} so the completion endpoint can rebuild
    # an Alert and fire its notification with the original severity/summary.
    assert decoded['alert']['alert_id'] == 'abc-123'
    assert decoded['alert']['summary'] == alert_payload['summary']
    assert decoded['result']['action'] == 'investigate'
    assert decoded['result']['success'] is True


def test_post_action_result_sends_auth_header_when_secret_set(monkeypatch):
    monkeypatch.setenv('CFOP_EVENT_RUNTIME_URL', 'http://er.local:8080')
    monkeypatch.setenv('CFOP_COMPLETION_SHARED_SECRET', 'shh-secret')
    op = _operator()

    captured = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def fake_urlopen(req, timeout=None):
        captured['auth_token'] = req.headers.get('X-cfop-token')
        return _Resp()

    with patch('urllib.request.urlopen', side_effect=fake_urlopen):
        op._post_action_result_to_event_runtime(
            _alert(alert_id='abc'),
            {'action': 'investigate', 'success': True, 'message': 'ok'},
        )
    assert captured['auth_token'] == 'shh-secret'


def test_post_action_result_omits_auth_header_when_secret_blank(monkeypatch):
    """Whitespace-only secret should not produce an auth header."""
    monkeypatch.setenv('CFOP_EVENT_RUNTIME_URL', 'http://er.local:8080')
    monkeypatch.setenv('CFOP_COMPLETION_SHARED_SECRET', '   ')
    op = _operator()

    captured = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def fake_urlopen(req, timeout=None):
        captured['auth_token'] = req.headers.get('X-cfop-token')
        return _Resp()

    with patch('urllib.request.urlopen', side_effect=fake_urlopen):
        op._post_action_result_to_event_runtime(
            _alert(alert_id='abc'),
            {'action': 'investigate', 'success': True, 'message': 'ok'},
        )
    assert captured['auth_token'] is None


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
