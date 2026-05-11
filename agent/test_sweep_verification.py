#!/usr/bin/env python3
"""Focused tests for sweep finding verification."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


class _StubKB:
    def get_setting(self, name, default=''):
        return ''


def _make_operator(chat_stub):
    operator = CFOperator.__new__(CFOperator)
    operator.config = {'chat': {'max_tool_iterations': 6}}
    operator.kb = _StubKB()
    operator._resolve_provider = lambda: ('ollama', 'http://localhost:11434', 'test-model')
    operator._get_infra_summary = lambda: 'Cluster namespaces: apps, monitoring, data'
    operator._chat_with_tools = chat_stub
    return operator


def test_verify_findings_keeps_freshly_verified_finding():
    captured = {}

    def chat_stub(**kwargs):
        captured.update(kwargs)
        return {
            'response': '[{"severity": "warning", "finding": "cfoperator is exposed through ingress", "evidence": "Fresh verification found service cfoperator and ingress cfoperator in apps namespace"}]',
            'tool_calls': 2,
        }

    operator = _make_operator(chat_stub)
    findings = [{
        'severity': 'warning',
        'finding': 'cfoperator may not be exposed',
        'evidence': 'Earlier sweep saw a missing route',
        'remediation': 'Inspect service and ingress wiring',
    }]

    verified = operator._verify_findings(findings)

    assert len(verified) == 1
    assert verified[0]['finding'] == 'cfoperator is exposed through ingress'
    assert verified[0]['remediation'] == 'Inspect service and ingress wiring'
    assert captured['max_iterations'] == 4
    assert 'DISPROVE a drafted finding' in captured['system_context']


def test_verify_findings_drops_disproved_or_unchecked_findings():
    responses = iter([
        {'response': '[]', 'tool_calls': 1},
        {
            'response': '[{"severity": "warning", "finding": "log stream is missing", "evidence": "No evidence provided"}]',
            'tool_calls': 0,
        },
    ])

    operator = _make_operator(lambda **kwargs: next(responses))
    findings = [
        {'severity': 'warning', 'finding': 'service is missing', 'evidence': 'Old evidence'},
        {'severity': 'warning', 'finding': 'log stream is missing', 'evidence': 'Old evidence'},
    ]

    assert operator._verify_findings(findings) == []


def test_verify_findings_returns_original_findings_when_verifier_fails():
    def chat_stub(**kwargs):
        raise RuntimeError('verification backend unavailable')

    operator = _make_operator(chat_stub)
    findings = [{'severity': 'warning', 'finding': 'service is missing', 'evidence': 'Old evidence'}]

    assert operator._verify_findings(findings) == findings


# ---------------------------------------------------------------------------
# Ground-truth suppressor (deterministic kubectl-based pre-filter)
# ---------------------------------------------------------------------------

def _snapshot(nodes=None, workloads=None, ingresses=None):
    return {
        'nodes': {n['name'].lower(): n for n in (nodes or [])},
        'workloads': set(w.lower() for w in (workloads or [])),
        'ingresses': set(i.lower() for i in (ingresses or [])),
    }


def test_ground_truth_suppresses_kubelet_claim_on_ready_node():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(nodes=[{
        'name': 'ubuntu-cm5-01',
        'ready': 'True',
        'memoryPressure': 'False',
        'diskPressure': 'False',
        'kubeletVersion': 'v1.31.4+k3s1',
    }])

    finding = {
        'severity': 'warning',
        'finding': 'The master node ubuntu-cm5-01 has a kubelet service issue that may impact cluster stability',
        'evidence': 'kubelet service appears not running on ubuntu-cm5-01',
    }

    reason = operator._ground_truth_suppress(finding, snapshot)
    assert reason is not None
    assert 'ubuntu-cm5-01' in reason
    assert 'k3s embeds the kubelet' in reason


def test_ground_truth_suppresses_missing_workload_when_pod_exists():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(workloads=['river-history-ingest-29625252-8ltx8'])

    finding = {
        'severity': 'warning',
        'finding': 'The system ubuntu-cm5-01 does not have any river-related services, containers, or files installed',
        'evidence': 'No river services found',
    }

    reason = operator._ground_truth_suppress(finding, snapshot)
    assert reason is not None
    assert 'river' in reason


def test_ground_truth_does_not_suppress_truly_missing_workload():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(workloads=['prometheus-0', 'kube-state-metrics-6744db59c4-rnj8k'])

    finding = {
        'severity': 'critical',
        'finding': 'The grafana-dashboards-loader deployment is not installed',
        'evidence': 'Cannot find grafana-dashboards-loader in any namespace',
    }

    assert operator._ground_truth_suppress(finding, snapshot) is None


def test_ground_truth_does_not_suppress_when_node_actually_not_ready():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(nodes=[{
        'name': 'raspberrypi3',
        'ready': 'False',
        'memoryPressure': 'False',
        'diskPressure': 'True',
        'kubeletVersion': 'v1.31.4+k3s1',
    }])

    finding = {
        'severity': 'critical',
        'finding': 'raspberrypi3 has a kubelet service issue impacting stability',
        'evidence': 'Node not posting ready status',
    }

    assert operator._ground_truth_suppress(finding, snapshot) is None


def test_ground_truth_ignores_generic_stopwords():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    # 'monitoring' / 'metrics' should never cause a substring hit even though
    # many cluster pods contain those words.
    snapshot = _snapshot(workloads=['kube-state-metrics-6744db59c4-rnj8k', 'prometheus-0'])

    finding = {
        'severity': 'critical',
        'finding': 'No active kubelet, containerd, or docker metrics found in monitoring system',
        'evidence': 'Prometheus has no active scrape targets for these',
    }

    # This finding is hard to verify deterministically; suppressor must NOT
    # hallucinate a match off the word "metrics" / "monitoring".
    assert operator._ground_truth_suppress(finding, snapshot) is None


def test_ground_truth_snapshot_is_none_without_k8s_tools():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    # _make_operator does not set operator.tools at all
    assert operator._ground_truth_snapshot() is None


# ---------------------------------------------------------------------------
# Pattern 3: phantom scrape-target / "not being scraped" claims
# ---------------------------------------------------------------------------

def test_ground_truth_suppresses_scrape_target_claim_when_workload_exists():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(workloads=['node-exporter-xk2p9'])

    finding = {
        'severity': 'warning',
        'finding': 'Prometheus has no scrape target for node-exporter; metrics are not being scraped',
        'evidence': 'prometheus_query kube_pod_info{pod="node-exporter"} returned no data',
    }

    reason = operator._ground_truth_suppress(finding, snapshot)
    assert reason is not None
    assert 'node-exporter' in reason
    assert 'prometheus_query result' in reason


def test_ground_truth_does_not_suppress_scrape_claim_for_truly_absent_workload():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(workloads=['prometheus-0', 'kube-state-metrics-6744db59c4-rnj8k'])

    finding = {
        'severity': 'warning',
        'finding': 'blackbox-exporter is not reporting metrics; no active scrape target found',
        'evidence': 'prometheus_query up{job="blackbox"} returned empty',
    }

    assert operator._ground_truth_suppress(finding, snapshot) is None


# ---------------------------------------------------------------------------
# Pattern 4: node-absent claims disproved by node presence in snapshot
# ---------------------------------------------------------------------------

def test_ground_truth_suppresses_node_absent_claim_when_node_in_snapshot():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(nodes=[{
        'name': 'ubuntu-cm5-02',
        'ready': 'True',
        'memoryPressure': 'False',
        'diskPressure': 'False',
        'kubeletVersion': 'v1.31.4+k3s1',
    }])

    finding = {
        'severity': 'critical',
        'finding': 'ubuntu-cm5-02 is missing from cluster; node not found in kube_node_info',
        'evidence': 'kube_node_info{node="ubuntu-cm5-02"} returned no series',
    }

    reason = operator._ground_truth_suppress(finding, snapshot)
    assert reason is not None
    assert 'ubuntu-cm5-02' in reason
    assert 'present in the cluster' in reason


def test_ground_truth_does_not_suppress_node_absent_claim_for_unknown_node():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(nodes=[{
        'name': 'ubuntu-cm5-01',
        'ready': 'True',
        'memoryPressure': 'False',
        'diskPressure': 'False',
        'kubeletVersion': 'v1.31.4+k3s1',
    }])

    finding = {
        'severity': 'critical',
        'finding': 'worker-node-99 is not registered in the cluster',
        'evidence': 'kubectl get nodes shows no worker-node-99',
    }

    assert operator._ground_truth_suppress(finding, snapshot) is None


# ---------------------------------------------------------------------------
# Pattern 5: phantom ingress/exposure claims
# ---------------------------------------------------------------------------

def test_ground_truth_suppresses_exposure_claim_when_ingress_exists():
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(
        workloads=['cfoperator-7d9f8b4c6-xkp2r', 'cfoperator-ingress'],
        ingresses=['cfoperator-ingress'],
    )

    finding = {
        'severity': 'warning',
        'finding': 'cfoperator is not exposed externally; no ingress rule found for the service',
        'evidence': 'k8s_get_ingresses returned no entries matching cfoperator',
    }

    reason = operator._ground_truth_suppress(finding, snapshot)
    assert reason is not None
    assert 'cfoperator' in reason
    assert 'ingress' in reason


def test_ground_truth_does_not_suppress_exposure_claim_when_only_pod_exists():
    # Pod/service exists but no ingress — "not exposed" may be a real finding.
    operator = _make_operator(lambda **kwargs: {'response': '[]', 'tool_calls': 0})
    snapshot = _snapshot(
        workloads=['internal-api-7d9f8b4c6-xkp2r'],
        ingresses=[],
    )

    finding = {
        'severity': 'warning',
        'finding': 'internal-api is not publicly accessible; no ingress rule found',
        'evidence': 'k8s_get_ingresses returned no entries for internal-api',
    }

    assert operator._ground_truth_suppress(finding, snapshot) is None