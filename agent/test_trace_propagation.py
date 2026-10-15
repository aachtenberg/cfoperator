"""The agent's half of "one alert is one trace" (CFOP-317).

The runtime's traceparent reaches the agent as an HTTP header, but the
investigation root opens later, on the worker thread, so the route stashes
the carrier on the alert dict and run_investigation pops it. What this
guards: the queued alert continues the runtime's trace, the key never
reaches _observe_alert, and an alert with no carrier is still a root.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

TRACE = '0af7651916cd43dd8448eb211c80319c'
PARENT = 'b7ad6b7169203331'
CARRIER = {'traceparent': f'00-{TRACE}-{PARENT}-01'}


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _operator():
    op = CFOperator.__new__(CFOperator)
    op._investigation_lock = threading.Lock()
    seen = {}
    op._observe_alert = lambda alert: seen.setdefault('alert', dict(alert))
    op._orient = lambda ctx: ctx
    op._act = lambda ctx: {'success': True, 'details': {'investigation_id': 7, 'outcome': 'monitoring'}}
    op._run_triage = lambda alert: seen.setdefault('triage_alert', dict(alert)) and {'action': 'notify'}
    return op, seen


def _alert(**extra):
    return {'summary': 'Pod web-0 not ready', 'severity': 'warning', 'alert_id': 'a1', **extra}


def _only(spans, name):
    (s,) = [s for s in spans.get_finished_spans() if s.name == name]
    return s


def test_a_queued_alert_continues_the_runtimes_trace(spans):
    op, seen = _operator()
    op.run_investigation(_alert(**{tracing.TRACE_CARRIER_KEY: dict(CARRIER)}))
    inv = _only(spans, 'investigation')
    assert format(inv.context.trace_id, '032x') == TRACE
    assert format(inv.parent.span_id, '016x') == PARENT
    assert tracing.TRACE_CARRIER_KEY not in seen['alert'], 'the carrier leaked past the root span'


def test_triage_continues_it_too(spans):
    op, seen = _operator()
    assert op.run_triage(_alert(**{tracing.TRACE_CARRIER_KEY: dict(CARRIER)})) == {'action': 'notify'}
    tri = _only(spans, 'triage')
    assert format(tri.context.trace_id, '032x') == TRACE
    assert tracing.TRACE_CARRIER_KEY not in seen['triage_alert']


def test_without_a_carrier_the_investigation_is_a_root(spans):
    op, _ = _operator()
    op.run_investigation(_alert())
    assert _only(spans, 'investigation').parent is None


def test_the_route_reads_flask_headers():
    """The routes hand request.headers (a werkzeug Headers) to carrier_from;
    it must read that object, case-insensitively, without the SDK."""
    from werkzeug.datastructures import Headers
    tracing.reset()
    assert tracing.carrier_from(Headers([('Traceparent', CARRIER['traceparent']),
                                         ('Content-Type', 'application/json')])) == CARRIER
    assert tracing.carrier_from(Headers([('Content-Type', 'application/json')])) is None


def test_disabled_tracing_still_pops_the_key():
    tracing.reset()
    op, seen = _operator()
    op.run_investigation(_alert(**{tracing.TRACE_CARRIER_KEY: dict(CARRIER)}))
    assert tracing.TRACE_CARRIER_KEY not in seen['alert']
