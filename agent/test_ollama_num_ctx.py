#!/usr/bin/env python3
"""Tests for the Ollama context-window handling (CFOP-168).

Investigation #2400: a long tool loop re-sends its whole history every
iteration, fills gemma4:26b's 32k window, the runner clips the prompt and one
turn of cfoperator_llm_latency_seconds runs into minutes. Two levers:

* `_bound_tool_history` — always on — collapses the oldest tool results so the
  whole prompt (history + tool schemas) stays under a fraction of the window;
* `options.num_ctx` — opt-in via llm.primary.num_ctx — because Ollama reloads a
  model whenever a request's num_ctx differs from the loaded runner's, and
  every other client of the model sends none.

Two kinds of test here. The unit tests pin the helpers' contract (oldest-first,
the recent floor, the turn just requested kept whole, schemas counted). The
scripted-loop tests at the bottom drive `_chat_with_tools_inner` through a fake
`/api/chat` and read the bodies it actually sent — they are the guard against
the loop no longer calling the bound, or a site no longer going through the
payload helper; the helper tests alone cannot see that.
"""

import copy
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator

STUB = CFOperator._COLLAPSED_TOOL_RESULT


def _operator(config=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config or {}
    return op


def _with_num_ctx(value):
    return _operator({'llm': {'primary': {'provider': 'ollama', 'num_ctx': value}}})


# --- _ollama_num_ctx ----------------------------------------------------------

def test_num_ctx_is_absent_by_default():
    """No config, no num_ctx: the runner keeps the window Ollama chose."""
    assert _operator({})._ollama_num_ctx() is None
    assert _operator({'llm': {'primary': {'provider': 'ollama'}}})._ollama_num_ctx() is None


def test_num_ctx_honours_llm_primary():
    assert _with_num_ctx(16384)._ollama_num_ctx() == 16384
    assert _with_num_ctx('32768')._ollama_num_ctx() == 32768


def test_num_ctx_floors_but_has_no_ceiling():
    assert _with_num_ctx(512)._ollama_num_ctx() == CFOperator._OLLAMA_NUM_CTX_FLOOR
    assert _with_num_ctx(262144)._ollama_num_ctx() == 262144


def test_num_ctx_garbage_and_empty_mean_absent():
    assert _with_num_ctx('lots')._ollama_num_ctx() is None
    assert _with_num_ctx('')._ollama_num_ctx() is None
    assert _operator({'llm': 'not-a-dict'})._ollama_num_ctx() is None


# --- _ollama_chat_payload -----------------------------------------------------

def test_payload_has_the_ollama_essentials_and_nothing_optional():
    msgs = [{'role': 'user', 'content': 'hi'}]
    payload = _operator({})._ollama_chat_payload('gemma4:26b', msgs, 0.7)
    assert payload == {'model': 'gemma4:26b', 'messages': msgs,
                       'stream': False, 'temperature': 0.7}


def test_payload_passes_format_and_tools_only_when_given():
    op = _operator({})
    msgs = [{'role': 'user', 'content': 'hi'}]
    tools = [{'type': 'function', 'function': {'name': 'k8s_get_pods'}}]
    assert _operator({})._ollama_chat_payload('m', msgs, 0.3, format='json')['format'] == 'json'
    assert op._ollama_chat_payload('m', msgs, 0.7, tools=tools)['tools'] == tools
    # An empty tool list (the final no-tools iteration) is not sent as `tools: []`.
    assert 'tools' not in op._ollama_chat_payload('m', msgs, 0.7, tools=[])
    assert 'format' not in op._ollama_chat_payload('m', msgs, 0.7)


def test_payload_sends_options_only_on_the_opt_in_path():
    """Unset: no `options` at all — the body is what shipped before the key
    existed. Set: num_ctx, and the temperature Ollama otherwise ignores."""
    msgs = [{'role': 'user', 'content': 'hi'}]
    assert 'options' not in _operator({})._ollama_chat_payload('m', msgs, 0.7)
    assert _with_num_ctx(16384)._ollama_chat_payload('m', msgs, 0.3)['options'] == {
        'num_ctx': 16384, 'temperature': 0.3}


# --- _ollama_history_budget ---------------------------------------------------

def test_history_budget_follows_num_ctx_and_falls_back_to_the_default_window():
    assert _operator({})._ollama_history_budget() == int(
        CFOperator._OLLAMA_DEFAULT_WINDOW * CFOperator._OLLAMA_HISTORY_FRACTION)
    assert _with_num_ctx(32768)._ollama_history_budget() == int(32768 * 0.75)


# --- _bound_tool_history ------------------------------------------------------

def _turn(i, calls=1, result_chars=6000):
    """One assistant message asking for `calls` tools, then its results —
    the shape the Ollama branch of the loop appends."""
    msgs = [{'role': 'assistant', 'content': '',
             'tool_calls': [{'function': {'name': f'tool_{i}_{k}', 'arguments': {}}}
                            for k in range(calls)]}]
    for k in range(calls):
        msgs.append({'role': 'tool',
                     'content': json.dumps({'i': i, 'k': k, 'data': 'x' * result_chars})})
    return msgs


def _history(n_turns, result_chars=6000):
    """system + user + n single-call turns."""
    msgs = [{'role': 'system', 'content': 'S' * 400},
            {'role': 'user', 'content': 'investigate the thing'}]
    for i in range(n_turns):
        msgs.extend(_turn(i, result_chars=result_chars))
    return msgs


def _tool_results(msgs):
    return [m for m in msgs if m['role'] == 'tool']


KEEP = CFOperator._OLLAMA_KEEP_RECENT_TOOL_RESULTS


def test_under_budget_history_is_left_alone():
    msgs = _history(3, result_chars=100)
    snapshot = json.dumps(msgs)
    assert CFOperator._bound_tool_history(msgs, budget_tokens=10_000) == 0
    assert json.dumps(msgs) == snapshot


def test_over_budget_history_shrinks_under_budget_oldest_first():
    msgs = _history(10)
    original = [m['content'] for m in _tool_results(msgs)]
    budget = 10_000  # ~15.4k tokens going in; four collapses fit, five are collapsible
    collapsed = CFOperator._bound_tool_history(msgs, budget_tokens=budget)
    assert collapsed > 0
    assert sum(CFOperator._estimate_tokens(m) for m in msgs) <= budget
    results = _tool_results(msgs)
    # The turn just requested and the floor before it are verbatim; the
    # collapsed ones are the oldest.
    assert [m['content'] for m in results[-(KEEP + 1):]] == original[-(KEEP + 1):]
    stubs = [i for i, m in enumerate(results) if m['content'] == STUB]
    assert stubs == list(range(collapsed)), "collapse walks oldest-first and stops when it fits"
    # It stops as soon as it fits — not everything collapsible was touched.
    assert collapsed < len(results) - KEEP - 1


def test_bound_never_touches_system_user_or_assistant_messages():
    msgs = _history(8)
    fixed_before = [m for m in msgs if m['role'] != 'tool']
    CFOperator._bound_tool_history(msgs, budget_tokens=1_000)
    assert [m for m in msgs if m['role'] != 'tool'] == fixed_before
    assert msgs[0]['role'] == 'system' and msgs[1]['role'] == 'user'


def test_bound_never_splits_the_turn_just_requested():
    """One assistant message can ask for several tools; the loop answers all
    of them before the next POST. Those results are what the model asked for
    this turn — they are never the ones collapsed, however big the burst."""
    msgs = _history(8)                 # 8 earlier single-call turns
    msgs.extend(_turn(99, calls=6))    # then a 6-call burst, ~9k tokens on its own
    collapsed = CFOperator._bound_tool_history(msgs, budget_tokens=10)
    results = _tool_results(msgs)
    assert all(m['content'] != STUB for m in results[-6:]), "the burst is kept whole"
    assert all(m['content'] != STUB for m in results[-6 - KEEP:-6]), "floor over earlier turns"
    assert all(m['content'] == STUB for m in results[:8 - KEEP])
    assert collapsed == 8 - KEEP


def test_bound_keeps_the_floor_even_when_it_cannot_fit():
    """Untouchable parts alone exceed the budget: collapse what may be, return."""
    msgs = _history(6)
    collapsed = CFOperator._bound_tool_history(msgs, budget_tokens=10)
    # 6 results: the current turn's one, KEEP earlier ones, the rest collapsible.
    assert collapsed == 6 - KEEP - 1
    results = _tool_results(msgs)
    assert all(m['content'] != STUB for m in results[-(KEEP + 1):])
    # Nothing left to collapse: a second pass is a no-op, not a crash or a re-count.
    assert CFOperator._bound_tool_history(msgs, budget_tokens=10) == 0


def test_bound_counts_the_fixed_part_of_the_request():
    """Tool schemas ride along on every turn; a history that fits on its own
    must still be collapsed when schemas + history do not."""
    msgs = _history(6, result_chars=1000)
    history_tokens = sum(CFOperator._estimate_tokens(m) for m in msgs)
    assert CFOperator._bound_tool_history(list(msgs), budget_tokens=history_tokens) == 0
    assert CFOperator._bound_tool_history(msgs, budget_tokens=history_tokens,
                                          fixed_tokens=4_600) > 0


def test_collapsed_stub_keeps_the_message_shape():
    """A collapsed result is still a tool message the provider accepts, and
    keeps any id the transport needs (OpenAI-shape rows carry tool_call_id)."""
    msgs = _history(6)
    msgs[3]['tool_call_id'] = 'call_0'   # the first turn's result
    CFOperator._bound_tool_history(msgs, budget_tokens=10)
    stub = msgs[3]
    assert stub['role'] == 'tool' and stub['tool_call_id'] == 'call_0'
    assert json.loads(stub['content'])['collapsed'] is True


def test_estimate_tokens_is_chars_over_four():
    assert CFOperator._estimate_tokens('x' * 400) == 100
    assert CFOperator._estimate_tokens(None) == 0
    assert CFOperator._estimate_tokens([]) == len('[]') // 4


# --- the loop itself ----------------------------------------------------------
#
# Same harness shape as test_empty_response_nudge.py: a scripted /api/chat and
# a registry whose tool returns a fat result. These read the bodies the loop
# sent, which is the only place the bound and the payload helper are visible.

TOOL_SCHEMA = {
    'type': 'function',
    'function': {'name': 'fetch_logs', 'description': 'Dump logs',
                 'parameters': {'type': 'object', 'properties': {}}},
}


def _loop_operator(num_ctx=None, result_chars=3000, max_iterations=12):
    op = CFOperator.__new__(CFOperator)
    primary = {'provider': 'ollama'}
    if num_ctx:
        primary['num_ctx'] = num_ctx
    op.config = {'chat': {'max_tool_iterations': max_iterations}, 'llm': {'primary': primary}}
    op.llm_timeout = 5
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {'call': args, 'logs': 'x' * result_chars},
    )
    op.kb = SimpleNamespace(get_setting=lambda *a, **k: '')
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'connection')
    return op


