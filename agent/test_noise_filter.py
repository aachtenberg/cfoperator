"""Tier-1 noise-filter tests: recovered-and-healthy detection + early-exit/downgrade.

The guard must silence the faster-whisper class (healthy now, one old restart)
while still investigating flapping, still-broken, and non-runtime cases.
"""

from datetime import datetime, timedelta, timezone

import pytest

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


# --- Gap 2: probe-class findings authored by the sweep (CFOP-21) --------------
#
# The sweep writes free-form prose with no structured resource fields, so both
# noise-filter gates missed the whole class: the trigger vocabulary had no
# probe wording, and _identify_pod's two shapes never matched the prose. These
# six triggers are the real ones from investigations 2122/2123/2125-2128 —
# ~11 minutes of model time on a pod that never left Ready.

PLANE_TRIGGERS = [
    "Deployment 'plane-api-wl' is experiencing intermittent readiness probe failures.",
    "Readiness probe failures in plane-api pod causing potential service unavailability",
    "The plane-api service is experiencing intermittent readiness probe failures due to request timeouts.",
    "The 'plane-api' pod in the 'plane' namespace is experiencing intermittent readiness "
    "probe failures caused by request timeouts.",
    "Recent readiness probe failures for 'plane-api' deployment",
    "Increase plane-api probe timeout: Update readinessProbe timeoutSeconds from 1 to 5 "
    "in the helm chart/manifest [proposed: gitops-patch]",
]

PLANE_API_POD = "plane-api-wl-79589b67b5-gthmp"


def _cluster_pod(name, namespace, ready_since_s=86400, restarts=0,
                 phase="Running", ready=True, ready_transition=True):
    """A pod as `kubectl get pods -A -o json` returns it."""
    cond = {"type": "Ready", "status": "True" if ready else "False"}
    if ready_transition:
        when = datetime.now(timezone.utc) - timedelta(seconds=ready_since_s)
        cond["lastTransitionTime"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"metadata": {"name": name, "namespace": namespace},
            "status": {"phase": phase, "conditions": [cond],
                       "containerStatuses": [{"restartCount": restarts}]}}


class _K8sCluster:
    """Serves both calls the filter makes: get_pods to resolve the name,
    get_pod_status to check the pinned pod's health."""

    def __init__(self, pods):
        self._pods = pods

    def get_pods(self, namespace="default", labels=None, all_namespaces=False):
        return {"success": True, "pods": self._pods}

    def get_pod_status(self, ns, pod):
        for p in self._pods:
            if p["metadata"]["name"] == pod and p["metadata"]["namespace"] == ns:
                return {"success": True, **p["status"]}
        return {"success": False}


def _op_cluster(pods):
    op = CFOperator.__new__(CFOperator)
    op.tools = type("T", (), {"k8s_tools": _K8sCluster(pods)})()
    return op


def _plane_cluster(**api_pod_kwargs):
    """The real plane namespace shape: several sibling workloads that must not
    be confused with plane-api, plus an unrelated namespace."""
    return _op_cluster([
        _cluster_pod(PLANE_API_POD, "plane", **api_pod_kwargs),
        _cluster_pod("plane-admin-wl-5b48d9bbbc-t875l", "plane"),
        _cluster_pod("plane-web-wl-5c54564f9c-tkglq", "plane"),
        _cluster_pod("plane-worker-wl-57f6d558b-n9nlc", "plane"),
        _cluster_pod("plane-beat-worker-wl-6ddc478478-mn4v5", "plane"),
        _cluster_pod("faster-whisper-fcf845fbb-g47bq", "ai"),
    ])


def test_recoverable_trigger_covers_probe_class():
    for trigger in PLANE_TRIGGERS:
        assert _RECOVERABLE_TRIGGER.search(trigger), trigger
    assert not _RECOVERABLE_TRIGGER.search("High CPU usage on headless-gpu")


@pytest.mark.parametrize("trigger", PLANE_TRIGGERS)
def test_real_sweep_probe_triggers_are_filtered(trigger):
    """Every phrasing the sweep actually produced must resolve to the one pod
    and read as recovered — the class, not one wording."""
    op = _plane_cluster()
    recovered, note, restarts = op._recovered_and_healthy({}, trigger)
    assert recovered is True, trigger
    assert f"plane/{PLANE_API_POD}" in note
    # investigate() early-exits on `recovered and restarts <= threshold`
    # (default recovered_restart_threshold: 3).
    assert restarts <= 3


def test_identify_pod_still_returns_none_for_sweep_prose():
    """The live-state resolver is deliberately NOT wired into _identify_pod:
    B1 outcome verification and the Phase-B remediation proposer share it and
    keep their narrower contract. If this starts passing a pod through, that
    widening was accidental."""
    for trigger in PLANE_TRIGGERS:
        assert CFOperator._identify_pod({}, trigger) is None, trigger


def test_ambiguous_trigger_naming_two_workloads_is_not_filtered():
    op = _plane_cluster()
    recovered, _, _ = op._recovered_and_healthy(
        {}, "Readiness probe failures on plane-api and plane-web in the plane namespace")
    assert recovered is False


def test_multi_replica_workload_is_not_filtered():
    """One workload but several pods — the filter checks a single pod's health,
    so it must not guess which replica the finding meant."""
    op = _op_cluster([
        _cluster_pod(PLANE_API_POD, "plane"),
        _cluster_pod("plane-api-wl-79589b67b5-k4t2p", "plane"),
    ])
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[4])
    assert recovered is False


def test_unknown_workload_is_not_filtered():
    op = _plane_cluster()
    recovered, _, _ = op._recovered_and_healthy(
        {}, "Readiness probe failures for 'ghost-service' deployment")
    assert recovered is False


def test_probe_trigger_not_filtered_when_readiness_just_flapped():
    """The flapping guard for this class. restartCount stays 0 however badly a
    readiness probe flaps, so Ready-hold time is the only signal that separates
    a settled pod from one being pulled in and out of its Service."""
    op = _plane_cluster(ready_since_s=60)
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is False


def test_probe_trigger_not_filtered_when_ready_transition_unknown():
    op = _plane_cluster(ready_transition=False)
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is False


def test_probe_trigger_note_reports_ready_hold_time():
    op = _plane_cluster(ready_since_s=7200)
    _, note, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert "Ready 120m" in note


def test_restart_class_is_not_subject_to_the_ready_stability_guard():
    """A restart-class trigger keeps its own guard (restartCount) and must not
    inherit the probe class's Ready-hold requirement."""
    op = _op_cluster([_cluster_pod("faster-whisper-fcf845fbb-g47bq", "ai",
                                   ready_since_s=5, restarts=1)])
    recovered, _, restarts = op._recovered_and_healthy({}, FW_TRIGGER)
    assert recovered is True and restarts == 1


def test_probe_trigger_not_filtered_when_pod_unhealthy_now():
    op = _plane_cluster(phase="CrashLoopBackOff", ready=False)
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is False
