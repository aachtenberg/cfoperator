"""Tier-1 noise-filter tests: recovered-and-healthy detection + early-exit/downgrade.

The guard must silence the faster-whisper class (healthy now, one old restart)
while still investigating flapping, still-broken, and non-runtime cases.
"""

from agent import CFOperator
from agent.agent import _RECOVERABLE_TRIGGER


def _healthy(restarts=1):
    return {"success": True, "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": restarts}]}


def _pending():
    return {"success": True, "phase": "Pending", "conditions": [], "containerStatuses": []}


class _K8s:
    def __init__(self, status):
        self._s = status
    def get_pod_status(self, ns, pod):
        return self._s


def _op(status):
    op = CFOperator.__new__(CFOperator)
    op.tools = type("T", (), {"k8s_tools": _K8s(status)})()
    return op


FW_TRIGGER = ("Container 'faster-whisper' in namespace 'ai' has restarted once "
              "(restartCount 1) and its previous container terminated with exit code 255")


def test_recoverable_trigger_regex():
    assert _RECOVERABLE_TRIGGER.search(FW_TRIGGER)
    assert _RECOVERABLE_TRIGGER.search("Pod x not ready for 30m")
    assert _RECOVERABLE_TRIGGER.search("OOMKilled")
    assert not _RECOVERABLE_TRIGGER.search("High CPU usage on headless-gpu")


def test_recovered_and_healthy_true_for_faster_whisper():
    op = _op(_healthy(restarts=1))
    recovered, note, restarts = op._recovered_and_healthy(
        {"namespace": "ai", "resource_name": "faster-whisper-x"}, FW_TRIGGER)
    assert recovered is True and restarts == 1
    assert "recovered" in note


def test_not_recovered_when_pod_pending():
    op = _op(_pending())
    recovered, _, _ = op._recovered_and_healthy(
        {"namespace": "ai", "resource_name": "fw-x"}, FW_TRIGGER)
    assert recovered is False


def test_not_recovered_for_non_runtime_trigger():
    op = _op(_healthy(restarts=1))
    recovered, _, _ = op._recovered_and_healthy(
        {"namespace": "ai", "resource_name": "fw-x"}, "High CPU usage on node")
    assert recovered is False  # healthy pod but not a runtime/restart alert


def test_early_exit_records_monitoring():
    op = _op(_healthy(restarts=1))
    # stub kb so _early_exit_monitoring can record
    op.kb = type("KB", (), {"update_investigation": lambda self, **k: None})()
    import time
    res = op._early_exit_monitoring(99, FW_TRIGGER, time.time(), "ai/fw healthy now (1 restart, recovered)")
    assert res["success"] is True
    assert res["details"]["outcome"] == "monitoring"
    assert res["details"]["preflight_skip"] is True
    assert "No action needed" in res["details"]["remediation"]


# threshold behaviour: low restarts -> noise; high restarts -> still real
def test_threshold_low_restarts_is_noise():
    op = _op(_healthy(restarts=2))
    recovered, _, restarts = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    assert recovered and restarts <= 3  # would early-exit


def test_threshold_high_restarts_still_investigated():
    op = _op(_healthy(restarts=11))
    recovered, _, restarts = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    # recovered True, but restarts > 3 => caller does NOT early-exit / downgrade
    assert recovered and restarts == 11


# --- Gap 1: sweep-finding restart suppression --------------------------------

class _K8sPods:
    def __init__(self, pods):
        self._pods = pods
    def get_pods(self, namespace="default", all_namespaces=False, labels=None):
        return {"pods": self._pods}


def _pod(name, phase="Running", ready=True, restarts=1):
    return {"metadata": {"name": name, "namespace": "ai"},
            "status": {"phase": phase,
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"restartCount": restarts}]}}


def _op_with_pods(pods):
    op = CFOperator.__new__(CFOperator)
    op.tools = type("T", (), {"k8s_tools": _K8sPods(pods)})()
    return op


FW_FINDING = "Container 'faster-whisper' in namespace 'ai' has restarted once (restartCount 1)"


def test_restart_finding_suppressed_when_healthy_low_restarts():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq", restarts=1)])
    reason = op._restart_finding_is_noise(FW_FINDING.lower(), 3)
    assert reason and "recovered transient" in reason


def test_restart_finding_kept_when_flapping():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq", restarts=11)])
    assert op._restart_finding_is_noise(FW_FINDING.lower(), 3) is None


def test_restart_finding_kept_when_unhealthy_now():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq", phase="CrashLoopBackOff", ready=False, restarts=2)])
    assert op._restart_finding_is_noise(FW_FINDING.lower(), 3) is None


def test_restart_finding_noop_when_no_match():
    op = _op_with_pods([])
    assert op._restart_finding_is_noise(FW_FINDING.lower(), 3) is None


def test_non_restart_finding_ignored():
    op = _op_with_pods([_pod("x")])
    assert op._restart_finding_is_noise("high cpu usage on node", 3) is None