def _ask(i):
    # Distinct arguments per call so the read-only memoizer cannot short-circuit.
    return {'role': 'assistant', 'content': '',
            'tool_calls': [{'function': {'name': 'fetch_logs', 'arguments': {'i': i}}}]}


def _final():
    return {'role': 'assistant',
            'content': 'Loki healthy.\nSTATUS: resolved\nRECOMMENDATION: No action needed'}


class _FakePost:
    """Scripted Ollama /api/chat responses; records each request payload."""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        # A copy, as sent: the loop passes its live message list, which the
        # bound mutates in place, so a reference would make every recorded
        # body read as the final one -- and per-request checks vacuous
        # (CFOP-390).
        self.payloads.append(copy.deepcopy(json))
        body = {'message': self.script.pop(0), 'prompt_eval_count': 10, 'eval_count': 5}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: body)


def _run(op, fake, monkeypatch):
    monkeypatch.setattr('requests.post', fake)
    return op._chat_with_tools_inner(
        provider_type='ollama', url='http://fake:11434', model='gemma4:26b',
        messages=[{'role': 'user', 'content': 'Investigate this alert: x'}],
        system_context='You are CFOperator.')


def _stubs_in(payload):
    return sum(1 for m in payload['messages'] if m.get('role') == 'tool' and m['content'] == STUB)


