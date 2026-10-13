"""LLM spans: what they carry, and what they must never carry (CFOP-316).

Through the real fallback walk and tool loop with a scripted transport, not
through cfshared.tracing alone. What this guards is the wiring: each rung is a
``chat {model}`` span under one ``llm_chain`` span, tool calls are children of
the rung that made them, the GenAI attributes are filled from the provider's
own response -- and no prompt, completion, tool argument, tool result or
exception message reaches the trace store.
"""

import os
import sys
import threading
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

# One marker planted in every piece of text that is the operator's or the
# model's: if it shows up anywhere in a span, that text leaked.
CANARY = 'zq7-canary-do-not-trace'
TOOL_SCHEMA = {
    'type': 'function',
    'function': {'name': 'k8s_get_pods', 'description': 'List pods',
                 'parameters': {'type': 'object', 'properties': {}}},
}
CHAIN = [('ollama', 'http://down:11434', 'gemma4:26b'),
         ('ollama', 'http://up:11434', 'qwen3:14b')]


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
    op.config = {'chat': {'max_tool_iterations': 4}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {'pods': [f'web-{CANARY}'], 'success': True},
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    op.llm = SimpleNamespace(record_success=lambda *a: None, record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    op._get_provider_chain = lambda backend, model: list(CHAIN)
    op._skipped_selection = lambda backend, head: None
    op._investigation_lock = threading.Lock()
    return op


class _Transport:
    """The first rung refuses the connection with the request quoted in its
    message, the way some SDKs do; the second calls a tool, then answers."""

    def __init__(self):
        self.script = [
            {'message': {'role': 'assistant', 'content': '',
                         'tool_calls': [{'function': {'name': 'k8s_get_pods',
                                                      'arguments': {'namespace': CANARY}}}]},
             'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 7},
            {'message': {'role': 'assistant',
                         'content': f'All good {CANARY}.\nSTATUS: resolved'},
             'done_reason': 'stop', 'prompt_eval_count': 150, 'eval_count': 20},
        ]

    def __call__(self, url, json=None, headers=None, timeout=None):
        if url.startswith('http://down'):
            raise requests.ConnectionError(f'refused while sending {json!r}')
        body = self.script.pop(0)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


def _walk(op, monkeypatch):
    monkeypatch.setattr('requests.post', _Transport())
    return op._chat_with_tools_with_fallback(
        messages=[{'role': 'user', 'content': f'Investigate: {CANARY}'}],
        system_context=f'You are CFOperator. {CANARY}')


def _every_string(span):
    """Everything a trace backend would store for this span, as text."""
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    for link in span.links:
        out.extend(str(v) for v in (link.attributes or {}).values())
    return out


def test_llm_spans_never_contain_prompts(spans, monkeypatch):
    """The canary is in the system prompt, the user message, the tool's
    arguments, the tool's result, the completion and the first rung's
    exception message. It must be in none of the spans."""
    result = _walk(_operator(), monkeypatch)
    assert CANARY in result['response']          # the text did flow -- just not here

    finished = spans.get_finished_spans()
    assert len(finished) == 4                    # chain, two rungs, one tool
    leaks = [(s.name, text) for s in finished for text in _every_string(s) if CANARY in text]
    assert leaks == [], f"prompt/completion/tool text reached a span: {leaks}"


def test_each_rung_is_a_child_of_the_chain_with_its_outcome(spans, monkeypatch):
    result = _walk(_operator(), monkeypatch)
    by_name = {s.name: s for s in spans.get_finished_spans()}
    chain = by_name['llm_chain']
    failed, answered = by_name['chat gemma4:26b'], by_name['chat qwen3:14b']
    tool = by_name['execute_tool k8s_get_pods']

    assert failed.parent.span_id == chain.context.span_id
    assert answered.parent.span_id == chain.context.span_id
    assert tool.parent.span_id == answered.context.span_id
    assert failed.start_time < answered.start_time

    assert failed.status.status_code.name == 'ERROR'
    assert failed.attributes['error.type'] == 'ConnectionError'
    assert failed.attributes['cfop.llm.error_kind'] == 'connection'

    assert answered.status.status_code.name == 'UNSET'
    a = answered.attributes
    assert a['gen_ai.operation.name'] == 'chat'
    assert a['gen_ai.system'] == a['gen_ai.provider.name'] == 'ollama'
    assert a['gen_ai.request.model'] == 'qwen3:14b'
    assert a['gen_ai.usage.input_tokens'] == result['input_tokens'] == 250
    assert a['gen_ai.usage.output_tokens'] == result['output_tokens'] == 27
    assert tuple(a['gen_ai.response.finish_reasons']) == ('stop', 'stop')
    assert a['cfop.llm.tool_calls'] == 1
    assert a['cfop.llm.stop_reason'] == 'answered'

    c = chain.attributes
    assert c['cfop.llm.chain_length'] == 2
    assert c['cfop.llm.answered_by'] == 'ollama/qwen3:14b'
    assert c['cfop.llm.rungs_tried'] == 2
    assert c['cfop.llm.fallback_used'] is True

    t = tool.attributes
    assert t['gen_ai.operation.name'] == 'execute_tool'
    assert t['gen_ai.tool.name'] == 'k8s_get_pods'
    assert t['cfop.tool.iteration'] == 1
    assert t['cfop.tool.cached'] is False
    assert t['cfop.tool.result'] == 'success'


def test_an_exhausted_chain_is_an_error_span(spans, monkeypatch):
    op = _operator()
    op._get_provider_chain = lambda backend, model: [CHAIN[0]]
    monkeypatch.setattr('requests.post', _Transport())
    with pytest.raises(requests.ConnectionError):
        op._chat_with_tools_with_fallback(messages=[{'role': 'user', 'content': 'x'}])
    chain = next(s for s in spans.get_finished_spans() if s.name == 'llm_chain')
    assert chain.status.status_code.name == 'ERROR'
    assert chain.attributes['error.type'] == 'ConnectionError'
    assert 'cfop.llm.answered_by' not in chain.attributes


def test_investigation_is_the_root_and_carries_the_alert_redacted(spans, monkeypatch):
    """The alert summary is the one free text on a trace: redacted, clipped,
    an attribute rather than the span name."""
    op = _operator()
    monkeypatch.setattr('requests.post', _Transport())
    op._observe_alert = lambda alert: {'alert': alert}
    op._orient = lambda ctx: ctx
    op._act = lambda ctx: op._chat_with_tools_with_fallback(
        messages=[{'role': 'user', 'content': 'x'}])
    op.run_investigation({
        'summary': 'Login failing: password=hunter2 for db ' + 'x' * 400,
        'severity': 'warning', 'source': 'alertmanager', 'alert_id': 'aid-1',
        'details': {'alertname': 'DBAuthFailing'},
    })
    by_name = {s.name: s for s in spans.get_finished_spans()}
    root = by_name['investigation']
    assert root.parent is None
    assert by_name['llm_chain'].parent.span_id == root.context.span_id
    assert root.attributes['cfop.alert.name'] == 'DBAuthFailing'
    summary = root.attributes['cfop.alert.summary']
    assert 'hunter2' not in summary
    assert len(summary) <= tracing.ALERT_TEXT_LIMIT + 1


def test_disabled_tracing_changes_nothing(monkeypatch):
    """Off is the default: the same walk, no provider installed, same answer."""
    tracing.reset()
    result = _walk(_operator(), monkeypatch)
    assert result['model'] == 'qwen3:14b' and result['fallback_used'] is True
    assert result['finish_reasons'] == ['stop', 'stop']
