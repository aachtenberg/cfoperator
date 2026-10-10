#!/usr/bin/env python3
"""How a tool loop ends, and what an investigation does with it (CFOP-271).

The loop is shared by sweeps, investigations and chat, but its end-of-budget
paths asked for the sweep's findings array. An investigation cut off by the
budget then had no STATUS line, _extract_status fell back to 'monitoring', and
the run was stored, embedded and recalled as if the model had judged the alert
worth watching. Nothing recorded that the budget ran out, and nothing stopped
a model that kept repeating the same call.

These drive _chat_with_tools_inner with scripted Ollama replies, and _act with
a scripted loop result.
"""

import inspect
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator

SCHEMAS = [
    {'type': 'function', 'function': {
        'name': n, 'description': n, 'parameters': {'type': 'object', 'properties': {}}}}
    for n in ('k8s_get_pods', 'k8s_get_events')
]
VERDICT = 'Loki is fine.\nSTATUS: resolved\nRECOMMENDATION: No action needed'
CALLER_MSG = 'Investigate this alert: loki-0 restarting'
# What the sweep's array request looks like. None of it may come from the loop
# itself, whichever caller is running it.
SWEEP_WORDING = re.compile(r'JSON array|"severity"|severity.*finding', re.I | re.S)


def _operator(max_iterations=10, stagnation_repeats=None):
    op = CFOperator.__new__(CFOperator)
    chat = {'max_tool_iterations': max_iterations}
    if stagnation_repeats is not None:
        chat['stagnation_repeats'] = stagnation_repeats
    op.config = {'chat': chat}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(get_schemas=lambda: SCHEMAS,
                               execute=lambda name, args: {'ok': name})
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    return op


def _calls(*names):
    return {'role': 'assistant', 'content': '',
            'tool_calls': [{'function': {'name': n, 'arguments': {}}} for n in names]}


def _text(content):
    return {'role': 'assistant', 'content': content}


class _FakePost:
    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        body = {'message': self.script.pop(0), 'prompt_eval_count': 1, 'eval_count': 1}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: body)


def _run(op, script, monkeypatch, max_iterations=None):
    fake = _FakePost(script)
    monkeypatch.setattr('requests.post', fake)
    result = op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='gemma4:26b',
        messages=[{'role': 'user', 'content': CALLER_MSG}],
        system_context='You are CFOperator. End with STATUS: and RECOMMENDATION:.',
        max_iterations=max_iterations)
    return result, fake


# ---- stop_reason ------------------------------------------------------------


def test_model_that_stops_on_its_own_is_answered(monkeypatch):
    result, _ = _run(_operator(), [_calls('k8s_get_pods'), _text(VERDICT)], monkeypatch)
    assert result['stop_reason'] == 'answered'


def test_one_shot_call_is_answered_not_cap(monkeypatch):
    # max_iterations=1 withholds tools on the first turn by design (triage,
    # classifiers). That is not a budget running out.
    result, _ = _run(_operator(), [_text(VERDICT)], monkeypatch, max_iterations=1)
    assert result['stop_reason'] == 'answered'


def test_answer_forced_on_the_final_turn_is_cap(monkeypatch):
    script = [_calls('k8s_get_pods'), _calls('k8s_get_events'), _text(VERDICT)]
    result, fake = _run(_operator(max_iterations=3), script, monkeypatch)
    assert result['stop_reason'] == 'cap'
    assert 'tools' not in fake.payloads[-1]


def test_summary_after_the_loop_is_cap_and_keeps_the_callers_question(monkeypatch):
    # The final turn still came back with tool calls, so the post-loop
    # summary call runs.
    script = [_calls('k8s_get_pods'), _calls('k8s_get_events'), _text(VERDICT)]
    result, fake = _run(_operator(max_iterations=2), script, monkeypatch)
    assert result['stop_reason'] == 'cap'
    summary = fake.payloads[-1]['messages']
    assert any(m['content'] == CALLER_MSG for m in summary)


def test_mid_loop_refusal_is_error(monkeypatch):
    # A non-transport failure after the first turn returns its error text as
    # the response (investigation #3040's shape); the result must say so.
    class _Refusal(_FakePost):
        def __call__(self, url, json=None, headers=None, timeout=None):
            if self.payloads:
                raise ValueError('400 Client Error: Bad Request')
            return super().__call__(url, json=json)

    fake = _Refusal([_calls('k8s_get_pods')])
    monkeypatch.setattr('requests.post', fake)
    result = _operator()._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='gemma4:26b',
        messages=[{'role': 'user', 'content': CALLER_MSG}], system_context='x')
    assert result['response'].startswith('Error during tool execution')
    assert result['stop_reason'] == 'error'


