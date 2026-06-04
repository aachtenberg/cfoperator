"""Tests for the Phase-B remediation proposer.

Focus: the classifier makes the *right* call on the cases that matter — most
importantly that it DECLINES the adguardhome-shape (hostNetwork + host-port
conflict), where the naive "add a toleration" fix would be actively wrong.
"""

import pytest

from remediation import (
    parse_unschedulable,
    classify,
    build_proposal,
    render_toleration_patch,
    RemediationProposer,
)

ADGUARD_MSG = (
    "0/7 nodes are available: 1 node(s) didn't have free ports for the requested pod "
    "ports, 1 node(s) had untolerated taint {gpu: true}, 5 node(s) didn't match Pod's "
    "node affinity/selector."
)
TAINT_ONLY_MSG = (
    "0/7 nodes are available: 1 node(s) had untolerated taint {workload: batch}, "
    "6 node(s) didn't match Pod's node affinity/selector."
)


def test_parse_unschedulable_pulls_blockers():
    r = parse_unschedulable(ADGUARD_MSG)
    assert r["free_ports"] is True
    assert r["untolerated_taint"] is True
    assert r["taint_detail"] == "gpu: true"
    assert r["affinity_mismatch"] is True


def test_parse_insufficient():
    r = parse_unschedulable("0/3 nodes are available: 3 Insufficient memory, 1 Insufficient cpu.")
    assert r["insufficient"] == ["cpu", "memory"]


def test_adguard_shape_declines_port_conflict():
    """The canonical trap: hostNetwork + free-ports => decline, never tolerate."""
    spec = {"hostNetwork": True, "nodeSelector": {"kubernetes.io/hostname": "raspberrypi3"}}
    decision, reason = classify(spec, parse_unschedulable(ADGUARD_MSG))
    assert decision == "decline_port_conflict"
    assert "host-port conflict" in reason


def test_hostport_without_hostnetwork_also_declines():
    spec = {"containers": [{"ports": [{"containerPort": 53, "hostPort": 53}]}]}
    decision, _ = classify(spec, parse_unschedulable(ADGUARD_MSG))
    assert decision == "decline_port_conflict"


def test_pinned_full_declines():
    spec = {"nodeSelector": {"disktype": "ssd"}}
    decision, _ = classify(spec, parse_unschedulable(
        "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector."))
    assert decision == "decline_pinned_full"


def test_insufficient_declines():
    decision, _ = classify({}, parse_unschedulable("0/3: 3 Insufficient memory."))
    assert decision == "decline_insufficient"


def test_non_pinned_taint_only_proposes_toleration():
    """A free-floating pod blocked solely by a taint is the one we propose for."""
    decision, _ = classify({}, parse_unschedulable(TAINT_ONLY_MSG))
    assert decision == "propose_toleration"


def test_build_proposal_patch_for_taint():
    p = build_proposal(
        namespace="batch", pod_name="job-x-abc", workload="job-x",
        pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra",
    )
    assert p.is_patch
    assert p.fix_class == "add_toleration"
    assert "workload" in p.patch_yaml  # taint key rendered
    assert p.repo == "aachtenberg/homelab-infra"
    assert "review before merge" in p.pr_body.lower()


def test_build_proposal_declines_adguard():
    p = build_proposal(
        namespace="iot", pod_name="adguardhome-x-8crhq", workload="adguardhome",
        pod_spec={"hostNetwork": True, "nodeSelector": {"kubernetes.io/hostname": "raspberrypi3"}},
        scheduler_message=ADGUARD_MSG, repo="aachtenberg/homelab-infra",
    )
    assert not p.is_patch
    assert p.kind == "decline"
    assert p.confidence >= 0.8  # confident decline
    assert "host-port conflict" in p.reason


def test_render_toleration_patch_uses_taint_key():
    snippet = render_toleration_patch("gpu: true")
    assert 'key: "gpu"' in snippet
    assert "Exists" in snippet


# --- IO wrapper with a fake k8s tool -----------------------------------------

class _FakeK8s:
    def __init__(self, status, events, describe_text):
        self._status, self._events, self._describe = status, events, describe_text

    def get_pod_status(self, ns, pod):
        return self._status

    def get_events(self, namespace="default"):
        return {"events": self._events}

    def describe(self, kind, name, namespace="default"):
        return {"output": self._describe}


def test_proposer_declines_adguard_end_to_end():
    k8s = _FakeK8s(
        status={"success": True, "phase": "Pending"},
        events=[{"reason": "FailedScheduling", "message": ADGUARD_MSG}],
        describe_text="Host Network:  true\nNode-Selectors:  kubernetes.io/hostname=raspberrypi3",
    )
    prop = RemediationProposer(k8s, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra"}])
    p = prop.propose_for("iot", "adguardhome-x-8crhq", workload="adguardhome")
    assert p is not None and p.kind == "decline"
    assert "host-port conflict" in p.reason


def test_proposer_noop_when_pod_running():
    k8s = _FakeK8s(status={"success": True, "phase": "Running"}, events=[], describe_text="")
    prop = RemediationProposer(k8s)
    assert prop.propose_for("iot", "x") is None


def test_proposer_noop_without_k8s():
    assert RemediationProposer(None).propose_for("iot", "x") is None