def test_loop_collapses_older_results_before_the_next_post(monkeypatch):
    """Eight 3000-char results against a 2048 window (budget 1536 tokens):
    from the point the history no longer fits, the bodies the loop sends carry
    collapsed stubs for the oldest results — and never for the newest."""
    op = _loop_operator(num_ctx=2048)
    fake = _FakePost([_ask(i) for i in range(8)] + [_final()])
    result = _run(op, fake, monkeypatch)
    assert 'STATUS: resolved' in result['response'] and result['tool_calls'] == 8
    stubs = [_stubs_in(p) for p in fake.payloads]
    assert stubs[-1] > 0, "the bound never ran at the loop site"
    assert stubs == sorted(stubs), "a collapsed result stays collapsed"
    for p in fake.payloads:
        tools = [m for m in p['messages'] if m.get('role') == 'tool']
        if tools:
            assert tools[-1]['content'] != STUB, "the result just fetched is always whole"
    # Every body went through the helper: num_ctx and temperature in options.
    assert all(p['options'] == {'num_ctx': 2048, 'temperature': 0.7} for p in fake.payloads)


def test_loop_leaves_a_short_investigation_alone(monkeypatch):
    """Three small results under the default window: nothing collapsed, and
    the body is byte-for-byte the pre-CFOP-168 shape (no `options`)."""
    op = _loop_operator(result_chars=200)
    fake = _FakePost([_ask(i) for i in range(3)] + [_final()])
    result = _run(op, fake, monkeypatch)
    assert 'STATUS: resolved' in result['response']
    assert all(_stubs_in(p) == 0 for p in fake.payloads)
    assert all('options' not in p for p in fake.payloads)
    assert all(p['stream'] is False and p['model'] == 'gemma4:26b' for p in fake.payloads)


