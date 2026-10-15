"""CFOP-408: a finished Job/CronJob pod is not a container-sweep finding.

With restartPolicy=Never a failed run is terminally Failed by design and stays
until ttlSecondsAfterFinished GC's it, so reporting it re-fires every sweep
tick for that window. Alertmanager's CronJob rules already own retained
failed Jobs. Guards both the direct status loop and the stopped list handed
to the LLM phase (which otherwise re-narrates the same Job in new words each
tick, defeating the text-keyed dedupe).
"""
from agent import CFOperator


class _Containers:
    runtime_names = ['kubernetes']

    def __init__(self, pods):
        self._pods = pods

    def list_containers(self, host=None):
        return list(self._pods)


def _sweep(monkeypatch, pods):
    op = CFOperator.__new__(CFOperator)
    op.containers = _Containers(pods)
    op.tools = None  # no k8s observation summary
    prompts = []

    def _fake_llm(prompt, **kw):
        prompts.append(prompt)
        return []
    monkeypatch.setattr(op, '_sweep_with_llm', _fake_llm)
    findings = op._sweep_containers()
    return findings, prompts


_JOB_POD = {'name': 'freshet-synthetic-check-29867587-8v6rz', 'host': 'apps', 'status': 'stopped'}
_DEPLOY_POD = {'name': 'immich-server-868f4dd875-p7kcc', 'host': 'apps', 'status': 'stopped'}
_RUNNING = {'name': 'loki-0', 'host': 'monitoring', 'status': 'running'}


def test_failed_job_pod_is_not_a_finding(monkeypatch):
    findings, prompts = _sweep(monkeypatch, [_JOB_POD, _RUNNING])
    assert findings == []
    assert 'freshet-synthetic-check' not in prompts[0]
    assert 'Stopped/unhealthy' not in prompts[0]


def test_failed_deployment_pod_still_reported(monkeypatch):
    findings, prompts = _sweep(monkeypatch, [_JOB_POD, _DEPLOY_POD, _RUNNING])
    assert [f['finding'] for f in findings] == ['immich-server-868f4dd875-p7kcc on apps: status=stopped']
    assert 'Stopped/unhealthy: immich-server-868f4dd875-p7kcc' in prompts[0]
    assert 'freshet-synthetic-check' not in prompts[0]


def test_llm_phase_is_told_not_to_report_retained_jobs(monkeypatch):
    _, prompts = _sweep(monkeypatch, [_RUNNING])
    assert 'Failed Job' in prompts[0] and 'Alertmanager' in prompts[0]
