"""A re-verification pass is one trace (CFOP-399).

reverify_row calls _chat_with_tools directly -- on purpose, to stay inside the
judge rung -- so no llm_chain or investigation root sits above the judge's
``chat`` span. Three traces a day arrived as a bare ``chat deepseek-v4-pro``
root that said nothing about which row was judged or what was decided. This
guards the root and what it carries: ids and model names, never the row's
recommendation, the verdict's note or the learning text.

The chat is stubbed at the operator, as in test_reverify; the stub opens a
span of its own, standing in for the real ``chat`` span, so the parent check
exercises the same context mechanism.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reverify as rv  # noqa: E402
from cfshared import tracing  # noqa: E402
from test_reverify import VERDICT_REJECT, _op, _row  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

CANARY = 'zq7-reverify-canary-do-not-trace'


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def _chat_under_its_own_span(response):
    def chat(*args, **kwargs):
        with tracing.span('chat deepseek-v4-pro'):
            return {'response': response}
    return chat


def _every_string(span):
    out = [span.name, span.status.description or '']
    for value in span.attributes.values():
        out.extend(map(str, value) if isinstance(value, tuple) else [str(value)])
    for event in span.events:
        out.append(event.name)
        out.extend(str(v) for v in event.attributes.values())
    return out


def test_a_pass_is_one_trace_with_the_judge_chat_underneath(spans):
    op = _op(providers=('deepseek',), model='deepseek-v4-pro')
    op._chat_with_tools.side_effect = _chat_under_its_own_span(
        VERDICT_REJECT.replace('K3S_URL is already', f'{CANARY} is already'))
    row = _row(payload={**_row()['payload'], 'recommendation': f'Point {CANARY} at the control plane'})

    assert rv.reverify_row(op, row, max_iterations=10) == 'rejected'

    finished = spans.get_finished_spans()
    named = {s.name: s for s in finished}
    root, chat = named['reverify'], named['chat deepseek-v4-pro']
    assert root.parent is None
    assert chat.parent.span_id == root.context.span_id, "the judge's chat is not under reverify"
    assert {s.context.trace_id for s in finished} == {root.context.trace_id}

    a = root.attributes
    assert a['cfop.remediation.id'] == 117
    assert a['cfop.remediation.class'] == 'node-action'
    assert a['cfop.remediation.risk'] == 'low'
    assert a['cfop.reverify.reporter'] == 'ollama/gemma4:26b'
    assert a['cfop.reverify.judge_backend'] == 'deepseek'
    assert a['cfop.reverify.judge_model'] == 'deepseek-v4-pro'
    assert a['cfop.reverify.outcome'] == 'rejected'

    leaks = [(s.name, t) for s in finished for t in _every_string(s) if CANARY in t]
    assert leaks == [], f"row or verdict text reached a span: {leaks}"


def test_no_peer_still_leaves_a_root_that_says_why(spans):
    op = _op(providers=('ollama',))
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    (root,) = spans.get_finished_spans()
    assert root.name == 'reverify'
    assert root.attributes['cfop.reverify.outcome'] == 'skipped'
    assert root.attributes['cfop.reverify.skip_reason'] == 'no_peer'


def test_an_unusable_verdict_is_a_skip_with_its_reason(spans):
    op = _op(providers=('deepseek',), response='not json at all')
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    (root,) = spans.get_finished_spans()
    assert root.attributes['cfop.reverify.outcome'] == 'skipped'
    assert root.attributes['cfop.reverify.skip_reason'] == 'no_verdict'


def test_disabled_tracing_passes_exactly_as_before():
    tracing.reset()
    op = _op(providers=('deepseek',), response=VERDICT_REJECT)
    assert rv.reverify_row(op, _row(), max_iterations=10) == 'rejected'
    op.kb.update_remediation_status.assert_called_once()