# --- CFOP-390: the window the runner has, and collapsing with hysteresis -----
#
# gemma4 uses sliding-window attention, so llama.cpp cannot resume a cached
# prompt from a partial prefix: any change to an earlier message re-processes
# the whole prompt (~3.5s at 13k tokens vs ~0.25s cached, measured live). The
# bound changed an earlier message on nearly every turn -- its 16k budget sat
# below what it may never collapse -- so these tests count PROMPT-CHANGING
# TURNS, the number that costs time.

class _FakePs:
    """Ollama /api/ps; records calls."""

    def __init__(self, models=None, raises=None):
        self.models, self.raises, self.calls = models or [], raises, []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if self.raises:
            raise self.raises
        return SimpleNamespace(status_code=200, json=lambda: {'models': self.models})


def _loaded(ctx, name='gemma4:26b'):
    return _FakePs([{'name': name, 'model': name, 'context_length': ctx}])


def test_budget_follows_the_loaded_runner_when_num_ctx_is_unset(monkeypatch):
    ps = _loaded(32768)
    monkeypatch.setattr('requests.get', ps)
    op = _operator({})
    assert op._ollama_history_budget('http://llm:11434', 'gemma4:26b') == int(32768 * 0.75)
    op._ollama_history_budget('http://llm:11434', 'gemma4:26b')
    assert len(ps.calls) == 1, "the runner's window is cached, not asked every turn"


def test_a_configured_num_ctx_wins_without_asking(monkeypatch):
    ps = _FakePs(raises=AssertionError('must not be asked'))
    monkeypatch.setattr('requests.get', ps)
    assert _with_num_ctx(16384)._ollama_history_budget('http://llm:11434', 'gemma4:26b') == 12288
    assert ps.calls == []


def test_an_unknown_window_falls_back_to_the_default(monkeypatch):
    default = int(CFOperator._OLLAMA_DEFAULT_WINDOW * CFOperator._OLLAMA_HISTORY_FRACTION)
    for ps in (_FakePs([]),                                   # model not loaded
               _loaded(8192, name='other:7b'),                # a different model
               _FakePs(raises=ConnectionError('down'))):      # server unreachable
        monkeypatch.setattr('requests.get', ps)
        assert _operator({})._ollama_history_budget('http://llm:11434', 'gemma4:26b') == default


def _simulate(turns, budget, low_water):
    """The live shape: ~2k system prompt, 4.6k of schemas, a 6000-char
    result per turn. Returns how many turns changed an earlier message."""
    msgs = [{'role': 'system', 'content': 's' * 8000}, {'role': 'user', 'content': 'u' * 400}]
    changed = 0
    for i in range(turns):
        msgs += [_ask(i), {'role': 'tool', 'content': 'r' * 6000}]
        before = [dict(m) for m in msgs]
        CFOperator._bound_tool_history(msgs, budget, fixed_tokens=4600, low_water_tokens=low_water)
        changed += msgs != before
    return changed


def test_hysteresis_leaves_the_prompt_unchanged_for_turns_between_collapses():
    """A 28-turn investigation against the live 32k runner: without
    hysteresis every turn past the budget is a prompt change; with it, a
    handful."""
    budget = int(32768 * CFOperator._OLLAMA_HISTORY_FRACTION)
    with_hysteresis = _simulate(28, budget, int(budget * CFOperator._OLLAMA_LOW_WATER_FRACTION))
    stop_at_budget = _simulate(28, budget, None)
    assert with_hysteresis <= 5, with_hysteresis
    assert stop_at_budget >= 12, stop_at_budget


def test_the_loop_budgets_against_the_runner_and_counts_prompt_changes(monkeypatch):
    """Through _chat_with_tools_inner: the budget comes from /api/ps, and the
    number of request bodies that are NOT a plain extension of the previous
    one equals history_collapses -- the figure the chat span reports."""
    monkeypatch.setattr('requests.get', _loaded(8192))
    op = _loop_operator(result_chars=3000, max_iterations=14)
    fake = _FakePost([_ask(i) for i in range(12)] + [_final()])
    result = _run(op, fake, monkeypatch)
    assert result['history_budget'] == int(8192 * 0.75)
    msgs = [p['messages'] for p in fake.payloads]
    rewritten = sum(1 for prev, cur in zip(msgs, msgs[1:]) if cur[:len(prev)] != prev)
    assert result['history_collapses'] == rewritten
    assert 1 <= rewritten <= 4, f"{rewritten} prompt rewrites in {len(msgs)} requests"


