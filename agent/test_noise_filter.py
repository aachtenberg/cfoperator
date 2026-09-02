"""Tier-1 noise-filter tests: recovered-and-healthy detection + early-exit/downgrade.

The guard must silence the faster-whisper / camera-api class (healthy now,
last restart aged out — even when lifetime restartCount is high) while still
investigating a recent restart, still-broken, and non-runtime cases.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent import CFOperator
from agent.agent import _RECOVERABLE_TRIGGER


def _iso_ago(seconds):
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _healthy(restarts=1, last_restart_s=86400):
    """A Running+Ready pod. last_restart_s=None omits lastState (unknown)."""
    cs = {"restartCount": restarts}
    if last_restart_s is not None and restarts > 0:
        cs["lastState"] = {"terminated": {"finishedAt": _iso_ago(last_restart_s)}}
    return {"success": True, "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [cs]}


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


# Age, not lifetime count: a settled last restart is noise even with a high
# count; a recent last restart is still real even with a low count.
def test_threshold_settled_last_restart_is_noise():
    op = _op(_healthy(restarts=2, last_restart_s=86400))
    recovered, note, restarts = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    assert recovered and restarts == 2
    assert "last restart" in note


def test_camera_api_high_lifetime_count_old_last_restart_is_recovered():
    """The CFOP-150 bug: lifetime restartCount 13, last termination 87 days
    ago, Ready the whole time. Count-gated early-exit refused this; age must
    accept it. Mutation-check: the recent-restart sibling below is the
    same shape with a fresh finishedAt, and must *not* recover."""
    eighty_seven_days = 87 * 86400
    op = _op(_healthy(restarts=13, last_restart_s=eighty_seven_days))
    recovered, note, restarts = op._recovered_and_healthy(
        {"namespace": "apps", "resource_name": "camera-api-x"}, FW_TRIGGER)
    assert recovered is True and restarts == 13
    assert "last restart 87d" in note


def test_recent_last_restart_still_investigated():
    op = _op(_healthy(restarts=1, last_restart_s=120))
    recovered, _, restarts = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    # recovered False: the age guard lives inside _recovered_and_healthy now,
    # so the caller only has to check the flag. Count=1 would have passed
    # the old recovered_restart_threshold: 3.
    assert recovered is False and restarts == 0


def test_unknown_last_restart_timestamp_is_not_filtered():
    """restartCount > 0 but no finishedAt: can't tell when it last died, so
    the filter must not silence. Same bias as the probe class's unknown
    Ready transition."""
    op = _op(_healthy(restarts=1, last_restart_s=None))
    recovered, _, _ = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    assert recovered is False


def test_never_restarted_is_recovered():
    op = _op(_healthy(restarts=0, last_restart_s=None))
    recovered, note, restarts = op._recovered_and_healthy({"namespace": "ai", "resource_name": "x"}, FW_TRIGGER)
    assert recovered is True and restarts == 0
    assert "never restarted" in note


# --- Gap 1: sweep-finding restart suppression --------------------------------

class _K8sPods:
    def __init__(self, pods):
        self._pods = pods
    def get_pods(self, namespace="default", all_namespaces=False, labels=None):
        return {"pods": self._pods}


def _pod(name, phase="Running", ready=True, restarts=1, last_restart_s=86400):
    cs = {"restartCount": restarts}
    if last_restart_s is not None and restarts > 0:
        cs["lastState"] = {"terminated": {"finishedAt": _iso_ago(last_restart_s)}}
    return {"metadata": {"name": name, "namespace": "ai"},
            "status": {"phase": phase,
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [cs]}}


def _op_with_pods(pods):
    op = CFOperator.__new__(CFOperator)
    op.tools = type("T", (), {"k8s_tools": _K8sPods(pods)})()
    return op


FW_FINDING = "Container 'faster-whisper' in namespace 'ai' has restarted once (restartCount 1)"


def test_restart_finding_suppressed_when_healthy_old_last_restart():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq", restarts=1)])
    reason = op._restart_finding_is_noise(FW_FINDING.lower())
    assert reason and "recovered transient" in reason


def test_restart_finding_suppressed_when_high_lifetime_count_but_old():
    """Sweep analogue of camera-api: count 13, last restart 87 days ago."""
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq",
                             restarts=13, last_restart_s=87 * 86400)])
    reason = op._restart_finding_is_noise(
        "container 'faster-whisper' in namespace 'ai' has restarted 13 times")
    assert reason and "recovered transient" in reason


def test_restart_finding_kept_when_last_restart_recent():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq",
                             restarts=1, last_restart_s=120)])
    assert op._restart_finding_is_noise(FW_FINDING.lower()) is None


def test_restart_finding_kept_when_last_restart_unknown():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq",
                             restarts=2, last_restart_s=None)])
    assert op._restart_finding_is_noise(FW_FINDING.lower()) is None


def test_restart_finding_kept_when_unhealthy_now():
    op = _op_with_pods([_pod("faster-whisper-fcf845fbb-g47bq", phase="CrashLoopBackOff", ready=False, restarts=2)])
    assert op._restart_finding_is_noise(FW_FINDING.lower()) is None


def test_restart_finding_noop_when_no_match():
    op = _op_with_pods([])
    assert op._restart_finding_is_noise(FW_FINDING.lower()) is None


def test_non_restart_finding_ignored():
    op = _op_with_pods([_pod("x")])
    assert op._restart_finding_is_noise("high cpu usage on node") is None


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
                 phase="Running", ready=True, ready_transition=True,
                 last_restart_s=None):
    """A pod as `kubectl get pods -A -o json` returns it."""
    cond = {"type": "Ready", "status": "True" if ready else "False"}
    if ready_transition:
        when = datetime.now(timezone.utc) - timedelta(seconds=ready_since_s)
        cond["lastTransitionTime"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    cs = {"restartCount": restarts}
    age = last_restart_s
    if age is None and restarts > 0:
        age = 86400  # settled default so restart-class tests that only
                     # care about Ready-hold don't fail the age guard
    if age is not None and restarts > 0:
        cs["lastState"] = {"terminated": {"finishedAt": _iso_ago(age)}}
    return {"metadata": {"name": name, "namespace": namespace},
            "status": {"phase": phase, "conditions": [cond],
                       "containerStatuses": [cs]}}


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
    # All three kubelet probe types belong to the class.
    assert _RECOVERABLE_TRIGGER.search("Startup probe failed for immich-server")
    assert _RECOVERABLE_TRIGGER.search("Liveness probe failed on immich-server")
    assert not _RECOVERABLE_TRIGGER.search("High CPU usage on headless-gpu")


@pytest.mark.parametrize("trigger", [
    "blackbox probe failing for immich-server",
    "unhealthy upstream reported by traefik for immich-server",
    "volume unhealthy on immich-server",
])
def test_probe_class_excludes_non_kubelet_probe_wording(trigger):
    """Bare "probe"/"unhealthy" reach findings that are not about a kubelet
    probe. For those the named pod being Ready says nothing about whether the
    reported problem is real, so pod health must not silence them."""
    assert not _RECOVERABLE_TRIGGER.search(trigger), trigger


@pytest.mark.parametrize("trigger", PLANE_TRIGGERS)
def test_real_sweep_probe_triggers_are_filtered(trigger):
    """Every phrasing the sweep actually produced must resolve to the one pod
    and read as recovered — the class, not one wording."""
    op = _plane_cluster()
    recovered, note, restarts = op._recovered_and_healthy({}, trigger)
    assert recovered is True, trigger
    assert f"plane/{PLANE_API_POD}" in note
    # investigate() early-exits on `recovered` alone: both class guards
    # (Ready-hold / last-restart age) live inside _recovered_and_healthy.


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


def test_probe_class_high_lifetime_count_still_recovered_when_ready_held():
    """The caller's old `pre_restarts <= 3` check blocked probe-class early-exit
    on a pod like camera-api (Ready for months, lifetime count 13). The Ready
    hold is this class's guard; lifetime count must not override it."""
    op = _plane_cluster(restarts=13, last_restart_s=87 * 86400)
    recovered, note, restarts = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is True and restarts == 13
    assert "Ready" in note


