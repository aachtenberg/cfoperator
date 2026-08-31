#!/usr/bin/env python3
"""Tests for the remediation-queue auto-execute gate.

Pure policy functions, no DB — the gate decides which recommendations may run
unattended, so it gets the same scrutiny as the worker-side classification.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import (  # noqa: E402
    _AUTO_REMEDIATION_MIN_CONFIDENCE,
    normalize_remediation_fields,
    remediation_is_auto_eligible,
)
from agent import CFOperator  # noqa: E402
from agent.agent import _SUMMARY_CONFIDENCE_CAP, _llm_provider_tag  # noqa: E402
import agent.agent as agent_mod  # noqa: E402


def _wire_flags(op):
    """Make op._remediation_flag read op.config (config-only), like the real method
    (a bare MagicMock would return a truthy mock and defeat the gating).

    The CFOP-78 pre-enqueue steps are wired real for the same reason: a bare
    MagicMock returns a truthy mock from _absorb_repeat_remediation (every
    enqueue would "fold" onto a mock id) and an ununpackable one from
    _commit_forked_recommendation. The real methods fail open against the
    MagicMock kb — which is itself the behaviour under test elsewhere."""
    op._remediation_flag = lambda name: bool((op.config.get('remediation') or {}).get(name))
    op._extract_remediation_identifiers = CFOperator._extract_remediation_identifiers
    op._absorb_repeat_remediation = (
        lambda details: CFOperator._absorb_repeat_remediation(op, details))
    op._commit_forked_recommendation = (
        lambda t, r, report='': CFOperator._commit_forked_recommendation(op, t, r, report=report))
    return op


def _fake_op(*, drain=False, reap=False, max_per_tick=3):
    """Minimal stand-in 'self' for calling the unbound drainer/reaper methods."""
    op = MagicMock()
    op.config = {"remediation": {
        "queue_drain": drain, "queue_reap": reap, "max_drain_per_tick": max_per_tick,
    }}
    # Unset change-record URL → prepare is a no-op pass-through (homelab default).
    op._change_record_url = lambda: ""
    op._prepare_node_action_change_record = lambda work: work
    # CFOP-71: no outstanding PRs by default, so drain tests exercise draining.
    # A bare MagicMock is not comparable to the int cap and would TypeError.
    op._open_remediation_pr_count = lambda: 0
    return _wire_flags(op)


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
    op._prepare_node_action_change_record = lambda work: work
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
    assert env["CFOP_GIT_REPO"]["value"] == "aachtenberg/homelab-infra"  # config default


def test_build_executor_manifest_per_item_repo():
    op = MagicMock()
    op._executor_config.return_value = {}
    work = {"id": 9, "remediation_class": "gitops-patch", "risk": "low",
            "payload": {"repo": "aachtenberg/cfoperator-deploy"}}
    m = CFOperator._build_executor_manifest(op, "cfop-executor-x", work)
    env = {e["name"]: e for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CFOP_GIT_REPO"]["value"] == "aachtenberg/cfoperator-deploy"  # payload wins


def test_build_executor_manifest_gitops_has_no_ssh_mount():
    """GitOps classes stay PR-only: no SSH secret, no node-action opt-in env."""
    op = MagicMock()
    op._executor_config.return_value = {"node_action": {"enabled": True}}
    work = {"id": 12, "remediation_class": "gitops-patch", "risk": "low"}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-g", work)["spec"]["template"]["spec"]
    assert "volumes" not in spec
    assert "volumeMounts" not in spec["containers"][0]
    env = {e["name"] for e in spec["containers"][0]["env"]}
    assert "CFOP_NODE_ACTION_ENABLED" not in env


def test_build_executor_manifest_node_action_mounts_ssh():
    op = MagicMock()
    op._executor_config.return_value = {"node_action": {"enabled": True, "host": "controller"}}
    work = {"id": 10, "remediation_class": "node-action", "risk": "low",
            "payload": {"recommendation": "fix perms"}}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-n", work)["spec"]["template"]["spec"]
    # SSH secret mounted at a staging dir, copied to ~/.ssh at runtime.
    vol = {v["name"]: v for v in spec["volumes"]}["ssh"]
    assert vol["secret"]["secretName"] == "cfop-forensics-ssh"
    mount = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}["ssh"]
    assert mount["mountPath"] == "/ssh-secret" and mount["readOnly"] is True
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"] if "value" in e}
    assert env["CFOP_NODE_ACTION_ENABLED"] == "true"
    assert env["CFOP_NODE_ACTION_HOST"] == "controller"
    assert env["CFOP_SSH_SECRET_DIR"] == "/ssh-secret"
    # model floor: unset node_action.model -> falls back to the top model, not ''.
    assert env["CFOP_EXEC_LLM_MODEL"] == "claude-opus-4-8"
    # Unset change-record URL → no change-record env on the Job (homelab default).
    assert "CFOP_EXEC_CHANGE_URL" not in env


def test_build_executor_manifest_node_action_model_floor_overrides_downgrade():
    """A cost downgrade of the generic executor model must not reach node-action."""
    op = MagicMock()
    op._executor_config.return_value = {
        "llm": {"model": "claude-haiku-4-5-20251001"},  # generic executor downgraded
        "node_action": {"enabled": True, "model": "claude-opus-4-8"},
    }
    work = {"id": 10, "remediation_class": "node-action", "risk": "low"}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-m", work)["spec"]["template"]["spec"]
    models = [e["value"] for e in spec["containers"][0]["env"] if e.get("name") == "CFOP_EXEC_LLM_MODEL"]
    assert models == ["claude-opus-4-8"]  # exactly one entry, the node-action floor


def test_build_executor_manifest_node_action_change_record_url():
    op = MagicMock()
    op._executor_config.return_value = {
        "image": "ghcr.io/aachtenberg/cfoperator-executor:test",
        "node_action": {
            "enabled": True,
            "change_record": {"url": "http://cfop-changerecord.apps.svc:8091"},
        },
    }
    work = {"id": 10, "remediation_class": "node-action", "risk": "low"}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-cr", work)["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"] if "value" in e}
    assert env["CFOP_EXEC_CHANGE_URL"] == "http://cfop-changerecord.apps.svc:8091"
    # GitOps manifests must not carry the change-record URL.
    gitops = CFOperator._build_executor_manifest(
        op, "cfop-executor-g2",
        {"id": 11, "remediation_class": "gitops-patch", "risk": "low"},
    )["spec"]["template"]["spec"]
    genv = {e["name"] for e in gitops["containers"][0]["env"]}
    assert "CFOP_EXEC_CHANGE_URL" not in genv


_PLAN = {
    "host": "controller",
    "commands": ["sudo -n chmod 600 /root/.ssh/config"],
    "explanation": "fix perms",
}


def test_unapproved_change_record_never_reaches_spawn():
    """Acceptance (#80): unapproved record never reaches run_ssh_plan — agent gate."""
    op = _fake_op(drain=True, max_per_tick=1)
    op.config["remediation"]["executor"] = {
        "image": "ghcr.io/aachtenberg/cfoperator-executor:test",
        "node_action": {"enabled": True, "host": "controller",
                        "change_record": {"url": "http://changerecord:8091"}},
    }
    op._change_record_url = lambda: "http://changerecord:8091"
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._generate_node_action_plan = lambda work: dict(_PLAN)
    # Use the real gate implementation.
    op._prepare_node_action_change_record = (
        lambda work: CFOperator._prepare_node_action_change_record(op, work)
    )
    work = {
        "id": 10,
        "remediation_class": "node-action",
        "risk": "med",
        "confidence": 0.6,
        "payload": {"recommendation": "fix perms", "target": {"host": "controller"}},
        "result": {"change_record": {"ref": "opaque-ref", "url": "http://pr/1", "plan": _PLAN}},
    }
    op.kb.claim_next_remediation.return_value = work
    with patch.object(agent_mod, "change_record_approval", return_value=None):
        spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 0
    op._spawn_remediation_executor.assert_not_called()
    op.kb.release_remediation_claim.assert_called_once()
    assert op.kb.release_remediation_claim.call_args[0][0] == 10


def test_approved_change_record_spawns_with_ref():
    op = _fake_op(drain=True, max_per_tick=1)
    op.config["remediation"]["executor"] = {
        "node_action": {"enabled": True,
                        "change_record": {"url": "http://changerecord:8091"}},
    }
    op._change_record_url = lambda: "http://changerecord:8091"
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._generate_node_action_plan = lambda work: dict(_PLAN)
    op._prepare_node_action_change_record = (
        lambda work: CFOperator._prepare_node_action_change_record(op, work)
    )
    work = {
        "id": 11,
        "remediation_class": "node-action",
        "risk": "med",
        "payload": {"recommendation": "fix", "target": {"host": "h"}},
        "result": {"change_record": {"ref": "opaque-ref", "url": "http://pr/9", "plan": _PLAN}},
    }
    op.kb.claim_next_remediation.return_value = work
    approval = {"identity": "carol", "timestamp": "t", "state": "merged"}
    with patch.object(agent_mod, "change_record_approval", return_value=approval):
        spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 1
    spawned_work = op._spawn_remediation_executor.call_args[0][1]
    assert spawned_work["change_record_ref"] == "opaque-ref"
    assert spawned_work["change_record_approval"]["identity"] == "carol"
    assert spawned_work["approved_plan"]["commands"] == _PLAN["commands"]


def test_unset_change_url_node_action_spawns_without_gate():
    """Unset URL → console-escalation path unchanged (no open/approval HTTP)."""
    op = _fake_op(drain=True, max_per_tick=1)
    op._change_record_url = lambda: ""
    op._prepare_node_action_change_record = (
        lambda work: CFOperator._prepare_node_action_change_record(op, work)
    )
    work = {"id": 12, "remediation_class": "node-action", "risk": "med",
            "payload": {"recommendation": "fix"}}
    op.kb.claim_next_remediation.return_value = work
    with patch.object(agent_mod, "change_record_open") as opened, \
         patch.object(agent_mod, "change_record_approval") as approved:
        spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 1
    opened.assert_not_called()
    approved.assert_not_called()
    op._spawn_remediation_executor.assert_called_once()


def test_awaiting_approval_does_not_starve_later_rows():
    """Released awaiting-approval rows are skipped for the rest of the tick."""
    op = _fake_op(drain=True, max_per_tick=3)
    op.config["remediation"]["executor"] = {
        "node_action": {"enabled": True,
                        "change_record": {"url": "http://changerecord:8091"}},
    }
    op._change_record_url = lambda: "http://changerecord:8091"
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._generate_node_action_plan = lambda work: dict(_PLAN)
    op._prepare_node_action_change_record = (
        lambda work: CFOperator._prepare_node_action_change_record(op, work)
    )
    awaiting = {
        "id": 10,
        "remediation_class": "node-action",
        "risk": "med",
        "payload": {"recommendation": "fix", "target": {"host": "h"}},
        "result": {"change_record": {"ref": "opaque-ref", "url": "http://pr/1", "plan": _PLAN}},
    }
    gitops = {"id": 20, "remediation_class": "gitops-patch", "risk": "low"}
    claim_excludes = []
    taken = set()  # rows that stayed claimed (spawned), not merely released

    def _claim(job_name, exclude_ids=None):
        skip = set(exclude_ids or []) | taken
        claim_excludes.append(set(exclude_ids or []))
        # Without exclude_ids, awaiting would win every time (higher priority).
        if 10 not in skip:
            return awaiting  # released mid-tick → reclaimable unless excluded
        if 20 not in skip:
            taken.add(20)
            return gitops
        return None

    op.kb.claim_next_remediation.side_effect = _claim
    with patch.object(agent_mod, "change_record_approval", return_value=None):
        spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 1
    op._spawn_remediation_executor.assert_called_once()
    assert op._spawn_remediation_executor.call_args[0][1]["id"] == 20
    assert op.kb.claim_next_remediation.call_count >= 2
    assert 10 in claim_excludes[1]


def test_change_record_409_fails_but_transport_releases():
    from change_record_client import ChangeRecordClientError
    op = _fake_op(drain=True, max_per_tick=1)
    op.config["remediation"]["executor"] = {
        "node_action": {"enabled": True,
                        "change_record": {"url": "http://changerecord:8091"}},
    }
    op._change_record_url = lambda: "http://changerecord:8091"
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._generate_node_action_plan = lambda work: dict(_PLAN)
    work = {
        "id": 13,
        "remediation_class": "node-action",
        "risk": "med",
        "payload": {"recommendation": "fix", "target": {"host": "h"}},
        "result": {"change_record": {"ref": "opaque-ref", "url": "http://pr/1", "plan": _PLAN}},
    }
    # 409 → fail
    with patch.object(agent_mod, "change_record_approval",
                      side_effect=ChangeRecordClientError("closed", status=409)):
        assert CFOperator._prepare_node_action_change_record(op, work) is None
    op.kb.fail_remediation.assert_called_once()
    op.kb.release_remediation_claim.assert_not_called()

    op.kb.reset_mock()
    # transport → release (no attempt burn)
    with patch.object(agent_mod, "change_record_approval",
                      side_effect=ChangeRecordClientError("blip", status=0)):
        assert CFOperator._prepare_node_action_change_record(op, work) is None
    op.kb.release_remediation_claim.assert_called_once()
    op.kb.fail_remediation.assert_not_called()


def test_open_stamps_plan_commands_into_record():
    op = _fake_op(drain=True, max_per_tick=1)
    op.config["remediation"]["executor"] = {
        "image": "ghcr.io/aachtenberg/cfoperator-executor:test",
        "node_action": {"enabled": True, "host": "controller",
                        "change_record": {"url": "http://changerecord:8091"}},
    }
    op._change_record_url = lambda: "http://changerecord:8091"
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._generate_node_action_plan = lambda work: dict(_PLAN)
    work = {
        "id": 14,
        "remediation_class": "node-action",
        "risk": "med",
        "payload": {"recommendation": "fix perms", "target": {"host": "controller"}},
        "result": {},
    }
    with patch.object(agent_mod, "change_record_open",
                      return_value={"ref": "new-ref", "url": "http://pr/2"}) as opened, \
         patch.object(agent_mod, "change_record_approval", return_value=None):
        assert CFOperator._prepare_node_action_change_record(op, work) is None
    intent = opened.call_args[0][1]
    assert intent["commands"] == _PLAN["commands"]
    assert intent["image"].startswith("ghcr.io/")
    assert "image_digest" not in intent


def test_build_executor_manifest_node_action_disabled_no_mount():
    """node-action class but opt-in off -> still no SSH mount (safe default)."""
    op = MagicMock()
    op._executor_config.return_value = {"node_action": {"enabled": False}}
    work = {"id": 10, "remediation_class": "node-action", "risk": "low"}
    spec = CFOperator._build_executor_manifest(op, "cfop-executor-d", work)["spec"]["template"]["spec"]
    assert "volumes" not in spec


# ---- feed hook ---------------------------------------------------------------


def _no_node_incident(op):
    """Wire the real node-incident collapse (CFOP-71).

    Deliberately the real method, not a None stub: with a MagicMock `tools`
    it traverses the genuine fail-open path (unreadable nodes -> no collapse),
    so these fixtures prove the collapse cannot fire when node readiness is
    unknown. A bare MagicMock would be truthy and silently rewrite every
    dedupe key.
    """
    op._normalize_host = CFOperator._normalize_host
    op._notready_nodes = lambda: CFOperator._notready_nodes(op)
    op._collapse_key_for_node_incident = (
        lambda details: CFOperator._collapse_key_for_node_incident(op, details))
    return op


def _confirming_judge(op):
    """Wire an explicitly confirming mutation judge (CFOP-70).

    Auto-eligible fixtures now traverse the gate, so a test that means to
    exercise the enqueue path has to say what the judge said. Left as a bare
    MagicMock the gate would fail closed and the fixture would silently stop
    testing enqueue at all.
    """
    op._judge_mutation_remediation = MagicMock(
        return_value={"verdict": "confirm", "reason": "ok", "model": "claude-opus-4-8"})
    return op


def test_maybe_queue_remediation_feeds_when_enabled():
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
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
    off = _wire_flags(MagicMock()); off.config = {"remediation": {"queue_feed": False}}
    assert CFOperator._maybe_queue_remediation(off, 1, {"remediation_class": "k8s-action"}) is None
    off.kb.queue_remediation.assert_not_called()

    on = _wire_flags(MagicMock()); on.config = {"remediation": {"queue_feed": True}}
    assert CFOperator._maybe_queue_remediation(on, 1, {"recommendation": "x"}) is None  # no class
    on.kb.queue_remediation.assert_not_called()


# ---- PR reconcile ------------------------------------------------------------


def test_parse_pr_url():
    assert CFOperator._parse_pr_url("https://github.com/o/r/pull/42") == ("o/r", 42)
    assert CFOperator._parse_pr_url("not a url") is None


def _reconcile_op(pr_data):
    op = _wire_flags(MagicMock())
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
    op = _wire_flags(MagicMock()); op.config = {"remediation": {"queue_verify": False}}
    assert CFOperator._reconcile_remediation_prs(op) == 0
    op.kb.list_remediations_by_status.assert_not_called()


# ---- metrics -----------------------------------------------------------------


def test_update_remediation_metrics_sets_gauge():
    import sys
    REMEDIATION_QUEUE = sys.modules[CFOperator.__module__].REMEDIATION_QUEUE
    op = MagicMock()
    op.last_metrics = 0  # not throttled
    op._REMEDIATION_STATUSES = CFOperator._REMEDIATION_STATUSES
    op.kb.count_remediations_by_status.return_value = {"queued": 2, "resolved": 1}
    CFOperator._update_remediation_metrics(op)
    assert REMEDIATION_QUEUE.labels(status="queued")._value.get() == 2
    assert REMEDIATION_QUEUE.labels(status="resolved")._value.get() == 1
    assert REMEDIATION_QUEUE.labels(status="claimed")._value.get() == 0  # absent -> 0


def test_update_remediation_metrics_throttles():
    import time as _t
    op = MagicMock()
    op.last_metrics = _t.time()  # just ran -> should skip
    op._REMEDIATION_STATUSES = CFOperator._REMEDIATION_STATUSES
    CFOperator._update_remediation_metrics(op)
    op.kb.count_remediations_by_status.assert_not_called()


# ---- morning-summary (sweep finding) feed ------------------------------------


def _feed_op(feed=True):
    op = MagicMock()
    op.config = {"remediation": {"queue_feed": feed}}
    op._SEVERITY_RISK = CFOperator._SEVERITY_RISK
    op.kb.queue_remediation.return_value = 1
    # wire real helpers the methods call on self (MagicMock would shadow them)
    op._parse_summary_recommendations = CFOperator._parse_summary_recommendations
    op._recommendation_is_investigate_shaped = CFOperator._recommendation_is_investigate_shaped
    op._feed_remediations_from_sweeps = lambda reports: CFOperator._feed_remediations_from_sweeps(op, reports)
    # CFOP-46 loop-break: default to "no open remediation" so dispatch tests
    # exercise the dispatch path; a bare MagicMock (truthy) would silently skip
    # every dispatch and pass tests that no longer guard anything.
    op._dispatch_dedupe_key = CFOperator._dispatch_dedupe_key
    op._open_remediation_for_key = lambda key: CFOperator._open_remediation_for_key(op, key)
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    # CFOP-53: mutation-shaped recs now go through the classifier. Default the
    # stub to a degraded result so legacy-path tests stay on the direct-manual
    # path deliberately (not via an exception); classified-lane tests override.
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    op._maybe_queue_remediation = (
        lambda inv_id, details: CFOperator._maybe_queue_remediation(op, inv_id, details))
    _confirming_judge(op)
    _no_node_incident(op)
    return _wire_flags(op)


def test_recommendation_is_investigate_shaped():
    assert CFOperator._recommendation_is_investigate_shaped(
        "Check CoreDNS logs for errors/latency")
    assert CFOperator._recommendation_is_investigate_shaped(
        "Monitor the stability of the Loki service")
    assert CFOperator._recommendation_is_investigate_shaped(
        "Investigate the PostgreSQL database for stability")
    assert CFOperator._recommendation_is_investigate_shaped(
        "Verify /api/chat responds 200")
    # genuinely human — stay on the manual queue
    assert not CFOperator._recommendation_is_investigate_shaped(
        "Physically check power and ethernet for ubuntu-cm5-01")
    assert not CFOperator._recommendation_is_investigate_shaped(
        "Check power supply and network switch for the Pi cluster")
    assert not CFOperator._recommendation_is_investigate_shaped(
        "Replace SD card on rpi3")
    assert not CFOperator._recommendation_is_investigate_shaped("do x")
    assert not CFOperator._recommendation_is_investigate_shaped("")


def test_feed_from_sweeps_disabled():
    op = _feed_op(feed=False)
    reports = [{"findings": [{"id": "a", "remediation": "x", "severity": "warning"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 0
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_not_called()


def test_feed_from_sweeps_dispatches_investigate_shaped():
    # #36/#37-class: "check/verify/monitor" sweep recs must become autonomous
    # investigations, not needs-human manuals.
    op = _feed_op()
    reports = [{"findings": [
        {"id": "f1", "finding": "Ollama 500s", "remediation": "Verify DNS on raspberrypi5",
         "severity": "warning", "resource_name": "ollama"},
        {"id": "f2", "finding": "healthy", "remediation": "No action required. Healthy.", "severity": "info"},
    ]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1  # 2nd skipped
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_called_once()
    arg = op.enqueue_investigation.call_args.args[0]
    assert arg["source"] == "sweep-investigate"
    assert "Verify DNS" in arg["summary"]
    assert arg["host"] == "ollama"


def test_feed_from_sweeps_queues_human_only_manual():
    op = _feed_op()
    reports = [{"findings": [
        {"id": "f1", "finding": "Pi down", "remediation": "Physically inspect power strip",
         "severity": "critical", "resource_name": "raspberrypi4"},
    ]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    op.enqueue_investigation.assert_not_called()
    # CFOP-53 regression guard: genuinely human work never spends a classifier
    # call — the _HUMAN_ONLY_SHAPED gate keeps it on the direct-manual path.
    op._classify_needs_action_recommendation.assert_not_called()
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "manual"
    assert kwargs["risk"] == "high"
    assert kwargs["dedupe_key"] == "sweep-f1"


def test_feed_from_sweeps_classifies_mutation_shaped():
    # CFOP-53 (live row #43): concrete "change this" recs are the ones most
    # like executor work — they must reach the classifier and enqueue with a
    # real class/confidence/provider instead of hardcoded manual/None.
    op = _feed_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "host": None, "repo": None})
    reports = [{
        "sweep_meta": {"provider": "ollama", "model": "gemma4:26b"},
        "findings": [
            {"id": "f43", "finding": "plane-api readiness flaps",
             "evidence": "probe timeout 1s under inference load",
             "remediation": "Increase readinessProbe timeoutSeconds to 5 for plane-api",
             "severity": "warning", "namespace": "plane"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    op._classify_needs_action_recommendation.assert_called_once()
    op.enqueue_investigation.assert_not_called()
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "gitops-patch"
    assert kwargs["confidence"] == 0.9
    assert kwargs["risk"] == "low"
    assert kwargs["investigation_id"] is None  # no source investigation
    assert kwargs["host_id"] == "plane"  # falls back to the finding's namespace
    assert kwargs["dedupe_key"] == "sweep-f43"
    payload = kwargs["payload"]
    assert payload["provider"] == "ollama/gemma4:26b"
    assert payload["dedupe_key"] == "sweep-f43"  # in both places (KB contract)
    assert "probe timeout 1s" in payload["rendered_context"]


def test_feed_from_sweeps_classifier_degrade_falls_back_to_manual():
    # Fail toward current behavior: a degraded classification (manual with no
    # confidence) must not change what the feed did before CFOP-53 — direct
    # manual enqueue with severity-derived risk, never a dropped finding.
    op = _feed_op()  # harness default stub IS the degrade shape
    reports = [{"findings": [
        {"id": "f1", "finding": "promtail OOM",
         "remediation": "Increase memory limit for promtail",
         "severity": "warning", "resource_name": "promtail"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    op._classify_needs_action_recommendation.assert_called_once()
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "manual"
    assert kwargs["risk"] == "med"  # severity-derived, not the degrade's 'high'
    assert kwargs["dedupe_key"] == "sweep-f1"
    assert kwargs["payload"]["finding"] == "promtail OOM"  # legacy payload shape


def test_feed_from_sweeps_classifier_exception_falls_back_to_manual():
    op = _feed_op()
    op._classify_needs_action_recommendation = MagicMock(side_effect=RuntimeError("llm down"))
    reports = [{"findings": [
        {"id": "f1", "finding": "promtail OOM",
         "remediation": "Increase memory limit for promtail",
         "severity": "critical", "resource_name": "promtail"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "manual"
    assert kwargs["dedupe_key"] == "sweep-f1"


def test_feed_from_sweeps_classifier_investigate_dispatches():
    # The rubric prefers 'investigate' for borderline recs the
    # _INVESTIGATE_SHAPED regex missed. Enqueuing that class would normalize
    # it to 'manual' (not in _REMEDIATION_CLASSES) and park the row — the
    # opposite of what the same class does everywhere else.
    op = _feed_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "investigate", "risk": "low", "confidence": 0.7,
        "host": None, "repo": None})
    reports = [{"findings": [
        {"id": "f7", "finding": "Loki flushes slow",
         "remediation": "Correlate flush latency with ingester restarts",
         "severity": "warning", "resource_name": "loki"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_called_once()
    arg = op.enqueue_investigation.call_args.args[0]
    assert arg["source"] == "sweep-investigate"
    assert arg["dedupe_key"] == CFOperator._dispatch_dedupe_key("loki", "Loki flushes slow")


def test_maybe_queue_remediation_truncates_host_id():
    # RemediationQueue.host_id is String(64); an over-long k8s resource name
    # must not reject the INSERT after classification already succeeded.
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": True}}
    op.kb.queue_remediation.return_value = 5
    details = {"remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
               "recommendation": "r", "host": "a" * 100}
    assert CFOperator._maybe_queue_remediation(op, None, details) == 5
    assert op.kb.queue_remediation.call_args.kwargs["host_id"] == "a" * 64


def test_feed_from_sweeps_classified_dedupe_not_double_enqueued():
    # A deduped classified row must not fall through and try the manual path —
    # one finding, one queue_remediation call.
    op = _feed_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "host": "plane", "repo": None})
    op.kb.queue_remediation.return_value = None  # deduped by kb
    reports = [{"findings": [
        {"id": "f43", "remediation": "Increase readinessProbe timeoutSeconds",
         "severity": "warning", "namespace": "plane"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 0
    op.kb.queue_remediation.assert_called_once()


def test_feed_from_sweeps_dedup_not_counted():
    op = _feed_op()
    op.kb.queue_remediation.return_value = None  # deduped by kb
    reports = [{"findings": [{"id": "f1", "remediation": "do x", "severity": "critical"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 0
    op.enqueue_investigation.assert_not_called()


# ---- summary structured-recommendations feed ---------------------------------

_SUMMARY = """## Morning Summary
All quiet overnight. A couple of low-priority items.

```json
{"recommendations": [
  {"title": "Ollama 500s on raspberrypi5", "recommendation": "Verify DNS / CoreDNS health",
   "host": "raspberrypi5", "remediation_class": "node-action", "risk": "med", "confidence": 0.6},
  {"title": "Corrupted Immich videos", "recommendation": "Quarantine Failed Videos dir",
   "host": "", "remediation_class": "node-action", "risk": "high", "confidence": 0.5}
]}
```
"""


def test_parse_summary_recommendations():
    recs = CFOperator._parse_summary_recommendations(_SUMMARY)
    assert len(recs) == 2
    assert recs[0]["title"].startswith("Ollama")
    assert CFOperator._parse_summary_recommendations("no json here") == []
    assert CFOperator._parse_summary_recommendations("```json\n{bad}\n```") == []


def test_strip_summary_recommendations_block():
    # The machine-readable json block must not leak into operator-facing channels.
    stripped = CFOperator._strip_summary_recommendations_block(_SUMMARY)
    assert "```json" not in stripped
    assert "recommendations" not in stripped
    assert "All quiet overnight." in stripped  # prose preserved
    # but the queue can still parse the original (strip happens after feed)
    assert len(CFOperator._parse_summary_recommendations(_SUMMARY)) == 2
    # no-op when there is no block, and safe on empty input
    assert CFOperator._strip_summary_recommendations_block("just text") == "just text"
    assert CFOperator._strip_summary_recommendations_block("") == ""


def test_feed_from_summary_routes_mutations_to_investigation():
    # Summary mutation-class recs are unverified hypotheses from the cheap model,
    # so they go to the investigation pipeline, not straight into the queue.
    op = _feed_op()
    n = CFOperator._feed_remediations_from_summary(op, _SUMMARY, [])
    assert n == 0
    op.kb.queue_remediation.assert_not_called()
    assert op.enqueue_investigation.call_count == 2  # both node-action recs
    # the proposed class is preserved for the investigator's traceability
    arg0 = op.enqueue_investigation.call_args_list[0].args[0]
    assert "[proposed: node-action]" in arg0["summary"]
    assert arg0["source"] == "summary-investigate" and arg0["host"] == "raspberrypi5"


def test_feed_from_summary_falls_back_to_sweeps_when_no_block():
    op = _feed_op()
    # non-investigate-shaped → still queues as manual via sweep fallback
    reports = [{"findings": [{"id": "f1", "remediation": "do x", "severity": "warning"}]}]
    n = CFOperator._feed_remediations_from_summary(op, "plain text, no json block", reports)
    assert n == 1  # fell back to sweep findings
    assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "sweep-f1"


def test_feed_from_summary_fallback_dispatches_investigate_shaped_sweeps():
    op = _feed_op()
    reports = [{"findings": [
        {"id": "e1ce1ec8", "finding": "DNS issues",
         "remediation": "Check CoreDNS logs for errors/latency", "severity": "warning"},
    ]}]
    n = CFOperator._feed_remediations_from_summary(op, "plain text, no json block", reports)
    assert n == 1
    op.kb.queue_remediation.assert_not_called()
    arg = op.enqueue_investigation.call_args.args[0]
    assert arg["source"] == "sweep-investigate"
    assert "Check CoreDNS" in arg["summary"]


def test_feed_from_summary_disabled():
    op = _feed_op(feed=False)
    assert CFOperator._feed_remediations_from_summary(op, _SUMMARY, []) == 0
    op.kb.queue_remediation.assert_not_called()


_SUMMARY_INV = """## Summary
```json
{"recommendations": [
  {"title": "Check ollama logs", "recommendation": "Verify /api/chat responds 200",
   "remediation_class": "investigate", "risk": "low", "confidence": 0.6},
  {"title": "Bump probe", "recommendation": "add probe timeout",
   "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9, "repo": "o/r"}
]}
```
"""


def test_feed_summary_routes_investigate_and_mutations_to_investigation():
    op = _feed_op()
    n = CFOperator._feed_remediations_from_summary(op, _SUMMARY_INV, [])
    # both the 'investigate' rec AND the unverified gitops-patch -> investigation;
    # nothing from the cheap summary becomes a remediation directly.
    assert op.enqueue_investigation.call_count == 2
    assert n == 0 and op.kb.queue_remediation.call_count == 0


_SUMMARY_MANUAL = """## Summary
```json
{"recommendations": [
  {"title": "Replace SD card on rpi3", "recommendation": "SD card wearing out; swap it",
   "host": "raspberrypi3", "remediation_class": "manual", "risk": "high", "confidence": 0.95}
]}
```
"""


def test_feed_summary_manual_queues_with_clamped_confidence():
    # Non-mutation (human-only) classes still queue, but the cheap model's
    # self-reported confidence is clamped so it can't look authoritative.
    op = _feed_op()
    n = CFOperator._feed_remediations_from_summary(op, _SUMMARY_MANUAL, [])
    assert n == 1
    op.enqueue_investigation.assert_not_called()
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["remediation_class"] == "manual"
    assert kw["confidence"] == _SUMMARY_CONFIDENCE_CAP  # clamped from 0.95


_SUMMARY_MISLABELLED_MANUAL = """## Summary
```json
{"recommendations": [
  {"title": "Connectivity/DNS issues", "recommendation": "Check CoreDNS logs for errors/latency",
   "host": "", "remediation_class": "manual", "risk": "med", "confidence": 0.7},
  {"title": "Promtail Loki resets", "recommendation": "Monitor Loki stability and Promtail connectivity",
   "host": "", "remediation_class": "manual", "risk": "med", "confidence": 0.6}
]}
```
"""


def test_feed_summary_mislabelled_manual_investigate_shaped_dispatches():
    # Cheap model often labels check/monitor items as manual; still investigate.
    op = _feed_op()
    n = CFOperator._feed_remediations_from_summary(op, _SUMMARY_MISLABELLED_MANUAL, [])
    assert n == 0
    op.kb.queue_remediation.assert_not_called()
    assert op.enqueue_investigation.call_count == 2
    summaries = [c.args[0]["summary"] for c in op.enqueue_investigation.call_args_list]
    assert any("Check CoreDNS" in s for s in summaries)
    assert any("Monitor Loki" in s for s in summaries)
    assert all("[proposed:" not in s for s in summaries)


# ---- read API serialization (operator console) -------------------------------


def test_remediation_row_dict_serializes():
    import types
    from datetime import datetime, timezone
    from knowledge_base import remediation_row_dict
    row = types.SimpleNamespace(
        id=5, status="needs-human", remediation_class="node-action", risk="med",
        confidence=0.6, host_id="rpi5", investigation_id=99, priority=5, attempts=0,
        pr_url=None, last_error=None, result={"resolution_note": "fixed by hand"},
        created_at=datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc),
        claimed_at=None, completed_at=None, payload={"recommendation": "verify DNS"})
    d = remediation_row_dict(row)
    assert d["id"] == 5 and d["status"] == "needs-human" and d["risk"] == "med"
    assert d["created_at"].startswith("2026-06-19T12:00")
    assert d["claimed_at"] is None
    assert d["payload"]["recommendation"] == "verify DNS"
    # the console renders the resolution note from result
    assert d["result"]["resolution_note"] == "fixed by hand"


# ---- operator console actions (manual close) ---------------------------------


def _console_client():
    """Flask test client wired to a stub operator whose kb records status writes.

    Auth is installed in its explicit dev-bypass mode. These tests are about
    what the handlers do with a body, not about who may call them — but the
    console's mutating routes carry @require_role(ROLE_ADMIN), which denies by
    default when no ConsoleAuth is present. Without this the requests 401 before
    reaching a handler, and the non-object-body tests below would still pass
    (401 != 500) while asserting nothing at all. Role enforcement itself is
    covered by test_web_auth_db.py.
    """
    from web_server import WebServer
    from web_auth import install_auth
    from flask import Flask
    import os
    import threading as _t

    from knowledge_base import KnowledgeBase

    operator = MagicMock()
    operator.kb.update_remediation_status.return_value = True
    operator.kb.get_remediation.return_value = {"id": 7, "status": "resolved"}
    operator.kb.reclassify_remediation.return_value = {"id": 7, "status": "queued"}
    # Real policy, not a truthy Mock — a bare MagicMock here would make the
    # approve handler 409 every row and silently invert what these tests see.
    operator.kb.remediation_approve_conflict = KnowledgeBase.remediation_approve_conflict
    operator._REMEDIATION_FLAGS = ("queue_feed", "queue_drain")
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = _t.Lock()
    server._setup_routes()

    # ConsoleAuth reads the flag once, at construction, so restoring the
    # environment afterwards leaves this app bypassed without leaking the
    # setting into any other test in the session.
    prior = os.environ.get("CFOP_AUTH_DISABLED")
    os.environ["CFOP_AUTH_DISABLED"] = "true"
    try:
        install_auth(server.app, ui_dir="ui")
    finally:
        if prior is None:
            os.environ.pop("CFOP_AUTH_DISABLED", None)
        else:
            os.environ["CFOP_AUTH_DISABLED"] = prior

    return server.app.test_client(), operator


def test_resolve_endpoint_closes_row_with_note():
    client, op = _console_client()
    resp = client.post("/api/remediations/7/resolve", json={"note": "fixed by hand"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "resolved"
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args == (7, "resolved")
    # the why goes in result, never last_error (which means failure)
    assert kwargs["result"] == {"resolved_by": "operator", "resolution_note": "fixed by hand"}
    assert "last_error" not in kwargs


def test_resolve_endpoint_allows_empty_note():
    client, op = _console_client()
    resp = client.post("/api/remediations/7/resolve", json={"note": "   "})
    assert resp.status_code == 200
    assert op.kb.update_remediation_status.call_args.kwargs["result"]["resolution_note"] is None


def test_resolve_endpoint_survives_non_object_body():
    """A bare JSON list/string is 'no note', not a 500."""
    client, op = _console_client()
    resp = client.post("/api/remediations/7/resolve", data="[1, 2]",
                       content_type="application/json")
    assert resp.status_code == 200
    assert op.kb.update_remediation_status.call_args.kwargs["result"]["resolution_note"] is None


@pytest.mark.parametrize("path", [
    "/api/remediations",
    "/api/remediations/7/reject",
    "/api/remediations/7/resolve",
    "/api/remediations/7/reclassify",
    "/api/remediation/flags",
])
@pytest.mark.parametrize("body", ["[1, 2]", '"note"', "7"])
def test_console_posts_never_500_on_non_object_body(path, body):
    """json_object() reads a non-object body as empty, so the endpoint's own
    field validation decides the status (200/400) — never AttributeError."""
    client, _ = _console_client()
    resp = client.post(path, data=body, content_type="application/json")
    assert resp.status_code != 500


def test_resolve_endpoint_404s_on_unknown_id():
    client, op = _console_client()
    op.kb.update_remediation_status.return_value = False
    resp = client.post("/api/remediations/999/resolve", json={"note": "x"})
    assert resp.status_code == 404


# ---- live flag resolution (console toggles) ----------------------------------


def test_remediation_flag_db_overrides_config():
    op = MagicMock()
    op.config = {"remediation": {"queue_drain": False}}
    op.kb.get_setting.return_value = "1"  # DB toggle says on
    assert CFOperator._remediation_flag(op, "queue_drain") is True
    op.kb.get_setting.return_value = "0"  # DB toggle says off, overrides config
    op.config = {"remediation": {"queue_drain": True}}
    assert CFOperator._remediation_flag(op, "queue_drain") is False


def test_remediation_flag_falls_back_to_config():
    op = MagicMock()
    op.kb.get_setting.return_value = ""  # no DB override
    op.config = {"remediation": {"queue_feed": True}}
    assert CFOperator._remediation_flag(op, "queue_feed") is True
    op.config = {"remediation": {}}
    assert CFOperator._remediation_flag(op, "queue_drain") is False


# ---- CFOP-22: reporting LLM + deep PR attempt on the queue ------------------


def test_llm_provider_tag():
    assert _llm_provider_tag({"backend": "xai", "model": "grok"}) == "xai/grok"
    assert _llm_provider_tag({"provider": "ollama", "model": "qwen"}) == "ollama/qwen"
    assert _llm_provider_tag({"backend": "anthropic"}) == "anthropic"
    assert _llm_provider_tag({}) is None
    assert _llm_provider_tag(None) is None


def test_maybe_queue_remediation_stamps_provider():
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": True}}
    op.kb.queue_remediation.return_value = 9
    details = {
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "recommendation": "fix mount", "host": "rpi4",
        "provider": "anthropic/claude-sonnet-4-6", "report": "long report",
    }
    assert CFOperator._maybe_queue_remediation(op, 41, details) == 9
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["provider"] == "anthropic/claude-sonnet-4-6"


def test_feed_from_sweeps_stamps_sweep_meta_provider():
    op = _feed_op()
    reports = [{
        "sweep_meta": {"provider": "ollama", "model": "qwen2.5:14b"},
        "findings": [{"id": "f9", "remediation": "replace SD card", "severity": "warning",
                      "resource_name": "rpi3"}],
    }]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["provider"] == "ollama/qwen2.5:14b"


def test_feed_from_summary_stamps_provider_on_manual():
    op = _feed_op()
    n = CFOperator._feed_remediations_from_summary(
        op, _SUMMARY_MANUAL, [], provider="xai/grok-4")
    assert n == 1
    assert op.kb.queue_remediation.call_args.kwargs["payload"]["provider"] == "xai/grok-4"


def test_store_deep_investigation_stamps_provider_and_pr_attempt():
    op = _wire_flags(MagicMock())
    op.config = {"remediation": {"queue_feed": True, "deep_open_prs": True}}
    op.kb.start_investigation.return_value = 2141
    op.kb.queue_remediation.return_value = 41
    op._embed_investigation = MagicMock()
    op._count_enqueued = MagicMock()
    op._maybe_open_pr_from_deep_diff = MagicMock(return_value={
        "status": "declined", "detail": "not a clean single-file unified diff",
    })
    # Use the real queue helper so provider lands in the payload.
    op._maybe_queue_remediation = lambda inv_id, details: CFOperator._maybe_queue_remediation(
        op, inv_id, details)
    _no_node_incident(op)

    alert = {"summary": "CIFS mount failed"}
    result = {"details": {
        "outcome": "needs_action", "host": "raspberrypi4", "model": "claude-sonnet-4-6",
        "recommendation": "patch mount unit", "report": "diff proposed",
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.65,
        "proposed_diff": "diff --git a/x b/x\n", "duration_s": 12.0,
    }}
    out = CFOperator.store_deep_investigation(op, alert, result)
    assert out["investigation_id"] == 2141
    assert out["remediation_id"] == 41
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["provider"] == "anthropic/claude-sonnet-4-6"
    op.kb.merge_remediation_payload.assert_called_once()
    args, kwargs = op.kb.merge_remediation_payload.call_args
    assert args[0] == 41
    assert args[1]["pr_attempt"]["status"] == "declined"
    assert "single-file" in args[1]["pr_attempt"]["detail"]
    assert kwargs.get("pr_url") is None


# ---- CFOP-46: needs_action investigations feed the queue ----------------------


def _na_op(feed=True):
    """Op wired for _queue_needs_action_remediation with the real queue helpers."""
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": feed}}
    op.kb.queue_remediation.return_value = 9
    op._count_enqueued = MagicMock()
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = lambda inv_id, details: CFOperator._maybe_queue_remediation(
        op, inv_id, details)
    op._queue_needs_action_remediation = lambda *a, **k: CFOperator._queue_needs_action_remediation(
        op, *a, **k)
    op._open_remediation_for_key = lambda key: CFOperator._open_remediation_for_key(op, key)
    op._stamp_opened_pr = lambda row, url: CFOperator._stamp_opened_pr(op, row, url)
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    # A bare MagicMock's git_repos() iterates empty, which now means "nothing
    # resolves" and would sink every gitops-manifest FIX for a reason unrelated
    # to what these tests check (CFOP-85).
    op.git_repos.return_value = [
        {"name": "homelab-infra", "github": "aachtenberg/homelab-infra"},
        {"name": "cfoperator", "github": "aachtenberg/cfoperator"},
    ]
    return op


def test_needs_action_enqueues_with_dedupe_key_in_both_places():
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.5,
        "host": None, "repo": "aachtenberg/homelab-infra"})
    alert = {"fingerprint": "abc123", "labels": {"instance": "raspberrypi5"}}
    rid = op._queue_needs_action_remediation(
        2195, "Promtail OOM", alert, "raise memory limit to 512Mi", "report text",
        provider="ollama/gemma4:26b")
    assert rid == 9
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["remediation_class"] == "gitops-patch"
    assert kw["investigation_id"] == 2195
    # The KB filters on payload['dedupe_key'] and does NOT inject it: the key
    # must be in both the kwarg and the payload or dedupe silently never fires.
    assert kw["dedupe_key"] == "alert-abc123"
    assert kw["payload"]["dedupe_key"] == "alert-abc123"
    assert kw["payload"]["provider"] == "ollama/gemma4:26b"
    assert kw["payload"]["repo"] == "aachtenberg/homelab-infra"
    assert kw["payload"]["source"] == "investigation"
    assert kw["host_id"] == "raspberrypi5"  # from alert labels when the classifier gives none
    op._count_enqueued.assert_called_once_with("investigation", "gitops-patch", "low", 0.5)


def test_needs_action_skips_empty_no_action_and_opened_pr():
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock()
    assert op._queue_needs_action_remediation(1, "t", {}, "", "r", provider="p") is None
    assert op._queue_needs_action_remediation(1, "t", {}, "No action needed", "r", provider="p") is None
    # inline unschedulable-pod proposer already opened a PR -> one fix, one driver
    prop = MagicMock()
    prop.pr_result = {"status": "opened", "html_url": "https://x"}
    assert op._queue_needs_action_remediation(1, "t", {}, "fix it", "r",
                                              provider="p", proposal=prop) is None
    op._classify_needs_action_recommendation.assert_not_called()
    op.kb.queue_remediation.assert_not_called()
    # a *declined* inline proposal must still enqueue
    op._classify_needs_action_recommendation.return_value = {
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None}
    declined = MagicMock()
    declined.pr_result = None
    assert op._queue_needs_action_remediation(1, "t", {}, "fix it", "r",
                                              provider="p", proposal=declined) == 9


def test_needs_action_flag_off_spends_no_llm_call():
    op = _na_op(feed=False)
    op._classify_needs_action_recommendation = MagicMock()
    assert op._queue_needs_action_remediation(1, "t", {}, "fix it", "r", provider="p") is None
    op._classify_needs_action_recommendation.assert_not_called()
    op.kb.queue_remediation.assert_not_called()


def test_classifier_llm_failure_degrades_to_needs_human():
    op = MagicMock()
    op._chat_with_tools_with_fallback = MagicMock(side_effect=RuntimeError("no providers"))
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints == {"remediation_class": "manual", "risk": "high",
                     "confidence": None, "host": None, "repo": None,
                     # the ladder ran out, so no model produced this result
                     "classifier_backend": None, "classifier_model": None}
    # and that degraded row can never clear the auto-execute gate
    nc, nr = normalize_remediation_fields(hints["remediation_class"], hints["risk"])
    assert remediation_is_auto_eligible(nc, nr, hints["confidence"]) is False


def _classifier_op():
    """Op wired for the classification ladder with real parse + no providers."""
    op = MagicMock()
    op._parse_remediation_classification = CFOperator._parse_remediation_classification
    op._get_provider_chain = MagicMock(return_value=[])  # no escalation target
    return op


_GOOD_CLASSIFICATION = ('{"remediation_class": "gitops-patch", "risk": "low", '
                        '"confidence": 0.9, "host": "", "repo": "o/r"}')


def test_classifier_ladder_exhausted_degrades_to_needs_human():
    op = _classifier_op()
    op._chat_with_tools_with_fallback = MagicMock(
        return_value={"response": "sure, sounds like a config problem to me",
                      "backend": "ollama", "model": "gemma4:26b"})
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints["remediation_class"] == "manual"
    assert hints["risk"] == "high"
    assert hints["confidence"] is None
    # the ladder ran: initial call + nudge retry (no distinct provider to escalate to)
    assert op._chat_with_tools_with_fallback.call_count == 2
    # and the degraded result can never clear the auto gate
    nc, nr = normalize_remediation_fields(hints["remediation_class"], hints["risk"])
    assert remediation_is_auto_eligible(nc, nr, hints["confidence"]) is False


def test_classifier_nudge_rescues_bad_shape():
    # The row-#42 failure mode: findings-array first, correct object after the
    # corrective retry that quotes the malformed output back (PR #76 pattern).
    op = _classifier_op()
    bad = '[{"severity": "info", "finding": "wrong shape"}]'
    op._chat_with_tools_with_fallback = MagicMock(side_effect=[
        {"response": bad, "backend": "ollama", "model": "gemma4:26b"},
        {"response": _GOOD_CLASSIFICATION, "backend": "ollama", "model": "gemma4:26b"},
    ])
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints["remediation_class"] == "gitops-patch"
    assert hints["confidence"] == 0.9
    # the cap-lift happy path: a nudge-rescued confident classification CAN
    # clear the auto gate (PR #134 review — assert the positive, not only the
    # degraded negative)
    nc, nr = normalize_remediation_fields(hints["remediation_class"], hints["risk"])
    assert remediation_is_auto_eligible(nc, nr, hints["confidence"]) is True
    retry_messages = op._chat_with_tools_with_fallback.call_args_list[1].kwargs["messages"]
    assert any(bad[:50] in str(m.get("content")) for m in retry_messages)  # quoted back
    assert any("single JSON object" in str(m.get("content")) for m in retry_messages)


def test_classifier_escalates_to_distinct_provider():
    # Nudge fails too -> one attempt on the first provider whose (backend, model)
    # differs from the one that answered wrongly; primary is skipped.
    op = _classifier_op()
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": "still not json", "backend": "ollama", "model": "gemma4:26b"})
    op._get_provider_chain = MagicMock(return_value=[
        ("ollama", "http://llm:11434", "gemma4:26b"),
        ("anthropic", "", "claude-sonnet-5"),
    ])
    op._chat_with_tools = MagicMock(return_value={"response": _GOOD_CLASSIFICATION})
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints["remediation_class"] == "gitops-patch"
    esc = op._chat_with_tools.call_args.kwargs
    assert esc["provider_type"] == "anthropic" and esc["model"] == "claude-sonnet-5"
    nc, nr = normalize_remediation_fields(hints["remediation_class"], hints["risk"])
    assert remediation_is_auto_eligible(nc, nr, hints["confidence"]) is True


def test_classifier_escalation_skips_every_provider_that_answered():
    # The fallback wrapper rotates on transport errors, so the first call and
    # the nudge may be served by DIFFERENT providers. Rung 3 must skip all of
    # them, not just the first (PR #134 review) — else it re-asks a model that
    # just produced garbage on this very ladder.
    op = _classifier_op()
    op._chat_with_tools_with_fallback = MagicMock(side_effect=[
        {"response": "junk", "backend": "ollama", "model": "gemma4:26b"},
        {"response": "more junk", "backend": "anthropic", "model": "claude-sonnet-5"},
    ])
    op._get_provider_chain = MagicMock(return_value=[
        ("ollama", "http://llm:11434", "gemma4:26b"),
        ("anthropic", "", "claude-sonnet-5"),
        ("groq", "", "gpt-oss-120b"),
    ])
    op._chat_with_tools = MagicMock(return_value={"response": _GOOD_CLASSIFICATION})
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints["remediation_class"] == "gitops-patch"
    esc = op._chat_with_tools.call_args.kwargs
    assert esc["provider_type"] == "groq" and esc["model"] == "gpt-oss-120b"


def test_classifier_prompt_example_is_never_auto_eligible():
    # Small local models parrot few-shot examples. If the prompt's worked
    # example were an auto-gate-clearing tuple with a real repo, a parroted
    # response would auto-queue a PR without classifying anything
    # (PR #134 review). The example must land needs-human.
    ex = agent_mod._CLASSIFIER_SAFE_EXAMPLE
    nc, nr = normalize_remediation_fields(ex["remediation_class"], ex["risk"])
    assert remediation_is_auto_eligible(nc, nr, ex["confidence"]) is False
    assert not ex["repo"]  # never a real GitOps slug the executor could target


def test_parse_remediation_classification():
    parse = CFOperator._parse_remediation_classification
    out = parse('```json\n{"remediation_class": "gitops-patch", "risk": "low", '
                '"confidence": 0.9, "host": "pi5", "repo": "o/r"}\n```')
    assert out["remediation_class"] == "gitops-patch"
    # NOT capped (CFOP-48): a confident classification may clear the >= 0.8
    # auto gate; the PR merge button is the human gate. This is the assertion
    # that keeps the cap-lift from regressing silently.
    assert out["confidence"] == 0.9
    # above the 0-1 scale = uncalibrated -> None (cannot clear the gate),
    # never clamped up to certainty; negatives clamp to harmless 0
    assert parse('{"remediation_class": "manual", "confidence": 7}')["confidence"] is None
    assert parse('{"remediation_class": "manual", "confidence": -1}')["confidence"] == 0.0
    assert out["host"] == "pi5" and out["repo"] == "o/r"
    assert parse("no json here") is None
    assert parse('{"risk": "low"}') is None  # class is mandatory
    # unknown/other classes pass through for normalize_remediation_fields to default
    assert parse('{"remediation_class": "investigate"}')["remediation_class"] == "investigate"


# ---- CFOP-60: an incoherent classification is not a classification --------
#
# Live row #49: `kubectl create job --from=cronjob/... -n data` came back as
# node-action with host null, confidence 1.0. node-action means "a host change
# over ssh/ansible", so with no host the row can never execute — it rode the
# queue to the executor, died on "node-action execution not enabled", and
# parked needs-human. These guard the CLASS (a class whose required field the
# model left empty is untrustworthy), not this one model's quirk.


def test_node_action_without_a_host_is_not_a_classification():
    parse = CFOperator._parse_remediation_classification
    # Row #49's actual shape: confident, well-formed JSON, incoherent answer.
    assert parse('{"remediation_class": "node-action", "risk": "low", '
                 '"confidence": 1.0, "host": "", "repo": ""}') is None
    assert parse('{"remediation_class": "node-action", "confidence": 0.9}') is None
    assert parse('{"remediation_class": "node-action", "host": "   "}') is None
    # With a host it is coherent and parses as before.
    ok = parse('{"remediation_class": "node-action", "risk": "med", '
               '"confidence": 0.8, "host": "raspberrypi5"}')
    assert ok is not None and ok["host"] == "raspberrypi5"


def test_other_classes_do_not_require_a_host():
    # Only node-action needs somewhere to ssh. A k8s-action acts on the
    # cluster and a gitops-patch on a repo, so a missing host is normal —
    # over-tightening here would park perfectly good rows.
    parse = CFOperator._parse_remediation_classification
    for rclass in ("k8s-action", "gitops-patch", "investigate", "manual"):
        out = parse('{"remediation_class": "%s", "confidence": 0.9}' % rclass)
        assert out is not None, rclass
        assert out["host"] is None


def test_incoherent_node_action_nudges_instead_of_dead_parking():
    """Row #49 end-to-end: the ladder gets a second opinion rather than
    accepting a class that cannot execute."""
    op = _classifier_op()
    incoherent = ('{"remediation_class": "node-action", "risk": "low", '
                  '"confidence": 1.0, "host": "", "repo": ""}')
    op._chat_with_tools_with_fallback = MagicMock(side_effect=[
        {"response": incoherent, "backend": "ollama", "model": "gemma4:26b"},
        {"response": _GOOD_CLASSIFICATION, "backend": "ollama", "model": "gemma4:26b"},
    ])
    hints = CFOperator._classify_needs_action_recommendation(
        op, "reservoir-ingest CronJob failing",
        "kubectl create job --from=cronjob/reservoir-ingest test-run -n data", {})
    # Second opinion accepted; the dead-parking node-action never survives.
    assert hints["remediation_class"] == "gitops-patch"
    assert op._chat_with_tools_with_fallback.call_count == 2


def test_kubectl_verbs_have_somewhere_to_land_in_the_rubric():
    """The rubric is the only definition of the classes and is shared verbatim
    by both feeds. Row #49 drifted because k8s-action's examples were all
    restart/destroy verbs, so a create verb had no anchor."""
    rubric = agent_mod._REMEDIATION_CLASS_RUBRIC
    assert "k8s-action" in rubric and "node-action" in rubric
    lower = rubric.lower()
    assert "create" in lower, "a create verb must have somewhere to land"
    assert "kubectl" in lower, "the kubectl/ssh boundary must be stated, not implied"


def test_investigate_shaped_covers_inspect_and_capture():
    """Row #49's rec was evidence-gathering in intent ('capture real-time logs
    and inspect the error output') but used neither word the regex knew."""
    shaped = CFOperator._recommendation_is_investigate_shaped
    assert shaped("inspect the scrape container's error output") is True
    assert shaped("trigger a test run to capture real-time logs") is True
    # The human-only exclusion still wins over an evidence-gathering verb.
    assert shaped("inspect the SD card and replace it if worn") is False
    assert shaped("restart the deployment") is False


def test_investigation_dedupe_key_precedence_and_stability():
    key = CFOperator._investigation_dedupe_key
    # dispatch stamp (summary/sweep loop-break) wins over everything
    assert key({"dedupe_key": "inv-dispatch-x", "fingerprint": "f"}, "rec") == "inv-dispatch-x"
    # then the Alertmanager fingerprint: stable per firing labelset, so six
    # differently-worded investigations of one alert collapse to one row
    assert key({"fingerprint": "f00d"}, "rec") == "alert-f00d"
    # no-alert fallback: stable, whitespace/case-insensitive, host-sensitive
    a = key({"labels": {"instance": "pi5"}}, "Raise  Memory Limit")
    b = key({"labels": {"instance": "pi5"}}, "raise memory limit")
    assert a == b and a.startswith("inv-")
    assert key({"labels": {"instance": "pi4"}}, "raise memory limit") != a


def test_feed_from_sweeps_skips_when_remediation_open():
    op = _feed_op()
    op.kb.find_open_remediation_by_dedupe_key.return_value = {"id": 44, "investigation_id": 2195}
    reports = [{"findings": [
        {"id": "f1", "finding": "Promtail OOM", "remediation": "Check promtail memory usage",
         "severity": "warning", "resource_name": "promtail"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1  # handled, not re-dispatched
    op.enqueue_investigation.assert_not_called()
    op.kb.queue_remediation.assert_not_called()


def test_feed_from_sweeps_stamps_dispatch_key():
    op = _feed_op()
    reports = [{"findings": [
        {"id": "f1", "finding": "Promtail OOM", "remediation": "Check promtail memory usage",
         "severity": "warning", "resource_name": "promtail"}]}]
    CFOperator._feed_remediations_from_sweeps(op, reports)
    arg = op.enqueue_investigation.call_args.args[0]
    assert arg["dedupe_key"] == CFOperator._dispatch_dedupe_key("promtail", "Promtail OOM")
    # the key the feed checked is the key it stamped — one contract end to end
    op.kb.find_open_remediation_by_dedupe_key.assert_called_once_with(arg["dedupe_key"])


def test_feed_from_summary_skips_when_remediation_open():
    op = _feed_op()
    op.kb.find_open_remediation_by_dedupe_key.return_value = {"id": 44, "investigation_id": 2195}
    n = CFOperator._feed_remediations_from_summary(op, _SUMMARY_MISLABELLED_MANUAL, [])
    assert n == 0
    op.enqueue_investigation.assert_not_called()


def test_feed_dispatch_fails_open_on_kb_error():
    # Suppression is the optimization; dispatching is the long-standing behavior.
    op = _feed_op()
    op.kb.find_open_remediation_by_dedupe_key.side_effect = RuntimeError("db down")
    reports = [{"findings": [
        {"id": "f1", "finding": "Promtail OOM", "remediation": "Check promtail memory",
         "severity": "warning", "resource_name": "promtail"}]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    op.enqueue_investigation.assert_called_once()


def test_needs_action_deduped_repeat_links_to_open_row():
    # A repeat firing whose enqueue is dedupe-suppressed must still return the
    # open row's id, or the console shows "none proposed" on the repeat
    # investigation while the row sits on the worklist (PR #131 review).
    op = _na_op()
    op.kb.queue_remediation.return_value = None  # dedupe-suppressed by the KB
    op.kb.find_open_remediation_by_dedupe_key.return_value = {"id": 44, "status": "needs-human"}
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.5,
        "host": None, "repo": None})
    rid = op._queue_needs_action_remediation(
        2200, "same alert again", {"fingerprint": "abc123"}, "same fix", "r",
        provider="ollama/gemma4:26b")
    assert rid == 44
    op.kb.find_open_remediation_by_dedupe_key.assert_called_once_with("alert-abc123")
    # and with no open row either, the repeat genuinely has nothing to link
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    assert op._queue_needs_action_remediation(
        2200, "same alert again", {"fingerprint": "abc123"}, "same fix", "r",
        provider="ollama/gemma4:26b") is None


def test_investigation_findings_remediation_helpers():
    from knowledge_base import (_investigation_remediation_id,
                                _investigation_remediation_pr_url)
    assert _investigation_remediation_id({"remediation_id": 7}) == 7
    assert _investigation_remediation_id({"remediation_id": "7"}) is None  # ints only
    assert _investigation_remediation_id(None) is None
    pr = {"remediation_proposal": {"remediation_pr": {
        "status": "opened", "html_url": "https://github.com/x/y/pull/9"}}}
    assert _investigation_remediation_pr_url(pr) == "https://github.com/x/y/pull/9"
    # declines and malformed shapes surface nothing
    assert _investigation_remediation_pr_url({"remediation_proposal": {
        "remediation_pr": {"status": "declined", "detail": "no manifest"}}}) is None
    assert _investigation_remediation_pr_url({"remediation_proposal": "junk"}) is None
    assert _investigation_remediation_pr_url({}) is None


# ---- CFOP-49: Approve must refuse manual-class rows ---------------------------


def test_remediation_approve_conflict_policy():
    from knowledge_base import remediation_approve_conflict
    # manual = human-only: refuse with a reason that names the legitimate exits
    reason = remediation_approve_conflict({"id": 42, "remediation_class": "manual"})
    assert reason is not None
    assert "Reclassify" in reason and "Resolve" in reason
    # every mechanizable class stays approvable — this is exactly what row #42
    # needed after its reclassify, so the happy path is the regression pin
    for rclass in ("gitops-patch", "k8s-action", "node-action"):
        assert remediation_approve_conflict({"remediation_class": rclass}) is None
    # absent/malformed rows are not this policy's problem (the handler 404s first)
    assert remediation_approve_conflict(None) is None
    assert remediation_approve_conflict({}) is None


def test_kb_method_delegates_to_policy():
    # web_server reaches the policy through operator.kb (it imports nothing
    # from agent/); the staticmethod must stay wired to the module function.
    from knowledge_base import KnowledgeBase
    assert KnowledgeBase.remediation_approve_conflict(
        {"remediation_class": "manual"}) is not None
    assert KnowledgeBase.remediation_approve_conflict(
        {"remediation_class": "gitops-patch"}) is None


def test_approve_endpoint_refuses_manual_rows_over_http():
    # The live defect was an HTTP POST (PR #135 review) — pin the handler, not
    # only the policy: 409 + reason + action hint, and no status write.
    client, op = _console_client()
    op.kb.get_remediation.return_value = {"id": 7, "status": "needs-human",
                                          "remediation_class": "manual"}
    resp = client.post("/api/remediations/7/approve")
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["action"] == "reclassify"
    assert "Reclassify" in body["error"] and "Resolve" in body["error"]
    assert body["remediation_class"] == "manual"
    op.kb.update_remediation_status.assert_not_called()


def test_approve_endpoint_queues_mechanizable_rows_over_http():
    # Row #42 after its reclassify — the happy path the refusal must not eat.
    client, op = _console_client()
    op.kb.get_remediation.return_value = {"id": 7, "status": "needs-human",
                                          "remediation_class": "gitops-patch"}
    resp = client.post("/api/remediations/7/approve")
    assert resp.status_code == 200
    op.kb.update_remediation_status.assert_called_once_with(7, "queued")


def test_approve_endpoint_404_when_row_missing():
    client, op = _console_client()
    op.kb.get_remediation.return_value = None
    resp = client.post("/api/remediations/7/approve")
    assert resp.status_code == 404
    op.kb.update_remediation_status.assert_not_called()


# ---- CFOP-70: the frontier-model mutation judge -------------------------------
#
# The incident these guard: raspberrypi4 went NotReady, and the local classifier
# returned gitops-patch / low / 1.0 three times for "remove the nodeSelector
# pinning immich-kiosk to raspberrypi4". The pin is deliberate — that node drives
# the physical TV — and an opus executor faithfully implemented the wrong
# instruction into PRs #99, #100 and #101. Nothing had asked whether the change
# should be made at all.

# The live shape of that classification, kept verbatim as the fixture.
_IMMICH_KIOSK_DETAILS = {
    "remediation_class": "gitops-patch", "risk": "low", "confidence": 1.0,
    "recommendation": ("Remove the nodeSelector kubernetes.io/hostname: raspberrypi4 "
                       "from the immich-kiosk deployment so it can schedule elsewhere"),
    "host": "raspberrypi4", "repo": "aachtenberg/homelab-infra",
    "trigger": "KubeDeploymentReplicasMismatch", "report": "immich-kiosk has 0/1 ready",
}


def _judge_op(judge_return=None, judge_raises=None):
    """Op wired for _maybe_queue_remediation with a controllable judge."""
    op = _no_node_incident(_wire_flags(MagicMock()))
    op.config = {"remediation": {"queue_feed": True}}
    op.kb.queue_remediation.return_value = 77
    op._count_enqueued = MagicMock()
    if judge_raises is not None:
        op._judge_mutation_remediation = MagicMock(side_effect=judge_raises)
    else:
        op._judge_mutation_remediation = MagicMock(return_value=judge_return)
    return op


def test_confident_mutation_does_not_enqueue_auto_eligible_without_the_judge():
    # The core of CFOP-70: gemma4's self-reported 1.0 is no longer sufficient on
    # its own. A downgrade verdict must strip the confidence that is the only
    # field capable of clearing the auto gate, so the row lands needs-human.
    op = _judge_op({"verdict": "downgrade", "model": "claude-opus-4-8",
                    "reason": "the nodeSelector looks deliberate"})
    assert CFOperator._maybe_queue_remediation(op, 2266, dict(_IMMICH_KIOSK_DETAILS)) == 77
    op._judge_mutation_remediation.assert_called_once()
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["confidence"] is None
    # class and risk are NOT rewritten — the classifier's honest read survives
    assert kwargs["remediation_class"] == "gitops-patch" and kwargs["risk"] == "low"
    nc, nr = normalize_remediation_fields(kwargs["remediation_class"], kwargs["risk"])
    assert remediation_is_auto_eligible(nc, nr, kwargs["confidence"]) is False
    assert "deliberate" in kwargs["payload"]["judge_reason"]


@pytest.mark.parametrize("judge_result,judge_exc", [
    # unavailable / unparseable both surface as an explicit downgrade verdict
    ({"verdict": "downgrade", "model": "claude-opus-4-8",
      "reason": "judge unavailable (no ANTHROPIC_API_KEY)"}, None),
    ({"verdict": "downgrade", "model": "claude-opus-4-8",
      "reason": "judge verdict unparseable"}, None),
])
def test_judge_failure_modes_park_and_never_auto_queue(judge_result, judge_exc):
    op = _judge_op(judge_result, judge_exc)
    CFOperator._maybe_queue_remediation(op, 1, dict(_IMMICH_KIOSK_DETAILS))
    assert op.kb.queue_remediation.call_args.kwargs["confidence"] is None


def test_judge_raising_parks_the_row_instead_of_escaping():
    # _judge_mutation_remediation catches its own transport errors, so a raise
    # here means a bug in the gate. A broken gate must not become an open gate —
    # and it must not escape either: the needs_action caller does not wrap this
    # call, so an exception would abort the enqueue and lose the row entirely.
    op = _judge_op(judge_raises=RuntimeError("boom"))
    assert CFOperator._maybe_queue_remediation(op, 1, dict(_IMMICH_KIOSK_DETAILS)) == 77
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["confidence"] is None  # parked, not executed
    assert "boom" in kwargs["payload"]["judge_reason"]


def test_judge_confirm_enqueues_at_full_confidence():
    # The mutation-check the issue asks for: force confirm and the immich-kiosk
    # case sails through exactly as it did before the gate. Without this the
    # fail-closed assertions above would pass for a gate that blocks everything.
    op = _judge_op({"verdict": "confirm", "model": "claude-opus-4-8", "reason": "fine"})
    assert CFOperator._maybe_queue_remediation(op, 2266, dict(_IMMICH_KIOSK_DETAILS)) == 77
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["confidence"] == 1.0
    nc, nr = normalize_remediation_fields(kwargs["remediation_class"], kwargs["risk"])
    assert remediation_is_auto_eligible(nc, nr, kwargs["confidence"]) is True
    assert "judge_reason" not in kwargs["payload"]


def test_judge_reject_records_the_row_then_closes_it():
    # 'reject' is not a silent drop: the queue is the single ledger, so the row
    # exists and carries why it was refused. Terminal status also releases the
    # dedupe key, so a genuine recurrence is judged afresh.
    op = _judge_op({"verdict": "reject", "model": "claude-opus-4-8",
                    "reason": "the pin is deliberate; removing it moves the kiosk off the TV"})
    assert CFOperator._maybe_queue_remediation(op, 2266, dict(_IMMICH_KIOSK_DETAILS)) == 77
    op.kb.queue_remediation.assert_called_once()
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args[0] == 77 and args[1] == "rejected"
    assert "deliberate" in kwargs["last_error"]


@pytest.mark.parametrize("rclass,risk,conf", [
    ("manual", "high", None),       # human-only work
    ("investigate", "low", 0.95),   # not a mutation at all
    ("gitops-patch", "high", 0.95),  # mutation, but not auto-eligible
    ("gitops-patch", "low", 0.5),   # mutation, but under the confidence bar
    ("node-action", "low", 1.0),    # never auto-eligible whatever the confidence
])
def test_non_auto_eligible_rows_skip_the_judge_entirely(rclass, risk, conf):
    # No cost regression: the judge is a frontier-model call, and a row that
    # cannot auto-execute has no unattended-mutation risk to review. It parks
    # at needs-human on the existing gate, as it always did.
    op = _judge_op({"verdict": "confirm", "model": "claude-opus-4-8", "reason": ""})
    details = {"remediation_class": rclass, "risk": risk, "confidence": conf,
               "recommendation": "do a thing", "host": "rpi4"}
    CFOperator._maybe_queue_remediation(op, 1, details)
    op._judge_mutation_remediation.assert_not_called()
    op.kb.queue_remediation.assert_called_once()


def _judging_op(complete=None, providers=("anthropic",), judge_model=None):
    """Op wired to run the real judge ladder over a controllable completion."""
    op = MagicMock()
    op._parse_judge_verdict = CFOperator._parse_judge_verdict
    op._JUDGE_SYSTEM_PROMPT = CFOperator._JUDGE_SYSTEM_PROMPT
    op._judge_providers = lambda: list(providers)
    op._judge_model = judge_model or (lambda b: agent_mod._JUDGE_MODEL_FLOOR[b])
    if complete is not None:
        op._complete_judge = complete
    return op


def test_judge_is_pinned_to_the_model_floor_despite_a_downgraded_executor():
    """Mirrors the node-action floor test: no config key can lower the judge."""
    op = _judging_op(MagicMock(return_value='{"verdict": "confirm", "reason": "ok"}'))
    op._executor_config.return_value = {"llm": {"model": "claude-haiku-4-5-20251001"}}
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "confirm"
    assert out["backend"] == "anthropic"
    assert out["model"] == agent_mod._ANTHROPIC_DEFAULT_EXEC_MODEL == "claude-opus-4-8"
    # (system_prompt, user_msg, backend, model)
    assert op._complete_judge.call_args[0][3] == "claude-opus-4-8"


def test_no_fast_tier_model_sits_in_the_judge_seat():
    # The peers must all be their vendor's frontier tier. A fast/mini/flash
    # model here would do routine judging the first time the peers above it were
    # unreachable — most of the way back to the bug CFOP-70 exists to fix, since
    # the whole premise is that a cheap model's confident wrong answer is what
    # opened three bad PRs.
    # Tokens, not substrings: 'mini' is inside 'gemini', so a substring check
    # fails a perfectly good Pro model.
    fast_tiers = {"flash", "mini", "haiku", "lite", "turbo", "instant", "small"}
    for backend, model in agent_mod._JUDGE_MODEL_FLOOR.items():
        tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
        clash = tokens & fast_tiers
        assert not clash, f"{backend} judge model {model} is a fast tier ({clash})"


def test_every_judge_backend_has_a_pinned_frontier_model():
    # A backend the operator can select but that has no pinned model would
    # KeyError in the ladder; an empty one would send a model-less request.
    assert set(agent_mod._JUDGE_MODEL_FLOOR) == {
        "deepseek", "anthropic", "xai", "gemini"}
    assert set(agent_mod._JUDGE_DEFAULT_ORDER) == set(agent_mod._JUDGE_MODEL_FLOOR)
    for backend, model in agent_mod._JUDGE_MODEL_FLOOR.items():
        assert model and isinstance(model, str), backend
    # anthropic reuses the one floor constant rather than repeating the string
    assert (agent_mod._JUDGE_MODEL_FLOOR["anthropic"]
            is agent_mod._ANTHROPIC_DEFAULT_EXEC_MODEL)


# Ids a vendor has stopped serving. The cfassist config_test.go pattern (#202):
# a denylist catches the recurrence without freezing the floor at one id in
# CI. Add to it when a live call says a name is gone — 'gemini-3.1-pro' sat in
# the floor for a week and 404'd the first time both peers above it were down
# (CFOP-107).
_RETIRED_JUDGE_MODEL_IDS = {"gemini-3.1-pro"}


def test_no_retired_model_id_sits_in_the_judge_floor():
    for backend, model in agent_mod._JUDGE_MODEL_FLOOR.items():
        assert model not in _RETIRED_JUDGE_MODEL_IDS, \
            f"{backend} judge model {model} is no longer served"


def test_judge_fails_over_to_the_next_peer_when_a_vendor_is_unreachable():
    # Availability failover, not answer-shopping: anthropic is down, so xai
    # rules instead. Before this, one missing key parked every remediation.
    calls = []

    def complete(system, user, backend, model):
        calls.append((backend, model))
        if backend == "anthropic":
            raise RuntimeError("529 overloaded")
        return '{"verdict": "reject", "reason": "the pin is deliberate"}'

    op = _judging_op(complete, providers=("anthropic", "xai"))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "reject"
    assert out["backend"] == "xai" and out["model"] == "grok-4.5"
    assert calls == [("anthropic", "claude-opus-4-8"), ("xai", "grok-4.5")]


def test_judge_does_not_shop_providers_for_a_parseable_answer():
    # A model that WAS reached and answered badly is a substantive failure, not
    # an availability one. Cycling vendors until one returns a parseable verdict
    # is shopping for a permissive answer — and 'confirm' is the only verdict
    # that unblocks the row. Park on the spot instead.
    calls = []

    def complete(system, user, backend, model):
        calls.append(backend)
        return "I reckon go for it"

    op = _judging_op(complete, providers=("anthropic", "xai", "gemini"))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert out["backend"] == "anthropic"          # never advanced past the first
    assert calls == ["anthropic", "anthropic"]    # the one-shot and its nudge only


def test_judge_with_no_keyed_provider_parks():
    op = _judging_op(MagicMock(), providers=())
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert "no frontier judge available" in out["reason"]
    op._complete_judge.assert_not_called()


def test_judge_parks_when_every_peer_is_unreachable():
    op = _judging_op(MagicMock(side_effect=RuntimeError("network down")),
                     providers=("anthropic", "xai", "gemini"))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert op._complete_judge.call_count == 3     # every peer tried
    assert "network down" in out["reason"]


def _providers_op(configured, keys):
    op = MagicMock()
    op.config = {"remediation": {"judge": {"providers": configured}}} if configured is not None \
        else {"remediation": {}}
    op._judge_api_key = lambda b: keys.get(b, "")
    return op


def test_judge_providers_defaults_to_the_full_peer_order():
    op = _providers_op(None, {"deepseek": "k", "anthropic": "k",
                              "xai": "k", "gemini": "k"})
    assert CFOperator._judge_providers(op) == list(agent_mod._JUDGE_DEFAULT_ORDER)


def test_judge_providers_skips_backends_with_no_key():
    op = _providers_op(None, {"xai": "k"})
    assert CFOperator._judge_providers(op) == ["xai"]


def test_judge_providers_honours_configured_order_and_drops_typos():
    # A typo must not be treated as a new frontier tier, and must not silently
    # produce a judge-less gate either — it is dropped, the rest still run.
    #
    # The typo is given a KEY on purpose: without one it would be dropped by
    # the no-key check and this test would pass even with the whitelist gone,
    # guarding nothing. Letting an unknown name through would KeyError on
    # _JUDGE_MODEL_FLOOR[backend] in the ladder.
    op = _providers_op(["gemini", "gemni", "anthropic"],
                       {"anthropic": "k", "xai": "k", "gemini": "k", "gemni": "k"})
    assert CFOperator._judge_providers(op) == ["gemini", "anthropic"]


def test_judge_providers_never_emits_a_backend_without_a_pinned_model():
    # The ladder indexes _JUDGE_MODEL_FLOOR[backend] directly, so anything this
    # returns must be a key of it or the gate raises instead of judging.
    op = _providers_op(["anthropic", "openai", "", None, "XAI"],
                       {b: "k" for b in ("anthropic", "xai", "gemini", "openai")})
    for backend in CFOperator._judge_providers(op):
        assert backend in agent_mod._JUDGE_MODEL_FLOOR
    # case-folded, so a capitalised entry still resolves rather than being lost
    assert CFOperator._judge_providers(op) == ["anthropic", "xai"]


def test_judge_providers_accepts_a_single_string():
    op = _providers_op("xai", {"anthropic": "k", "xai": "k"})
    assert CFOperator._judge_providers(op) == ["xai"]


def _post_returning(status, text, payload=None):
    """A requests.post double: HTTP status + raw body, raising like requests does."""
    import requests
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.json.return_value = payload or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status} Client Error for url: https://vendor.example/v1", response=resp)
    else:
        resp.raise_for_status.return_value = None
    return MagicMock(return_value=resp)


def _keyed_op():
    op = MagicMock()
    op._judge_api_key = lambda backend: "k"
    return op


def test_anthropic_judge_request_carries_no_sampling_parameter():
    # Opus 4.7 and later reject temperature/top_p/top_k with a 400 — guard the
    # class, not just the one that parked every auto-eligible row (CFOP-117).
    post = _post_returning(200, "", {"content": [
        {"type": "text", "text": '{"verdict": "confirm", "reason": "ok"}'}]})
    with patch("requests.post", post):
        out = CFOperator._complete_judge(_keyed_op(), "sys", "user",
                                         "anthropic", "claude-opus-4-8")
    assert "confirm" in out
    body = post.call_args.kwargs["json"]
    assert body["model"] == "claude-opus-4-8"
    assert not {"temperature", "top_p", "top_k"} & set(body), sorted(body)


def test_judge_http_failure_names_the_vendors_message_not_just_the_status():
    # raise_for_status carries the status line only; the body is where a vendor
    # says WHICH thing was wrong, and the exception text is what the parked
    # row's reason shows (CFOP-117: "404" for a three-vendor failure).
    import requests
    post = _post_returning(400, '{"type":"error","error":{"type":"invalid_request_error",'
                                '"message":"`temperature` is deprecated for this model."}}')
    with patch("requests.post", post), pytest.raises(requests.HTTPError) as exc:
        CFOperator._complete_judge(_keyed_op(), "sys", "user",
                                   "anthropic", "claude-opus-4-8")
    assert "400" in str(exc.value)
    assert "`temperature` is deprecated" in str(exc.value)


def test_openai_compat_judge_failure_names_the_vendors_message_too():
    # Same on the compat wire — the xAI 403 was "credits exhausted", which the
    # status line alone reads as an auth problem. That wire still pins
    # temperature 0: xAI and Gemini accept it, and a veto should not be
    # sampled differently each run where the vendor lets us say so.
    import requests
    post = _post_returning(403, '{"code":"permission-denied","error":"Your team has '
                                'used all available credits or reached its limit"}')
    with patch("requests.post", post), pytest.raises(requests.HTTPError) as exc:
        CFOperator._complete_judge(_keyed_op(), "sys", "user", "xai", "grok-4.5")
    assert "403" in str(exc.value) and "credits" in str(exc.value)
    assert post.call_args.kwargs["json"]["temperature"] == 0


def test_parked_row_reason_names_the_vendors_message_when_every_peer_refuses():
    # CFOP-117's done-when is the ROW, not the exception: with every peer
    # refusing the request, the reason the operator reads must carry what the
    # last vendor said, not only its status line. Runs the real ladder over
    # the real _complete_judge so the composition is pinned, not assumed.
    op = _judging_op(providers=("anthropic", "gemini"))
    op._judge_api_key = lambda backend: "k"
    op._complete_judge = lambda *args: CFOperator._complete_judge(op, *args)
    op._notready_nodes.return_value = []
    anthropic_400 = _post_returning(
        400, '{"type":"error","error":{"message":"`temperature` is deprecated for this model."}}'
    ).return_value
    gemini_404 = _post_returning(
        404, '{"error":{"code":404,"message":"models/gemini-3.1-pro is not found for API version v1beta"}}'
    ).return_value
    post = MagicMock(side_effect=[anthropic_400, gemini_404])
    with patch("requests.post", post):
        out = CFOperator._judge_mutation_remediation(
            op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert post.call_count == 2                       # both peers tried, both refused
    assert out["verdict"] == "downgrade" and out["backend"] is None
    assert "404" in out["reason"]
    assert "gemini-3.1-pro is not found" in out["reason"], out["reason"]


def test_deepseek_leads_the_default_judge_order():
    # CFOP-121: cheapest capable rung first, frontier peers as failover. The
    # order is what prod runs — there is no remediation.judge block in the
    # deployed config, so the default IS the live setting.
    assert agent_mod._JUDGE_DEFAULT_ORDER[0] == "deepseek"
    assert agent_mod._JUDGE_MODEL_FLOOR["deepseek"] == "deepseek-v4-pro"
    # and it must be reachable as an OpenAI-compat backend, not a fourth branch
    assert "deepseek" in agent_mod.OPENAI_COMPAT_PROVIDERS


def test_runtime_fast_tier_tokens_cover_the_guarded_set():
    # The CI floor guard keeps its own literal set on purpose (an independent
    # check); this asserts the RUNTIME refusal knows at least as much, so a
    # config value cannot slip past a guard CI would have caught.
    assert {"flash", "mini", "haiku", "lite", "turbo", "instant", "small"} \
        <= agent_mod._JUDGE_FAST_TIER_TOKENS


def _model_op(setting="", config_model=""):
    op = MagicMock()
    op.kb.get_setting.return_value = setting
    op.config = {"remediation": {"judge": {"models": {"deepseek": config_model}}}} \
        if config_model else {"remediation": {}}
    op._judge_model_setting = lambda b: CFOperator._judge_model_setting(op, b)
    op._judge_model_config = lambda b: CFOperator._judge_model_config(op, b)
    return op


def test_configured_judge_model_wins_over_the_floor():
    # CFOP-121 relaxes CFOP-70's outright pin: the two rungs that broke this
    # week were pinned to ids no operator could change from config.
    assert CFOperator._judge_model(_model_op(config_model="deepseek-v9-pro"),
                                   "deepseek") == "deepseek-v9-pro"
    # the console setting outranks config, mirroring _triage_model
    assert CFOperator._judge_model(_model_op(setting="deepseek-v8-pro",
                                             config_model="deepseek-v9-pro"),
                                   "deepseek") == "deepseek-v8-pro"


def test_unset_judge_model_falls_back_to_the_floor():
    assert CFOperator._judge_model(_model_op(), "deepseek") == "deepseek-v4-pro"
    # a DB read failure must not break the gate
    op = _model_op()
    op.kb.get_setting.side_effect = RuntimeError("db down")
    assert CFOperator._judge_model(op, "deepseek") == "deepseek-v4-pro"


@pytest.mark.parametrize("demoted", ["deepseek-v4-flash", "claude-haiku-4-5",
                                     "gemini-3.6-flash", "grok-4-mini"])
def test_a_fast_tier_model_cannot_be_configured_into_the_judge_seat(demoted):
    # The knob may move the judge sideways or up, never down into the tier
    # whose confident wrong answers the gate exists to catch (CFOP-70).
    assert CFOperator._judge_model(_model_op(config_model=demoted),
                                   "deepseek") == "deepseek-v4-pro"
    assert CFOperator._judge_model(_model_op(setting=demoted),
                                   "deepseek") == "deepseek-v4-pro"


def test_judge_skips_the_peer_that_wrote_the_recommendation():
    # deepseek-v4-pro is both a judge rung and the backend the console selects
    # for investigations, so without this the reporter would rule on its own
    # recommendation — the seat CFOP-70 rejected. The match is on the VENDOR,
    # not the exact id: see the configured-id case below.
    calls = []

    def complete(system, user, backend, model):
        calls.append((backend, model))
        return '{"verdict": "confirm", "reason": "independent look"}'

    op = _judging_op(complete, providers=("deepseek", "anthropic"))
    details = dict(_IMMICH_KIOSK_DETAILS, provider="deepseek/deepseek-v4-pro")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert calls == [("anthropic", "claude-opus-4-8")]   # deepseek never asked
    assert out["backend"] == "anthropic" and out["verdict"] == "confirm"


def test_a_differently_reported_row_still_uses_the_first_peer():
    # The skip is keyed on the exact reporting model, not on the backend being
    # deepseek — an ollama-reported row is judged by deepseek as normal.
    calls = []

    def complete(system, user, backend, model):
        calls.append((backend, model))
        return '{"verdict": "confirm", "reason": "ok"}'

    op = _judging_op(complete, providers=("deepseek", "anthropic"))
    details = dict(_IMMICH_KIOSK_DETAILS, provider="ollama/gemma4:26b")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert calls == [("deepseek", "deepseek-v4-pro")]
    assert out["backend"] == "deepseek"



@pytest.mark.parametrize("reporter", [
    "deepseek/deepseek-v4-pro",          # the ordinary backend/model tag
    "DeepSeek/DeepSeek-V4-Pro",          # case is not significant
    "deepseek",                          # _llm_provider_tag's backend-only form
    "deepseek/models/deepseek-v4-pro",   # a vendor listing id with an infix
])
def test_self_review_is_matched_on_the_vendor_whatever_shape_the_tag_takes(reporter):
    assert agent_mod._judge_is_self_review(reporter, "deepseek", "deepseek-v4-pro")
    assert not agent_mod._judge_is_self_review(reporter, "anthropic", "claude-opus-4-8")


def test_self_review_catches_a_bare_model_tag_with_no_backend():
    # _llm_provider_tag returns the model alone when no backend was recorded;
    # the head split cannot see a vendor there, so compare against our model.
    assert agent_mod._judge_is_self_review("deepseek-v4-pro", "deepseek", "deepseek-v4-pro")
    assert not agent_mod._judge_is_self_review("gemma4:26b", "deepseek", "deepseek-v4-pro")


def test_self_review_ignores_an_absent_reporter():
    assert not agent_mod._judge_is_self_review("", "deepseek", "deepseek-v4-pro")
    assert not agent_mod._judge_is_self_review(None, "deepseek", "deepseek-v4-pro")


def test_repointing_the_backend_does_not_re_open_the_self_review_seat():
    # The regression the CFOP-121 knob creates: with an exact backend/model
    # match, setting judge_model_deepseek to any other DeepSeek id makes the
    # skip false and DeepSeek judges DeepSeek-reported work again — switching
    # off a safety guard as a side effect of a cost knob.
    calls = []

    def complete(system, user, backend, model):
        calls.append((backend, model))
        return '{"verdict": "confirm", "reason": "independent look"}'

    op = _judging_op(complete, providers=("deepseek", "anthropic"),
                     judge_model=lambda b: ("deepseek-v4-pro-0711" if b == "deepseek"
                                            else agent_mod._JUDGE_MODEL_FLOOR[b]))
    details = dict(_IMMICH_KIOSK_DETAILS, provider="deepseek/deepseek-v4-pro")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert calls == [("anthropic", "claude-opus-4-8")]
    assert out["backend"] == "anthropic"


def test_a_deepseek_reported_row_parks_when_every_other_peer_is_down():
    # The consequence of failing closed on self-review, stated as a test: the
    # availability win from leading with DeepSeek does NOT extend to rows
    # DeepSeek reported. In the 400/403/404 state that motivated CFOP-121,
    # those park — which is the safe direction, but it is not "no rows park".
    def complete(system, user, backend, model):
        raise RuntimeError(f"{backend} unreachable")

    op = _judging_op(complete, providers=("deepseek", "anthropic", "xai", "gemini"))
    details = dict(_IMMICH_KIOSK_DETAILS, provider="deepseek/deepseek-v4-pro")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade" and out["backend"] is None
    # the surviving reason is the transport failure, not the skip
    assert "unreachable" in out["reason"]


def test_the_fast_tier_denylist_covers_the_names_vendors_actually_ship():
    # Not a frontier allowlist, and the docstring says so — this pins the
    # markers that ARE refused, including 'nano', which the first cut missed.
    for demoted in ("gpt-5-nano", "deepseek-v4-flash", "claude-haiku-4-5",
                    "gemini-3.6-flash", "grok-4-mini", "veo-3.1-fast-generate",
                    "some-model-lite", "o4-micro", "llama-tiny"):
        assert agent_mod._is_fast_tier_model(demoted), demoted
    # and does not fire on frontier ids that merely contain the letters
    for kept in ("gemini-3.1-pro-preview", "claude-opus-4-8", "deepseek-v4-pro",
                 "grok-4.5"):
        assert not agent_mod._is_fast_tier_model(kept), kept


def test_judge_parks_when_the_only_peer_is_the_reporter():
    # Fail closed, and say WHY — an operator reading this row should not have
    # to guess that the judge was skipped rather than unreachable.
    op = _judging_op(MagicMock(), providers=("deepseek",))
    details = dict(_IMMICH_KIOSK_DETAILS, provider="deepseek/deepseek-v4-pro")
    out = CFOperator._judge_mutation_remediation(op, details, "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade" and out["backend"] is None
    op._complete_judge.assert_not_called()
    assert "wrote this recommendation" in out["reason"]
    assert "deepseek/deepseek-v4-pro" in out["reason"]


def test_judge_unavailable_downgrades_rather_than_confirming():
    op = _judging_op(MagicMock(side_effect=RuntimeError("ANTHROPIC_API_KEY required")))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert "unavailable" in out["reason"]


def test_judge_unparseable_nudges_once_then_downgrades():
    op = _judging_op(MagicMock(return_value="I think you should probably do it"))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "downgrade"
    assert op._complete_judge.call_count == 2  # one-shot + one nudge, no third rung
    assert "unparseable" in out["reason"]


def test_judge_nudge_rescues_a_fenced_verdict():
    op = _judging_op(MagicMock(side_effect=[
        "sure thing",
        '```json\n{"verdict": "reject", "reason": "the pin is deliberate"}\n```',
    ]))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "reject" and "deliberate" in out["reason"]
    # the malformed reply is quoted back on the retry (the PR #76 pattern)
    assert "sure thing" in op._complete_judge.call_args[0][1]


@pytest.mark.parametrize("raw", [
    "",
    "no json here",
    '{"reason": "missing the verdict"}',
    '{"verdict": "approve", "reason": "not one of the three"}',  # never coerced to confirm
    '{"verdict": "yes"}',
    '[{"verdict": "confirm"}]',  # array, not object
])
def test_parse_judge_verdict_rejects_anything_it_does_not_recognise(raw):
    assert CFOperator._parse_judge_verdict(raw) is None


def test_parse_judge_verdict_accepts_the_three_verdicts():
    for v in ("confirm", "downgrade", "reject"):
        out = CFOperator._parse_judge_verdict('{"verdict": "%s", "reason": "r"}' % v)
        assert out == {"verdict": v, "reason": "r"}
    # case and surrounding prose are tolerated, same as the classifier parser
    out = CFOperator._parse_judge_verdict('Verdict below.\n{"verdict": "CONFIRM"}')
    assert out["verdict"] == "confirm"


def test_judge_prompt_never_frames_confirm_as_the_default():
    # A gate whose prompt nudges toward approval is decoration. The prompt must
    # tell the model that uncertainty means downgrade, and must name the class
    # of mistake that actually happened (removing a deliberate constraint).
    prompt = CFOperator._JUDGE_SYSTEM_PROMPT
    assert "When you are unsure, downgrade" in prompt
    assert "DELIBERATE" in prompt
    for verdict in ("confirm", "downgrade", "reject"):
        assert verdict in prompt


def test_classifier_identity_is_recorded_on_the_payload():
    # This incident needed a code read to work out which model decided to open
    # the PR. The row now says so.
    op = _judge_op({"verdict": "confirm", "model": "claude-opus-4-8", "reason": "ok"})
    details = dict(_IMMICH_KIOSK_DETAILS,
                   classifier_backend="ollama", classifier_model="gemma4:26b")
    CFOperator._maybe_queue_remediation(op, 2266, details)
    decided = op.kb.queue_remediation.call_args.kwargs["payload"]["decided_by"]
    assert decided["classifier"] == {"backend": "ollama", "model": "gemma4:26b"}
    assert decided["judge"]["model"] == "claude-opus-4-8"
    assert decided["judge"]["verdict"] == "confirm"


def test_classifier_stamps_the_model_that_answered():
    op = _classifier_op()
    op._chat_with_tools_with_fallback = MagicMock(return_value={
        "response": _GOOD_CLASSIFICATION, "backend": "ollama", "model": "gemma4:26b"})
    hints = CFOperator._classify_needs_action_recommendation(op, "trig", "fix it", {})
    assert hints["classifier_backend"] == "ollama"
    assert hints["classifier_model"] == "gemma4:26b"


# ---- CFOP-71: one dead node is one incident ----------------------------------
#
# raspberrypi4 went NotReady at 06:18:23Z and by 11:06 that single fact had
# become ~15 investigations, 12 needs-human rows and 4 open PRs. The 12 rows are
# all manual / target.host raspberrypi4, all some rewording of "physically check
# the power and network cable". They are one fact, twelve times.

# The alerts one dead node actually fired, each with its own legitimate
# fingerprint and therefore its own dedupe key.
_RPI4_SYMPTOMS = [
    ("NodeNotReady", "alert-aaa1"),
    ("KubeDeploymentReplicasMismatch", "alert-bbb2"),
    ("KubePodNotReady", "alert-ccc3"),
    ("KubePodNotReady", "alert-ddd4"),
    ("ArgoCDMetricsAbsent", "alert-eee5"),
    ("PromtailMemoryHigh", "alert-fff6"),
]


def _nodes_op(nodes):
    """Op whose cluster reports these nodes, wired with the real collapse."""
    op = _wire_flags(MagicMock())
    op.config = {"remediation": {"queue_feed": True}}
    op.tools.k8s_tools.get_nodes.return_value = {"success": True, "nodes": nodes}
    op._normalize_host = CFOperator._normalize_host
    op._notready_nodes = lambda: CFOperator._notready_nodes(op)
    op._node_incident_dedupe_key = CFOperator._node_incident_dedupe_key
    op._collapse_key_for_node_incident = (
        lambda d: CFOperator._collapse_key_for_node_incident(op, d))
    op._record_absorbed_symptom = (
        lambda key, d: CFOperator._record_absorbed_symptom(op, key, d))
    op._count_enqueued = MagicMock()
    _confirming_judge(op)
    return op


_RPI4_DOWN = [{"name": "raspberrypi4", "ready": "False"},
              {"name": "raspberrypi2", "ready": "True"}]


def test_symptoms_of_a_notready_node_collapse_onto_one_row():
    op = _nodes_op(_RPI4_DOWN)
    op.kb.queue_remediation.return_value = 51
    # First symptom: no incident row yet, so one is created.
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    first = CFOperator._maybe_queue_remediation(op, 2253, {
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "recommendation": "physically check the power and network cable",
        "host": "raspberrypi4", "trigger": _RPI4_SYMPTOMS[0][0],
        "dedupe_key": _RPI4_SYMPTOMS[0][1]})
    assert first == 51
    assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "node-down-raspberrypi4"
    # and the key lands in BOTH places, or the KB filter never matches
    assert op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe_key"] == "node-down-raspberrypi4"

    # Every later symptom folds onto it — no second row, whatever its own key.
    op.kb.find_open_remediation_by_dedupe_key.return_value = {"id": 51}
    op.kb.queue_remediation.reset_mock()
    for trigger, key in _RPI4_SYMPTOMS[1:]:
        rid = CFOperator._maybe_queue_remediation(op, 2260, {
            "remediation_class": "manual", "risk": "high", "confidence": None,
            "recommendation": "check the pod", "host": "raspberrypi4",
            "trigger": trigger, "dedupe_key": key})
        assert rid == 51, f"{trigger} opened its own row"
    op.kb.queue_remediation.assert_not_called()
    # the incident row records what it absorbed
    absorbed = [c.args[1] for c in op.kb.record_remediation_absorbed.call_args_list]
    assert absorbed == [t for t, _ in _RPI4_SYMPTOMS[1:]]


def test_symptoms_on_a_ready_node_still_enqueue_independently():
    # The collapse must be conditional, not a blanket host merge: a disk-full
    # and a crash-looping pod on a healthy host are two problems, not one.
    op = _nodes_op(_RPI4_DOWN)
    op.kb.queue_remediation.return_value = 90
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    for key in ("alert-disk", "alert-crashloop"):
        CFOperator._maybe_queue_remediation(op, 1, {
            "remediation_class": "manual", "risk": "high", "confidence": None,
            "recommendation": "look at it", "host": "raspberrypi2",  # Ready
            "trigger": "t", "dedupe_key": key})
    keys = [c.kwargs["dedupe_key"] for c in op.kb.queue_remediation.call_args_list]
    assert keys == ["alert-disk", "alert-crashloop"]  # untouched
    op.kb.record_remediation_absorbed.assert_not_called()


def test_collapse_fails_open_when_node_readiness_is_unreadable():
    # A dedupe optimisation must never be the reason a real problem goes
    # unrecorded. Every failure to read nodes leaves the key alone.
    for nodes_result in ({"success": False},
                         Exception("kubectl timed out")):
        op = _nodes_op([])
        if isinstance(nodes_result, Exception):
            op.tools.k8s_tools.get_nodes.side_effect = nodes_result
        else:
            op.tools.k8s_tools.get_nodes.return_value = nodes_result
        op.kb.queue_remediation.return_value = 5
        CFOperator._maybe_queue_remediation(op, 1, {
            "remediation_class": "manual", "risk": "high", "confidence": None,
            "recommendation": "r", "host": "raspberrypi4", "dedupe_key": "alert-x"})
        assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "alert-x"


def test_collapse_needs_no_k8s_tools_to_be_safe():
    op = _nodes_op([])
    op.tools.k8s_tools = None
    op.kb.queue_remediation.return_value = 5
    CFOperator._maybe_queue_remediation(op, 1, {
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "recommendation": "r", "host": "raspberrypi4", "dedupe_key": "alert-x"})
    assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "alert-x"


@pytest.mark.parametrize("raw,expected", [
    ("raspberrypi4", "raspberrypi4"),
    ("raspberrypi4:9100", "raspberrypi4"),        # the Prometheus instance label
    ("RaspberryPi4", "raspberrypi4"),
    ("http://raspberrypi4:9100/metrics", "raspberrypi4"),
    ("raspberrypi4.local.", "raspberrypi4.local"),
    ("", ""),
    (None, ""),
])
def test_normalize_host_strips_what_labels_carry(raw, expected):
    assert CFOperator._normalize_host(raw) == expected


def test_collapse_matches_an_fqdn_against_a_short_node_name():
    op = _nodes_op(_RPI4_DOWN)
    key = CFOperator._collapse_key_for_node_incident(op, {"host": "raspberrypi4.lan:9100"})
    assert key == "node-down-raspberrypi4"


def test_collapse_ignores_a_host_that_is_not_a_node():
    # A pod or service name must not be mistaken for a node.
    op = _nodes_op(_RPI4_DOWN)
    assert CFOperator._collapse_key_for_node_incident(op, {"host": "immich-kiosk"}) is None
    assert CFOperator._collapse_key_for_node_incident(op, {"host": ""}) is None


# ---- CFOP-126: the collapse reads the hosts INSIDE a target id --------------
# A FIX target id is prose as often as a hostname ("raspberrypi4, raspberrypi5",
# "raspberrypi4 (192.168.0.116)"), and the row's host is that id verbatim; #82
# and #83 sat in needs-human for 4.5 h after #81 auto-resolved because neither
# string matched a node. The fold finds the NotReady node inside the string by
# asking the INVENTORY, never by knowing what a hostname looks like -- an
# install names its nodes "aweoriujwoedf", and the guard has to hold there.

_PI5_DOWN = [{"name": "raspberrypi4", "ready": "True"},
             {"name": "raspberrypi5", "ready": "Unknown"}]


@pytest.mark.parametrize("target_id,nodes,expected", [
    # the two live shapes, #82 and #83
    ("raspberrypi4, raspberrypi5", _PI5_DOWN, "node-down-raspberrypi5"),
    ("raspberrypi4 (192.168.0.116)", _RPI4_DOWN, "node-down-raspberrypi4"),
    # an arbitrary name: nothing about its shape is recognised, only the inventory;
    # the word "node" and the Ready sibling are passed over
    ("node aweoriujwoedf and sandbox-01",
     [{"name": "aweoriujwoedf", "ready": "False"}, {"name": "sandbox-01", "ready": "True"}],
     "node-down-aweoriujwoedf"),
    # FQDN both ways: short target vs FQDN-registered node ...
    ("aweoriujwoedf (10.0.0.7)",
     [{"name": "aweoriujwoedf.example.com", "ready": "False"}],
     "node-down-aweoriujwoedf.example.com"),
    # ... and FQDN target vs short-registered node
    ("aweoriujwoedf.example.com, other.example.com",
     [{"name": "aweoriujwoedf", "ready": "False"}, {"name": "other", "ready": "True"}],
     "node-down-aweoriujwoedf"),
    # the IP IS the registered name: the inventory decides, a regex would have stripped it
    ("kiosk (192.168.0.116)",
     [{"name": "192.168.0.116", "ready": "False"}],
     "node-down-192.168.0.116"),
])
def test_collapse_finds_the_notready_node_inside_a_target_id(target_id, nodes, expected):
    op = _nodes_op(nodes)
    assert CFOperator._collapse_key_for_node_incident(op, {"host": target_id}) == expected


def test_collapse_keys_on_the_registered_name_so_recovery_can_find_it():
    # The recovery sweep builds keys from registered node names
    # (resolve_node_incidents_for_ready_hosts); a key built from the alert's
    # spelling would never be matched by it, and the row would never close.
    op = _nodes_op([{"name": "aweoriujwoedf.example.com", "ready": "False"}])
    key = CFOperator._collapse_key_for_node_incident(op, {"host": "aweoriujwoedf"})
    assert key == "node-down-aweoriujwoedf.example.com"


def test_a_list_of_ready_hosts_does_not_fold():
    op = _nodes_op([{"name": "raspberrypi4", "ready": "True"},
                    {"name": "raspberrypi5", "ready": "True"}])
    assert CFOperator._collapse_key_for_node_incident(
        op, {"host": "raspberrypi4, raspberrypi5"}) is None


def test_unknown_tokens_are_ignored_not_guessed():
    # A pod name, an IP nobody registered, the word "node": none is a host
    # just because it sits in the host field.
    op = _nodes_op(_RPI4_DOWN)
    assert CFOperator._collapse_key_for_node_incident(
        op, {"host": "immich-kiosk, 10.0.0.9 and node"}) is None


def test_two_fqdns_in_different_domains_are_not_the_same_host():
    op = _nodes_op([{"name": "db.staging.example", "ready": "False"}])
    assert CFOperator._collapse_key_for_node_incident(
        op, {"host": "db.prod.example"}) is None


def test_a_bare_token_shared_by_two_fqdns_is_ambiguous_and_does_not_fold():
    # PR #218 review: "db" names both db.prod.example and db.staging.example.
    # Folding onto whichever sorts first would be a guess, and nothing else on
    # this path guesses. Ambiguous is unknown: no fold, whatever the order.
    for nodes in ([{"name": "db.prod.example", "ready": "False"},
                   {"name": "db.staging.example", "ready": "False"}],
                  [{"name": "db.staging.example", "ready": "False"},
                   {"name": "db.prod.example", "ready": "False"}]):
        op = _nodes_op(nodes)
        assert CFOperator._collapse_key_for_node_incident(op, {"host": "db"}) is None
    # ... but an exact registered name beside them is not ambiguous
    op = _nodes_op([{"name": "db", "ready": "False"},
                    {"name": "db.prod.example", "ready": "False"},
                    {"name": "db.staging.example", "ready": "False"}])
    assert CFOperator._collapse_key_for_node_incident(op, {"host": "db"}) == "node-down-db"
    # ... and a unique first label still is a match
    op = _nodes_op([{"name": "db.prod.example", "ready": "False"},
                    {"name": "web.prod.example", "ready": "False"}])
    assert CFOperator._collapse_key_for_node_incident(
        op, {"host": "db"}) == "node-down-db.prod.example"


def test_collapse_writes_the_registered_name_onto_details():
    # PR #218 review: the collapse is the one place that knows which registered
    # name matched, so it carries that forward instead of the enqueue path
    # recovering it by slicing the key it just built.
    op = _nodes_op(_PI5_DOWN)
    details = {"host": "raspberrypi4, raspberrypi5"}
    assert CFOperator._collapse_key_for_node_incident(op, details) == "node-down-raspberrypi5"
    assert details["host"] == "raspberrypi5"
    # no match -> nothing written
    details = {"host": "immich-kiosk"}
    assert CFOperator._collapse_key_for_node_incident(op, details) is None
    assert details["host"] == "immich-kiosk"


@pytest.mark.parametrize("raw,expected", [
    ("raspberrypi4, raspberrypi5", ["raspberrypi4", "raspberrypi5"]),
    ("raspberrypi4 (192.168.0.116)", ["raspberrypi4", "192.168.0.116"]),
    ("a and b", ["a", "b"]),
    ("a & b / c", ["a", "b", "c"]),
    ("a/b", ["a", "b"]),
    ("sandbox-01 and andrew-node", ["sandbox-01", "andrew-node"]),   # never a substring split
    ("http://node.lan:9100/metrics", ["node.lan"]),
    ("Node.LAN:9100", ["node.lan"]),
    ("raspberrypi4, raspberrypi4", ["raspberrypi4"]),
    ("", []),
    (None, []),
])
def test_host_candidates_is_lexical_only(raw, expected):
    assert agent_mod._host_candidates(raw) == expected


def test_incident_row_created_from_a_multi_host_target_names_the_node():
    # #82 enqueued with host_id "raspberrypi4, raspberrypi5" -- not a host
    # anything can ssh to, and not what the row is about. When the node tier
    # creates the incident row it names the node; the FIX targets stay as the
    # model wrote them.
    op = _nodes_op(_PI5_DOWN)
    op.kb.queue_remediation.return_value = 82
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    targets = [{"kind": "host", "id": "raspberrypi4, raspberrypi5"}]
    rid = CFOperator._maybe_queue_remediation(op, 2308, {
        "remediation_class": "node-action", "risk": "low", "confidence": None,
        "recommendation": "physically inspect raspberrypi4 and raspberrypi5",
        "host": "raspberrypi4, raspberrypi5", "targets": targets,
        "trigger": "NodeUnreachable", "dedupe_key": "tgt-4ee3c1d7214b03bd"})
    assert rid == 82
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["dedupe_key"] == "node-down-raspberrypi5"
    assert kwargs["host_id"] == "raspberrypi5"
    assert kwargs["payload"]["target"] == {"host": "raspberrypi5"}
    assert kwargs["payload"]["targets"] == targets   # as the model wrote them


def test_a_row_the_node_tier_does_not_claim_keeps_its_host_as_written():
    # No inventory match -> nothing more honest to show than what was named.
    op = _nodes_op([{"name": "raspberrypi4", "ready": "True"},
                    {"name": "raspberrypi5", "ready": "True"}])
    op.kb.queue_remediation.return_value = 7
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    CFOperator._maybe_queue_remediation(op, 1, {
        "remediation_class": "node-action", "risk": "low", "confidence": None,
        "recommendation": "check both", "host": "raspberrypi4, raspberrypi5",
        "trigger": "t", "dedupe_key": "tgt-x"})
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["dedupe_key"] == "tgt-x"
    assert kwargs["host_id"] == "raspberrypi4, raspberrypi5"


# ---- CFOP-71: the open-PR cap reaches the executor path ----------------------


def test_drainer_refuses_to_spawn_past_the_open_pr_cap():
    # Four cfop/ PRs were open against a configured max_open_prs of 3, because
    # the cap lived only in the agent-side proposer.
    op = _fake_op(drain=True)
    op.config["remediation"]["max_open_prs"] = 3
    op._open_remediation_pr_count = lambda: 3
    assert CFOperator._drain_remediation_queue(op) == 0
    # the row is LEFT QUEUED, not claimed and not failed — no executor Job and
    # no frontier-model diff is burned producing a PR that cannot open yet
    op.kb.claim_next_remediation.assert_not_called()
    op._spawn_remediation_executor.assert_not_called()
    op.kb.fail_remediation.assert_not_called()


def test_drainer_spawns_while_under_the_cap():
    # Mutation-check for the cap: under it, draining is unchanged.
    op = _fake_op(drain=True, max_per_tick=1)
    op.config["remediation"]["max_open_prs"] = 3
    op._open_remediation_pr_count = lambda: 2
    op.kb.claim_next_remediation.side_effect = [{"id": 1, "remediation_class": "gitops-patch"}]
    assert CFOperator._drain_remediation_queue(op) == 1
    op._spawn_remediation_executor.assert_called_once()


def test_drainer_cap_stops_mid_tick_when_it_is_reached():
    op = _fake_op(drain=True, max_per_tick=5)
    op.config["remediation"]["max_open_prs"] = 2
    counts = iter([0, 1, 2, 2, 2])
    op._open_remediation_pr_count = lambda: next(counts)
    op.kb.claim_next_remediation.side_effect = [
        {"id": i, "remediation_class": "gitops-patch"} for i in range(1, 6)]
    assert CFOperator._drain_remediation_queue(op) == 2  # stopped at the cap


def test_open_pr_count_includes_committed_work_not_just_open_prs():
    # The executor spawn is ASYNC: a row does not reach 'pr-open' until its Job
    # posts back, which can be many ticks later. Counting only opened PRs let a
    # single tick claim against a stale count and blow through the cap.
    op = MagicMock()
    op.kb.count_remediations_by_status.return_value = {
        "claimed": 1, "executing": 1, "pr-open": 2, "verifying": 1,
        "needs-human": 12, "resolved": 40,  # must NOT count toward the PR cap
    }
    assert CFOperator._open_remediation_pr_count(op) == 5
    # one grouped query, not a row pull per status — the old call also capped at
    # 50 rows, so a cap above 50 would have silently stopped counting
    op.kb.list_remediations_by_status.assert_not_called()
    # a transient DB error must not stall the whole queue: this is a volume
    # guard, not a safety gate
    op.kb.count_remediations_by_status.side_effect = RuntimeError("db down")
    assert CFOperator._open_remediation_pr_count(op) == 0


def test_a_spawn_burst_cannot_exceed_the_cap_within_one_tick():
    # 1 PR already open, cap 3, 3 auto-eligible rows queued, max_drain_per_tick
    # 3. Before the fix this spawned all three against a stale count of 1 and
    # produced 4 open PRs against a cap of 3 — the exact defect the cap exists
    # to prevent, reached by a spawn burst instead of duplicate symptoms.
    state = {"pr-open": 1, "verifying": 0, "claimed": 0, "executing": 0}
    op = _fake_op(drain=True, max_per_tick=3)
    op.config["remediation"]["max_open_prs"] = 3
    op.kb.count_remediations_by_status = lambda: dict(state)
    op._open_remediation_pr_count = lambda: CFOperator._open_remediation_pr_count(op)
    rows = [{"id": i, "remediation_class": "gitops-patch"} for i in (1, 2, 3)]

    def claim(job, exclude_ids=None):
        # the spawn is async — the row goes to 'claimed', not to 'pr-open'
        if rows:
            r = rows.pop(0)
            state["claimed"] += 1
            return r
        return None

    op.kb.claim_next_remediation = claim
    spawned = CFOperator._drain_remediation_queue(op)
    assert spawned == 2, "the third spawn would have made 4 PRs against a cap of 3"
    assert state["pr-open"] + state["claimed"] == 3


# ---- CFOP-71: recovery closes its own paperwork ------------------------------


def test_recovered_node_auto_resolves_its_incident_row():
    op = _nodes_op([{"name": "raspberrypi4", "ready": "True"},
                    {"name": "raspberrypi2", "ready": "True"}])
    op.kb.resolve_node_incidents_for_ready_hosts.return_value = [51]
    assert CFOperator._resolve_recovered_node_incidents(op) == 1
    hosts = op.kb.resolve_node_incidents_for_ready_hosts.call_args[0][0]
    assert sorted(hosts) == ["raspberrypi2", "raspberrypi4"]


def test_recovery_sweep_passes_only_ready_hosts():
    op = _nodes_op(_RPI4_DOWN)
    op.kb.resolve_node_incidents_for_ready_hosts.return_value = []
    CFOperator._resolve_recovered_node_incidents(op)
    hosts = op.kb.resolve_node_incidents_for_ready_hosts.call_args[0][0]
    assert hosts == ["raspberrypi2"]  # the still-down node keeps its row


def test_recovery_sweep_is_silent_when_the_cluster_is_unreadable():
    op = _nodes_op([])
    op.tools.k8s_tools.get_nodes.side_effect = RuntimeError("no kubectl")
    assert CFOperator._resolve_recovered_node_incidents(op) == 0
    op.kb.resolve_node_incidents_for_ready_hosts.assert_not_called()



# ---- follow-ups from the PR #164 review --------------------------------------


def test_judge_returning_a_non_verdict_parks_instead_of_escaping():
    # The gate must not trust its own return shape either: a non-dict would
    # AttributeError out of _maybe_queue_remediation, and the needs_action
    # caller does not wrap it, so the row would be lost rather than parked.
    for bogus in (None, "confirm", ["confirm"], 42):
        op = _judge_op(bogus)
        assert CFOperator._maybe_queue_remediation(op, 1, dict(_IMMICH_KIOSK_DETAILS)) == 77
        kwargs = op.kb.queue_remediation.call_args.kwargs
        assert kwargs["confidence"] is None, f"{bogus!r} slipped through the gate"
        assert "no verdict" in kwargs["payload"]["judge_reason"]


def test_folded_symptom_links_to_the_incident_even_when_absorb_fails_open():
    # _record_absorbed_symptom fails open (None) on a transient KB error or a
    # race with a sibling symptom's row creation. The enqueue is then refused
    # by the KB's own dedupe, and the caller's fallback lookup must use the
    # COLLAPSED key — with the pre-collapse per-alert key it reports "none
    # proposed" while the incident row sits there under the node key.
    op = _nodes_op(_RPI4_DOWN)
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = lambda i, d: CFOperator._maybe_queue_remediation(op, i, d)
    op._open_remediation_for_key = lambda k: CFOperator._open_remediation_for_key(op, k)
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": "raspberrypi4", "repo": None})
    op._record_absorbed_symptom = lambda k, d: None      # fails open
    op.kb.queue_remediation.return_value = None          # KB dedupe refuses
    op.kb.find_open_remediation_by_dedupe_key = (
        lambda key: {"id": 51} if key == "node-down-raspberrypi4" else None)

    rid = CFOperator._queue_needs_action_remediation(
        op, 2266, "KubePodNotReady", {"fingerprint": "abc123"},
        "check the pod on raspberrypi4", "report", provider="p")
    assert rid == 51, "the investigation lost its link to the incident row"


def test_only_paperwork_rows_auto_resolve_when_a_node_recovers():
    from knowledge_base import node_incident_is_auto_resolvable as ok
    # pure paperwork: a notification nobody has acted on
    assert ok("queued") is True
    assert ok("needs-human") is True
    # an executor holds a lease — the reaper owns these
    assert ok("claimed") is False
    assert ok("executing") is False
    # a real PR is open against the row; _reconcile_remediation_prs only tracks
    # 'pr-open', so resolving here would orphan the PR from its reconciler
    assert ok("pr-open") is False
    assert ok("verifying") is False
    # an automated attempt genuinely failed: the node coming back does not mean
    # the fix worked, and 'resolved' is the field dashboards key off
    assert ok("failed") is False
    # whitelist, not blacklist — an unknown status is never auto-resolved
    assert ok("some-future-status") is False



def test_node_recovery_sweep_is_not_gated_on_the_reaper_flag():
    # It used to hang off _reap_remediations, which put the CFOP-71 recovery
    # half behind queue_reap — a flag documented as independently enableable and
    # defaulting to false. With reap off, a recovered node kept its stale
    # needs-human row forever, which is most of what the collapse was for.
    op = _nodes_op([{"name": "raspberrypi4", "ready": "True"}])
    op.config = {"remediation": {"queue_feed": True, "queue_reap": False}}
    _wire_flags(op)
    op.kb.resolve_node_incidents_for_ready_hosts.return_value = [51]
    assert CFOperator._resolve_recovered_node_incidents(op) == 1


def test_node_recovery_sweep_is_gated_on_the_feed_that_creates_the_rows():
    op = _nodes_op([{"name": "raspberrypi4", "ready": "True"}])
    op.config = {"remediation": {"queue_feed": False}}
    _wire_flags(op)
    assert CFOperator._resolve_recovered_node_incidents(op) == 0
    op.kb.resolve_node_incidents_for_ready_hosts.assert_not_called()


def test_reaper_no_longer_drives_the_recovery_sweep():
    # The two are independent ticks now; reaping must not be the thing that
    # closes recovered incidents.
    op = _fake_op(reap=True)
    op.kb.requeue_stale_remediations.return_value = 0
    CFOperator._reap_remediations(op)
    op._resolve_recovered_node_incidents.assert_not_called()


def test_gemini_is_not_in_the_investigation_fallback_chain():
    # Registering gemini so the judge can reach it must not silently reroute
    # INVESTIGATION escalation: adding it to fallback_order would put it between
    # xAI and Anthropic, so a paid escalation that used to reach Opus would
    # reach whatever the config's gemini entry names instead.
    import inspect
    src = inspect.getsource(CFOperator._get_provider_chain)
    line = next(l for l in src.splitlines() if "fallback_order = [" in l)
    assert "gemini" not in line, line
    # DeepSeek, same decision (CFOP-110): registered and selectable, never an
    # automatic escalate.
    assert "deepseek" not in line, line
    assert "anthropic" in line and "xai" in line
    # ...but they ARE registered providers, so the judge and the admin picker
    # can still select them by name
    assert "gemini" in agent_mod.OPENAI_COMPAT_PROVIDERS
    assert "deepseek" in agent_mod.OPENAI_COMPAT_PROVIDERS


def test_judge_requests_leave_headroom_for_thinking_models():
    # The verdict is two short fields; the budget is almost all headroom. A
    # model that reasons before answering spends it before emitting JSON, and a
    # truncated verdict parks the row — at 1024 that looks like a stuck gate.
    assert agent_mod._JUDGE_MAX_TOKENS >= 4096


def test_judge_is_told_which_nodes_are_down():
    # The prompt tells it to refuse "the node is down, so its pods being
    # unschedulable is the node's problem" — it needs the Ready state to apply
    # that, rather than inferring a dead node from a report that may only say
    # "immich-kiosk has 0/1 ready".
    op = _judging_op(MagicMock(return_value='{"verdict": "reject", "reason": "node is down"}'))
    op._notready_nodes = lambda: {"raspberrypi4"}
    CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    user_msg = op._complete_judge.call_args[0][1]
    assert "raspberrypi4" in user_msg
    assert "NOT Ready" in user_msg


def test_judge_is_told_when_the_cluster_is_healthy():
    op = _judging_op(MagicMock(return_value='{"verdict": "confirm", "reason": "ok"}'))
    op._notready_nodes = lambda: set()
    CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert "All nodes are Ready" in op._complete_judge.call_args[0][1]


def test_judge_still_rules_when_node_state_is_unreadable():
    # Losing the snapshot must not park a row the judge could have ruled on —
    # the gate already fails closed for real failures, and this is context.
    op = _judging_op(MagicMock(return_value='{"verdict": "confirm", "reason": "ok"}'))
    op._notready_nodes = MagicMock(side_effect=RuntimeError("no kubectl"))
    out = CFOperator._judge_mutation_remediation(
        op, dict(_IMMICH_KIOSK_DETAILS), "gitops-patch", "low", 1.0)
    assert out["verdict"] == "confirm"


def test_sweep_human_only_recs_also_fold_onto_a_node_incident():
    # The human-only sweep path enqueues directly rather than through
    # _maybe_queue_remediation, so it would otherwise miss the collapse: a
    # morning-summary echo of an already-down node would open its own row.
    op = _feed_op()
    op.tools.k8s_tools.get_nodes.return_value = {
        "success": True, "nodes": [{"name": "raspberrypi4", "ready": "False"}]}
    op._notready_nodes = lambda: CFOperator._notready_nodes(op)
    op._collapse_key_for_node_incident = (
        lambda d: CFOperator._collapse_key_for_node_incident(op, d))
    op._normalize_host = CFOperator._normalize_host
    op._node_incident_dedupe_key = CFOperator._node_incident_dedupe_key
    op._record_absorbed_symptom = lambda k, d: 51   # incident row already open

    reports = [{"findings": [{
        "severity": "critical", "finding": "raspberrypi4 is unreachable",
        "recommendation": "physically check the power and network cable",
        "resource_name": "raspberrypi4", "namespace": "kube-system",
    }]}]
    op._feed_remediations_from_sweeps(reports)
    op.kb.queue_remediation.assert_not_called()   # folded, not a 13th row


# ---- CFOP-108: a checklist is not a fix -------------------------------------
#
# Live row #80: investigation #2305 ended with "verify connectivity … check
# port 10250 … check iptables" and a FIX whose target.kind was host. CFOP-80
# classifies that to node-action by kind alone, and node-action is never
# auto-eligible, so a list of commands the agent could have run itself was
# parked for a human — who, asked in chat, watched the same model run them in
# one turn. These guard the branch that sends such a rec back for one more
# pass instead, and every way that branch must NOT fire.

_CHECKLIST_REC = ("Verify network connectivity and firewall rules between the control "
                  "plane and raspberrypi2 on port 10250, and ensure k3s-agent is "
                  "correctly configured to expose the kubelet API.")


def _host_fix(steps):
    """The #80 FIX shape, with the given steps."""
    return {"targets": [{"kind": "host", "id": "raspberrypi2", "repo": ""}],
            "observed": [{"source": "k8s_get_pod_logs",
                          "value": "proxy error while dialing 192.168.0.146:10250, code 502"}],
            "steps": steps,
            "verify": {"command": "kubectl get nodes raspberrypi2", "expect": "Ready"},
            "rejected": [], "risk": "low"}


_CHECKLIST_STEPS = ["Check if port 10250 is open on raspberrypi2",
                    "Verify k3s-agent configuration for kubelet API exposure",
                    "Check for any intermediate firewall or iptables rules blocking 10250"]


def _checklist_op():
    op = _na_op()
    op.enqueue_investigation = MagicMock(return_value={"status": "queued"})
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "node-action", "risk": "low", "confidence": 0.7,
        "host": "raspberrypi2", "repo": None})
    return op


_ALERT = {"fingerprint": "abc123", "labels": {"instance": "raspberrypi2"}}


def test_a_checklist_recommendation_gets_a_followup_pass_not_a_row():
    op = _checklist_op()
    rid = op._queue_needs_action_remediation(2305, "Cloudflared tunnel instability", _ALERT,
                                             _CHECKLIST_REC, "report", provider="ollama/gemma4:26b")
    assert rid is None
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_called_once()
    alert = op.enqueue_investigation.call_args.args[0]
    assert alert["source"] == "investigation-followup"
    assert alert["followup_of"] == 2305
    assert alert["host"] == "raspberrypi2"
    # The same key the row would have carried, so the eventual enqueue (or
    # the next morning's feed) sees this as the same problem.
    assert alert["dedupe_key"] == "alert-abc123"
    assert "verify" not in alert["summary"].lower().split("do not recommend further")[1]
    assert "Run those checks now" in alert["summary"]


def test_the_live_80_fix_shape_is_a_checklist_too():
    """A valid FIX skips the classifier (CFOP-80); a valid FIX made of checks
    must still not become a row. The classifier-not-called assertion is also
    what proves the fixture validated, i.e. that the FIX branch was the one
    exercised."""
    op = _checklist_op()
    rid = op._queue_needs_action_remediation(
        2305, "Cloudflared tunnel instability", _ALERT, _CHECKLIST_REC, "report",
        provider="p", structured_fix=_host_fix(_CHECKLIST_STEPS))
    assert rid is None
    op._classify_needs_action_recommendation.assert_not_called()
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_called_once()


def test_a_mutating_step_keeps_the_row():
    """"Check the config, then restart k3s-agent" is a conditional fix. The
    executor has something to run; the row stays."""
    op = _checklist_op()
    steps = _CHECKLIST_STEPS + ["Restart k3s-agent on raspberrypi2"]
    rid = op._queue_needs_action_remediation(
        2305, "t", _ALERT, _CHECKLIST_REC, "report", provider="p",
        structured_fix=_host_fix(steps))
    assert rid == 9
    op.enqueue_investigation.assert_not_called()


def test_a_mutating_recommendation_keeps_the_row_even_with_a_check_verb():
    op = _checklist_op()
    rid = op._queue_needs_action_remediation(
        2305, "t", _ALERT, "Verify the memory limit and raise it to 512Mi", "report",
        provider="p")
    assert rid == 9
    op.enqueue_investigation.assert_not_called()


def test_the_followup_itself_cannot_spawn_another():
    """The loop guard. Mutation check: drop the followup_of test in
    _dispatch_checklist_followup and this goes red."""
    op = _checklist_op()
    second_pass = dict(_ALERT, followup_of=2305, source="investigation-followup")
    rid = op._queue_needs_action_remediation(2306, "Follow-up to investigation #2305",
                                             second_pass, _CHECKLIST_REC, "report",
                                             provider="p")
    assert rid == 9, "a second checklist must land with a human, not loop"
    op.enqueue_investigation.assert_not_called()


def test_human_only_work_still_parks_even_when_worded_as_a_check():
    op = _checklist_op()
    rid = op._queue_needs_action_remediation(
        2305, "t", _ALERT, "Physically check the SD card and reseat it", "report",
        provider="p")
    assert rid == 9
    op.enqueue_investigation.assert_not_called()


def test_a_full_investigation_queue_degrades_to_the_row():
    """queue.Full on dispatch must not lose the finding: today's row is the
    fallback, not silence."""
    import queue as _queue
    op = _checklist_op()
    op.enqueue_investigation.side_effect = _queue.Full()
    rid = op._queue_needs_action_remediation(2305, "t", _ALERT, _CHECKLIST_REC, "report",
                                             provider="p")
    assert rid == 9
    op.kb.queue_remediation.assert_called_once()


def test_an_open_row_under_the_key_means_the_evidence_is_already_parked():
    """Same guard as the sweep branch: re-gathering evidence a human is
    already holding IS the loop. The investigation links to that row."""
    op = _checklist_op()
    op.kb.queue_remediation.return_value = None
    op.kb.find_open_remediation_by_dedupe_key.return_value = {"id": 44, "status": "needs-human"}
    rid = op._queue_needs_action_remediation(2305, "t", _ALERT, _CHECKLIST_REC, "report",
                                             provider="p")
    assert rid == 44
    op.enqueue_investigation.assert_not_called()


def test_a_notready_hosts_checklist_folds_onto_the_node_incident():
    """CFOP-71 wins: one dead node fires many "check X on <host>" recs and
    they are one incident, not one follow-up investigation each."""
    op = _nodes_op(_RPI4_DOWN)
    op.enqueue_investigation = MagicMock()
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = lambda i, d: CFOperator._maybe_queue_remediation(op, i, d)
    op._open_remediation_for_key = lambda k: CFOperator._open_remediation_for_key(op, k)
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "node-action", "risk": "low", "confidence": 0.7,
        "host": "raspberrypi4", "repo": None})
    op._record_absorbed_symptom = lambda k, d: 51
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
    rid = CFOperator._queue_needs_action_remediation(
        op, 2266, "KubePodNotReady", {"fingerprint": "abc123"},
        "Check the pod on raspberrypi4 and verify the kubelet is up", "report", provider="p")
    assert rid == 51
    op.enqueue_investigation.assert_not_called()


def test_followups_are_keyed_so_a_rerun_cannot_spawn_a_second():
    """enqueue_investigation dedupes on idempotency_key only, and this path
    never makes a row for the open-row guard to find — so the key is the
    only thing between a retried needs_action and two follow-ups."""
    op = _checklist_op()
    op._queue_needs_action_remediation(2305, "t", _ALERT, _CHECKLIST_REC, "report", provider="p")
    alert = op.enqueue_investigation.call_args.args[0]
    assert alert["idempotency_key"] == "investigate-followup:alert-abc123"


def test_a_deduped_followup_is_still_not_a_row():
    """The first follow-up owns the problem; the repeat neither re-dispatches
    nor falls back to parking a row."""
    op = _checklist_op()
    op.enqueue_investigation.return_value = {"status": "deduped"}
    rid = op._queue_needs_action_remediation(2305, "t", _ALERT, _CHECKLIST_REC, "report",
                                             provider="p")
    assert rid is None
    op.kb.queue_remediation.assert_not_called()


def test_the_parent_investigation_learns_where_its_work_went():
    """Returning None reads as "nothing proposed" downstream; the parent must
    be able to say a child is doing the work. run_investigation pops this
    into findings['followup_dispatched']."""
    op = _checklist_op()
    op._queue_needs_action_remediation(2305, "t", _ALERT, _CHECKLIST_REC, "report", provider="p")
    assert op._checklist_followups[2305] == "investigate-followup:alert-abc123"


# ---- CFOP-116: a PR the investigation opened itself ---------------------------
#
# Row #85: the model called github_create_pr, opened homelab-infra #114, and
# recommended merging it. The row landed needs-human with pr_url null (the URL
# only in prose), no link anywhere, and an Approve button that would have had
# the executor open a second PR. The tool result is the evidence; it has to
# reach the row.

_OPENED = "https://github.com/aachtenberg/homelab-infra/pull/114"


def _pinning_fix():
    """Single-target gitops-manifest at low risk: the one auto-eligible shape."""
    return {
        "targets": [{"kind": "gitops-manifest",
                     "id": "k3s/base/monitoring/loki-statefulset.yml",
                     "repo": "aachtenberg/homelab-infra"}],
        "observed": [{"source": "github_get_file_contents k3s/base/monitoring/loki-statefulset.yml",
                      "value": "image: grafana/loki:latest"}],
        "steps": ["merge PR #114"],
        "verify": {"command": "kubectl -n monitoring get sts loki -o yaml", "expect": "loki:3.7.7"},
        "rejected": [{"alternative": "patch the live object", "why_not": "ArgoCD reverts it"}],
        "risk": "low",
    }


def test_dispatch_records_only_a_pr_that_was_actually_opened():
    from agent.agent import _ToolLoopStats
    op = MagicMock()
    stats = _ToolLoopStats()

    def run(name, result, cached=False):
        op._cached_tool_exec.return_value = ("content", result, cached)
        CFOperator._dispatch_tool_call(op, name, {}, stats=stats, tool_cache={},
                                       max_result_chars=1000, iteration=0, max_iterations=5)

    run("github_create_pr", {"success": True, "html_url": _OPENED})
    run("github_create_pr", {"success": False, "error": "422 branch already exists"})
    run("github_get_pr", {"success": True, "html_url": "https://github.com/o/r/pull/8"})
    run("github_create_pr", "Error: tool crashed")
    run("github_create_pr", {"success": True, "html_url": "https://github.com/o/r/pull/9"},
        cached=True)
    assert stats.opened_prs == [_OPENED]
    assert stats.result("done")["opened_prs"] == [_OPENED]
    assert stats.tool_calls == 5  # counting is unchanged


def test_feed_enqueues_a_pr_the_model_opened_as_pr_open_and_never_auto():
    op = _na_op()
    rid = op._queue_needs_action_remediation(
        2312, "Pin loki and grafana image tags", {"fingerprint": "f1"},
        f"Merge PR #114 ({_OPENED}) so ArgoCD syncs the pinned tags", "report",
        provider="deepseek/deepseek-v4-pro", structured_fix=_pinning_fix(),
        opened_prs=[_OPENED])
    assert rid == 9
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["pr_url"] == _OPENED
    # The FIX is the one auto-eligible shape (0.8). With the PR already open
    # there is nothing left to auto-execute, so no confidence and no judge —
    # the judge's question is about unattended execution, and a human merge
    # is not that.
    assert kw["confidence"] is None
    op._judge_mutation_remediation.assert_not_called()
    op._count_enqueued.assert_called_once_with("investigation", "gitops-patch", "low", None)
    # Without a PR the same FIX still walks the gate as before.
    op2 = _na_op()
    op2._queue_needs_action_remediation(
        2312, "t", {"fingerprint": "f1"}, "pin the image tags", "report",
        provider="p", structured_fix=_pinning_fix())
    kw2 = op2.kb.queue_remediation.call_args.kwargs
    assert kw2["pr_url"] is None and kw2["confidence"] == 0.8


def test_feed_stamps_the_opened_pr_on_the_row_it_deduped_onto():
    # The first run proposed and parked; this run opened the PR. The open row
    # is the one on the operator's screen, so it gets the link and leaves the
    # executor's reach.
    op = _na_op()
    op.kb.queue_remediation.return_value = None  # the KB saw the key already
    # Shaped like row #85: the column is null and the serializer offers the
    # prose URL as named_pr_url. That is a link, not a tracked PR — the stamp
    # must read the column, or a #85-shaped row could never be promoted.
    op.kb.find_open_remediation_by_dedupe_key.return_value = {
        "id": 40, "status": "needs-human", "pr_url": None, "named_pr_url": _OPENED,
        "payload": {"repo": "aachtenberg/homelab-infra",
                    "recommendation": f"Merge PR #114 ({_OPENED})"}}
    rid = op._queue_needs_action_remediation(
        2313, "t", {"fingerprint": "f1"}, "merge the PR", "report", provider="p",
        structured_fix=_pinning_fix(), opened_prs=[_OPENED])
    assert rid == 40
    op.kb.update_remediation_status.assert_called_once_with(40, "pr-open", pr_url=_OPENED)


@pytest.mark.parametrize("row", [
    {"id": 41, "status": "claimed", "pr_url": None},   # the executor holds it
    {"id": 42, "status": "needs-human", "pr_url": "https://github.com/o/r/pull/1"},  # has one
])
def test_stamping_leaves_rows_with_a_driver_or_a_pr_alone(row):
    op = _na_op()
    op.kb.queue_remediation.return_value = None
    op.kb.find_open_remediation_by_dedupe_key.return_value = row
    assert op._queue_needs_action_remediation(
        2313, "t", {"fingerprint": "f1"}, "merge the PR", "report", provider="p",
        structured_fix=_pinning_fix(), opened_prs=[_OPENED]) == row["id"]
    op.kb.update_remediation_status.assert_not_called()


def test_queue_remediation_inserts_an_opened_pr_as_pr_open():
    from contextlib import contextmanager
    from knowledge_base import KnowledgeBase
    kb = KnowledgeBase.__new__(KnowledgeBase)
    added = []

    class _Session:
        def add(self, item):
            added.append(item)

        def flush(self):
            added[-1].id = 85

    @contextmanager
    def scope():
        yield _Session()

    kb.session_scope = scope
    rid = kb.queue_remediation("gitops-patch", {"recommendation": "merge"},
                               investigation_id=2312, risk="low", confidence=0.8,
                               pr_url=_OPENED)
    assert rid == 85
    assert added[-1].status == "pr-open" and added[-1].pr_url == _OPENED
    # The ordinary path is untouched: same inputs without a PR are auto-eligible.
    kb.queue_remediation("gitops-patch", {"recommendation": "merge"}, risk="low", confidence=0.8)
    assert added[-1].status == "queued" and added[-1].pr_url is None


def test_row_dict_surfaces_a_pr_named_in_the_rows_own_repo():
    import types
    from knowledge_base import remediation_row_dict

    def row(pr_url, payload):
        return types.SimpleNamespace(
            id=85, status="needs-human", remediation_class="gitops-patch", risk="low",
            confidence=None, host_id="default", investigation_id=2312, priority=5,
            attempts=0, pr_url=pr_url, last_error=None, result=None, created_at=None,
            claimed_at=None, completed_at=None, payload=payload)

    rec = f"Merge PR #114 ({_OPENED}) so ArgoCD syncs the pinned image tags."

    def named(pr_url, payload):
        d = remediation_row_dict(row(pr_url, payload))
        return d["pr_url"], d["named_pr_url"]

    # row #85 as it sits in the DB: the PR only in prose. A link, not a
    # tracked PR — pr_url stays the column.
    assert named(None, {"repo": "aachtenberg/homelab-infra", "recommendation": rec}) == (None, _OPENED)
    # GitHub slugs are case-insensitive
    assert named(None, {"repo": "Aachtenberg/Homelab-Infra", "recommendation": rec}) == (None, _OPENED)
    # the short name the model (and older rows) emit, and a repo only on a target
    assert named(None, {"repo": "homelab-infra", "recommendation": rec}) == (None, _OPENED)
    assert named(None, {"targets": [{"kind": "gitops-manifest", "id": "x.yml",
                                     "repo": "aachtenberg/homelab-infra"}],
                        "recommendation": rec}) == (None, _OPENED)
    # a PR in someone else's repo is context the model cited, not the fix
    assert named(None, {"repo": "aachtenberg/homelab-infra",
                        "recommendation": "Upstream fix: https://github.com/grafana/loki/pull/999"}) == (None, None)
    # no repo on the row -> nothing to match against, no guess
    assert named(None, {"recommendation": rec}) == (None, None)
    # once the column is set it is the one link
    later = "https://github.com/aachtenberg/homelab-infra/pull/115"
    assert named(later, {"repo": "aachtenberg/homelab-infra", "recommendation": rec}) == (later, None)


def test_approve_is_refused_while_a_pr_is_open():
    from knowledge_base import remediation_approve_conflict
    reason = remediation_approve_conflict({"remediation_class": "gitops-patch", "pr_url": _OPENED})
    assert reason and _OPENED in reason and "second PR" in reason
    assert remediation_approve_conflict({"remediation_class": "gitops-patch",
                                         "pr_url": None}) is None
    # A PR the recommendation merely names is a link for the operator, not a
    # gate: a row citing an already-merged PR in its own repo stays approvable.
    assert remediation_approve_conflict({"remediation_class": "gitops-patch",
                                         "pr_url": None, "named_pr_url": _OPENED}) is None


def test_approve_endpoint_refuses_rows_with_an_open_pr_over_http():
    client, op = _console_client()
    op.kb.get_remediation.return_value = {"id": 85, "status": "needs-human",
                                          "remediation_class": "gitops-patch",
                                          "pr_url": _OPENED}
    resp = client.post("/api/remediations/85/approve")
    assert resp.status_code == 409
    body = resp.get_json()
    assert _OPENED in body["error"]
    assert body["action"] == "review-pr"
    op.kb.update_remediation_status.assert_not_called()


def test_investigation_pr_url_falls_back_to_a_pr_the_model_opened():
    from knowledge_base import _investigation_remediation_pr_url
    assert _investigation_remediation_pr_url({"opened_prs": [_OPENED]}) == _OPENED
    assert _investigation_remediation_pr_url({"opened_prs": []}) is None


def _folding_op():
    """An enqueue that the CFOP-71 node collapse folds onto incident row #51."""
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": True}}
    op._collapse_key_for_node_incident = lambda d: "node-raspberrypi4"
    op._record_absorbed_symptom = lambda k, d: 51   # incident row already open
    op._stamp_opened_pr = lambda row, url: CFOperator._stamp_opened_pr(op, row, url)
    return op


def test_a_pr_opened_for_a_folded_symptom_lands_on_the_node_incident_row():
    # The symptom's row never exists; the incident row is the one an operator
    # acts on, so it is the one that must carry the PR and refuse the executor.
    op = _folding_op()
    op.kb.get_remediation.return_value = {"id": 51, "status": "needs-human", "pr_url": None}
    details = {"remediation_class": "gitops-patch", "risk": "low", "confidence": 0.8,
               "recommendation": "merge the PR", "host": "raspberrypi4",
               "dedupe_key": "alert-x", "pr_url": _OPENED}
    assert CFOperator._maybe_queue_remediation(op, 7, details) == 51
    op.kb.update_remediation_status.assert_called_once_with(51, "pr-open", pr_url=_OPENED)
    op.kb.queue_remediation.assert_not_called()


def test_a_folded_pr_the_incident_row_cannot_take_is_reported_not_dropped(caplog):
    op = _folding_op()
    op.kb.get_remediation.return_value = {"id": 51, "status": "claimed", "pr_url": None}
    details = {"remediation_class": "gitops-patch", "risk": "low", "confidence": 0.8,
               "recommendation": "merge the PR", "host": "raspberrypi4",
               "dedupe_key": "alert-x", "pr_url": _OPENED}
    import logging
    with caplog.at_level(logging.WARNING, logger="cfoperator"):
        assert CFOperator._maybe_queue_remediation(op, 7, details) == 51
    op.kb.update_remediation_status.assert_not_called()
    assert any(_OPENED in r.getMessage() and "#51" in r.getMessage()
               for r in caplog.records), "an orphaned PR must be logged, not silently dropped"


# ---- CFOP-130: the summary feed's own branch runs the node collapse ---------
#
# The primary recommendations branch of _feed_remediations_from_summary
# enqueues through kb.queue_remediation directly, not through the
# _maybe_queue_remediation choke point, so it has to replay the CFOP-71 fold
# itself. Live rows #93 (investigation, node-down-raspberrypi5) and #94
# (morning summary, summary-verify-raspberrypi5-connectivity) were the same
# dead node under two keys, 7h apart.

_RPI5_DOWN = [{"name": "raspberrypi5", "ready": "False"},
              {"name": "raspberrypi2", "ready": "True"}]
_ALL_READY = [{"name": "raspberrypi5", "ready": "True"},
              {"name": "raspberrypi2", "ready": "True"}]


def _summary_with_host(host):
    """A summary block carrying the human-only rec that produced live row #94."""
    return ('## Summary\n```json\n{"recommendations": [\n'
            '  {"title": "Verify raspberrypi5 connectivity",\n'
            '   "recommendation": "Check physical power supply and network '
            'cabling for raspberrypi5.",\n'
            f'   "host": "{host}", "remediation_class": "manual",\n'
            '   "risk": "low", "confidence": 0.5}\n]}\n```\n')


def _summary_nodes_op(nodes, open_rows=None):
    """A summary-feed op whose cluster reports these nodes.

    _feed_op already wires the real collapse (via _no_node_incident); this only
    gives it a readable inventory plus the absorb helper, so the fold can
    actually fire instead of taking the fail-open path.
    """
    op = _feed_op()
    op.tools.k8s_tools.get_nodes.return_value = {"success": True, "nodes": nodes}
    op._node_incident_dedupe_key = CFOperator._node_incident_dedupe_key
    op._record_absorbed_symptom = (
        lambda key, d: CFOperator._record_absorbed_symptom(op, key, d))
    rows = open_rows or {}
    op.kb.find_open_remediation_by_dedupe_key.side_effect = lambda k: rows.get(k)
    return op


def test_summary_rec_folds_onto_an_open_node_incident_row():
    # #94 must never be created: #93 already covers this dead node.
    op = _summary_nodes_op(_RPI5_DOWN,
                           open_rows={"node-down-raspberrypi5": {"id": 93}})
    n = CFOperator._feed_remediations_from_summary(
        op, _summary_with_host("raspberrypi5"), [])
    assert n == 0
    op.kb.queue_remediation.assert_not_called()
    op.enqueue_investigation.assert_not_called()
    rid, note = op.kb.record_remediation_absorbed.call_args.args
    assert rid == 93 and "raspberrypi5" in note


@pytest.mark.parametrize("host", [
    "raspberrypi5",                       # as the alert labels it
    "raspberrypi5 (192.168.0.216)",       # as gemma4 often writes it (CFOP-126)
    "raspberrypi4, raspberrypi5",         # a two-host target, one of them down
])
def test_summary_rec_opens_the_incident_row_under_the_node_key(host):
    # No incident row yet: this rec becomes it, keyed and hosted on the node,
    # so the investigation feed's later enqueue folds onto THIS row.
    op = _summary_nodes_op(_RPI5_DOWN)
    assert CFOperator._feed_remediations_from_summary(
        op, _summary_with_host(host), []) == 1
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["dedupe_key"] == "node-down-raspberrypi5"
    assert kw["host_id"] == "raspberrypi5"
    assert kw["payload"]["dedupe_key"] == "node-down-raspberrypi5"
    assert kw["payload"]["target"]["host"] == "raspberrypi5"
    op.kb.record_remediation_absorbed.assert_not_called()


def test_summary_rec_for_a_healthy_host_keeps_its_own_key():
    # The collapse is conditional on the node being down — a rec about a Ready
    # host is its own finding and must not be merged into anything.
    op = _summary_nodes_op(_ALL_READY)
    assert CFOperator._feed_remediations_from_summary(
        op, _summary_with_host("raspberrypi5"), []) == 1
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["dedupe_key"] == "summary-verify-raspberrypi5-connectivity"
    assert kw["host_id"] == "raspberrypi5"


def test_summary_fold_fails_open_when_the_incident_row_cannot_be_read():
    # A KB error must degrade to today's behaviour (a second row under the node
    # key), never to a dropped finding.
    op = _summary_nodes_op(_RPI5_DOWN)
    op.kb.find_open_remediation_by_dedupe_key.side_effect = RuntimeError("kb down")
    assert CFOperator._feed_remediations_from_summary(
        op, _summary_with_host("raspberrypi5"), []) == 1
    assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "node-down-raspberrypi5"
