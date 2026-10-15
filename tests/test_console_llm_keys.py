"""Provider keys from Admin → LLM (CFOP-111), through the real console over
a dict-backed knowledge base (the fixture from test_console_provider_registry).

What this guards: a GET never carries a key, from any endpoint; a key set
here makes the provider available and listable with no env and no restart;
clearing it hands back to the env; blank is "unchanged"; a failed write is
never a 200; the writes are admin-gated.
"""

from repo_paths import REPO_ROOT
import os
import sys

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

from test_console_provider_registry import server  # noqa: E402,F401 -- the fixture
from agent import OPENAI_COMPAT_PROVIDERS, llm_keys  # noqa: E402

ENV_KEY = 'gsk_env_secret_value_1234567890'
CONSOLE_KEY = 'AIza_console_secret_value_0987654321'


@pytest.fixture(autouse=True)
def session_secret(monkeypatch):
    monkeypatch.setenv('CFOP_SESSION_SECRET', 'unit-test-session-secret-0123456789')
    llm_keys._warned_unreadable.clear()


def _rows(client):
    resp = client.get('/api/settings/llm_keys')
    assert resp.status_code == 200
    return {r['id']: r for r in resp.get_json()['providers']}


def _providers(client):
    resp = client.get('/api/providers')
    assert resp.status_code == 200
    return {p['id']: p for p in resp.get_json()['providers']}


def test_every_hosted_provider_has_a_row_and_no_endpoint_carries_a_value(server, monkeypatch):
    c = server.app.test_client()
    monkeypatch.setenv('GROQ_API_KEY', ENV_KEY)
    assert c.put('/api/settings/llm_keys/gemini', json={'key': CONSOLE_KEY}).status_code == 200
    rows = _rows(c)
    assert set(rows) == set(OPENAI_COMPAT_PROVIDERS) | {'anthropic'}
    assert rows['groq']['source'] == 'env' and rows['groq']['hint'] == '7890'
    assert rows['gemini']['source'] == 'console' and rows['gemini']['configured'] is True
    assert rows['xai']['source'] is None and rows['xai']['configured'] is False
    for path in ('/api/settings/llm_keys', '/api/providers', '/api/settings/provider'):
        body = c.get(path).get_data(as_text=True)
        assert ENV_KEY not in body and CONSOLE_KEY not in body, path


def test_a_console_key_makes_a_provider_available_with_no_env(server):
    c = server.app.test_client()
    assert _providers(c)['gemini']['available'] is False
    r = c.put('/api/settings/llm_keys/gemini', json={'key': CONSOLE_KEY})
    assert r.status_code == 200
    assert r.get_json()['provider']['source'] == 'console'
    assert _providers(c)['gemini']['available'] is True
    stored = server.settings['llm_key_gemini']
    assert stored.startswith('enc:v1:') and CONSOLE_KEY not in stored


def test_clear_falls_back_to_env(server, monkeypatch):
    c = server.app.test_client()
    monkeypatch.setenv('GROQ_API_KEY', ENV_KEY)
    assert c.put('/api/settings/llm_keys/groq', json={'key': 'gsk_console_key_11111111'}).status_code == 200
    assert _rows(c)['groq']['source'] == 'console'
    assert llm_keys.api_key(server.operator.kb, 'groq') == 'gsk_console_key_11111111'
    assert c.delete('/api/settings/llm_keys/groq').status_code == 200
    assert _rows(c)['groq']['source'] == 'env'
    assert llm_keys.api_key(server.operator.kb, 'groq') == ENV_KEY
    monkeypatch.delenv('GROQ_API_KEY')
    assert _rows(c)['groq']['configured'] is False
    assert _providers(c)['groq']['available'] is False


def test_blank_means_unchanged_not_cleared(server):
    c = server.app.test_client()
    assert c.put('/api/settings/llm_keys/xai', json={'key': 'xai-first-value-2222222222'}).status_code == 200
    assert c.put('/api/settings/llm_keys/xai', json={}).status_code == 400
    assert c.put('/api/settings/llm_keys/xai', json={'key': '   '}).status_code == 400
    assert llm_keys.api_key(server.operator.kb, 'xai') == 'xai-first-value-2222222222'


def test_unknown_provider_is_404(server):
    c = server.app.test_client()
    assert c.put('/api/settings/llm_keys/ollama', json={'key': 'x'}).status_code == 404
    assert c.delete('/api/settings/llm_keys/nope').status_code == 404


def test_no_session_secret_refuses_to_store(server, monkeypatch):
    c = server.app.test_client()
    monkeypatch.delenv('CFOP_SESSION_SECRET')
    assert c.get('/api/settings/llm_keys').get_json()['storage'] == 'unavailable'
    r = c.put('/api/settings/llm_keys/xai', json={'key': 'xai-value-3333333333'})
    assert r.status_code == 503
    assert 'llm_key_xai' not in server.settings


def test_a_failed_write_is_never_a_success(server):
    c = server.app.test_client()
    server.operator.kb.set_setting.side_effect = RuntimeError('db down')
    assert c.put('/api/settings/llm_keys/xai', json={'key': 'xai-value-4444444444'}).status_code == 503
    assert c.delete('/api/settings/llm_keys/xai').status_code == 503


def test_a_rotated_secret_shows_as_unreadable_and_env_applies(server, monkeypatch):
    c = server.app.test_client()
    assert c.put('/api/settings/llm_keys/deepseek', json={'key': 'sk-console-value-5555555555'}).status_code == 200
    monkeypatch.setenv('CFOP_SESSION_SECRET', 'rotated-secret-after-the-fact')
    row = _rows(c)['deepseek']
    assert row['source'] == 'unreadable' and row['configured'] is False
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-env-value-6666666666')
    row = _rows(c)['deepseek']
    assert row['source'] == 'unreadable' and row['configured'] is True and row['hint'] == '6666'


def test_model_listing_uses_the_console_key(server, monkeypatch):
    import web_server
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen['url'], seen['headers'] = url, headers
        return type('R', (), {'raise_for_status': lambda self: None,
                              'json': lambda self: {'data': [{'id': 'llama-3.3-70b-versatile'}]}})()
    monkeypatch.setattr(web_server.requests, 'get', fake_get)
    c = server.app.test_client()
    assert c.get('/api/models/groq').status_code == 500          # no key anywhere
    assert c.put('/api/settings/llm_keys/groq', json={'key': 'gsk_console_key_77777777'}).status_code == 200
    resp = c.get('/api/models/groq')
    assert resp.status_code == 200 and resp.get_json()['models'] == ['llama-3.3-70b-versatile']
    assert seen['headers']['Authorization'] == 'Bearer gsk_console_key_77777777'


def test_key_writes_are_admin_gated():
    """The CFOP-58 guard shape: the decorator between the route and the
    handler must be the admin gate, or a member could rotate a key."""
    src = (REPO_ROOT / 'web_server.py').read_text(encoding='utf-8')
    for handler in ('def set_llm_key', 'def clear_llm_key'):
        idx = src.index(handler)
        preceding = src[:idx].rsplit('@self.app.route', 1)[1]
        assert 'require_role(ROLE_ADMIN)' in preceding, f'{handler} lost its ROLE_ADMIN gate'


def test_admin_page_wires_the_keys_card():
    html = (REPO_ROOT / 'ui' / 'admin.html').read_text(encoding='utf-8')
    assert 'id="keys-card"' in html
    assert '/api/settings/llm_keys' in html
    assert 'function saveProviderKey' in html and 'function clearProviderKey' in html
    assert 'type="password"' in html
    # The old copy claimed keys could only live in sealed secrets.
    assert 'API keys stay in' not in html
