"""One alert is one trace (CFOP-317): the helpers and the runtime's half.

The runtime opens an ``alert`` root around each alert and its two calls to
the agent carry the W3C traceparent; the agent continues the trace (its
half lives in agent/test_trace_propagation.py). What this guards: inject
writes the current span's trace onto the headers and nothing else, a
parented span really is a child of the remote span, the runtime's real
handler and triage engine both send the header from under the ``alert``
span, and with tracing off nothing is added at all.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from cfshared import tracing
from event_runtime.engine import EventRuntime
from event_runtime.http_actions import HTTPInvestigateActionHandler, HTTPTriageDecisionEngine, _agent_headers
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope, Decision
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import DecisionEngine
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

CANARY = 'zq7-alert-canary-do-not-trace'


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _alert():
    return Alert(source='test', severity=AlertSeverity.WARNING,
                 summary=f'Pod web-0 not ready {CANARY}',
                 details={'alertname': 'PodNotReady'}, alert_id='a-1')


class _Investigate(DecisionEngine):
    name = 'always-investigate'

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(action='investigate', confidence=1.0, reasoning='test')


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _runtime(tmp_path):
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path))]))
    plugins.register_decision_engine(_Investigate())
    plugins.register_action_handler(HTTPInvestigateActionHandler('http://agent.test:8083'))
    return EventRuntime(plugins)


def _trace_id_of(traceparent: str) -> str:
    return traceparent.split('-')[1]


# ---- the helpers -------------------------------------------------------------

def test_inject_is_a_noop_while_off():
    tracing.reset()
    assert tracing.inject({'Content-Type': 'application/json'}) == {'Content-Type': 'application/json'}


def test_carrier_from_needs_no_sdk_and_is_case_insensitive():
    tracing.reset()
    tp = '00-' + 'a' * 32 + '-' + 'b' * 16 + '-01'
    assert tracing.carrier_from({'Traceparent': tp, 'X-Other': '1'}) == {'traceparent': tp}
    assert tracing.carrier_from({'X-Other': '1'}) is None


def test_inject_then_parent_continues_the_trace(spans):
    headers = {}
    with tracing.span('alert'):
        tracing.inject(headers)
    assert 'traceparent' in headers
    with tracing.span('investigation', parent=tracing.carrier_from(headers)):
        pass
    finished = {s.name: s for s in spans.get_finished_spans()}
    assert finished['investigation'].context.trace_id == finished['alert'].context.trace_id
    assert finished['investigation'].parent.span_id == finished['alert'].context.span_id


def test_service_name_precedence(monkeypatch):
    monkeypatch.delenv('OTEL_SERVICE_NAME', raising=False)
    assert tracing._service_name({}, 'cfoperator-event-runtime') == 'cfoperator-event-runtime'
    monkeypatch.setenv('OTEL_SERVICE_NAME', 'from-env')
    assert tracing._service_name({}, 'cfoperator-event-runtime') == 'from-env'
    assert tracing._service_name({'service_name': 'from-config'}, 'x') == 'from-config'


# ---- the runtime --------------------------------------------------------------

def test_the_investigate_dispatch_carries_the_alert_spans_trace(spans, tmp_path):
    runtime = _runtime(tmp_path)
    with patch('event_runtime.http_actions.urllib.request.urlopen',
               return_value=_Response(b'{"status": "accepted"}')) as urlopen:
        result = runtime.handle_alert(_alert())

    assert result['status'] == 'completed' and result['action'] == 'investigate'
    (root,) = [s for s in spans.get_finished_spans() if s.name == 'alert']
    assert root.parent is None
    assert root.attributes['cfop.alert.status'] == 'completed'
    assert root.attributes['cfop.alert.action'] == 'investigate'

    req = urlopen.call_args[0][0]
    assert req.full_url.endswith('/v1/investigate')
    traceparent = req.get_header('Traceparent')
    assert traceparent, 'the dispatch to the agent carried no traceparent'
    assert _trace_id_of(traceparent) == format(root.context.trace_id, '032x')


def test_the_triage_call_carries_it_too(spans):
    engine = HTTPTriageDecisionEngine('http://agent.test:8083')
    with patch('event_runtime.http_actions.urllib.request.urlopen',
               return_value=_Response(b'{"action": "notify", "confidence": 0.9, "reason": "x"}')) as urlopen:
        with tracing.span('alert'):
            assert engine._call_triage(_alert())['action'] == 'notify'
    (root,) = spans.get_finished_spans()
    req = urlopen.call_args[0][0]
    assert req.full_url.endswith('/v1/triage')
    assert _trace_id_of(req.get_header('Traceparent')) == format(root.context.trace_id, '032x')


def test_the_alert_span_carries_no_summary_text_beyond_the_clip(spans, tmp_path):
    """alert_attributes clips the summary; the canary sits past the clip."""
    runtime = _runtime(tmp_path)
    alert = _alert()
    alert.summary = 'x' * tracing.ALERT_TEXT_LIMIT + CANARY
    with patch('event_runtime.http_actions.urllib.request.urlopen', return_value=_Response(b'{}')):
        runtime.handle_alert(alert)
    for s in spans.get_finished_spans():
        for value in s.attributes.values():
            assert CANARY not in str(value), (s.name, value)


def test_disabled_tracing_adds_no_header_and_handles_the_alert_as_before(tmp_path):
    tracing.reset()
    assert 'traceparent' not in {k.lower() for k in _agent_headers()}
    runtime = _runtime(tmp_path)
    with patch('event_runtime.http_actions.urllib.request.urlopen', return_value=_Response(b'{}')) as urlopen:
        result = runtime.handle_alert(_alert())
    assert result['status'] == 'completed'
    assert urlopen.call_args[0][0].get_header('Traceparent') is None
