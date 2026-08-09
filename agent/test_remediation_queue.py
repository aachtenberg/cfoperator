#!/usr/bin/env python3
"""Tests for the remediation-queue auto-execute gate.

Pure policy functions, no DB — the gate decides which recommendations may run
unattended, so it gets the same scrutiny as the worker-side classification.
"""

import os
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
from agent.agent import _SUMMARY_CONFIDENCE_CAP  # noqa: E402
import agent.agent as agent_mod  # noqa: E402


def _wire_flags(op):
    """Make op._remediation_flag read op.config (config-only), like the real method
    (a bare MagicMock would return a truthy mock and defeat the gating)."""
    op._remediation_flag = lambda name: bool((op.config.get('remediation') or {}).get(name))
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


def test_maybe_queue_remediation_feeds_when_enabled():
    op = _wire_flags(MagicMock())
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
    kwargs = op.kb.queue_remediation.call_args.kwargs
    assert kwargs["remediation_class"] == "manual"
    assert kwargs["risk"] == "high"
    assert kwargs["dedupe_key"] == "sweep-f1"


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

    operator = MagicMock()
    operator.kb.update_remediation_status.return_value = True
    operator.kb.get_remediation.return_value = {"id": 7, "status": "resolved"}
    operator.kb.reclassify_remediation.return_value = {"id": 7, "status": "queued"}
    operator._REMEDIATION_FLAGS = ("queue_feed", "queue_drain")
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server.sock = None
    server.ws_clients = []
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