def test_probe_trigger_not_filtered_when_ready_transition_unknown():
    op = _plane_cluster(ready_transition=False)
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is False


def test_probe_trigger_note_reports_ready_hold_time():
    op = _plane_cluster(ready_since_s=7200)
    _, note, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert "Ready 120m" in note


def test_restart_class_is_not_subject_to_the_ready_stability_guard():
    """A restart-class trigger keeps its own guard (last-restart age) and must
    not inherit the probe class's Ready-hold requirement."""
    op = _op_cluster([_cluster_pod("faster-whisper-fcf845fbb-g47bq", "ai",
                                   ready_since_s=5, restarts=1,
                                   last_restart_s=86400)])
    recovered, note, restarts = op._recovered_and_healthy({}, FW_TRIGGER)
    assert recovered is True and restarts == 1
    assert "last restart" in note


def test_exact_workload_name_beats_prefix_siblings():
    """"cert-manager" names one workload exactly and prefix-matches two more.
    The exact hit wins rather than the trio reading as ambiguous."""
    op = _op_cluster([
        _cluster_pod("cert-manager-6d9c8f7bb4-aaaaa", "cert-manager"),
        _cluster_pod("cert-manager-webhook-77b4c9d5f-bbbbb", "cert-manager"),
        _cluster_pod("cert-manager-cainjector-5f6d8c4b7-ccccc", "cert-manager"),
    ])
    assert op._resolve_pod_from_cluster("readiness probe failures for cert-manager") == (
        "cert-manager", "cert-manager-6d9c8f7bb4-aaaaa")


def test_exact_and_prefix_tokens_together_still_resolve():
    """Both 'plane-api' (prefix) and 'plane-api-wl' (exact) name the same
    workload, and 'plane-web' names another. Classification is computed across
    all tokens, so the exact hit wins independently of set iteration order —
    breaking on the first matching token made this outcome hash-dependent."""
    op = _plane_cluster()
    trigger = ("Readiness probe failures: the plane-api service, deployment "
               "'plane-api-wl', is timing out while plane-web is fine")
    assert op._resolve_pod_from_cluster(trigger) == ("plane", PLANE_API_POD)


def test_probe_trigger_not_filtered_when_pod_unhealthy_now():
    op = _plane_cluster(phase="CrashLoopBackOff", ready=False)
    recovered, _, _ = op._recovered_and_healthy({}, PLANE_TRIGGERS[3])
    assert recovered is False
