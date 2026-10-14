"""An investigation's phases show up as spans under its root (CFOP-387).

Through the real run_investigation → _orient → _act with a scripted
transport. The phases stay real; only what they reach out to is stubbed,
because stubbing a phase method on the instance would bypass its decorator
and remove the very span under test. The embedding service is the real one
too, so its `embeddings {model}` span is the one production emits.
"""

import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from embedding_service import EmbeddingService  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

CANARY = 'zq7-investigation-canary-do-not-trace'
# Not a recoverable-condition trigger, so the noise check exits early and the
# run reaches a real needs_action verdict.
TRIGGER = f'Disk usage above 90% on node-a {CANARY}'
ANSWER = (f'The disk on node-a is full ({CANARY}).\n'
          f'STATUS: needs_action\nRECOMMENDATION: expand the volume backing {CANARY}')


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _transport(url, json=None, headers=None, timeout=None):
    """Embeddings get a vector; the investigation gets its verdict; the FIX
    nudge (no FIX in the verdict) gets an empty object back."""
    if url.endswith('/api/embeddings'):
        body = {'embedding': [0.1] * 8}
    else:
        last = (json or {}).get('messages', [{}])[-1].get('content', '')
        text = ANSWER if 'Investigate this alert' in str(last) else '{}'
        body = {'message': {'role': 'assistant', 'content': text}, 'done_reason': 'stop',
                'prompt_eval_count': 40, 'eval_count': 12}
    return SimpleNamespace(status_code=200, text='', raise_for_status=lambda: None,
                           json=lambda: body)


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': 4}}
    op.llm_timeout = 5
    op._investigation_lock = threading.Lock()
    op.tools = SimpleNamespace(get_schemas=lambda policy=None: [], execute=lambda n, a: {})
    op.kb = MagicMock()
    op.kb.start_investigation.return_value = 42
    op.kb.get_setting.return_value = ''
    op.kb._kb.find_learnings_hybrid.return_value = []
    op.kb._kb.find_similar_investigations_hybrid.return_value = []
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    es = EmbeddingService(ollama_url='http://emb:11434', model='nomic-embed-text')
    es._available = True
    op.embeddings = es
    op._get_provider_chain = lambda backend, model: [('ollama', 'http://up:11434', 'gemma4:26b')]
    op._skipped_selection = lambda backend, head: None
    op.git_repos = lambda: []
    op._remediation_flag = lambda name: False      # queue_feed off: queueing exits early
    return op


def _every_string(span):
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    return out


def _run(monkeypatch):
    monkeypatch.setattr('requests.post', _transport)
    return _operator().run_investigation({'summary': TRIGGER, 'severity': 'warning',
                                          'details': {'alertname': 'DiskFull'}})


def test_investigation_phases_are_children_of_the_root(spans, monkeypatch):
    result = _run(monkeypatch)
    assert result['details']['outcome'] == 'needs_action'

    finished = spans.get_finished_spans()
    named = {}
    for s in finished:
        named.setdefault(s.name, []).append(s)
    (root,) = named['investigation']
    assert {s.context.trace_id for s in finished} == {root.context.trace_id}

    for phase in ('investigation.orient', 'investigation.noise_check',
                  'investigation.verify_outcome', 'investigation.structured_fix',
                  'investigation.propose_remediation', 'investigation.queue_remediation',
                  'embed_investigation'):
        assert phase in named, f"missing {phase}: {sorted(named)}"
        for s in named[phase]:
            assert s.parent.span_id == root.context.span_id, phase

    # The main chain is a sibling of the phases; the FIX nudge's chain hangs
    # under structured_fix.
    chains = named['llm_chain']
    parents = {c.parent.span_id for c in chains}
    assert root.context.span_id in parents
    assert named['investigation.structured_fix'][0].context.span_id in parents

    # Embedding round trips: one under orient (the query), one under
    # embed_investigation (the record).
    emb = named['embeddings nomic-embed-text']
    emb_parents = {e.parent.span_id for e in emb}
    assert named['investigation.orient'][0].context.span_id in emb_parents
    assert named['embed_investigation'][0].context.span_id in emb_parents
    assert emb[0].attributes['gen_ai.operation.name'] == 'embeddings'


def test_investigation_phase_spans_carry_no_text(spans, monkeypatch):
    _run(monkeypatch)
    leaks = [(s.name, t) for s in spans.get_finished_spans() for t in _every_string(s)
             if CANARY in t and not s.name == 'investigation']
    assert leaks == [], f"alert/answer text reached a phase span: {leaks}"
    # The root's summary is the one place alert text goes, redacted and clipped.
    (root,) = [s for s in spans.get_finished_spans() if s.name == 'investigation']
    assert len(root.attributes['cfop.alert.summary']) <= tracing.ALERT_TEXT_LIMIT + 1


def test_embedding_http_error_marks_the_span(spans, monkeypatch):
    def refuse(url, json=None, timeout=None):
        return SimpleNamespace(status_code=500, text=f'boom {CANARY}', json=lambda: {})
    monkeypatch.setattr('requests.post', refuse)
    es = EmbeddingService(ollama_url='http://emb:11434', model='nomic-embed-text')
    es._available = True
    assert es.generate_embedding(f'some text {CANARY}', use_cache=False) is None
    (s,) = spans.get_finished_spans()
    assert s.name == 'embeddings nomic-embed-text'
    assert s.status.status_code.name == 'ERROR'
    assert s.attributes['error.type'] == 'HTTP500'
    assert not any(CANARY in t for t in _every_string(s))


def test_embedding_cache_hit_is_not_a_span(spans, monkeypatch):
    calls = []

    def ok(url, json=None, timeout=None):
        calls.append(url)
        return SimpleNamespace(status_code=200, text='', json=lambda: {'embedding': [0.2] * 8})
    monkeypatch.setattr('requests.post', ok)
    es = EmbeddingService(ollama_url='http://emb:11434', model='nomic-embed-text')
    es._available = True
    es.generate_embedding('the same text twice')
    es.generate_embedding('the same text twice')
    assert len(calls) == 1
    assert len(spans.get_finished_spans()) == 1
