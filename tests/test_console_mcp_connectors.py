"""MCP connectors from Admin → Connectors (CFOP-443), through the real console
over a dict-backed knowledge base and a real tool registry.

What this guards: a header value never comes back out of any endpoint and is
never stored in the clear; a save makes the connector's tools live with no
restart, and a removal takes them away; an edit that omits the headers keeps
them; a database that cannot be read refuses the write rather than saving a
list with every other connector missing; the writes are admin-gated.
"""

from repo_paths import REPO_ROOT
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

from test_console_provider_registry import server  # noqa: E402,F401 -- the fixture
from tools import ToolPolicy, ToolRegistry  # noqa: E402
from tools import mcp_connectors as mc  # noqa: E402

SECRET_VALUE = 'plane_api_secret_value_0123456789'
URL = 'https://plane-mcp.example/http/api-key/mcp'
ADMIN = ToolPolicy(actor_role='admin')


def _tool(name, read_only=False):
    return SimpleNamespace(name=name, description=f'{name} things',
                           annotations=SimpleNamespace(readOnlyHint=read_only),
                           inputSchema={'type': 'object', 'properties': {}})


@pytest.fixture
def console(server, monkeypatch):
    monkeypatch.setenv('CFOP_SESSION_SECRET', 'unit-test-session-secret-0123456789')
    seen = []

    def lister(url, headers):
        seen.append(headers)
        return [_tool('workitem'), _tool('get_pql_reference', read_only=True)]

    monkeypatch.setattr(mc, 'list_remote_tools', lister)
    server.operator.config = {'infrastructure': {'hosts': {}}, 'search': {}}
    server.operator.tools = ToolRegistry(server.operator)
    server.seen_headers = seen
    return server


def _put(c, name, body):
    return c.put(f'/api/settings/mcp_connectors/{name}', json=body)


def test_a_save_makes_the_tools_live_and_no_endpoint_carries_a_header_value(console):
    c = console.app.test_client()
    r = _put(c, 'plane', {'url': URL, 'headers': f'x-api-key: {SECRET_VALUE}\nx-workspace-slug: cfoperator'})
    assert r.status_code == 200, r.get_json()
    row = r.get_json()['connectors'][0]
    assert row['status']['state'] == 'ok'
    assert set(row['status']['tools']) == {'mcp_plane_workitem', 'mcp_plane_get_pql_reference'}
    assert row['header_names'] == ['x-api-key', 'x-workspace-slug']

    offered = {s['function']['name'] for s in console.operator.tools.get_schemas(policy=ADMIN)}
    assert {'mcp_plane_workitem', 'mcp_plane_get_pql_reference'} <= offered
    assert console.seen_headers[-1]['x-api-key'] == SECRET_VALUE

    stored = console.settings[mc.SETTING_KEY]
    assert SECRET_VALUE not in stored and 'enc:v1:' in stored
    for resp in (r, c.get('/api/settings/mcp_connectors'),
                 c.post('/api/settings/mcp_connectors/refresh', json={})):
        assert SECRET_VALUE not in resp.get_data(as_text=True)


def test_an_edit_without_headers_keeps_them_and_empty_clears_them(console):
    c = console.app.test_client()
    assert _put(c, 'plane', {'url': URL, 'headers': {'x-api-key': SECRET_VALUE}}).status_code == 200
    assert _put(c, 'plane', {'url': URL, 'enabled': True, 'tools': 'workitem'}).status_code == 200
    entry = mc.loads(console.settings[mc.SETTING_KEY])[0]
    assert mc.headers_for(entry)[0] == {'x-api-key': SECRET_VALUE}
    assert entry['tools'] == ['workitem']
    assert _put(c, 'plane', {'headers': {}}).status_code == 200
    assert mc.loads(console.settings[mc.SETTING_KEY])[0]['header_names'] == []