# ---- the loop names no answer format -----------------------------------------


def test_summary_call_does_not_ask_for_the_sweep_array(monkeypatch):
    script = [_calls('k8s_get_pods'), _calls('k8s_get_events'), _text(VERDICT)]
    _, fake = _run(_operator(max_iterations=2), script, monkeypatch)
    added = [m['content'] for m in fake.payloads[-1]['messages']
             if m['role'] == 'user' and m['content'] != CALLER_MSG]
    assert added
    for content in added:
        assert not SWEEP_WORDING.search(content), content


def test_loop_source_names_no_answer_format():
    # Every provider branch, including the OpenAI-compatible final nudge this
    # file does not drive: the format belongs to the caller's system prompt.
    src = inspect.getsource(CFOperator._chat_with_tools_inner)
    assert not SWEEP_WORDING.search(src)


# ---- stagnation --------------------------------------------------------------


def test_repeating_one_call_stops_before_the_cap(monkeypatch):
    script = [_calls('k8s_get_pods')] * 4 + [_text(VERDICT)]
    result, fake = _run(_operator(max_iterations=10), script, monkeypatch)
    assert result['stop_reason'] == 'stagnation'
    assert len(fake.payloads) == 5
    assert 'tools' not in fake.payloads[-1]  # the forced final turn


def test_alternating_repeats_are_stagnation_too(monkeypatch):
    script = [_calls('k8s_get_pods'), _calls('k8s_get_events'),
              _calls('k8s_get_pods'), _calls('k8s_get_events'),
              _calls('k8s_get_pods'), _text(VERDICT)]
    result, fake = _run(_operator(max_iterations=10), script, monkeypatch)
    assert result['stop_reason'] == 'stagnation'
    assert len(fake.payloads) == 6


def test_a_new_call_resets_the_streak():
    stats = sys.modules[CFOperator.__module__]._ToolLoopStats()
    for name, args in [('a', {}), ('a', {}), ('a', {}), ('b', {}), ('a', {})]:
        stats.note_call(name, args)
    assert stats.repeat_streak == 1
    stats.note_call('a', {'x': 1})  # different args: a new call
    assert stats.repeat_streak == 0


def test_stagnation_check_can_be_turned_off(monkeypatch):
    script = [_calls('k8s_get_pods')] * 5 + [_text(VERDICT)]
    result, fake = _run(_operator(max_iterations=6, stagnation_repeats=0),
                        script, monkeypatch)
    assert result['stop_reason'] == 'cap'
    assert len(fake.payloads) == 6


# ---- what an investigation stores --------------------------------------------


def _act(response, stop_reason):
    captured, embedded = {}, []
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    op.kb = SimpleNamespace(
        start_investigation=lambda trigger, alert_id=None: 77,
        update_investigation=lambda **kw: captured.update(kw) or True)
    op._noise_config = lambda: {'enabled': False}
    op._chat_with_tools_with_fallback = lambda **kw: {
        'backend': 'ollama', 'model': 'demo', 'response': response,
        'tool_calls': 30, 'stop_reason': stop_reason}
    op._verify_investigation_outcome = lambda outcome, alert, trigger: (outcome, '')
    op._maybe_propose_remediation = lambda *a, **k: None
    op._embed_investigation = lambda *a, **k: embedded.append(a)
    op._act({'trigger': 'loki-0 restarting', 'alert': {},
             'known_learnings': [], 'similar_investigations': []})
    return captured, embedded


def test_cap_without_status_is_failed_not_monitoring():
    captured, embedded = _act('Checked pods and events; still looking.', 'cap')
    assert captured['outcome'] == 'failed'
    assert captured['findings']['stop_reason'] == 'cap'
    assert 'no verdict' in captured['findings']['error']
    assert embedded == []


def test_error_text_is_failed_not_monitoring():
    # Investigation #3040: a Groq 400's error text, stored as 'monitoring'.
    captured, _ = _act('Error during tool execution: 400 Client Error: Bad Request', 'error')
    assert captured['outcome'] == 'failed'


def test_forced_answer_with_status_keeps_its_verdict():
    captured, embedded = _act('Pod is flapping.\nSTATUS: monitoring\nRECOMMENDATION: watch it',
                              'stagnation')
    assert captured['outcome'] == 'monitoring'
    assert captured['findings']['stop_reason'] == 'stagnation'
    assert 'error' not in captured['findings']
    assert embedded


def test_answered_run_records_no_stop_reason():
    captured, _ = _act(VERDICT, 'answered')
    assert captured['outcome'] == 'resolved'
    assert 'stop_reason' not in captured['findings']
