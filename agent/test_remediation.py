"""Tests for the Phase-B remediation proposer.

Focus: the classifier makes the *right* call on the cases that matter — most
importantly that it DECLINES the adguardhome-shape (hostNetwork + host-port
conflict), where the naive "add a toleration" fix would be actively wrong.
"""

import base64

import pytest
import yaml

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


# --- live PR path (#9) --------------------------------------------------------

from remediation import apply_toleration, locate_manifest, _is_secret_path

DEPLOY_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-worker
  namespace: batch
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: batch-worker
    spec:
      containers:
        - name: worker
          image: worker:latest
"""


def test_apply_toleration_inserts_block_and_preserves_file():
    out = apply_toleration(DEPLOY_YAML, "workload: batch")
    assert out is not None
    assert "tolerations:" in out
    assert 'key: "workload"' in out
    # original content preserved (no reformat)
    assert "image: worker:latest" in out
    assert "name: batch-worker" in out
    # parses and toleration is under the pod spec
    docs = [d for d in yaml.safe_load_all(out)]
    tols = docs[0]["spec"]["template"]["spec"]["tolerations"]
    assert tols[0]["key"] == "workload"
    assert docs[0]["spec"]["template"]["spec"]["containers"][0]["name"] == "worker"


def test_apply_toleration_declines_when_already_tolerating():
    y = DEPLOY_YAML.replace(
        "    spec:\n      containers:",
        "    spec:\n      tolerations:\n        - key: x\n          operator: Exists\n      containers:")
    assert apply_toleration(y, "workload: batch") is None


def test_apply_toleration_declines_non_workload():
    assert apply_toleration("kind: ConfigMap\nmetadata:\n  name: x\n", "gpu: true") is None


def test_secret_path_refusal():
    assert _is_secret_path("k3s/base/iot/sealed-secrets/adguard.yaml")
    assert _is_secret_path("apps/.env")
    assert not _is_secret_path("k3s/base/batch/worker.yml")


class _FakeGH:
    """Records requests and returns canned responses keyed by (method, path-prefix)."""
    def __init__(self, files=None, branch_exists=False):
        self.calls = []
        self.files = files or {}
        self.branch_exists = branch_exists
        self.created_pull = None

    def request(self, method, path, *, body=None, params=None, cache_ttl=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/branches/main"):
            return {"success": True, "data": {"commit": {"commit": {"tree": {"sha": "treesha"}}}}}
        if method == "GET" and "/git/trees/" in path:
            return {"success": True, "data": {"tree": [
                {"type": "blob", "path": "k3s/base/batch/batch-worker.yml"},
                {"type": "blob", "path": "k3s/base/batch/other.yml"},
            ]}}
        if method == "GET" and "/contents/" in path:
            p = path.split("/contents/", 1)[1]
            if p in self.files:
                return {"success": True, "data": {"encoding": "base64", "sha": "filesha",
                        "content": base64.b64encode(self.files[p].encode()).decode()}}
            return {"success": False, "status": 404}
        if method == "GET" and "/git/ref/heads/cfop/" in path:
            return {"success": self.branch_exists, "status": 200 if self.branch_exists else 404}
        if method == "GET" and "/git/ref/heads/main" in path:
            return {"success": True, "data": {"object": {"sha": "basesha"}}}
        if method == "POST" and path.endswith("/git/refs"):
            return {"success": True, "data": {}}
        if method == "PUT" and "/contents/" in path:
            return {"success": True, "data": {"commit": {"sha": "newsha"}}}
        if method == "POST" and path.endswith("/pulls"):
            self.created_pull = body
            return {"success": True, "data": {"number": 42, "html_url": "https://github.com/x/y/pull/42"}}
        return {"success": False, "status": 404}


def test_locate_manifest_finds_by_name_and_kind():
    gh = _FakeGH(files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML})
    found = locate_manifest(gh, "aachtenberg/homelab-infra", "batch-worker", "batch", "main")
    assert found is not None
    path, text, sha = found
    assert path == "k3s/base/batch/batch-worker.yml"
    assert sha == "filesha"


def test_open_pr_end_to_end_opens_pull():
    from remediation import RemediationProposer, build_proposal
    gh = _FakeGH(files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML})
    prop = build_proposal(namespace="batch", pod_name="batch-worker-abc", workload="batch-worker",
                          pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra")
    proposer = RemediationProposer(
        None, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}],
        open_prs=True, github=gh)
    res = proposer.open_pr(prop, "batch", "batch-worker")
    assert res["status"] == "opened"
    assert res["pr_number"] == 42
    # the committed content actually contains the toleration
    put = [c for c in gh.calls if c[0] == "PUT"][0]
    committed = base64.b64decode(put[2]["content"]).decode()
    assert "tolerations:" in committed and 'key: "workload"' in committed


def test_open_pr_dedupes_existing_branch():
    from remediation import RemediationProposer, build_proposal
    gh = _FakeGH(files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML}, branch_exists=True)
    prop = build_proposal(namespace="batch", pod_name="batch-worker-abc", workload="batch-worker",
                          pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra")
    proposer = RemediationProposer(None, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra"}],
                                   open_prs=True, github=gh)
    res = proposer.open_pr(prop, "batch", "batch-worker")
    assert res["status"] == "skipped"
    assert gh.created_pull is None  # never opened a PR


def test_open_pr_noop_when_flag_off():
    from remediation import RemediationProposer, build_proposal
    gh = _FakeGH(files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML})
    prop = build_proposal(namespace="batch", pod_name="batch-worker-abc", workload="batch-worker",
                          pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra")
    proposer = RemediationProposer(None, open_prs=False, github=gh)
    assert proposer.open_pr(prop, "batch", "batch-worker") is None


# --- #11: global open-PR cap --------------------------------------------------

class _FakeGHWithPulls(_FakeGH):
    def __init__(self, open_pr_branches, **kw):
        super().__init__(**kw)
        self._open_branches = open_pr_branches
    def request(self, method, path, *, body=None, params=None, cache_ttl=None):
        if method == "GET" and path.endswith("/pulls"):
            return {"success": True, "data": [
                {"head": {"ref": b}} for b in self._open_branches]}
        return super().request(method, path, body=body, params=params, cache_ttl=cache_ttl)


def test_open_pr_capped_when_too_many_open():
    from remediation import RemediationProposer, build_proposal
    gh = _FakeGHWithPulls(
        open_pr_branches=["cfop/remediate-a", "cfop/remediate-b", "cfop/remediate-c"],
        files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML})
    prop = build_proposal(namespace="batch", pod_name="batch-worker-abc", workload="batch-worker",
                          pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra")
    proposer = RemediationProposer(
        None, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}],
        open_prs=True, github=gh, max_open_prs=3)
    res = proposer.open_pr(prop, "batch", "batch-worker")
    assert res["status"] == "capped"
    assert gh.created_pull is None


def test_open_pr_allowed_under_cap():
    from remediation import RemediationProposer, build_proposal
    gh = _FakeGHWithPulls(open_pr_branches=["cfop/remediate-a"],
                          files={"k3s/base/batch/batch-worker.yml": DEPLOY_YAML})
    prop = build_proposal(namespace="batch", pod_name="batch-worker-abc", workload="batch-worker",
                          pod_spec={}, scheduler_message=TAINT_ONLY_MSG, repo="aachtenberg/homelab-infra")
    proposer = RemediationProposer(
        None, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}],
        open_prs=True, github=gh, max_open_prs=3)
    res = proposer.open_pr(prop, "batch", "batch-worker")
    assert res["status"] == "opened"
