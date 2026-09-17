"""CFOP-133: auto-eligibility and reaper knobs come from config.

The shipped tuples stay the default so an omitted key is a no-op. Config can
narrow or (within the enum, minus park-only classes) add; it cannot invent a
class or auto-enable one that exists to park.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import (
    _AUTO_REMEDIATION_CLASSES,
    _AUTO_REMEDIATION_MIN_CONFIDENCE,
    _NEVER_AUTO_CLASSES,
    _REMEDIATION_LEASE_TIMEOUT_S,
    _REMEDIATION_MAX_ATTEMPTS,
    _SUMMARY_CONFIDENCE_CAP,
    remediation_is_auto_eligible,
    resolve_auto_policy,
)


def test_omitted_config_keeps_the_shipped_defaults():
    policy = resolve_auto_policy(None)
    assert policy.auto_classes == _AUTO_REMEDIATION_CLASSES
    assert policy.min_confidence == _AUTO_REMEDIATION_MIN_CONFIDENCE
    assert policy.lease_timeout_s == _REMEDIATION_LEASE_TIMEOUT_S
    assert policy.max_attempts == _REMEDIATION_MAX_ATTEMPTS
    assert policy.summary_confidence_cap == _SUMMARY_CONFIDENCE_CAP


def test_empty_remediation_block_keeps_the_shipped_defaults():
    assert resolve_auto_policy({}).auto_classes == _AUTO_REMEDIATION_CLASSES


def test_config_can_drop_k8s_action():
    """k8s-action is not park-only: config can opt it in, then drop it.

    The shipped default is gitops-patch only (CFOP-128), so passing that
    list is not a drop — start from an explicit opt-in.
    """
    opted = resolve_auto_policy({
        "auto": {"classes": ["gitops-patch", "k8s-action"]},
    })
    assert opted.auto_classes == ("gitops-patch", "k8s-action")
    assert remediation_is_auto_eligible(
        "k8s-action", "low", 1.0,
        classes=opted.auto_classes, min_confidence=opted.min_confidence,
    ) is True

    dropped = resolve_auto_policy({"auto": {"classes": ["gitops-patch"]}})
    assert dropped.auto_classes == ("gitops-patch",)
    assert remediation_is_auto_eligible(
        "k8s-action", "low", 1.0,
        classes=dropped.auto_classes, min_confidence=dropped.min_confidence,
    ) is False
    assert remediation_is_auto_eligible(
        "gitops-patch", "low", 1.0,
        classes=dropped.auto_classes, min_confidence=dropped.min_confidence,
    ) is True


def test_empty_class_list_disables_all_auto():
    policy = resolve_auto_policy({"auto": {"classes": []}})
    assert policy.auto_classes == ()
    assert remediation_is_auto_eligible(
        "gitops-patch", "low", 1.0,
        classes=policy.auto_classes, min_confidence=policy.min_confidence,
    ) is False


def test_config_can_add_node_action():
    """CFOP-131's enablement is a default-list edit, not a new code path."""
    policy = resolve_auto_policy({
        "auto": {"classes": ["gitops-patch", "k8s-action", "node-action"]},
    })
    assert "node-action" in policy.auto_classes
    assert remediation_is_auto_eligible(
        "node-action", "low", 1.0,
        classes=policy.auto_classes, min_confidence=policy.min_confidence,
    ) is True


def test_park_only_classes_cannot_be_added_from_config():
    policy = resolve_auto_policy({
        "auto": {"classes": ["gitops-patch", *sorted(_NEVER_AUTO_CLASSES)]},
    })
    assert policy.auto_classes == ("gitops-patch",)
    for name in _NEVER_AUTO_CLASSES:
        assert remediation_is_auto_eligible(
            name, "low", 1.0,
            classes=policy.auto_classes, min_confidence=policy.min_confidence,
        ) is False


def test_unknown_class_is_dropped():
    policy = resolve_auto_policy({
        "auto": {"classes": ["gitops-patch", "not-a-class", "teleport"]},
    })
    assert policy.auto_classes == ("gitops-patch",)


