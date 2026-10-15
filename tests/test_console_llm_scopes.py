"""Models per scope from Admin → LLM (CFOP-122), through the real console
over a dict-backed knowledge base (the fixture from
test_console_provider_registry).

What this guards: every seat is listed with its effective value and
provenance; a PUT changes what the agent resolves; a judge cannot be set
below its floor; reset hands back to config; a failed write is never a 200
-- here and on the three older settings endpoints that used to say
success with nothing persisted; the writes are admin-gated; the Admin
card is wired.
"""

from repo_paths import REPO_ROOT
import os
import sys

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

from test_console_provider_registry import server  # noqa: E402,F401 -- the fixture
from agent import CFOperator, LLM_SCOPES  # noqa: E402
from agent.agent import _JUDGE_MODEL_FLOOR  # noqa: E402


@pytest.fixture(autouse=True)
def _real_resolvers(server):
    """The fixture's operator is a MagicMock; the routes under test read
    through the agent's real resolvers, so bind those (and a dict config)
    onto it. What the console shows is then what the agent would run."""
    from types import MethodType
    op = server.operator
    if not isinstance(op.config, dict):
        op.config = {'llm': {}, 'remediation': {}}
    for name in ('_scope_llm', '_scope_llm_config', '_triage_model',
                 '_judge_model', '_judge_model_setting', '_judge_model_config', '_executor_config'):
        setattr(op, name, MethodType(getattr(CFOperator, name), op))


def _rows(client):
    resp = client.get('/api/settings/llm_scopes')
    assert resp.status_code == 200
    return {r['scope']: r for r in resp.get_json()['scopes']}


def test_every_seat_is_listed_with_provenance(server):
    rows = _rows(server.app.test_client())
    for scope in LLM_SCOPES:
        assert rows[scope]['editable'] is True and rows[scope]['source'] == 'default'
        assert rows[scope]['effective'] == {'backend': 'auto', 'model': ''}
    for backend, floor in _JUDGE_MODEL_FLOOR.items():
        row = rows[f'judge:{backend}']
        assert row['editable'] is True and row['source'] == 'floor'
        assert row['effective'] == {'model': floor} and row['floor'] == floor
    assert rows['triage']['editable'] is False
    assert rows['executor']['editable'] is False and rows['executor']['kind'] == 'chain'
    assert rows['deep']['editable'] is False


def test_a_put_changes_what_the_agent_resolves(server):
    c = server.app.test_client()
    r = c.put('/api/settings/llm_scopes/sweep', json={'backend': 'groq', 'model': 'llama-3.3-70b'})
    assert r.status_code == 200
    assert r.get_json()['row']['source'] == 'console'
    assert server.settings == {'sweep_backend': 'groq', 'sweep_model': 'llama-3.3-70b'}
    assert server.operator._scope_llm('sweep') == {'backend': 'groq', 'model': 'llama-3.3-70b', 'source': 'console'}
    # 'auto' with a model: the chain picks the backend, the model is pinned.
    assert c.put('/api/settings/llm_scopes/summary', json={'backend': 'auto', 'model': 'gemma4:26b'}).status_code == 200
    assert server.settings['summary_backend'] == '' and server.settings['summary_model'] == 'gemma4:26b'
    assert _rows(c)['summary']['effective'] == {'backend': 'auto', 'model': 'gemma4:26b'}


def test_reset_hands_back_to_config(server):
    c = server.app.test_client()
    server.operator.config['llm'] = {'scopes': {'classifier': {'backend': 'xai', 'model': 'grok-3-mini'}}}
    assert _rows(c)['classifier']['source'] == 'config'
    assert c.put('/api/settings/llm_scopes/classifier', json={'backend': 'ollama'}).status_code == 200
    assert _rows(c)['classifier']['source'] == 'console'
    assert c.delete('/api/settings/llm_scopes/classifier').status_code == 200
    row = _rows(c)['classifier']
    assert row['source'] == 'config' and row['effective'] == {'backend': 'xai', 'model': 'grok-3-mini'}


