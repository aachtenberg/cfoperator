#!/usr/bin/env python3
"""Console Gemini looked hung; it was never answering (CFOP-112).

Two stacked failures, both live on 2026-08-27/28. Picking Gemini in the chip
stores ``selected_backend`` and nothing else, the registry deliberately gives
Gemini no default model (#199/#201), and ``_resolve_provider`` handed the
chain ``('gemini', None, '')`` — Google answered 400 in 150 ms, 31 times, and
gemma4 quietly did the work under a chip that still said Gemini. Once a model
was picked from Google's listing it was stored as ``models/gemini-…``, and a
Gemini Pro's thinking on iteration 1 ran into the 120 s read timeout, which
the tool loop turned into "Error during tool execution" instead of rotating
the chain.

These pin the four fixes: a hosted backend with no model is not callable;
the skip is announced on the ``fallback`` event; the registry's namespace
prefix comes off on resolve; Gemini's request parameters come from the
registry; a transport failure on any iteration propagates — unless a
mutating tool already ran in that loop.
"""

import os
import sys
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator, OPENAI_COMPAT_PROVIDERS, normalize_model_id

TOOL = 'k8s_get_pods'
TOOL_SCHEMA = {
    'type': 'function',
    'function': {'name': TOOL, 'description': 'List pods',
                 'parameters': {'type': 'object', 'properties': {}}},
}
ALL_KEYS = [cfg['key_env'] for cfg in OPENAI_COMPAT_PROVIDERS.values()] + ['ANTHROPIC_API_KEY']


class _KB:
    def __init__(self, **settings):
        self.settings = dict(settings)

    def get_setting(self, name, default=''):
        return self.settings.get(name, default)


def _operator(max_iterations=4, **settings):
    op = CFOperator.__new__(CFOperator)
    op.kb = _KB(**settings)
    op.config = {
        'llm': {'primary': {'url': 'http://ollama:11434', 'model': 'gemma4:26b'}},
        'chat': {'max_tool_iterations': max_iterations},
    }
    op.llm_timeout = 5
    op.llm = SimpleNamespace(record_success=lambda *a: None,
                             record_failure=lambda *a: None,
                             classify_error=lambda *a, **k: 'timeout',
                             get_next_provider=lambda: None)
    op.tools = SimpleNamespace(get_schemas=lambda: [TOOL_SCHEMA],
                               execute=lambda name, args: {'pods': []})
    return op


@pytest.fixture
def no_keys(monkeypatch):
    for env in ALL_KEYS:
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def _compat_msg(content='', tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = [
            {'id': f'call_{i}', 'type': 'function',
             'function': {'name': n, 'arguments': '{}'}}
            for i, n in enumerate(tool_calls)]
    return message


class _FakePost:
    """Scripted chat/completions responses per model; an Exception item is raised."""

    def __init__(self, scripts):
        self.scripts = {m: list(s) for m, s in scripts.items()}
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        item = self.scripts[json['model']].pop(0)
        if isinstance(item, Exception):
            raise item
        body = {'choices': [{'message': item}], 'usage': {}}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: body)


def _run_inner(op, fake, monkeypatch, provider='gemini', model='gemini-3.6-flash'):
    monkeypatch.setattr('requests.post', fake)
    return op._chat_with_tools_inner(
        provider_type=provider, url=None, model=model,
        messages=[{'role': 'user', 'content': 'is loki healthy?'}],
        system_context='You are CFOperator.')


# ---- 1. a hosted backend with no model is not callable ---------------------


