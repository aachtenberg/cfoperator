"""Tests for the deterministic enumeration layer.

The layer's contract: facts come from APIs (never invented), every request is
logged for the report's tameness appendix, failures degrade rather than kill
the pass, and result sizes are capped so the pass stays bounded.
"""

import json
from unittest.mock import patch

import pytest

from inventory import (
    MAX_SERIES_PER_QUERY,
    QueryLog,
    _summarize_instant,
    _summarize_nodes,
    _summarize_targets,
    enumerate_fleet,
    enumerate_kubernetes,
    enumerate_prometheus,
)


def _targets_payload():
    return {"data": {"activeTargets": [
        {"health": "up", "labels": {"job": "node", "instance": "pi5:9100"}},
        {"health": "up", "labels": {"job": "node", "instance": "pi4:9100"}},
        {"health": "down", "labels": {"job": "postgres", "instance": "db:9187"}},
    ]}}


def test_targets_summary_groups_by_job():
    s = _summarize_targets(_targets_payload())
    assert s["job_count"] == 2
    assert s["jobs"]["node"] == {"targets": 2, "up": 2, "down": 0,
                                 "instances": ["pi5:9100", "pi4:9100"]}
    assert s["jobs"]["postgres"]["down"] == 1
    assert s["truncated"] is False


def test_instant_summary_caps_series():
    payload = {"data": {"result": [
        {"metric": {"instance": f"host{i}"}, "value": [0, str(i)]}
        for i in range(MAX_SERIES_PER_QUERY + 50)
    ]}}
    out = _summarize_instant(payload)
    assert len(out) == MAX_SERIES_PER_QUERY
    assert out[0] == {"labels": {"instance": "host0"}, "value": "0"}


def test_summaries_tolerate_none_payload():
    """A dead source degrades the report; it must not raise."""
    assert _summarize_targets(None)["job_count"] == 0
    assert _summarize_instant(None) == []
    assert _summarize_nodes(None) == []


def test_node_summary_extracts_roles_and_capacity():
    payload = {"items": [{
        "metadata": {"name": "pi5", "labels": {
            "node-role.kubernetes.io/control-plane": "true",
            "kubernetes.io/arch": "arm64"}},
        "status": {"nodeInfo": {"architecture": "arm64", "osImage": "Ubuntu",
                                "kubeletVersion": "v1.30"},
                   "capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"}},
    }]}
    nodes = _summarize_nodes(payload)
    assert nodes == [{"name": "pi5", "roles": ["control-plane"], "arch": "arm64",
                      "os_image": "Ubuntu", "kubelet": "v1.30",
                      "capacity": {"cpu": "4", "memory": "8Gi"}}]


def test_get_json_logs_success_and_failure():
    """The tameness appendix is only honest if _get_json logs every request,
    including the ones that failed."""
    import io as _io
    from unittest.mock import MagicMock
    from inventory import _get_json

    log = QueryLog()
    cm = MagicMock()
    cm.__enter__.return_value = _io.BytesIO(b'{"ok": true}')
    cm.__exit__.return_value = False
    with patch("inventory.urllib.request.urlopen", return_value=cm):
        assert _get_json("http://x", log, "prometheus", "GET /x") == {"ok": True}
    with patch("inventory.urllib.request.urlopen", side_effect=OSError("boom")):
        assert _get_json("http://x", log, "prometheus", "GET /y") is None
    assert [e["status"] for e in log.entries] == ["ok", "failed: boom"]


def test_enumerate_prometheus_is_a_fixed_bounded_request_set():
    log = QueryLog()

    def fake_get(url, log_, source, what, headers=None, context=None, timeout=30):
        log_.record(source, what, "ok")
        return None

    with patch("inventory._get_json", side_effect=fake_get) as gj:
        enumerate_prometheus("http://prom:9090", log)
    # One targets call + the fixed instant-query set — nothing else.
    assert gj.call_count == len(log.entries) == 8
    assert all(e["source"] == "prometheus" for e in log.entries)


def test_enumerate_fleet_requires_prometheus_url():
    with pytest.raises(ValueError, match="PROMETHEUS_URL"):
        enumerate_fleet({}, QueryLog())


def test_kubernetes_skipped_without_access_and_logged():
    log = QueryLog()
    assert enumerate_kubernetes({}, log) is None
    assert log.entries[-1]["status"] == "skipped"


def test_kubernetes_env_url_enumeration():
    log = QueryLog()

    def fake_get(url, log_, source, what, headers=None, context=None, timeout=30):
        log_.record(source, what, "ok")
        if "/nodes" in url:
            return {"items": [{"metadata": {"name": "n1"}, "status": {}}]}
        if "/namespaces" in url:
            return {"items": [{"metadata": {"name": "default"}}]}
        return {"items": [{"metadata": {"namespace": "apps"}},
                          {"metadata": {"namespace": "apps"}}]}

    with patch("inventory._get_json", side_effect=fake_get):
        facts = enumerate_kubernetes(
            {"CFOP_DISCOVERY_K8S_URL": "https://k8s:6443", "CFOP_DISCOVERY_K8S_TOKEN": "t"}, log)
    assert facts["nodes"][0]["name"] == "n1"
    assert facts["namespaces"] == ["default"]
    assert facts["deployments_per_namespace"] == {"apps": 2}
    assert facts["daemonsets_per_namespace"] == {"apps": 2}
    assert [e for e in log.entries if e["source"] == "kubernetes"]


def test_bearer_token_sent_to_k8s_only():
    """The SA token must go to the k8s API and never leak into Prometheus calls."""
    seen = {}

    def fake_get(url, log_, source, what, headers=None, context=None, timeout=30):
        seen[source] = headers
        return None

    with patch("inventory._get_json", side_effect=fake_get):
        enumerate_fleet({"PROMETHEUS_URL": "http://prom:9090",
                         "CFOP_DISCOVERY_K8S_URL": "https://k8s:6443",
                         "CFOP_DISCOVERY_K8S_TOKEN": "secret"}, QueryLog())
    assert seen["kubernetes"] == {"Authorization": "Bearer secret"}
    assert not seen["prometheus"]
