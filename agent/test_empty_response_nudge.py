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
                             record_failure=lambda *a: None)
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
