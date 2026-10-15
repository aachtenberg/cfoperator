"""Per-scope models (CFOP-122): one resolver, DB over config over the chain
default, reaching the three unattended call sites that used to read their
own keys (sweep) or ride the chat default (summary, classifier)."""

import os
import re
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator, LLM_SCOPES  # noqa: E402


class _KB:
    def __init__(self, **d):
        self.d = dict(d)

    def get_setting(self, key, default='', *, strict=False):
        return self.d.get(key, default)

    def set_setting(self, key, value):
        self.d[key] = value


def _op(config=None, **settings):
    op = CFOperator.__new__(CFOperator)
    op.kb = _KB(**settings)
    op.config = config or {'llm': {}}
    return op


def test_console_over_config_over_default():
    cfg = {'llm': {'scopes': {'sweep': {'backend': 'groq', 'model': 'llama-3.3-70b'}}}}
    assert _op(cfg)._scope_llm('sweep') == {'backend': 'groq', 'model': 'llama-3.3-70b', 'source': 'config'}
    assert _op(cfg, sweep_backend='ollama', sweep_model='gemma4:26b')._scope_llm('sweep') == \
        {'backend': 'ollama', 'model': 'gemma4:26b', 'source': 'console'}
    assert _op()._scope_llm('sweep') == {'backend': 'auto', 'model': None, 'source': 'default'}
    # A console backend alone: that backend's selected model, like the chat chip.
    assert _op(cfg, sweep_backend='xai')._scope_llm('sweep') == {'backend': 'xai', 'model': None, 'source': 'console'}
    # A console model alone rides the chain.
    assert _op(sweep_model='m')._scope_llm('sweep') == {'backend': 'auto', 'model': 'm', 'source': 'console'}


def test_every_scope_resolves_and_an_unknown_one_is_refused():
    op = _op()
    for scope in LLM_SCOPES:
        assert op._scope_llm_kwargs(scope) == {'backend': 'auto', 'model': None}
    with pytest.raises(ValueError):
        op._scope_llm('investigation')


def test_a_db_read_failure_falls_through_to_config():
    op = _op({'llm': {'scopes': {'summary': {'model': 'gemma4:26b'}}}})
    op.kb.get_setting = MagicMock(side_effect=ConnectionError('offline'))
    assert op._scope_llm('summary') == {'backend': 'auto', 'model': 'gemma4:26b', 'source': 'config'}


def test_a_failure_on_the_second_read_does_not_keep_half_a_console_value():
    """Backend read fine, model read raised: not "console: groq, no model"."""
    op = _op({'llm': {'scopes': {'summary': {'backend': 'xai', 'model': 'grok-3'}}}})
    op.kb.get_setting = MagicMock(side_effect=['groq', ConnectionError('offline')])
    assert op._scope_llm('summary') == {'backend': 'xai', 'model': 'grok-3', 'source': 'config'}


def test_a_hand_written_db_backend_is_lowercased_like_config():
    assert _op(sweep_backend='Ollama')._scope_llm('sweep')['backend'] == 'ollama'


def test_the_sweep_runs_on_the_scope_model():
    op = _op(sweep_backend='deepseek', sweep_model='deepseek-chat')
    op._get_sweep_max_iterations = lambda: 3
    op._build_sweep_system_prompt = lambda task, skill_name=None: 'sys'
    op._chat_with_tools_with_fallback = MagicMock(return_value={'response': '[]', 'backend': 'deepseek', 'model': 'deepseek-chat'})
    op._sweep_with_llm('look around')
    kwargs = op._chat_with_tools_with_fallback.call_args.kwargs
    assert (kwargs['backend'], kwargs['model']) == ('deepseek', 'deepseek-chat')


def test_the_classifier_runs_on_the_scope_model():
    op = _op(classifier_backend='groq')
    op.git_repos = lambda: []
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        'response': '{"remediation_class": "manual", "risk": "high", "confidence": 0.5, "host": "", "repo": ""}',
        'backend': 'groq', 'model': 'llama-3.3-70b'})
    out = op._classify_needs_action_recommendation('t', 'r', {}, '')
    assert out['remediation_class'] == 'manual'
    kwargs = op._chat_with_tools_with_fallback.call_args.kwargs
    assert (kwargs['backend'], kwargs['model']) == ('groq', None)


def test_each_unattended_call_site_takes_its_scope():
    """The class of regression: a fourth unattended path, or one of these
    three, calling the chain without a scope and silently riding the chat
    default again. Parsed from source, like the single-lookup guard."""
    src = open(os.path.join(os.path.dirname(__file__), 'agent.py'), encoding='utf-8').read()
    for method, scope in (('def _sweep_with_llm', 'sweep'),
                          ('def _generate_morning_summary', 'summary'),
                          ('def _classify_needs_action_recommendation', 'classifier')):
        start = src.index(method)
        end = src.index('\n    def ', start + 10)
        body = src[start:end]
        calls = [m.start() for m in re.finditer(r'_chat_with_tools_with_fallback\(', body)]
        assert calls, method
        first = body[calls[0]:calls[0] + 600]
        assert f"_scope_llm_kwargs('{scope}')" in first, f'{method} does not resolve the {scope} scope'
    assert "get_setting('sweep_backend'" not in src and "get_setting('sweep_model'" not in src, \
        'the sweep keys are read through _scope_llm now'
