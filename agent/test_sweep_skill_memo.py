#!/usr/bin/env python3
"""Tests for sweep skill-injection and tool-result memoization.

Sweep phases were improvising their investigation and re-listing cluster state
dozens of times (observed: 36x k8s_get_pods, 102x k8s_get_pod_logs in one
phase). Two mitigations: inject a loaded skill's procedure as the ordered
playbook, and memoize read-only tool results so a repeated identical call
returns a stub instead of re-running.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator


def _operator(skills=None):
    op = CFOperator.__new__(CFOperator)
    op.skills = skills or {}
    op._get_infra_summary = lambda: "Cluster namespaces: apps, monitoring, data"
    return op


# --- _build_sweep_system_prompt + skill injection ---------------------------

def test_prompt_without_skill_has_no_procedure_section():
    op = _operator()
    p = op._build_sweep_system_prompt("Check containers.")
    assert "PROCEDURE" not in p
    assert "Check containers." in p


def test_prompt_injects_loaded_skill_procedure():
    op = _operator({
        "k3s-cluster-health": {
            "name": "k3s-cluster-health",
            "description": "Cluster health check",
            "instructions": "1. k8s_get_nodes()\n2. k8s_get_all_unhealthy()",
        }
    })
    p = op._build_sweep_system_prompt("Check containers.", skill_name="k3s-cluster-health")
    assert "PROCEDURE" in p
    assert "k8s_get_nodes()" in p
    assert "k8s_get_all_unhealthy()" in p


def test_prompt_with_unknown_skill_degrades_gracefully():
    op = _operator()  # no skills loaded
    p = op._build_sweep_system_prompt("Check containers.", skill_name="k3s-cluster-health")
    assert "PROCEDURE" not in p          # nothing injected
    assert "Check containers." in p      # but the prompt is still valid


# --- _cached_tool_exec memoization ------------------------------------------

class _CountingTools:
    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return {"tool": name, "args": args, "n": len(self.calls)}


def _memo_operator():
    op = CFOperator.__new__(CFOperator)
    op.tools = _CountingTools()
    return op


def test_repeated_memoizable_call_is_not_re_executed():
    op = _memo_operator()
    cache = {}
    c1, r1, cached1 = op._cached_tool_exec("k8s_get_pods", {"ns": "apps"}, cache, 6000)
    c2, r2, cached2 = op._cached_tool_exec("k8s_get_pods", {"ns": "apps"}, cache, 6000)
    assert cached1 is False and cached2 is True
    assert len(op.tools.calls) == 1          # tool ran only once
    assert "cached" in c2 and "re-fetch" in c2
    assert r2 == r1                          # same result object handed back


def test_different_args_are_cached_separately():
    op = _memo_operator()
    cache = {}
    op._cached_tool_exec("k8s_get_pods", {"ns": "apps"}, cache, 6000)
    _, _, cached = op._cached_tool_exec("k8s_get_pods", {"ns": "monitoring"}, cache, 6000)
    assert cached is False                   # different args -> distinct call
    assert len(op.tools.calls) == 2


def test_non_memoizable_tool_always_executes():
    op = _memo_operator()
    cache = {}
    op._cached_tool_exec("ssh_execute", {"cmd": "uptime"}, cache, 6000)
    _, _, cached = op._cached_tool_exec("ssh_execute", {"cmd": "uptime"}, cache, 6000)
    assert cached is False                   # mutating tool is never memoized
    assert len(op.tools.calls) == 2


def test_memoizable_set_covers_the_known_offenders():
    # the tools that drove the 36x / 102x re-fetching must be memoized
    for t in ("k8s_get_pods", "k8s_get_nodes", "k8s_get_deployments",
              "k8s_get_pod_logs", "loki_query"):
        assert t in CFOperator._MEMOIZABLE_TOOLS


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
