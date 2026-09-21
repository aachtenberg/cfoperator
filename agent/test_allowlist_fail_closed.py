"""The node-action allowlist must fail closed when the KB is unreadable.

CFOP-133 documented three states (value / '' unset=ceiling / None=refuse) and
tested the None path by stubbing ``kb.get_setting`` to raise. That is what a
plain KnowledgeBase does. Production uses ResilientKnowledgeBase, whose
``get_setting`` catches internally and returns the default — so the except
branch never ran, a postgres blip looked like unset, and the next Job got the
full ceiling. CFOP-197 is that gap: ``strict=True`` makes the read raise so
both the Job path and the console GET see None.

These tests go through a real ResilientKnowledgeBase (stub inner + health
monitor, real wrapper method). A MagicMock that raises cannot catch a
regression that lives *inside* get_setting.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from agent import CFOperator
from knowledge_base import ResilientKnowledgeBase


_CEILING = {
    "allow_binaries": ["chmod", "chown", "systemctl"],
    "allow_systemctl_verbs": ["restart", "status"],
    "max_commands": 3,
}


class _Inner:
    def __init__(self, values=None, raises=False):
        self.values = dict(values or {})
        self.raises = raises
        self.calls = []

    def get_setting(self, key, default=None):
        self.calls.append(key)
        if self.raises:
            raise RuntimeError("db down")
        return self.values.get(key, default)


class _Monitor:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.marked = 0

    def is_healthy(self):
        return self._healthy

    def mark_unhealthy(self):
        self._healthy = False
        self.marked += 1


def _rkb(*, healthy=True, values=None, inner_raises=False):
    """A ResilientKnowledgeBase with just the bits get_setting touches."""
    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._kb = _Inner(values, raises=inner_raises)
    rkb._health_monitor = _Monitor(healthy)
    return rkb


def _op(rkb, ceiling=None):
    op = MagicMock()
    op._executor_config.return_value = {
        "node_action": {"enabled": True, "host": "controller",
                        **(ceiling if ceiling is not None else _CEILING)}}
    op.kb = rkb
    op._node_action_setting = lambda n: CFOperator._node_action_setting(op, n)
    op._node_action_allowlist = lambda: CFOperator._node_action_allowlist(op)
    return op


def _manifest_env(op):
    work = {"id": 10, "remediation_class": "node-action", "risk": "low",
            "payload": {"recommendation": "fix perms"}}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-n", work)
    containers = spec["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in containers["env"] if "value" in e}


# ---- get_setting itself ------------------------------------------------------

def test_default_get_setting_still_degrades_to_default_when_down():
    """Degrade-open callers (intervals, triage model, …) must keep working."""
    rkb = _rkb(healthy=False)
    assert rkb.get_setting("anything", "fallback") == "fallback"
    assert rkb.get_setting("anything", "") == ""


def test_strict_raises_when_already_unhealthy():
    rkb = _rkb(healthy=False, values={"node_action_allow_binaries": "systemctl"})
    try:
        rkb.get_setting("node_action_allow_binaries", "", strict=True)
    except ConnectionError as e:
        assert "offline" in str(e).lower()
    else:
        raise AssertionError("strict=True must raise when the DB is down")
    # Did not consult a possibly-stale inner store — same as set_setting.
    assert rkb._kb.calls == []


def test_strict_raises_and_marks_unhealthy_on_inner_exception():
    rkb = _rkb(healthy=True, inner_raises=True)
    try:
        rkb.get_setting("node_action_allow_binaries", "", strict=True)
    except RuntimeError as e:
        assert "db down" in str(e)
    else:
        raise AssertionError("strict=True must re-raise the inner failure")
    assert rkb._health_monitor.marked == 1
    assert rkb._health_monitor.is_healthy() is False


def test_strict_unset_is_still_the_empty_string():
    """The flag is about errors, not about missing keys. '' remains unset."""
    rkb = _rkb(healthy=True, values={})
    assert rkb.get_setting("node_action_allow_binaries", "", strict=True) == ""


def test_strict_returns_the_stored_value_when_healthy():
    rkb = _rkb(healthy=True, values={"node_action_allow_binaries": "systemctl"})
    assert rkb.get_setting("node_action_allow_binaries", "", strict=True) == "systemctl"


# ---- the Job env, through the live wrapper -----------------------------------

def test_a_resilient_outage_refuses_rather_than_restoring_the_ceiling(caplog):
    """The production path. Mutation-check: drop strict=True and this restores
    chmod,chown,systemctl — the silent undo CFOP-197 exists to prevent.
    """
    rkb = _rkb(
        healthy=False,
        values={"node_action_allow_binaries": "systemctl",
                "node_action_allow_systemctl_verbs": "restart"},
    )
    with caplog.at_level(logging.WARNING, logger="cfoperator"):
        env = _manifest_env(_op(rkb))
    assert env["CFOP_NODE_ACTION_ALLOW_BINARIES"] == ""
    assert env["CFOP_NODE_ACTION_ALLOW_SYSTEMCTL_VERBS"] == ""
    assert any("allowlist setting" in r.getMessage() for r in caplog.records), (
        "a blip must be visible at warning, not inferred from a debug line"
    )


def test_a_resilient_inner_error_refuses_rather_than_restoring_the_ceiling():
    rkb = _rkb(
        healthy=True, inner_raises=True,
        values={"node_action_allow_binaries": "systemctl"},
    )
    env = _manifest_env(_op(rkb))
    assert env["CFOP_NODE_ACTION_ALLOW_BINARIES"] == ""


def test_a_healthy_resilient_kb_still_narrows():
    rkb = _rkb(
        healthy=True,
        values={"node_action_allow_binaries": "systemctl",
                "node_action_allow_systemctl_verbs": "restart"},
    )
    env = _manifest_env(_op(rkb))
    assert env["CFOP_NODE_ACTION_ALLOW_BINARIES"] == "systemctl"
    assert env["CFOP_NODE_ACTION_ALLOW_SYSTEMCTL_VERBS"] == "restart"


def test_a_healthy_resilient_kb_unset_is_still_the_ceiling():
    env = _manifest_env(_op(_rkb(healthy=True, values={})))
    assert env["CFOP_NODE_ACTION_ALLOW_BINARIES"] == "chmod,chown,systemctl"
