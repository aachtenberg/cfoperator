"""A Python model of cfop-dedupe's decision, for tests only (CFOP-538).

Production never imports this: the agent asks the service (``dedupe/``), and
when the service cannot answer the exact key alone decides
(``dedupe_client.fallback``). The tests that drive the agent's enqueue sites
still need something to answer the way the service does, without a Go binary
in the Python job. This is that answer: ``decide()`` mirrors
``dedupe/decide.go`` tier for tier, and ``test_dedupe_parity.py`` holds it to
every shared case in ``dedupe/testdata/``, the same files the Go tests run. A
case where the two disagree fails there, so a test passing against this model
is a test passing against the service's behaviour.

The identifier and target helpers below were the agent's own until the
service took the decision over (CFOP-78, CFOP-453, CFOP-494). The host helpers
stay in agent.py, which still uses them for the CFOP-108 checklist veto.

``agent/conftest.py`` installs ``serve`` in place of ``dedupe_client.decide``
for every agent test, so a test that wires a service URL is answered here;
``wire()`` gives a MagicMock operator the real enqueue funnel.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from agent.agent import CFOperator, _host_candidates, _match_known_host

#: dedupe/decide.go foldWindow: the fold and the alias lookup read the newest
#: 100 open rows; the exact-key check reads every open row.
FOLD_WINDOW = 100
#: The agent's [:200] on the summary a fold records.
ABSORBED_LIMIT = 200
TEST_URL = "http://cfop-dedupe.test:8093"

_TERMINAL = ("resolved", "rejected")

# Hard identifiers a recommendation names — the things two differently-worded
# recommendations for the same fix reliably share (CFOP-78). Deliberately only
# shapes with near-zero prose collision: dotted-quad IPs, UPPER_SNAKE env/config
# keys (the underscore requirement keeps out STATUS/ERROR/HTTP), and
# <table>_id <n> row references. Bare hostnames and workload names are NOT
# extracted: "needs-human" and "grok-4.6" are dash-words too, and a false fold
# hides a real incident, which is strictly worse than a duplicate row.
# The port stays when present: this cluster runs many services per address,
# so a bare IP is a weak identity — "restart nginx on 192.168.0.131" and the
# stale tunnel rule for 192.168.0.131:80 are different problems on one box.
_IDENT_IP = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?)\b')
_IDENT_ENV = re.compile(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b')
_IDENT_ROW = re.compile(r'\b([A-Za-z][A-Za-z0-9]*_id)\W{0,3}(\d+)')

# CFOP-453: one FIX target rendered two ways on two runs
# (`data/eccc-ingest-script-mg664c47g9`, then `eccc-ingest-script-mg664c47g9`).
# Normalized for COMPARISON only. A kustomize generator suffix is ten
# characters from kustomize's own alphabet (no vowels, no 0/1/a/e/i/l/o/u), so
# `-production` or `-ingest` can never be mistaken for one.
_KUSTOMIZE_HASH_SUFFIX = re.compile(r'-[bcdfghkmnpqrstvwxz2456789]{10}$')


def extract_identifiers(text) -> frozenset:
    """The hard identifiers a recommendation names. Empty means "names
    nothing matchable": never fold on it, never fold onto everything."""
    raw = str(text or '')
    ids = set()
    for match in _IDENT_IP.finditer(raw):
        ids.add(match.group(1))
    for match in _IDENT_ENV.finditer(raw):
        ids.add(match.group(0))
    for match in _IDENT_ROW.finditer(raw):
        ids.add(f"{match.group(1).lower()}:{match.group(2)}")
    return frozenset(ids)


def normalize_target_id(target_id) -> str:
    tid = str(target_id or '').strip().lower().strip('/')
    return _KUSTOMIZE_HASH_SUFFIX.sub('', tid)


def target_identity(targets) -> frozenset:
    """(kind, repo, normalized id) per usable FIX target; empty means the row
    names nothing matchable."""
    out = set()
    for t in targets or []:
        if not isinstance(t, dict):
            continue
        kind = str(t.get('kind') or '').strip().lower()
        tid = normalize_target_id(t.get('id'))
        if kind and tid:
            out.add((kind, str(t.get('repo') or '').strip().lower(), tid))
    return frozenset(out)


def _same_target(a, b) -> bool:
    """Same kind and repo, and the ids equal or one a path-segment suffix of
    the other: `data/eccc-ingest-script` is `eccc-ingest-script`, but
    `base/apps/kustomization.yml` is not `base/monitoring/kustomization.yml`."""
    if a[0] != b[0] or a[1] != b[1]:
        return False
    x, y = a[2], b[2]
    return x == y or x.endswith('/' + y) or y.endswith('/' + x)


def targets_contained(new, old) -> bool:
    """Every target on one side has a match on the other. Containment, not
    intersection: two fixes that each touch something the other does not are
    two fixes."""
    if not new or not old:
        return False

    def covered(xs, ys):
        return all(any(_same_target(x, y) for y in ys) for x in xs)
    return covered(new, old) or covered(old, new)


def _key(row) -> str:
    return str((row.get('payload') or {}).get('dedupe_key') or '')


def _carries(row, key) -> bool:
    payload = row.get('payload') or {}
    return bool(key) and (payload.get('dedupe_key') == key
                          or key in (payload.get('dedupe_aliases') or []))


