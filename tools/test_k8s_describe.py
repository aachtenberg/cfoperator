"""k8s_describe tells the model why it failed (CFOP-397).

Seventeen of thirty-one calls failed in one day, and the model saw only
``success: false`` with an empty description: kubectl's NotFound or Forbidden
line never left ``describe()``, so it could not correct the namespace or stop
asking for a kind it may not read. The schema half: the namespace is required,
and ``secret`` is no longer offered as an example of what to describe.
"""

from tools.k8s import K8sTools


def _tools(monkeypatch, result):
    tools = K8sTools()
    calls = []

    def fake(args, timeout=30):
        calls.append(args)
        return result

    monkeypatch.setattr(tools, '_run_kubectl', fake)
    return tools, calls


def test_a_failed_describe_carries_kubectl_reason(monkeypatch):
    tools, calls = _tools(monkeypatch, {
        'success': False, 'stdout': '', 'exit_code': 1,
        'stderr': 'Error from server (NotFound): pods "web-0" not found\n'})
    out = tools.describe('pod', 'web-0', namespace='apps')
    assert out['success'] is False
    assert 'NotFound' in out['error'] and 'web-0' in out['error']
    assert out['namespace'] == 'apps'
    assert calls == [['describe', 'pod', 'web-0', '-n', 'apps']]


def test_forbidden_reads_as_forbidden(monkeypatch):
    tools, _ = _tools(monkeypatch, {
        'success': False, 'stdout': '', 'exit_code': 1,
        'stderr': 'Error from server (Forbidden): ingresses.networking.k8s.io "x" is forbidden: '
                  'User "system:serviceaccount:apps:cfoperator" cannot get resource "ingresses"'})
    assert 'Forbidden' in tools.describe('ingress', 'x', namespace='apps')['error']


def test_a_timeout_is_reported_too(monkeypatch):
    tools, _ = _tools(monkeypatch, {'success': False, 'error': 'Command timed out after 30s'})
    assert 'timed out' in tools.describe('pod', 'x', namespace='apps')['error']


def test_the_reason_is_clipped(monkeypatch):
    tools, _ = _tools(monkeypatch, {'success': False, 'stdout': '', 'exit_code': 1,
                                    'stderr': 'E' * 5000})
    assert len(tools.describe('pod', 'x', namespace='apps')['error']) <= K8sTools._DESCRIBE_ERROR_LIMIT


def test_a_successful_describe_is_unchanged(monkeypatch):
    tools, _ = _tools(monkeypatch, {'success': True, 'stdout': 'Name: web-0\n', 'stderr': '',
                                    'exit_code': 0})
    out = tools.describe('pod', 'web-0', namespace='apps')
    assert out == {'success': True, 'description': 'Name: web-0\n', 'resource_type': 'pod',
                   'name': 'web-0', 'namespace': 'apps'}
    assert 'error' not in out


def _describe_schema():
    schemas = [s for s in K8sTools().get_schemas() if s['name'] == 'k8s_describe']
    (schema,) = schemas
    return schema['parameters']


def test_schema_requires_the_namespace():
    """A call that omits it searches ``default``, where almost nothing runs,
    and that was most of the NotFounds."""
    assert 'namespace' in _describe_schema()['required']


def test_schema_does_not_offer_secrets():
    """The agent may not read secrets (CFOP-312); naming them as a describable
    kind invited a guaranteed Forbidden."""
    assert 'secret' not in _describe_schema()['properties']['resource_type']['description'].lower().replace(
        'secrets are not readable', '')
