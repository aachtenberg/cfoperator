"""The one-shot LLM calls emit the same ``chat {model}`` span as the tool loop
(CFOP-386): the mutation judge, the node-action plan, correlation analysis
and learning extraction.

Each is driven through its real function with a scripted ``requests.post``
in every response shape it speaks (Anthropic, OpenAI-compatible, Ollama).
The canary sits in the prompt, the completion and an HTTP error body; none
of it may reach a span. The error-body case matters most: the judge's own
exception deliberately carries the vendor's message (CFOP-117), so the span
has to be marked by status code alone.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

CANARY = 'zq7-oneshot-canary-do-not-trace'


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.reason = 'Bad Request' if status >= 400 else 'OK'
        self.url = 'https://example.invalid'

    def json(self):
        if isinstance(self._body, str):
            raise ValueError('not json')
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Client Error', response=self)


def _post(status, body, sent=None):
    def post(url, json=None, headers=None, timeout=None):
        if sent is not None:
            sent.append(json)
        return _Resp(status, body)
    return post


def _anthropic(text):
    return {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 120, 'output_tokens': 30}}


def _openai(text):
    return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 110, 'completion_tokens': 25}}


def _ollama(text):
    return {'message': {'role': 'assistant', 'content': text}, 'done_reason': 'stop',
            'prompt_eval_count': 90, 'eval_count': 20}


def _every_string(span):
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    return out


def _no_canary(exporter):
    leaks = [(s.name, t) for s in exporter.get_finished_spans() for t in _every_string(s)
             if CANARY in t]
    assert leaks == [], f"prompt/completion/error text reached a span: {leaks}"


def _only_chat(exporter):
    chats = [s for s in exporter.get_finished_spans() if s.name.startswith('chat ')]
    assert len(chats) == 1, [s.name for s in exporter.get_finished_spans()]
    return chats[0]


def _keyed_op():
    op = MagicMock()
    op._judge_api_key = lambda backend: 'k'
    return op


# --- the judge ---------------------------------------------------------------

def test_anthropic_judge_call_is_a_chat_span(spans, monkeypatch):
    verdict = json.dumps({'verdict': 'confirm', 'reason': f'fine {CANARY}'})
    monkeypatch.setattr('requests.post', _post(200, _anthropic(verdict)))
    out = CFOperator._complete_judge(_keyed_op(), f'judge {CANARY}', f'evidence {CANARY}',
                                     'anthropic', 'claude-opus-4-8')
    assert CANARY in out                         # the text flowed -- just not into the trace
    s = _only_chat(spans)
    assert s.name == 'chat claude-opus-4-8'
    assert s.attributes['cfop.llm.purpose'] == 'judge'
    assert s.attributes['gen_ai.provider.name'] == 'anthropic'
    assert s.attributes['gen_ai.usage.input_tokens'] == 120
    assert s.attributes['gen_ai.usage.output_tokens'] == 30
    assert tuple(s.attributes['gen_ai.response.finish_reasons']) == ('end_turn',)
    _no_canary(spans)


def test_openai_compat_judge_call_is_a_chat_span(spans, monkeypatch):
    monkeypatch.setattr('requests.post', _post(200, _openai(f'{{"verdict":"reject","reason":"{CANARY}"}}')))
    CFOperator._complete_judge(_keyed_op(), 'sys', f'user {CANARY}', 'xai', 'grok-4')
    s = _only_chat(spans)
    assert s.name == 'chat grok-4'
    assert s.attributes['gen_ai.provider.name'] == 'xai'
    assert s.attributes['gen_ai.usage.input_tokens'] == 110
    assert tuple(s.attributes['gen_ai.response.finish_reasons']) == ('stop',)
    _no_canary(spans)


def test_judge_http_error_marks_the_span_by_status_only(spans, monkeypatch):
    """The judge's exception names the vendor's message on purpose (CFOP-117).
    The span must not: an error body can quote the request."""
    body = {'type': 'error', 'error': {'message': f'bad request echoing {CANARY}'}}
    monkeypatch.setattr('requests.post', _post(400, body))
    with pytest.raises(requests.HTTPError) as exc:
        CFOperator._complete_judge(_keyed_op(), 'sys', 'user', 'anthropic', 'claude-opus-4-8')
    assert CANARY in str(exc.value)              # behaviour unchanged for the queue row
    s = _only_chat(spans)
    assert s.status.status_code.name == 'ERROR'
    assert s.attributes['error.type'] == 'HTTP400'
    assert s.attributes['http.response.status_code'] == 400
    _no_canary(spans)


def test_transport_failure_marks_the_span_with_the_class_only(spans, monkeypatch):
    def refuse(url, **kwargs):
        raise requests.ConnectionError(f'refused while sending {kwargs.get("json")!r}')
    monkeypatch.setattr('requests.post', refuse)
    with pytest.raises(requests.ConnectionError):
        CFOperator._complete_judge(_keyed_op(), f'sys {CANARY}', 'user', 'anthropic', 'claude-opus-4-8')
    s = _only_chat(spans)
    assert s.status.status_code.name == 'ERROR'
    assert s.attributes['error.type'] == 'ConnectionError'
    _no_canary(spans)


# --- node-action plan --------------------------------------------------------

def test_node_action_plan_call_is_a_chat_span(spans, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    op = MagicMock()
    op._executor_config = lambda: {'node_action': {'model': 'claude-opus-4-8'}}
    monkeypatch.setattr('requests.post', _post(200, _anthropic(f'plan: {CANARY}')))
    CFOperator._complete_node_action_plan(op, f'plan this {CANARY}')
    s = _only_chat(spans)
    assert s.name == 'chat claude-opus-4-8'
    assert s.attributes['cfop.llm.purpose'] == 'node_action_plan'
    _no_canary(spans)


# --- learnings and correlations: Ollama shape, under their parent ------------

def _local_op():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op.llm_timeout = 5
    op._resolve_provider = lambda *a, **k: ('ollama', 'http://up:11434', 'gemma4:26b')
    op.kb = MagicMock()
    op.kb.get_operational_summary.return_value = {'investigations': {'total': 1}}
    op.kb.find_learnings.return_value = []
    return op


def test_learning_extraction_is_a_chat_span_under_the_investigation(spans, monkeypatch):
    learnings = json.dumps({'learnings': [{'learning_type': 'solution', 'title': CANARY,
                                           'description': CANARY, 'applies_when': CANARY}]})
    monkeypatch.setattr('requests.post', _post(200, _ollama(learnings)))
    with tracing.span('investigation') as root:
        _local_op()._extract_learnings(7, f'trigger {CANARY}', {'response': CANARY})
    s = _only_chat(spans)
    assert s.name == 'chat gemma4:26b'
    assert s.attributes['cfop.llm.purpose'] == 'learnings'
    assert s.attributes['gen_ai.usage.input_tokens'] == 90
    assert tuple(s.attributes['gen_ai.response.finish_reasons']) == ('stop',)
    # investigation → investigation.extract_learnings (CFOP-387) → chat
    (phase,) = [x for x in spans.get_finished_spans() if x.name == 'investigation.extract_learnings']
    assert s.parent.span_id == phase.context.span_id
    assert phase.parent.span_id == root.get_span_context().span_id
    _no_canary(spans)


def test_correlation_analysis_is_a_chat_span_under_the_sweep_phase(spans, monkeypatch):
    """The 22.7s that sweep.correlations spent with no child in the first
    live sweep trace (CFOP-385) is this call."""
    monkeypatch.setattr('requests.post', _post(200, _ollama(json.dumps({'insights': []}))))
    with tracing.span('sweep.correlations') as phase:
        _local_op()._analyze_correlations([{'finding': CANARY, 'severity': 'warning'}], [])
    s = _only_chat(spans)
    assert s.attributes['cfop.llm.purpose'] == 'correlations'
    assert s.parent.span_id == phase.get_span_context().span_id
    _no_canary(spans)


def test_learning_extraction_http_error_is_an_error_span(spans, monkeypatch):
    """These callers parse the body whatever the status; the span must not
    read as a healthy call when the provider refused."""
    monkeypatch.setattr('requests.post', _post(500, {'error': f'boom {CANARY}'}))
    _local_op()._extract_learnings(7, 'trigger', {})
    s = _only_chat(spans)
    assert s.status.status_code.name == 'ERROR'
    assert s.attributes['error.type'] == 'HTTP500'
    _no_canary(spans)


def test_disabled_tracing_leaves_the_judge_unchanged(monkeypatch):
    tracing.reset()
    sent = []
    monkeypatch.setattr('requests.post', _post(200, _anthropic('{"verdict":"confirm","reason":"x"}'), sent))
    out = CFOperator._complete_judge(_keyed_op(), 'sys', 'user', 'anthropic', 'claude-opus-4-8')
    assert 'confirm' in out and sent and sent[0]['model'] == 'claude-opus-4-8'