def test_min_confidence_and_reaper_knobs_read_from_config():
    policy = resolve_auto_policy({
        "auto": {"min_confidence": 0.95},
        "lease_timeout_s": 60,
        "max_attempts": 7,
        "summary_confidence_cap": 0.25,
    })
    assert policy.min_confidence == 0.95
    assert policy.lease_timeout_s == 60
    assert policy.max_attempts == 7
    assert policy.summary_confidence_cap == 0.25
    assert remediation_is_auto_eligible(
        "gitops-patch", "low", 0.9,
        classes=policy.auto_classes, min_confidence=policy.min_confidence,
    ) is False


def test_broken_numbers_fall_back_to_the_shipped_defaults():
    policy = resolve_auto_policy({
        "auto": {"min_confidence": "nope"},
        "lease_timeout_s": 0,
        "max_attempts": -3,
        "summary_confidence_cap": 2.0,
    })
    assert policy.min_confidence == _AUTO_REMEDIATION_MIN_CONFIDENCE
    assert policy.lease_timeout_s == _REMEDIATION_LEASE_TIMEOUT_S
    assert policy.max_attempts == _REMEDIATION_MAX_ATTEMPTS
    assert policy.summary_confidence_cap == _SUMMARY_CONFIDENCE_CAP


def test_module_gate_without_kwargs_is_unchanged():
    """Existing callers that do not pass policy still see today's gate."""
    assert remediation_is_auto_eligible("gitops-patch", "low", 0.9) is True
    assert remediation_is_auto_eligible("k8s-action", "low", 0.8) is False
    assert remediation_is_auto_eligible("node-action", "low", 1.0) is False


def test_broken_numbers_are_logged(capsys):
    """A typo'd ConfigMap value must not fail silently (PR #262 review)."""
    resolve_auto_policy({
        "auto": {"min_confidence": "0,8"},
        "max_attempts": 0,
    })
    logged = capsys.readouterr().out
    assert "auto.min_confidence" in logged
    assert "max_attempts" in logged
    assert "unparseable" in logged
    assert "out-of-range" in logged


def test_reload_config_refreshes_the_kb_policy():
    """CFOP-77 live reload has to move the cached gate, not just self.config.

    queue_remediation / reclassify read kb.remediation_policy(), and the
    console POST and chat gitops-patch paths never go through
    _auto_policy_of. A reload that skipped apply_remediation_policy would
    keep auto-executing against process-start lists.
    """
    from unittest.mock import MagicMock
    from agent import CFOperator
    from agent.agent import _install_kb_auto_policy

    class FakeKB:
        def __init__(self):
            self.apply_remediation_policy(None)

        def apply_remediation_policy(self, rcfg=None):
            self._policy = resolve_auto_policy(rcfg)

        def remediation_policy(self):
            return self._policy

    op = MagicMock()
    op.config = {
        "infrastructure": {"hosts": {}},
        "git": {},
        "remediation": {"auto": {"classes": ["gitops-patch", "k8s-action"]}},
    }
    op.kb = FakeKB()
    _install_kb_auto_policy(op)
    assert "k8s-action" in op.kb.remediation_policy().auto_classes

    new_cfg = {
        "infrastructure": {"hosts": {}},
        "git": {},
        "remediation": {"auto": {"classes": ["gitops-patch"]}},
    }
    op._load_config = lambda path: new_cfg
    op._load_git_registry = lambda: None
    op._refresh_git_tools = lambda: None
    op.git_repos = lambda: []
    op._git_repos_source = "config"
    CFOperator.reload_config(op)
    assert op.kb.remediation_policy().auto_classes == ("gitops-patch",)
    assert remediation_is_auto_eligible(
        "k8s-action", "low", 1.0,
        classes=op.kb.remediation_policy().auto_classes,
        min_confidence=op.kb.remediation_policy().min_confidence,
    ) is False
