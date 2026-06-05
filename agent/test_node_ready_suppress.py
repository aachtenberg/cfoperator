"""The 'all nodes NotReady' false positive (Prometheus metric misread) must be
suppressed by the ground-truth filter when the nodes are actually Ready."""
from agent import CFOperator


def _snapshot(ready=True):
    nodes = {}
    for n in ("raspberrypi", "raspberrypi2", "raspberrypi3", "headless-gpu", "ubuntu-cm5-01"):
        nodes[n] = {"ready": "True" if ready else "False",
                    "memoryPressure": "False", "diskPressure": "False",
                    "kubeletVersion": "v1.31.4+k3s1"}
    return {"nodes": nodes, "workloads": set(), "ingresses": set()}


FINDING = {
    "finding": "Ready condition false on multiple nodes (headless-gpu, raspberrypi, raspberrypi2, raspberrypi3, ubuntu-cm5-01)",
    "evidence": 'kube_node_status_condition{condition="Ready", status="false"} returned these nodes as NotReady.',
}


def test_node_ready_false_positive_suppressed():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    reason = op._ground_truth_suppress(FINDING, _snapshot(ready=True))
    assert reason and "Ready=True" in reason


def test_real_notready_is_kept():
    # if the snapshot actually shows NotReady, the finding must survive
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    assert op._ground_truth_suppress(FINDING, _snapshot(ready=False)) is None


def test_keyword_variants_match():
    op = CFOperator.__new__(CFOperator)
    op.config = {}
    for ev in ['status="false"', "NotReady", "Ready condition false", "status=false"]:
        f = {"finding": "raspberrypi node issue", "evidence": ev}
        assert op._ground_truth_suppress(f, _snapshot(ready=True)), ev