def _collapse(raw, notready) -> Tuple[str, str]:
    """CFOP-71: the node-incident key and node for a host field, or ('', '')."""
    down = sorted({str(n).lower() for n in (notready or ()) if n})
    if not down:
        return '', ''
    for token in _host_candidates(raw):
        node = _match_known_host(token, down)
        if node:
            return CFOperator._node_incident_dedupe_key(node), node
    return '', ''


def _lookup(open_rows, window, key) -> Optional[Dict[str, Any]]:
    """find_open_remediation_by_dedupe_key: own key over every open row, then
    an alias among the newest window."""
    for r in open_rows:
        if _key(r) == key:
            return r
    for r in window:
        if _carries(r, key):
            return r
    return None


def _fold_repeat(c, window, absorbed) -> Optional[Dict[str, Any]]:
    new_ids = extract_identifiers(c.get('recommendation'))
    new_targets = target_identity(c.get('targets') or c.get('fold_targets'))
    if not new_ids and not new_targets:
        return None
    for row in window:
        payload = row.get('payload') or {}
        row_key = _key(row)
        if row_key and row_key == c.get('dedupe_key'):
            continue
        tier, shared = '', []
        if targets_contained(new_targets, target_identity(payload.get('targets')
                                                          or payload.get('fold_targets'))):
            tier, shared = 'target', sorted({t[2] for t in new_targets})
        elif new_ids:
            row_ids = extract_identifiers(payload.get('recommendation'))
            if row_ids and (new_ids <= row_ids or row_ids <= new_ids):
                tier, shared = 'identifier', sorted(new_ids & row_ids)
        if not tier:
            continue
        out = {'decision': 'fold', 'row_id': row['id'], 'tier': tier, 'shared': shared,
               'reason': f"same {tier} as open row #{row['id']}", 'absorbed': absorbed}
        if c.get('dedupe_key') and c['dedupe_key'] != row_key:
            out['alias'] = c['dedupe_key']
        return out
    return None


def decide(request: Dict[str, Any]) -> Dict[str, Any]:
    """dedupe/decide.go Decide: node collapse, then the target and identifier
    folds, then the exact key."""
    c = request.get('candidate') or {}
    open_rows = [r for r in request.get('open_rows') or [] if r.get('status') not in _TERMINAL]
    window = open_rows[:FOLD_WINDOW]
    key = str(c.get('dedupe_key') or '')
    absorbed = str(c.get('trigger') or c.get('recommendation') or '')[:ABSORBED_LIMIT]
    node_key, node = _collapse(c.get('host') or c.get('title'),
                               (request.get('vocabulary') or {}).get('notready_nodes'))
    if node_key and node_key != key:
        row = _lookup(open_rows, window, node_key)
        if row:
            return {'decision': 'fold', 'row_id': row['id'], 'tier': 'node', 'shared': [node],
                    'reason': f"node {node} is NotReady; its incident row #{row['id']} covers this",
                    'absorbed': absorbed}
        key = node_key
    if not node_key:
        folded = _fold_repeat(c, window, absorbed)
        if folded:
            return folded
    if key:
        for row in open_rows:
            if _key(row) == key:
                return {'decision': 'suppress', 'row_id': row['id'], 'tier': 'key',
                        'dedupe_key': key, 'reason': f"open row #{row['id']} already carries {key}"}
    out = {'decision': 'new', 'dedupe_key': key, 'reason': 'no open row covers it'}
    if node_key:
        out['host'] = node
        out['reason'] = f"node {node} is NotReady; this row is its incident ({node_key})"
    # The service omits empty fields; so does this, so a test comparing the
    # two compares like with like.
    return {k: v for k, v in out.items() if v not in ('', None)}


#: Every request ``serve`` answered, oldest first, for tests that assert on
#: what was asked.
REQUESTS: List[Dict[str, Any]] = []


def serve(url: str, request: Dict[str, Any], **_kw) -> Dict[str, Any]:
    """``dedupe_client.decide``'s shape, answered by ``decide``."""
    REQUESTS.append(request)
    return decide(request)


def wire(op, url: str = TEST_URL, *, model_step: bool = False):
    """Give a MagicMock operator the real enqueue funnel: the lock, the
    decision and the fold, answered through ``serve`` (agent/conftest.py),
    and the choke point's methods around them.

    ``model_step`` False is the model_dedupe flag off (CFOP-545): no hint is
    asked for. True binds the real ``_dedupe_model_hint``, which reads the
    flags and the ``dedupe`` seat off ``op`` like any other caller would."""
    op._remediation_lock = threading.RLock()
    op._dedupe_url = lambda: url
    op._dedupe_nodes = lambda cand: CFOperator._dedupe_nodes(op, cand)
    op._dedupe_apply_hint = lambda *a: CFOperator._dedupe_apply_hint(op, *a)
    op._dedupe_model_hint = ((lambda path, cand: CFOperator._dedupe_model_hint(op, path, cand))
                             if model_step else (lambda path, cand: None))
    op._dedupe_decide = lambda path, cand, **kw: CFOperator._dedupe_decide(op, path, cand, **kw)
    op._apply_dedupe_fold = lambda d: CFOperator._apply_dedupe_fold(op, d)
    op._dedupe_row_open = lambda rid: CFOperator._dedupe_row_open(op, rid)
    # The choke point's own pieces, which a bare MagicMock would answer with
    # mocks (CFOP-538 split them out when the judge left the lock).
    op._fold_choke_point = lambda d, pr_url: CFOperator._fold_choke_point(op, d, pr_url)
    op._judge_for_enqueue = lambda *a: CFOperator._judge_for_enqueue(op, *a)
    return op