# --- chat.history: the tunables are configuration, not constants -------------

def test_history_tunables_come_from_chat_history(monkeypatch):
    monkeypatch.setattr('requests.get', _FakePs([]))
    op = _operator({'chat': {'history': {'budget_fraction': 0.5, 'low_water_fraction': 0.4,
                                         'keep_recent': 2, 'fallback_window': 8192,
                                         'runner_window_ttl_seconds': 60}}})
    s = op._history_settings()
    assert s == {'budget_fraction': 0.5, 'low_water_fraction': 0.4, 'keep_recent': 2,
                 'fallback_window': 8192, 'runner_window_ttl_seconds': 60}
    assert op._ollama_history_budget('http://llm:11434', 'gemma4:26b') == 4096


def test_history_tunables_are_clamped_and_garbage_is_ignored():
    s = _operator({'chat': {'history': {'budget_fraction': 5, 'low_water_fraction': 'lots',
                                        'keep_recent': -3, 'fallback_window': 100}}})._history_settings()
    assert s['budget_fraction'] == 0.95
    assert s['low_water_fraction'] == CFOperator._OLLAMA_LOW_WATER_FRACTION
    assert s['keep_recent'] == 0
    assert s['fallback_window'] == CFOperator._OLLAMA_NUM_CTX_FLOOR


def test_unset_history_tunables_keep_todays_numbers():
    s = _operator({})._history_settings()
    assert s == {'budget_fraction': 0.75, 'low_water_fraction': 0.6, 'keep_recent': 4,
                 'fallback_window': 16384, 'runner_window_ttl_seconds': 300}


def test_chat_history_reaches_the_agent_through_load_config(tmp_path, monkeypatch):
    """Through the real loader and merge, not a hand-built dict."""
    from cfshared import config as shared_config
    path = tmp_path / 'config.yaml'
    path.write_text('chat:\n  history:\n    low_water_fraction: 0.5\n    keep_recent: 3\n')
    op = _operator(shared_config.load_config(str(path)))
    s = op._history_settings()
    assert (s['low_water_fraction'], s['keep_recent'], s['budget_fraction']) == (0.5, 3, 0.75)


def test_the_loop_passes_keep_recent_through(monkeypatch):
    """keep_recent 1 lets the bound collapse more than the default 4 would."""
    monkeypatch.setattr('requests.get', _loaded(4096))
    stubs = {}
    for keep in (1, 4):
        op = _loop_operator(result_chars=3000, max_iterations=10)
        op.config['chat']['history'] = {'keep_recent': keep}
        fake = _FakePost([_ask(i) for i in range(8)] + [_final()])
        _run(op, fake, monkeypatch)
        stubs[keep] = _stubs_in(fake.payloads[-1])
    assert stubs[1] > stubs[4], stubs


def test_a_cold_model_is_asked_again_once_it_has_loaded(monkeypatch):
    """CodeRabbit on #330: on a cold start /api/ps does not list the model
    until the loop's first request loads it. "Not listed" must not be
    cached, or the rest of the loop budgets on the fallback window."""
    answers = [[], [{'name': 'gemma4:26b', 'model': 'gemma4:26b', 'context_length': 32768}]]

    def ps(url, timeout=None):
        models = answers.pop(0) if answers else answers_last
        return SimpleNamespace(status_code=200, json=lambda: {'models': models})
    answers_last = [{'name': 'gemma4:26b', 'model': 'gemma4:26b', 'context_length': 32768}]
    monkeypatch.setattr('requests.get', ps)
    op = _operator({})
    fallback = int(CFOperator._OLLAMA_DEFAULT_WINDOW * CFOperator._OLLAMA_HISTORY_FRACTION)
    assert op._ollama_history_budget('http://llm:11434', 'gemma4:26b') == fallback   # cold
    assert op._ollama_history_budget('http://llm:11434', 'gemma4:26b') == int(32768 * 0.75)


def test_an_unreachable_server_is_not_asked_every_turn(monkeypatch):
    ps = _FakePs(raises=ConnectionError('down'))
    monkeypatch.setattr('requests.get', ps)
    op = _operator({})
    op._ollama_history_budget('http://llm:11434', 'gemma4:26b')
    op._ollama_history_budget('http://llm:11434', 'gemma4:26b')
    assert len(ps.calls) == 1

