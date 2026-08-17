#!/usr/bin/env python3
"""Tests for the empty-final-response nudge in the tool-calling loop.

gemma4:26b routinely ends investigations with a message that has neither
tool calls nor text (benchmarks/empty_response_sim.py: 10/10 on a healthy
cluster). The loop used to return that '' verbatim, and _extract_status('')
silently classified it as 'monitoring' (investigations #1880/#1884/#1885/
#1889). These cover the mitigation: one nudge retry with a bonus round,
then EmptyLLMResponseError so the provider fallback chain rotates.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator, EMPTY_RESPONSE_NUDGE, EmptyLLMResponseError

TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': 'k8s_get_pods',
        'description': 'List pods',
        'parameters': {'type': 'object', 'properties': {}},
    },
}


def _operator(max_iterations=5):
    op = CFOperator.__new__(CFOperator)
    op.config = {'chat': {'max_tool_iterations': max_iterations}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {'pods': [{'name': 'loki-0', 'status': 'Running'}]},
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


def _ollama_msg(content='', tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = [
            {'function': {'name': n, 'arguments': {}}} for n in tool_calls]
    return message


class _FakePost:
    """Scripted Ollama /api/chat responses; records each request payload."""

    def __init__(self, scripts):
        # scripts: model -> list of message dicts, consumed in order
        self.scripts = {m: list(s) for m, s in scripts.items()}
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        message = self.scripts[json['model']].pop(0)
        body = {'message': message, 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200,
                               raise_for_status=lambda: None,
                               json=lambda: body)


def _run(op, fake, monkeypatch, model='gemma4:26b'):
    monkeypatch.setattr('requests.post', fake)
    return op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model=model,
        messages=[{'role': 'user', 'content': 'Investigate this alert: x'}],
        system_context='You are CFOperator.')


def test_empty_final_is_nudged_once_and_recovers(monkeypatch):
    op = _operator()
    fake = _FakePost({'gemma4:26b': [
        _ollama_msg(tool_calls=['k8s_get_pods']),
        _ollama_msg(content=''),  # empty final — must trigger the nudge
        _ollama_msg(content='All healthy.\nSTATUS: resolved\nRECOMMENDATION: No action needed'),
    ]})
    result = _run(op, fake, monkeypatch)
    assert 'STATUS: resolved' in result['response']
    assert result['tool_calls'] == 1
    # the nudge went out as a user message in the third request
    nudges = [m for m in fake.payloads[-1]['messages']
              if m.get('role') == 'user' and m.get('content') == EMPTY_RESPONSE_NUDGE]
    assert len(nudges) == 1


def test_empty_after_nudge_raises_for_fallback(monkeypatch):
    op = _operator()
    fake = _FakePost({'gemma4:26b': [
        _ollama_msg(content=''),
        _ollama_msg(content='   '),  # whitespace-only counts as empty
    ]})
    with pytest.raises(EmptyLLMResponseError):
        _run(op, fake, monkeypatch)


def test_empty_on_final_iteration_gets_bonus_round(monkeypatch):
    # The common gemma4 shape: tools every round, then empty on the forced
    # tool-less final round. The nudge must grant one round PAST the cap.
    op = _operator(max_iterations=2)
    fake = _FakePost({'gemma4:26b': [
        _ollama_msg(tool_calls=['k8s_get_pods']),
        _ollama_msg(content=''),  # final round (tools withheld) — empty
        _ollama_msg(content='Loki healthy.\nSTATUS: resolved\nRECOMMENDATION: No action needed'),
    ]})
    result = _run(op, fake, monkeypatch)
    assert 'STATUS: resolved' in result['response']
    # rounds 2 and 3 are at/past the cap, so tools must be withheld
    assert 'tools' in fake.payloads[0]
    assert 'tools' not in fake.payloads[1]
    assert 'tools' not in fake.payloads[2]


def test_persistent_empty_rotates_provider_chain(monkeypatch):
    op = _operator()
    op._get_provider_chain = lambda backend='auto', model=None: [
        ('ollama', 'http://fake:11434', 'gemma4:26b'),
        ('ollama', 'http://fake:11434', 'qwen3.6:27b'),
    ]
    fake = _FakePost({
        'gemma4:26b': [_ollama_msg(content=''), _ollama_msg(content='')],
        'qwen3.6:27b': [
            _ollama_msg(content='No 503s found.\nSTATUS: resolved\nRECOMMENDATION: No action needed'),
        ],
    })
    monkeypatch.setattr('requests.post', fake)
    result = op._chat_with_tools_with_fallback(
        messages=[{'role': 'user', 'content': 'Investigate this alert: x'}],
        system_context='You are CFOperator.')
    assert result['model'] == 'qwen3.6:27b'
    assert result['fallback_used'] is True
    assert 'STATUS: resolved' in result['response']


def test_nonempty_response_unaffected(monkeypatch):
    op = _operator()
    fake = _FakePost({'gemma4:26b': [
        _ollama_msg(content='Fine.\nSTATUS: resolved\nRECOMMENDATION: No action needed'),
    ]})
    result = _run(op, fake, monkeypatch)
    assert result['response'].startswith('Fine.')
    assert all(EMPTY_RESPONSE_NUDGE != m.get('content')
               for p in fake.payloads for m in p['messages'])


# ---- LLM_EMPTY_FINALS counter (CFOP-28) -----------------------------------
#
# The nudge above recovers the turn silently, so an operator choosing between
# local models had no way to see which of them needs the second prompt. These
# guard the *shape* of that counter, not a number: a first empty and a second
# empty must land on distinct series, and the series must be per provider and
# per model. Each test uses its own model label and asserts a delta, so it is
# immune to increments from the other tests sharing this process.


def _empty_finals(model, disposition, provider='ollama'):
    value = REGISTRY.get_sample_value(
        'cfoperator_llm_empty_final_responses_total',
        {'provider': provider, 'model': model, 'disposition': disposition})
    return value or 0.0


def test_first_empty_counts_as_nudged():
    model = 'counter-probe-first'
    before_nudged = _empty_finals(model, 'nudged')
    before_exhausted = _empty_finals(model, 'exhausted')

    sent, budget = CFOperator._handle_empty_final(
        False, 1, 5, [], 'ollama', model)

    assert sent is True
    assert budget == 2
    assert _empty_finals(model, 'nudged') == before_nudged + 1
    # A recovered formatting quirk must not be recorded as a model failure.
    assert _empty_finals(model, 'exhausted') == before_exhausted


def test_second_empty_counts_distinctly_as_exhausted():
    model = 'counter-probe-second'
    before_nudged = _empty_finals(model, 'nudged')
    before_exhausted = _empty_finals(model, 'exhausted')

    with pytest.raises(EmptyLLMResponseError):
        CFOperator._handle_empty_final(True, 1, 5, [], 'ollama', model)

    assert _empty_finals(model, 'exhausted') == before_exhausted + 1
    # The give-up must not inflate the "nudge absorbed it" series — that is
    # the whole point of splitting them.
    assert _empty_finals(model, 'nudged') == before_nudged


def test_empty_finals_are_labelled_per_provider_and_model():
    """An empty final for one model/provider must not show up under another."""
    mine, other = 'counter-probe-mine', 'counter-probe-other'
    before_other = _empty_finals(other, 'nudged')
    before_other_provider = _empty_finals(mine, 'nudged', provider='groq')
    before_mine = _empty_finals(mine, 'nudged')

    CFOperator._handle_empty_final(False, 1, 5, [], 'ollama', mine)

    assert _empty_finals(mine, 'nudged') == before_mine + 1
    assert _empty_finals(other, 'nudged') == before_other
    assert _empty_finals(mine, 'nudged', provider='groq') == before_other_provider


def test_tool_loop_reaches_the_counter(monkeypatch):
    """The chokepoint must stay wired to the real loop, not just be callable."""
    model = 'counter-probe-loop'
    op = _operator()
    fake = _FakePost({model: [
        _ollama_msg(content=''),
        _ollama_msg(content='Healthy.\nSTATUS: resolved\nRECOMMENDATION: No action needed'),
    ]})
    before = _empty_finals(model, 'nudged')

    result = _run(op, fake, monkeypatch, model=model)

    assert 'STATUS: resolved' in result['response']
    assert _empty_finals(model, 'nudged') == before + 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
