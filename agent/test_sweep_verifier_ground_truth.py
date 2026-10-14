#!/usr/bin/env python3
"""The sweep verifier after CFOP-398.

Three things, each found in one day of Tempo traces:

* a kind the ServiceAccount may not list costs only itself. One kubectl call
  for all eight kinds failed as a whole on a Forbidden ingresses, so the
  workload half of the ground-truth suppressor sat empty on every sweep;
* a verdict forced at the tool cap is kept but marked, so the span and the
  report say which verdicts came from the clipped summary call rather than
  from the model finishing its disproof (29 of 33 did);
* the verifier is handed the names that already resolve, so its 2-4 calls go
  to the disproof instead of to k8s_get_pods.

Operator and chat stub as in test_sweep_verification.
"""

import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from test_sweep_verification import _make_operator  # noqa: E402


class _FakeK8s:
    """kubectl as the live agent's ServiceAccount sees it: it may not list
    ingresses. Every readable kind answers with the same two names."""

    def __init__(self, forbidden=('ingresses',)):
        self.forbidden = set(forbidden)
        self.calls = []

    def get_nodes(self):
        return {'success': True, 'nodes': [{'name': 'ubuntu-cm5-01', 'ready': 'True',
                                            'memoryPressure': 'False', 'diskPressure': 'False'}]}

    def _run_kubectl(self, args, timeout=30):
        self.calls.append(args)
        kind = args[1]
        if kind in self.forbidden:
            return {'success': False, 'stdout': '', 'exit_code': 1,
                    'stderr': f'Error from server (Forbidden): {kind} is forbidden: User '
                              f'"system:serviceaccount:apps:cfoperator" cannot list resource "{kind}"'}
        return {'success': True, 'exit_code': 0, 'stderr': '',
                'stdout': f'{kind}/river-history-ingest-1\n{kind}/plane-api\n'}


def _with_k8s(chat_stub, k8s):
    operator = _make_operator(chat_stub)
    operator.tools = SimpleNamespace(k8s_tools=k8s)
    return operator


def _llm_must_not_run(**kwargs):
    raise AssertionError('the LLM verifier ran; the ground-truth stage should have settled it')


def _finding(text, evidence='old evidence'):
    return {'severity': 'warning', 'finding': text, 'evidence': evidence}


def test_ground_truth_snapshot_survives_a_forbidden_kind(caplog):
    k8s = _FakeK8s()
    operator = _with_k8s(_llm_must_not_run, k8s)
    with caplog.at_level(logging.WARNING):
        snapshot = operator._ground_truth_snapshot()

    assert 'river-history-ingest-1' in snapshot['workloads']
    assert snapshot['ingresses'] == set()
    assert 'ubuntu-cm5-01' in snapshot['nodes']
    assert [a[1] for a in k8s.calls] == list(CFOperator._GROUND_TRUTH_KINDS), \
        "one kubectl call per kind, so a Forbidden kind cannot take the others down"
    warned = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any('ingresses' in m and 'Forbidden' in m for m in warned), warned


def test_ground_truth_still_suppresses_with_a_kind_forbidden():
    """The failure this fixes: one Forbidden kind used to empty the whole
    snapshot, so the suppressor never fired and this claim reached the LLM."""
    operator = _with_k8s(_llm_must_not_run, _FakeK8s())
    finding = _finding('The system ubuntu-cm5-01 does not have any river-related services installed',
                       'No river services found')
    assert operator._verify_findings([finding]) == []


def test_a_capped_verification_is_kept_but_marked():
    def chat_stub(**kwargs):
        return {'response': '[{"severity": "warning", "finding": "plane-api restarts every few minutes", '
                            '"evidence": "fresh"}]',
                'tool_calls': 3, 'stop_reason': 'cap'}

    out = _make_operator(chat_stub)._verify_findings([_finding('plane-api restarts every few minutes')])
    assert len(out) == 1
    assert out[0]['verification'] == 'capped'


def test_an_answered_verification_is_not_marked():
    def chat_stub(**kwargs):
        return {'response': '[{"severity": "warning", "finding": "plane-api restarts every few minutes", '
                            '"evidence": "fresh"}]',
                'tool_calls': 2, 'stop_reason': 'answered'}

    out = _make_operator(chat_stub)._verify_findings([_finding('plane-api restarts every few minutes')])
    assert len(out) == 1 and 'verification' not in out[0]


def test_the_verifier_is_handed_the_names_that_already_resolve():
    captured = {}

    def chat_stub(**kwargs):
        captured.update(kwargs)
        return {'response': '[]', 'tool_calls': 1}

    operator = _with_k8s(chat_stub, _FakeK8s())
    operator._verify_findings([_finding('plane-api on ubuntu-cm5-01 restarts every few minutes',
                                        'restart counter climbing')])

    msg = captured['messages'][0]['content']
    assert 'Already resolved from the live cluster' in msg
    resolved = msg.split('Already resolved from the live cluster', 1)[1]
    assert 'plane-api' in resolved
    assert 'river-history-ingest-1' not in resolved, 'a workload the finding never names rode along'
    assert 'ubuntu-cm5-01=True' in resolved


