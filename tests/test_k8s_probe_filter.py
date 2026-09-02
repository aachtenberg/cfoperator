"""Tests for kubelet-probe disconnect filtering in K8sTools.get_pod_logs.

Sweep #1001 misattributed mosquitto kubelet-probe disconnect noise as IoT
device instability, and laundered it into learning #1456. The filter strips
the noise at the tool layer so the LLM never sees it.
"""

import json

from tools.k8s import K8sTools, _collapse_probe_disconnects


PROBE_LINE = (
    "2026-05-23T12:27:39: Client 10.42.0.1 [10.42.0.1:60414] "
    "disconnected: connection closed by client."
)
REAL_CLIENT_LINE = (
    "2026-05-23T12:27:40: Client auto-2822DC44-A273-BD33-E667-A7CE1E194363 "
    "[10.42.1.0:50816] disconnected: connection closed by client."
)
PICO_TIMEOUT_LINE = (
    "2026-05-23T10:00:12: Client pico_202ef2a82ef0a5af "
    "[10.0.0.30:52730] disconnected: exceeded timeout."
)


def test_collapse_drops_probe_lines():
    logs = "\n".join([PROBE_LINE, REAL_CLIENT_LINE, PROBE_LINE, PICO_TIMEOUT_LINE])
    out, suppressed = _collapse_probe_disconnects(logs)
    assert suppressed == 2
    assert PROBE_LINE not in out
    assert REAL_CLIENT_LINE in out
    assert PICO_TIMEOUT_LINE in out


def test_collapse_noop_when_no_probe_pattern():
    logs = "\n".join([REAL_CLIENT_LINE, PICO_TIMEOUT_LINE, "random log noise"])
    out, suppressed = _collapse_probe_disconnects(logs)
    assert suppressed == 0
    assert out == logs


def test_get_pod_logs_filters_when_pod_has_tcp_probe(monkeypatch):
    tools = K8sTools()
    pod_spec = {
        "spec": {
            "containers": [{
                "name": "mosquitto",
                "livenessProbe": {"tcpSocket": {"port": 1883}},
                "readinessProbe": {"tcpSocket": {"port": 1883}},
            }]
        }
    }
    logs_stdout = "\n".join([PROBE_LINE, REAL_CLIENT_LINE, PROBE_LINE])

    def fake_run(args, timeout=30):
        if 'logs' in args:
            return {'success': True, 'stdout': logs_stdout}
        if 'get' in args and 'pod' in args:
            return {'success': True, 'stdout': json.dumps(pod_spec)}
        return {'success': False}

    monkeypatch.setattr(tools, '_run_kubectl', fake_run)
    out = tools.get_pod_logs('iot', 'mosquitto-0')

    assert out['success']
    assert PROBE_LINE not in out['logs']
    assert REAL_CLIENT_LINE in out['logs']
    assert '2 probe-noise disconnect lines suppressed' in out['logs']


def test_get_pod_logs_no_filter_when_pod_has_no_tcp_probe(monkeypatch):
    tools = K8sTools()
    pod_spec = {"spec": {"containers": [{"name": "app"}]}}  # no probes
    logs_stdout = "\n".join([PROBE_LINE, REAL_CLIENT_LINE])

    def fake_run(args, timeout=30):
        if 'logs' in args:
            return {'success': True, 'stdout': logs_stdout}
        if 'get' in args and 'pod' in args:
            return {'success': True, 'stdout': json.dumps(pod_spec)}
        return {'success': False}

    monkeypatch.setattr(tools, '_run_kubectl', fake_run)
    out = tools.get_pod_logs('iot', 'whatever')

    assert out['logs'] == logs_stdout
    assert 'suppressed' not in out['logs']


def test_get_tcp_probe_ports_handles_missing_pod(monkeypatch):
    tools = K8sTools()
    monkeypatch.setattr(
        tools, '_run_kubectl',
        lambda args, timeout=30: {'success': False, 'stderr': 'NotFound'}
    )
    assert tools._get_tcp_probe_ports('ns', 'gone') == set()
