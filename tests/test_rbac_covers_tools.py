"""Every kind the agent's kubectl calls list is one the chart grants (CFOP-397).

The live agent listed cronjobs, jobs and ingresses that its ServiceAccount
could not read. Nothing failed loudly: the sweep verifier's ground-truth
snapshot came back empty on every sweep, and the model was told to inspect
Ingresses it would only ever see Forbidden on. The kinds come from the source
-- the ``['get', '<kind>', ...]`` calls in tools/k8s.py and the agent's
``_GROUND_TRUTH_KINDS`` -- so a kind added to the code without a grant fails
here rather than in production.

The deployed role lives in the private deploy repo and is not seen here; the
chart's read ClusterRole is the one this repo owns.
"""

import re

from repo_paths import REPO_ROOT
from test_helm_chart import rendered_chart_docs

CORE = ''
GROUP_OF = {
    'pods': CORE, 'services': CORE, 'endpoints': CORE, 'events': CORE, 'nodes': CORE,
    'namespaces': CORE, 'persistentvolumeclaims': CORE, 'persistentvolumes': CORE,
    'deployments': 'apps', 'daemonsets': 'apps', 'statefulsets': 'apps', 'replicasets': 'apps',
    'jobs': 'batch', 'cronjobs': 'batch',
    'ingresses': 'networking.k8s.io',
}
# kubectl accepts the singular; RBAC names the plural.
PLURAL = {'pod': 'pods', 'deployment': 'deployments', 'service': 'services', 'node': 'nodes',
          'ingress': 'ingresses', 'event': 'events', 'namespace': 'namespaces', 'job': 'jobs',
          'cronjob': 'cronjobs', 'endpoint': 'endpoints'}

SOURCES = (REPO_ROOT / 'tools' / 'k8s.py', REPO_ROOT / 'agent' / 'agent.py')


def kinds_the_agent_lists() -> set:
    kinds = set()
    for path in SOURCES:
        text = path.read_text()
        for arg in re.findall(r"'get',\s*'([a-z,/]+)'", text):
            kinds.update(arg.split(','))
        block = re.search(r"_GROUND_TRUTH_KINDS\s*=\s*\((.*?)\)", text, re.S)
        if block:
            kinds.update(re.findall(r"'([a-z]+)'", block.group(1)))
    return {PLURAL.get(k, k) for k in kinds}


def _read_role() -> dict:
    roles = [d for d in rendered_chart_docs()
             if d.get('kind') == 'ClusterRole' and d['metadata']['name'].endswith('-read')]
    (role,) = roles
    return role


def _granted(role: dict) -> set:
    out = set()
    for rule in role['rules']:
        if not {'get', 'list'} <= set(rule.get('verbs', [])):
            continue
        for group in rule.get('apiGroups', []):
            for resource in rule.get('resources', []):
                out.add((group, resource))
    return out


def test_the_source_scan_is_not_vacuous():
    kinds = kinds_the_agent_lists()
    assert {'pods', 'nodes', 'cronjobs', 'ingresses'} <= kinds, kinds


def test_every_kind_the_agent_lists_is_granted_by_the_read_role():
    granted = _granted(_read_role())
    for kind in sorted(kinds_the_agent_lists()):
        assert kind in GROUP_OF, f"{kind!r}: add its API group to GROUP_OF in this test"
        assert (GROUP_OF[kind], kind) in granted, (
            f"the agent lists {kind} ({GROUP_OF[kind] or 'core'}) but the chart's read "
            "ClusterRole does not grant get/list on it -- it would be Forbidden at runtime")