def test_a_partial_listing_never_says_a_name_does_not_exist():
    """Review finding on #333: with a kind Forbidden, "no workload matches"
    would read as "does not exist" to a verifier told not to re-check."""
    captured = {}

    def chat_stub(**kwargs):
        captured.update(kwargs)
        return {'response': '[]', 'tool_calls': 1}

    operator = _with_k8s(chat_stub, _FakeK8s(forbidden=('cronjobs', 'ingresses')))
    operator._verify_findings([_finding('the nightly-backup-run cronjob is missing', 'no such job')])
    msg = captured['messages'][0]['content']
    assert 'No workload in the cluster matches' not in msg
    assert 'incomplete: cronjobs, ingresses could not be read' in msg
    assert 'inconclusive' in msg
    # And it must not send the 2-4 call budget at the kinds that just said Forbidden.
    assert 'check' not in msg.split('Already resolved from the live cluster', 1)[1].lower()


def _river_cluster():
    """The named pod plus fourteen river-* siblings: more than the hint's cut."""
    names = ['river-history-ingest-1'] + [f'river-{s}-poller' for s in
                                           'ab cd ef gh ij kl mn op qr st uv wx yz zz'.split()]
    k8s = _FakeK8s(forbidden=())
    k8s._run_kubectl = lambda args, timeout=30: {
        'success': True, 'exit_code': 0, 'stderr': '',
        'stdout': ''.join(f'{args[1]}/{n}\n' for n in names)}
    return k8s


def test_a_segment_of_a_hyphenated_name_does_not_widen_the_hint():
    """Review on #333: `\\b` splits river-history-ingest-1 into river, history
    and ingest too, and `river` alone matched every river-* sibling."""
    operator = _with_k8s(lambda **kw: {'response': '[]', 'tool_calls': 1}, _river_cluster())
    snapshot = operator._ground_truth_snapshot()
    hint = operator._resolved_identities(_finding('river-history-ingest-1 restarts every hour'), snapshot)
    assert 'river-history-ingest-1' in hint
    assert 'poller' not in hint, hint


def test_the_named_pod_survives_the_cut():
    """Exact matches rank first, so twelve siblings cannot push the pod the
    finding named off the list."""
    operator = _with_k8s(lambda **kw: {'response': '[]', 'tool_calls': 1}, _river_cluster())
    snapshot = operator._ground_truth_snapshot()
    # A finding that names the pod AND a bare `river` that is not one of its
    # segments' siblings' prefix … the plain word rides along as a prefix match.
    hint = operator._resolved_identities(
        _finding('river-history-ingest-1 and the other river jobs restart', 'river pods'), snapshot)
    listed = hint.split(': ', 1)[1].split('\n', 1)[0].split(', ')
    assert listed[0] == 'river-history-ingest-1'
    assert len(listed) <= 12


def test_a_complete_listing_may_say_no_name_matches():
    captured = {}

    def chat_stub(**kwargs):
        captured.update(kwargs)
        return {'response': '[]', 'tool_calls': 1}

    operator = _with_k8s(chat_stub, _FakeK8s(forbidden=()))
    operator._verify_findings([_finding('the ghost-service-x deployment restarts', 'x')])
    msg = captured['messages'][0]['content']
    assert 'No workload in the cluster matches' in msg
    assert 'Could not list' not in msg


def test_a_stagnation_stop_is_marked_like_the_cap():
    def chat_stub(**kwargs):
        return {'response': '[{"severity": "warning", "finding": "plane-api restarts every few minutes", '
                            '"evidence": "fresh"}]',
                'tool_calls': 3, 'stop_reason': 'stagnation'}

    out = _make_operator(chat_stub)._verify_findings([_finding('plane-api restarts every few minutes')])
    assert out[0]['verification'] == 'stagnation'


def test_a_plain_token_matches_only_a_first_segment():
    """`server` must not pull every *-server-* in the cluster into the hint."""
    k8s = _FakeK8s(forbidden=())
    k8s._run_kubectl = lambda args, timeout=30: {
        'success': True, 'exit_code': 0, 'stderr': '',
        'stdout': f'{args[1]}/server-main\n{args[1]}/nginx-server-7f\n'}
    operator = _with_k8s(lambda **kw: {'response': '[]', 'tool_calls': 1}, k8s)
    snapshot = operator._ground_truth_snapshot()
    hint = operator._resolved_identities(_finding('the server process is slow'), snapshot)
    assert 'server-main' in hint and 'nginx-server-7f' not in hint


def test_no_preamble_without_a_snapshot():
    captured = {}

    def chat_stub(**kwargs):
        captured.update(kwargs)
        return {'response': '[]', 'tool_calls': 1}

    _make_operator(chat_stub)._verify_findings([_finding('plane-api restarts')])
    assert 'Already resolved' not in captured['messages'][0]['content']