def test_removing_a_connector_takes_its_tools_away(console):
    c = console.app.test_client()
    assert _put(c, 'plane', {'url': URL}).status_code == 200
    assert 'mcp_plane_workitem' in console.operator.tools.tools
    assert c.delete('/api/settings/mcp_connectors/plane').status_code == 200
    assert not any(n.startswith('mcp_') for n in console.operator.tools.tools)
    assert c.delete('/api/settings/mcp_connectors/plane').status_code == 404


def test_a_disabled_connector_offers_nothing(console):
    c = console.app.test_client()
    r = _put(c, 'plane', {'url': URL, 'enabled': False})
    assert r.get_json()['connectors'][0]['status']['state'] == 'disabled'
    assert not any(n.startswith('mcp_') for n in console.operator.tools.tools)


@pytest.mark.parametrize('name,body', [
    ('Plane', {'url': URL}),
    ('plane', {'url': 'ftp://nope/mcp'}),
    ('plane', {}),
    ('plane', {'url': URL, 'headers': 'no colon'}),
])
def test_bad_input_is_400_and_nothing_is_written(console, name, body):
    c = console.app.test_client()
    assert _put(c, name, body).status_code in (400, 404)
    assert mc.SETTING_KEY not in console.settings


def test_an_unreadable_database_refuses_the_write(console):
    """A non-strict read would see "no connectors" during an outage, and the
    save would store a list with every other connector gone."""
    c = console.app.test_client()
    assert _put(c, 'plane', {'url': URL}).status_code == 200
    before = console.settings[mc.SETTING_KEY]

    def get_setting(key, default='', strict=False):
        if strict:
            raise RuntimeError('db down')
        return default
    console.operator.kb.get_setting.side_effect = get_setting
    assert _put(c, 'grafana', {'url': URL}).status_code == 503
    assert c.delete('/api/settings/mcp_connectors/plane').status_code == 503
    assert console.settings[mc.SETTING_KEY] == before
    body = c.get('/api/settings/mcp_connectors').get_json()
    assert body['db_error'] and body['connectors'] == []


def test_no_session_secret_refuses_headers_but_not_a_connector_without_them(console, monkeypatch):
    c = console.app.test_client()
    monkeypatch.delenv('CFOP_SESSION_SECRET')
    assert c.get('/api/settings/mcp_connectors').get_json()['storage'] == 'unavailable'
    assert _put(c, 'plane', {'url': URL, 'headers': {'x-api-key': SECRET_VALUE}}).status_code == 503
    assert mc.SETTING_KEY not in console.settings
    assert _put(c, 'plane', {'url': URL}).status_code == 200


def test_a_failed_write_is_never_a_success(console):
    c = console.app.test_client()
    console.operator.kb.set_setting.side_effect = RuntimeError('db down')
    assert _put(c, 'plane', {'url': URL}).status_code == 503


def test_connector_writes_are_admin_gated():
    """The CFOP-58 guard shape: the decorator between the route and the
    handler must be the admin gate, or a member could point the chat at a
    server of their choosing."""
    src = (REPO_ROOT / 'web_server.py').read_text(encoding='utf-8')
    for handler in ('def put_mcp_connector', 'def delete_mcp_connector', 'def refresh_mcp_connectors'):
        idx = src.index(handler)
        preceding = src[:idx].rsplit('@self.app.route', 1)[1]
        assert 'require_role(ROLE_ADMIN)' in preceding, f'{handler} lost its ROLE_ADMIN gate'


def test_admin_page_wires_the_connectors_tab():
    html = (REPO_ROOT / 'ui' / 'admin.html').read_text(encoding='utf-8')
    assert 'data-tab="connectors"' in html and 'id="panel-connectors"' in html
    assert "'connectors'" in html.split('const TABS', 1)[1].split('\n', 1)[0]
    assert '/api/settings/mcp_connectors' in html
    for fn in ('function loadConns', 'function saveConn', 'function removeConn', 'function refreshConns'):
        assert fn in html, fn
