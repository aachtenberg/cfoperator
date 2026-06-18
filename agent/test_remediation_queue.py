#!/usr/bin/env python3
"""Tests for the remediation-queue auto-execute gate.

Pure policy functions, no DB — the gate decides which recommendations may run
unattended, so it gets the same scrutiny as the worker-side classification.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import (  # noqa: E402
    _AUTO_REMEDIATION_MIN_CONFIDENCE,
    normalize_remediation_fields,
    remediation_is_auto_eligible,
)
from agent import CFOperator  # noqa: E402


def _fake_op(*, drain=False, reap=False, max_per_tick=3):
    """Minimal stand-in 'self' for calling the unbound drainer/reaper methods."""
    op = MagicMock()
    op.config = {"remediation": {
        "queue_drain": drain, "queue_reap": reap, "max_drain_per_tick": max_per_tick,
    }}
    return op


def test_normalize_defaults_conservatively():
    # unknown class -> manual (human-only); unknown risk -> high (never auto)
    assert normalize_remediation_fields("bogus", "low") == ("manual", "low")
    assert normalize_remediation_fields("gitops-patch", "weird") == ("gitops-patch", "high")
    assert normalize_remediation_fields("k8s-action", "med") == ("k8s-action", "med")


def test_auto_eligible_happy_path():
    assert remediation_is_auto_eligible("gitops-patch", "low", 0.9) is True
    # exactly at the threshold is eligible
    assert remediation_is_auto_eligible("k8s-action", "low", _AUTO_REMEDIATION_MIN_CONFIDENCE) is True


def test_auto_eligible_blocks_unsafe_cases():
    # node-action / manual never auto, even when low-risk and fully confident
    assert remediation_is_auto_eligible("node-action", "low", 1.0) is False
    assert remediation_is_auto_eligible("manual", "low", 1.0) is False
    # any risk above low blocks
    assert remediation_is_auto_eligible("gitops-patch", "med", 1.0) is False
    assert remediation_is_auto_eligible("gitops-patch", "high", 1.0) is False
    # below threshold / missing confidence blocks
    assert remediation_is_auto_eligible("gitops-patch", "low", 0.5) is False
    assert remediation_is_auto_eligible("gitops-patch", "low", None) is False


# ---- drainer / reaper orchestration (unbound methods, faked self) ------------


def test_drain_disabled_claims_nothing():
    op = _fake_op(drain=False)
    assert CFOperator._drain_remediation_queue(op) == 0
    op.kb.claim_next_remediation.assert_not_called()


def test_drain_claims_until_empty_and_spawns():
    op = _fake_op(drain=True, max_per_tick=5)
    # two work orders then an empty queue
    op.kb.claim_next_remediation.side_effect = [
        {"id": 1, "remediation_class": "gitops-patch", "risk": "low"},
        {"id": 2, "remediation_class": "k8s-action", "risk": "low"},
        None,
    ]
    spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 2
    assert op._spawn_remediation_executor.call_count == 2
    op.kb.fail_remediation.assert_not_called()


def test_drain_respects_max_per_tick():
    op = _fake_op(drain=True, max_per_tick=1)
    op.kb.claim_next_remediation.return_value = {
        "id": 9, "remediation_class": "gitops-patch", "risk": "low"}
    assert CFOperator._drain_remediation_queue(op) == 1
    assert op.kb.claim_next_remediation.call_count == 1  # stopped at the cap


def test_drain_spawn_failure_fails_the_claim():
    op = _fake_op(drain=True, max_per_tick=1)
    op.kb.claim_next_remediation.return_value = {
        "id": 7, "remediation_class": "gitops-patch", "risk": "low"}
    op._spawn_remediation_executor.side_effect = RuntimeError("boom")
    assert CFOperator._drain_remediation_queue(op) == 0
    op.kb.fail_remediation.assert_called_once()
    assert op.kb.fail_remediation.call_args[0][0] == 7  # the claimed id is failed


def test_reap_disabled_is_noop():
    op = _fake_op(reap=False)
    assert CFOperator._reap_remediations(op) == 0
    op.kb.requeue_stale_remediations.assert_not_called()


def test_reap_enabled_calls_kb():
    op = _fake_op(reap=True)
    op.kb.requeue_stale_remediations.return_value = 2
    assert CFOperator._reap_remediations(op) == 2
    op.kb.requeue_stale_remediations.assert_called_once()


# ---- executor Job manifest ---------------------------------------------------


def test_build_executor_manifest_shape():
    op = MagicMock()
    op._executor_config.return_value = {}  # exercise defaults
    work = {"id": 11, "remediation_class": "gitops-patch", "risk": "low"}
    m = CFOperator._build_executor_manifest(op, "cfop-executor-abc", work)
    assert m["kind"] == "Job"
    assert m["metadata"]["name"] == "cfop-executor-abc"
    spec = m["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "cfoperator-executor"
    assert spec["containers"][0]["imagePullPolicy"] == "Always"
    env = {e["name"]: e for e in spec["containers"][0]["env"]}
    # completion URL embeds the remediation id; work order is serialized in
    assert env["CFOP_COMPLETION_URL"]["value"].endswith("/11/complete")
    assert "gitops-patch" in env["CFOP_REMEDIATION_JSON"]["value"]
    # GitHub token comes from a secret, never inline
    assert env["GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "GITHUB_TOKEN"
    assert env["CFOP_EXEC_LLM_BACKEND"]["value"] == "anthropic"


# ---- feed hook ---------------------------------------------------------------


def test_maybe_queue_remediation_feeds_when_enabled():
    op = MagicMock()
    op.config = {"remediation": {"queue_feed": True}}
    op.kb.queue_remediation.return_value = 7
    details = {"remediation_class": "k8s-action", "risk": "low", "confidence": 0.9,
               "recommendation": "restart", "host": "rpi5"}
    assert CFOperator._maybe_queue_remediation(op, 3, details) == 7
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "k8s-action"
    assert kwargs["investigation_id"] == 3
    assert kwargs["confidence"] == 0.9


def test_maybe_queue_remediation_off_or_unclassified():
    off = MagicMock(); off.config = {"remediation": {"queue_feed": False}}
    assert CFOperator._maybe_queue_remediation(off, 1, {"remediation_class": "k8s-action"}) is None
    off.kb.queue_remediation.assert_not_called()

    on = MagicMock(); on.config = {"remediation": {"queue_feed": True}}
    assert CFOperator._maybe_queue_remediation(on, 1, {"recommendation": "x"}) is None  # no class
    on.kb.queue_remediation.assert_not_called()


# ---- PR reconcile ------------------------------------------------------------


def test_parse_pr_url():
    assert CFOperator._parse_pr_url("https://github.com/o/r/pull/42") == ("o/r", 42)
    assert CFOperator._parse_pr_url("not a url") is None


def _reconcile_op(pr_data):
    op = MagicMock()
    op.config = {"remediation": {"queue_verify": True}}
    op.kb.list_remediations_by_status.return_value = [
        {"id": 1, "pr_url": "https://github.com/o/r/pull/5", "payload": {}}]
    gh = MagicMock()
    gh.request.return_value = {"success": True, "data": pr_data}
    op._github_write_client.return_value = gh
    op._parse_pr_url = CFOperator._parse_pr_url  # use the real static parser
    return op


def test_reconcile_merged_resolves_and_verifies():
    op = _reconcile_op({"merged": True})
    assert CFOperator._reconcile_remediation_prs(op) == 1
    assert op.kb.update_remediation_status.call_args.args == (1, "resolved")
    op._verify_remediation.assert_called_once()


def test_reconcile_closed_unmerged_rejects():
    op = _reconcile_op({"merged": False, "state": "closed"})
    assert CFOperator._reconcile_remediation_prs(op) == 1
    assert op.kb.update_remediation_status.call_args.args == (1, "rejected")


def test_reconcile_off_is_noop():
    op = MagicMock(); op.config = {"remediation": {"queue_verify": False}}
    assert CFOperator._reconcile_remediation_prs(op) == 0
    op.kb.list_remediations_by_status.assert_not_called()