def test_hosted_backend_with_no_model_is_not_callable(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator(selected_backend='gemini')
    # Nothing names a Gemini model: not the console, not llm.fallback, not
    # the registry (deliberately). Before: ('gemini', None, '') and a 400.
    assert op._resolve_provider('gemini') is None
    assert op._resolve_provider('anthropic') is None
    # The registry default still makes deepseek callable on a key alone.
    assert op._resolve_provider('deepseek') == ('deepseek', None, 'deepseek-v4-pro')


def test_console_selection_resolves_and_loses_the_listing_prefix(no_keys):
    op = _operator(selected_backend='gemini',
                   gemini_selected_model='models/gemini-3.6-flash')
    # The live selection was stored with Google's prefix before the strip;
    # it must keep resolving, and go out bare.
    assert op._resolve_provider('gemini') == ('gemini', None, 'gemini-3.6-flash')
    assert op._resolve_provider('auto') == ('gemini', None, 'gemini-3.6-flash')
    # An explicit override is normalised the same way.
    assert op._resolve_provider('gemini', model='models/gemini-3.7-flash') == \
        ('gemini', None, 'gemini-3.7-flash')


# ---- 2. the skip is announced, not silent ----------------------------------


def _events_of(op, backend='auto'):
    events = []
    op._chat_with_tools = lambda **kw: {'response': 'ok', 'tool_calls': 0}
    result = op._chat_with_tools_with_fallback(
        messages=[{'role': 'user', 'content': 'x'}], backend=backend,
        event_callback=lambda kind, data: events.append((kind, data)))
    return result, events


def test_chain_skips_a_selected_backend_with_no_model_and_says_why(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator(selected_backend='gemini')
    chain = op._get_provider_chain('auto')
    assert chain and chain[0][0] == 'ollama'
    assert all(p[0] != 'gemini' for p in chain)

    result, events = _events_of(op)
    assert result['backend'] == 'ollama'
    assert events and events[0][0] == 'fallback'
    assert events[0][1]['from'] == 'gemini/(not callable)'
    assert events[0][1]['to'] == 'ollama/gemma4:26b'
    assert 'no model selected' in events[0][1]['reason']


def test_chain_skips_a_selected_backend_with_no_key_and_names_the_variable(no_keys):
    op = _operator(selected_backend='gemini', gemini_selected_model='gemini-3.6-flash')
    _, events = _events_of(op)
    assert events[0][0] == 'fallback'
    assert events[0][1]['reason'] == 'GEMINI_API_KEY not set'


def test_a_usable_selection_is_the_chain_head_with_no_announcement(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator(selected_backend='gemini', gemini_selected_model='gemini-3.6-flash')
    assert op._get_provider_chain('auto')[0] == ('gemini', None, 'gemini-3.6-flash')
    result, events = _events_of(op)
    assert result['backend'] == 'gemini'
    assert [e for e in events if e[0] == 'fallback'] == []


# ---- 3. the namespace prefix is the registry's, not a gemini branch --------


def test_normalize_model_id_strips_only_the_owning_providers_prefix():
    assert normalize_model_id('gemini', 'models/gemini-3.6-flash') == 'gemini-3.6-flash'
    assert normalize_model_id('gemini', 'gemini-3.6-flash') == 'gemini-3.6-flash'
    # Another provider's ids are not touched by gemini's prefix.
    assert normalize_model_id('deepseek', 'models/deepseek-v4-pro') == 'models/deepseek-v4-pro'
    assert normalize_model_id('ollama', '  qwen3:14b ') == 'qwen3:14b'
    assert normalize_model_id('gemini', None) == ''


def test_every_declared_prefix_is_a_namespace():
    # The guard for the class: a provider that declares a prefix declares a
    # namespace separator, so stripping it cannot eat part of a model name.
    for backend, cfg in OPENAI_COMPAT_PROVIDERS.items():
        prefix = cfg.get('model_id_prefix')
        if prefix is not None:
            assert prefix and prefix.endswith('/'), backend


# ---- 4. request parameters come from the registry --------------------------


def test_gemini_requests_carry_the_registry_params_and_others_do_not(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    no_keys.setenv('DEEPSEEK_API_KEY', 'k')
    op = _operator()

    fake = _FakePost({'gemini-3.6-flash': [_compat_msg('Loki is healthy.')]})
    result = _run_inner(op, fake, no_keys)
    assert result['response'] == 'Loki is healthy.'
    payload = fake.payloads[0]
    assert payload['reasoning_effort'] == 'low'
    assert payload['max_tokens'] == 16384

    fake = _FakePost({'deepseek-v4-pro': [_compat_msg('Loki is healthy.')]})
    _run_inner(op, fake, no_keys, provider='deepseek', model='deepseek-v4-pro')
    payload = fake.payloads[0]
    assert 'reasoning_effort' not in payload
    assert payload['max_tokens'] == 4096


# ---- 5. a transport failure on any iteration rotates the chain -------------


def test_read_timeout_after_a_tool_call_propagates(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator()
    fake = _FakePost({'gemini-3.6-flash': [
        _compat_msg(tool_calls=[TOOL]),
        requests.exceptions.ReadTimeout('Read timed out. (read timeout=120)'),
    ]})
    with pytest.raises(requests.exceptions.ReadTimeout):
        _run_inner(op, fake, no_keys)


def test_mid_loop_timeout_reaches_the_next_provider(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    no_keys.setenv('DEEPSEEK_API_KEY', 'k')
    op = _operator()
    op._get_provider_chain = lambda backend='auto', model=None: [
        ('gemini', None, 'gemini-3.6-flash'),
        ('deepseek', None, 'deepseek-v4-pro'),
    ]
    fake = _FakePost({
        'gemini-3.6-flash': [_compat_msg(tool_calls=[TOOL]),
                             requests.exceptions.ReadTimeout('read timeout=120')],
        'deepseek-v4-pro': [_compat_msg('from deepseek')],
    })
    no_keys.setattr('requests.post', fake)
    events = []
    result = op._chat_with_tools_with_fallback(
        messages=[{'role': 'user', 'content': 'is loki healthy?'}],
        system_context='You are CFOperator.',
        event_callback=lambda kind, data: events.append((kind, data)))
    assert result['response'] == 'from deepseek'
    assert result['backend'] == 'deepseek'
    assert result['fallback_used'] is True
    # The next provider starts from the caller's messages: the partial
    # gemini loop (its tool call and result) is not carried across.
    roles = [m['role'] for m in fake.payloads[-1]['messages']]
    assert 'tool' not in roles
    assert ('fallback', {'from': 'gemini/gemini-3.6-flash', 'to': 'deepseek/deepseek-v4-pro',
                         'reason': 'read timeout=120'}) in events


@pytest.mark.parametrize('status, propagates', [(503, True), (400, False)])
def test_http_status_mid_loop_5xx_rotates_4xx_is_reported(no_keys, status, propagates):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator()
    err = requests.exceptions.HTTPError(f'{status} for url',
                                        response=SimpleNamespace(status_code=status))
    fake = _FakePost({'gemini-3.6-flash': [_compat_msg(tool_calls=[TOOL]), err]})
    if propagates:
        with pytest.raises(requests.exceptions.HTTPError):
            _run_inner(op, fake, no_keys)
    else:
        # A refusal is this provider's answer to this request shape (CFOP-118's
        # distinction); replaying it elsewhere would likely refuse the same.
        result = _run_inner(op, fake, no_keys)
        assert result['response'].startswith('Error during tool execution')


def test_a_parse_failure_mid_loop_is_still_reported_not_replayed(no_keys):
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator()
    fake = _FakePost({'gemini-3.6-flash': [_compat_msg(tool_calls=[TOOL]),
                                           ValueError('malformed body')]})
    result = _run_inner(op, fake, no_keys)
    assert result['response'].startswith('Error during tool execution')


def test_failover_is_withheld_once_a_mutating_tool_ran(no_keys):
    # The chain restarts the next provider from the caller's messages and
    # re-runs tools. A restart already executed must not be executed again
    # on another model, so a later transport failure reports instead.
    no_keys.setenv('GEMINI_API_KEY', 'k')
    op = _operator()
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {'restarted': True},
        tools={TOOL: {'mutating': True}},   # the flag CFOP-124 owns
    )
    fake = _FakePost({'gemini-3.6-flash': [_compat_msg(tool_calls=[TOOL]),
                                           requests.exceptions.ReadTimeout('t')]})
    result = _run_inner(op, fake, no_keys)
    assert result['response'].startswith('Error during tool execution')

    # Same loop, tool not marked: the timeout rotates the chain as usual.
    op.tools = SimpleNamespace(
        get_schemas=lambda: [TOOL_SCHEMA],
        execute=lambda name, args: {'pods': []},
        tools={TOOL: {'mutating': False}},
    )
    fake = _FakePost({'gemini-3.6-flash': [_compat_msg(tool_calls=[TOOL]),
                                           requests.exceptions.ReadTimeout('t')]})
    with pytest.raises(requests.exceptions.ReadTimeout):
        _run_inner(op, fake, no_keys)
