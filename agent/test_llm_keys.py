"""The one lookup for hosted-provider keys (agent/llm_keys.py, CFOP-111).

Console-set over env, encrypted at rest under CFOP_SESSION_SECRET, unreadable
after a rotation (and then the env answers), refused without a secret, and
reaching the two places a key decides anything in the agent: whether a rung
is in the fallback chain and whether a judge backend is eligible.
"""

import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_keys  # noqa: E402
from agent import CFOperator, OPENAI_COMPAT_PROVIDERS  # noqa: E402


class _KB:
    def __init__(self):
        self.d = {}

    def get_setting(self, key, default=''):
        return self.d.get(key, default)

    def set_setting(self, key, value):
        self.d[key] = value


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv('CFOP_SESSION_SECRET', 'unit-test-session-secret-0123456789')
    for cfg in OPENAI_COMPAT_PROVIDERS.values():
        monkeypatch.delenv(cfg['key_env'], raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    llm_keys._warned_unreadable.clear()


def test_console_wins_over_env_and_clear_restores_env(monkeypatch):
    kb = _KB()
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-env-value-0000000000')
    llm_keys.set_key(kb, 'deepseek', 'sk-console-value-1111111111')
    assert llm_keys.api_key(kb, 'deepseek') == 'sk-console-value-1111111111'
    assert llm_keys.source(kb, 'deepseek') == 'console'
    stored = kb.d['llm_key_deepseek']
    assert stored.startswith('enc:v1:') and 'console-value' not in stored
    llm_keys.clear_key(kb, 'deepseek')
    assert kb.d['llm_key_deepseek'] == ''          # NOT NULL column: '' is unset
    assert llm_keys.api_key(kb, 'deepseek') == 'sk-env-value-0000000000'
    assert llm_keys.source(kb, 'deepseek') == 'env'


def test_env_alone_and_no_kb(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', ' sk-ant-123456789012 ')
    assert llm_keys.api_key(_KB(), 'anthropic') == 'sk-ant-123456789012'
    assert llm_keys.api_key(None, 'anthropic') == 'sk-ant-123456789012'
    assert llm_keys.source(_KB(), 'anthropic') == 'env'


def test_a_backend_with_no_key_is_empty():
    assert llm_keys.key_env_for('ollama') is None
    assert llm_keys.api_key(_KB(), 'ollama') == ''
    assert llm_keys.source(_KB(), 'ollama') is None


def test_every_registry_provider_and_anthropic_are_hosted():
    assert llm_keys.hosted_backends() == [*OPENAI_COMPAT_PROVIDERS, 'anthropic']
    for backend in llm_keys.hosted_backends():
        assert llm_keys.key_env_for(backend).endswith('_API_KEY')


def test_a_rotated_secret_reads_as_unset_with_one_warning(monkeypatch, caplog):
    kb = _KB()
    llm_keys.set_key(kb, 'groq', 'gsk_console_value_22222222')
    monkeypatch.setenv('CFOP_SESSION_SECRET', 'a-different-secret-after-rotation')
    with caplog.at_level(logging.WARNING):
        assert llm_keys.api_key(kb, 'groq') == ''
        assert llm_keys.source(kb, 'groq') == 'unreadable'
        assert llm_keys.api_key(kb, 'groq') == ''
    warnings = [r for r in caplog.records if 'unreadable' in r.message and 'groq' in r.message]
    assert len(warnings) == 1
    monkeypatch.setenv('GROQ_API_KEY', 'gsk_env_value_33333333')
    assert llm_keys.api_key(kb, 'groq') == 'gsk_env_value_33333333'
    # A fresh set under the new secret reads again.
    llm_keys.set_key(kb, 'groq', 'gsk_console_value_44444444')
    assert llm_keys.api_key(kb, 'groq') == 'gsk_console_value_44444444'


def test_no_session_secret_refuses_to_store(monkeypatch):
    monkeypatch.delenv('CFOP_SESSION_SECRET')
    kb = _KB()
    assert llm_keys.storage_available() is False
    with pytest.raises(llm_keys.KeysUnavailable):
        llm_keys.set_key(kb, 'xai', 'xai-value-5555555555')
    assert kb.d == {}


def test_blank_long_and_unknown_are_rejected():
    kb = _KB()
    with pytest.raises(ValueError):
        llm_keys.set_key(kb, 'xai', '   ')
    with pytest.raises(ValueError):
        llm_keys.set_key(kb, 'xai', 'x' * (llm_keys.MAX_KEY_LENGTH + 1))
    with pytest.raises(ValueError):
        llm_keys.set_key(kb, 'ollama', 'anything')
    with pytest.raises(ValueError):
        llm_keys.clear_key(kb, 'nope')
    assert kb.d == {}


def test_status_never_carries_a_value(monkeypatch):
    kb = _KB()
    monkeypatch.setenv('XAI_API_KEY', 'xai-env-value-6666666666')
    llm_keys.set_key(kb, 'gemini', 'AIza-console-value-7777777777')
    rows = {r['id']: r for r in llm_keys.status(kb)}
    assert rows['xai'] == {'id': 'xai', 'label': rows['xai']['label'], 'key_env': 'XAI_API_KEY',
                           'configured': True, 'source': 'env', 'hint': '6666'}
    assert rows['gemini']['source'] == 'console' and rows['gemini']['hint'] == '7777'
    assert rows['anthropic']['configured'] is False and rows['anthropic']['source'] is None
    dumped = json.dumps(llm_keys.status(kb))
    assert 'env-value' not in dumped and 'console-value' not in dumped


def _operator():
    op = CFOperator.__new__(CFOperator)
    op.kb = _KB()
    op.config = {
        'llm': {'primary': {'url': 'http://ollama:11434', 'model': 'local-m'},
                'fallback': [{'provider': 'xai', 'model': 'grok-3'}]},
        'remediation': {'judge': {'providers': ['xai']}},
    }
    op.llm = SimpleNamespace(get_next_provider=lambda: ('ollama', 'http://ollama:11434', 'local-m'))
    return op


def test_a_console_key_puts_a_rung_in_the_chain_and_makes_a_judge_eligible():
    op = _operator()
    assert not [p for p in op._get_provider_chain('auto') if p[0] == 'xai']
    assert op._judge_providers() == []
    assert op._provider_unusable_reason('xai') == 'XAI_API_KEY not set (env, or Admin → LLM)'
    llm_keys.set_key(op.kb, 'xai', 'xai-console-value-8888888888')
    assert [p for p in op._get_provider_chain('auto') if p[0] == 'xai'] == [('xai', None, 'grok-3')]
    assert op._judge_providers() == ['xai']
    assert op._judge_api_key('xai') == 'xai-console-value-8888888888'
    assert op._provider_unusable_reason('xai') is None
    key, url = op._openai_compat_request_config('xai')
    assert key == 'xai-console-value-8888888888' and url.endswith('/chat/completions')