def test_a_judge_cannot_go_below_its_floor(server):
    c = server.app.test_client()
    r = c.put('/api/settings/llm_scopes/judge:xai', json={'model': 'grok-3-mini-fast'})
    assert r.status_code == 400 and 'fast-tier' in r.get_json()['error']
    assert 'judge_model_xai' not in server.settings
    assert c.put('/api/settings/llm_scopes/judge:xai', json={'model': 'grok-4.5-0901'}).status_code == 200
    assert server.settings['judge_model_xai'] == 'grok-4.5-0901'
    row = _rows(c)['judge:xai']
    assert row['source'] == 'console' and row['effective'] == {'model': 'grok-4.5-0901'}
    assert server.operator._judge_model('xai') == 'grok-4.5-0901'
    assert c.delete('/api/settings/llm_scopes/judge:xai').status_code == 200
    assert _rows(c)['judge:xai']['source'] == 'floor'


def test_bad_requests(server):
    c = server.app.test_client()
    assert c.put('/api/settings/llm_scopes/sweep', json={'backend': 'nope'}).status_code == 400
    assert c.put('/api/settings/llm_scopes/sweep', json={}).status_code == 400
    assert c.put('/api/settings/llm_scopes/judge:xai', json={}).status_code == 400
    for ro in ('triage', 'executor', 'deep', 'judge:ollama', 'investigation'):
        assert c.put(f'/api/settings/llm_scopes/{ro}', json={'model': 'm'}).status_code == 404, ro
        assert c.delete(f'/api/settings/llm_scopes/{ro}').status_code == 404, ro
    assert server.settings == {}


def test_a_failed_write_is_never_a_success_here_or_on_the_older_endpoints(server):
    c = server.app.test_client()
    server.operator.kb.set_setting.side_effect = RuntimeError('could not connect to db-host:5432')
    for method, path, body in (
            ('put', '/api/settings/llm_scopes/sweep', {'backend': 'groq'}),
            ('delete', '/api/settings/llm_scopes/sweep', None),
            ('put', '/api/settings/llm_scopes/judge:xai', {'model': 'grok-4.5'}),
            ('post', '/api/settings/provider', {'backend': 'groq'}),
            ('post', '/api/models/groq/select', {'model': 'llama-3.3-70b'}),
            ('post', '/api/settings/max_tool_iterations', {'max_tool_iterations': 12}),
    ):
        resp = getattr(c, method)(path, json=body)
        assert resp.status_code == 503, (method, path, resp.status_code)
        assert 'db-host' not in resp.get_data(as_text=True)


def test_a_saved_write_is_reported_saved_even_when_the_row_cannot_be_rebuilt(server):
    c = server.app.test_client()
    server.operator._triage_model = lambda: (_ for _ in ()).throw(RuntimeError('triage lookup broke'))
    r = c.put('/api/settings/llm_scopes/sweep', json={'backend': 'groq'})
    assert r.status_code == 200 and r.get_json() == {'success': True, 'row': None}
    assert server.settings['sweep_backend'] == 'groq'


def test_the_deep_row_never_claims_a_default_it_cannot_see(server, monkeypatch):
    monkeypatch.delenv('CFOP_DEEP_MODEL', raising=False)
    assert _rows(server.app.test_client())['deep']['source'] == 'unknown'
    monkeypatch.setenv('CFOP_DEEP_MODEL', 'deep-m')
    row = _rows(server.app.test_client())['deep']
    assert row['source'] == 'env' and row['effective'] == {'model': 'deep-m'}


def test_scope_writes_are_admin_gated():
    src = (REPO_ROOT / 'web_server.py').read_text(encoding='utf-8')
    for handler in ('def set_llm_scope', 'def reset_llm_scope'):
        idx = src.index(handler)
        preceding = src[:idx].rsplit('@self.app.route', 1)[1]
        assert 'require_role(ROLE_ADMIN)' in preceding, f'{handler} lost its ROLE_ADMIN gate'


def test_admin_page_wires_the_scopes_card():
    html = (REPO_ROOT / 'ui' / 'admin.html').read_text(encoding='utf-8')
    assert 'id="scopes-card"' in html
    assert '/api/settings/llm_scopes' in html
    assert 'function saveLlmScope' in html and 'function resetLlmScope' in html
    assert 'loadLlmScopes()' in html
