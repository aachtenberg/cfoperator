"""A sweep is one trace (CFOP-385).

Through the real _deep_system_sweep, the real fallback walk and, for the
parallel path, the real LangGraph graph -- not through cfshared.tracing alone.
What this guards is the shape: every phase is a child of ``sweep``, the LLM
calls hang under their phase rather than starting traces of their own, and
nothing the model said about the operator's systems lands on a span.

The parallel test is the one that matters for upgrades. Phases run on
LangGraph worker threads; they keep their parent only because LangGraph copies
contextvars into those threads. If a version stops doing that, the phase spans
become orphan roots and this fails.
"""

import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

CANARY = 'zq7-sweep-canary-do-not-trace'
FINDINGS_JSON = ('[{"severity": "warning", "finding": "disk filling on node-a %s", '
                 '"evidence": "node_filesystem_avail_bytes %s"}]' % (CANARY, CANARY))


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _answer(url, json=None, headers=None, timeout=None):
    """Every phase answers straight away with one finding carrying the canary.
    Stateless, so concurrent phases on the parallel path can share it."""
    body = {'message': {'role': 'assistant', 'content': FINDINGS_JSON},
            'done_reason': 'stop', 'prompt_eval_count': 50, 'eval_count': 10}
    return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _operator(pool=None):
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': 4},
                 'ooda': {'sweep': {'metrics': True, 'logs': True, 'containers': False,
                                    'baseline_drift': False, 'learning_consolidation': False}}}
    op.llm_timeout = 5
    op.metrics, op.logs, op.containers = object(), object(), None
    op.ollama_pool = pool
    op.tools = SimpleNamespace(get_schemas=lambda policy=None: [], execute=lambda name, args: {})
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '', store_sweep_report=lambda **k: None,
                            _kb=MagicMock())
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    op.embeddings = SimpleNamespace(is_available=lambda: False)
    op._get_provider_chain = lambda backend, model: [('ollama', 'http://up:11434', 'gemma4:26b')]
    op._skipped_selection = lambda backend, head: None
    op._build_sweep_system_prompt = lambda task, skill_name=None: f'You sweep. {CANARY}'
    op._get_sweep_max_iterations = lambda: 4
    op._verify_single_finding = lambda finding, max_iterations, snapshot=None: finding
    op._post_findings_to_event_runtime = lambda findings: None
    op._get_resolved_findings = lambda findings: []
    op._generate_sweep_report = lambda findings: {'severity': 'info', 'findings': findings,
                                                  'summary': f'{len(findings)} findings'}
    op._ephemeral_service_names = lambda: set()
    op._analyze_correlations = lambda findings, patterns: None
    return op


def _every_string(span):
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    return out


def _by_name(finished):
    out = {}
    for s in finished:
        out.setdefault(s.name, []).append(s)
    return out


def test_a_sequential_sweep_is_one_trace(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    _operator()._deep_system_sweep()

    finished = spans.get_finished_spans()
    named = _by_name(finished)
    (root,) = named['sweep']
    assert root.parent is None
    assert {s.context.trace_id for s in finished} == {root.context.trace_id}, \
        "a sweep must be ONE trace; some span started its own"

    for phase in ('sweep.metrics', 'sweep.logs', 'sweep.embedding_backfill',
                  'sweep.verify_findings', 'sweep.publish', 'sweep.report',
                  'sweep.metric_snapshot', 'sweep.correlations'):
        (s,) = named[phase]
        assert s.parent.span_id == root.context.span_id, phase

    # Each LLM phase's chain hangs under that phase, not under the root.
    chains = named['llm_chain']
    assert len(chains) == 2
    phase_ids = {named['sweep.metrics'][0].context.span_id, named['sweep.logs'][0].context.span_id}
    assert {c.parent.span_id for c in chains} == phase_ids
    for chat in named['chat gemma4:26b']:
        assert chat.parent.span_id in {c.context.span_id for c in chains}

    assert root.attributes['cfop.sweep.mode'] == 'sequential'
    assert root.attributes['cfop.sweep.findings_raw'] == 2
    assert root.attributes['cfop.sweep.findings'] >= 1
    assert named['sweep.metrics'][0].attributes['cfop.sweep.phase_findings'] == 1


def test_sweep_spans_never_carry_finding_text(spans, monkeypatch):
    """The canary is in the system prompt and in every finding and its
    evidence. Spans carry counts; the text stays out."""
    monkeypatch.setattr('requests.post', _answer)
    _operator()._deep_system_sweep()
    leaks = [(s.name, t) for s in spans.get_finished_spans() for t in _every_string(s) if CANARY in t]
    assert leaks == [], f"sweep/finding text reached a span: {leaks}"


class _Pool:
    """Two instances, so _deep_system_sweep takes the LangGraph path."""

    def __init__(self):
        self.lock = threading.Lock()
        self.threads = set()

    def available_count(self):
        return 2

    def checkout(self):
        with self.lock:
            self.threads.add(threading.current_thread().name)
        return SimpleNamespace(name='inst', url='http://up:11434', model='gemma4:26b')

    def checkin(self, instance):
        pass


def test_parallel_phases_keep_their_parent_across_langgraph_threads(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    pool = _Pool()
    _operator(pool=pool)._deep_system_sweep()

    named = _by_name(spans.get_finished_spans())
    (root,) = named['sweep']
    assert root.attributes['cfop.sweep.mode'] == 'parallel'
    assert threading.current_thread().name not in pool.threads, \
        "phases ran on the caller's thread; this test is not exercising the fan-out"
    for phase in ('sweep.metrics', 'sweep.logs'):
        (s,) = named[phase]
        assert s.parent is not None and s.parent.span_id == root.context.span_id, \
            f"{phase} lost its parent on a LangGraph worker thread"
        assert s.attributes['cfop.sweep.phase_findings'] == 1
    # The pool path calls _chat_with_tools directly: chat under the phase.
    phase_ids = {named['sweep.metrics'][0].context.span_id, named['sweep.logs'][0].context.span_id}
    assert {c.parent.span_id for c in named['chat gemma4:26b']} == phase_ids


def test_disabled_tracing_sweeps_exactly_as_before(monkeypatch):
    tracing.reset()
    monkeypatch.setattr('requests.post', _answer)
    op = _operator()
    stored = []
    op.kb.store_sweep_report = lambda **k: stored.append(k)
    op._deep_system_sweep()
    assert len(stored) == 1 and len(stored[0]['findings']) == 1   # deduped to one
