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
from agent.agent import _SUMMARY_CONFIDENCE_CAP, _llm_provider_tag  # noqa: E402
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
    op = _wire_flags(MagicMock())
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


# ---- CFOP-22: reporting LLM + deep PR attempt on the queue ------------------


def test_llm_provider_tag():
    assert _llm_provider_tag({"backend": "xai", "model": "grok"}) == "xai/grok"
    assert _llm_provider_tag({"provider": "ollama", "model": "qwen"}) == "ollama/qwen"
    assert _llm_provider_tag({"backend": "anthropic"}) == "anthropic"
    assert _llm_provider_tag({}) is None
    assert _llm_provider_tag(None) is None


def test_maybe_queue_remediation_stamps_provider():
    op = _wire_flags(MagicMock())
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
    op = _wire_flags(MagicMock())
    op.config = {"remediation": {"queue_feed": feed}}
    op.kb.queue_remediation.return_value = 9
    op._count_enqueued = MagicMock()
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = lambda inv_id, details: CFOperator._maybe_queue_remediation(
        op, inv_id, details)
    op._queue_needs_action_remediation = lambda *a, **k: CFOperator._queue_needs_action_remediation(
        op, *a, **k)
    op._open_remediation_for_key = lambda key: CFOperator._open_remediation_for_key(op, key)
    op.kb.find_open_remediation_by_dedupe_key.return_value = None
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
                     "confidence": None, "host": None, "repo": None}
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
