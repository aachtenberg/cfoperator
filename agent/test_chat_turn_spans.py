"""A console chat turn is one trace (CFOP-388).

Through the real handle_chat_message_stream with a scripted chain, for each
kind of turn it dispatches: plain chat, a /skill, and the summary phrase. The
canary sits in the message, the history, an unregistered skill name and the
answer; a span may say who asked (role) and which transcript (session id),
never what was said.
"""

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

CANARY = 'zq7-chat-canary-do-not-trace'


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _answer(url, json=None, headers=None, timeout=None):
    body = {'message': {'role': 'assistant', 'content': f'Here you go: {CANARY}'},
            'done_reason': 'stop', 'prompt_eval_count': 30, 'eval_count': 8}
    return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': 3}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(get_schemas=lambda policy=None: [], execute=lambda n, a: {})
    op.kb = MagicMock()
    op.kb.get_setting.return_value = ''
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    op._get_provider_chain = lambda backend, model: [('ollama', 'http://up:11434', 'gemma4:26b')]
    op._skipped_selection = lambda backend, head: None
    op._expand_slash_shortcut = lambda message: message
    op._build_chat_system_context = lambda tool_policy=None: f'You are CFOperator. {CANARY}'
    op.skills = {'k3s-health': {'name': 'k3s-health', 'description': 'Check the cluster',
                                'instructions': 'Look at nodes and pods.'}}
    op.containers = op.metrics = op.logs = op.alerts = None
    return op


def _turn(op, message, exporter, **kw):
    events = list(op.handle_chat_message_stream(
        message, [{'role': 'user', 'content': f'earlier {CANARY}'},
                  {'role': 'assistant', 'content': f'reply {CANARY}'}], **kw))
    # 'done' is queued from inside the turn's span, so the span can still be
    # closing on its thread when the generator returns.
    deadline = time.time() + 5
    while time.time() < deadline:
        roots = [s for s in exporter.get_finished_spans() if s.name == 'chat_turn']
        if roots:
            return events, roots[0]
        time.sleep(0.01)
    raise AssertionError('no chat_turn span')


def _every_string(span):
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    return out


def _no_canary(exporter):
    leaks = [(s.name, t) for s in exporter.get_finished_spans() for t in _every_string(s) if CANARY in t]
    assert leaks == [], f"chat text reached a span: {leaks}"


def _children(exporter, parent):
    return [s for s in exporter.get_finished_spans()
            if s.parent is not None and s.parent.span_id == parent.context.span_id]


def test_a_chat_turn_is_one_trace_with_who_but_not_what(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    events, root = _turn(_operator(), f'why is node-a slow? {CANARY}', spans,
                         actor_role='admin', session_id=123)
    assert events[-1]['event'] == 'done' and CANARY in events[-1]['data']['response']
    assert root.parent is None
    assert {s.context.trace_id for s in spans.get_finished_spans()} == {root.context.trace_id}
    a = root.attributes
    assert a['cfop.chat.session_id'] == 123
    assert a['cfop.chat.role'] == 'admin'
    assert a['cfop.chat.verify_only'] is False
    assert a['cfop.chat.kind'] == 'chat'
    assert [s.name for s in _children(spans, root)] == ['llm_chain']
    _no_canary(spans)


def test_a_turn_without_a_transcript_or_role_still_traces(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    _, root = _turn(_operator(), 'hello', spans)
    assert 'cfop.chat.session_id' not in root.attributes      # absent, not "None"
    assert root.attributes['cfop.chat.role'] == 'member'       # the console's own fallback


def test_a_registered_skill_is_named_under_the_turn(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    _, root = _turn(_operator(), f'/k3s-health look at {CANARY}', spans, actor_role='member')
    assert root.attributes['cfop.chat.kind'] == 'skill'
    (skill,) = _children(spans, root)
    assert skill.name == 'skill'
    assert skill.attributes['cfop.skill.name'] == 'k3s-health'
    assert [s.name for s in _children(spans, skill)] == ['llm_chain']
    _no_canary(spans)


def test_an_unregistered_skill_name_is_not_trusted(spans, monkeypatch):
    """`/<anything>` is user text until the registry vouches for it."""
    monkeypatch.setattr('requests.post', _answer)
    _, root = _turn(_operator(), f'/{CANARY} please', spans)
    (skill,) = _children(spans, root)
    assert skill.attributes['cfop.skill.name'] == 'unknown'
    _no_canary(spans)


def test_the_summary_phrase_runs_morning_summary_under_the_turn(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    _, root = _turn(_operator(), 'summary', spans)
    assert root.attributes['cfop.chat.kind'] == 'summary'
    assert 'morning_summary' in [s.name for s in _children(spans, root)]


def test_a_scheduled_morning_summary_is_its_own_root(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _answer)
    try:
        _operator()._generate_morning_summary()
    except Exception:
        pass   # the span is the subject here, not the summary's content
    (s,) = [x for x in spans.get_finished_spans() if x.name == 'morning_summary']
    assert s.parent is None
