#!/usr/bin/env python3
"""What the node-action allowlist refused (CFOP-319).

A refused plan reached the row as one line, the first refusal, and the plan it
came from was thrown away, so tuning the allowlist meant guessing what else the
model had asked for. Every refusal is now kept, logged and counted, with a
bounded `binary` label.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import REGISTRY

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402
from node_action_plan import AllowList, plan_refusals, validate_plan  # noqa: E402

ALLOW = AllowList(frozenset({"systemctl", "chmod", "journalctl"}),
                  frozenset({"restart", "is-active"}), 3)


# ---- plan_refusals ------------------------------------------------------------


@pytest.mark.parametrize("command, kind, binary", [
    ("rm -rf /tmp/x", "denied_binary", "rm"),
    ("sudo -n reboot", "denied_binary", "reboot"),
    ("docker restart immich", "not_allowlisted", "docker"),
    ("chmod 600 /x; reboot", "metachar", "chmod"),
    ("systemctl stop nginx", "systemctl_verb", "systemctl"),
    ("sudo systemctl restart x", "sudo_form", "systemctl"),
    ("chmod 'unterminated", "unparseable", "chmod"),
    ("   ", "empty", ""),
])
def test_each_refusal_has_its_kind_and_program(command, kind, binary):
    [r] = plan_refusals([command], ALLOW)
    assert (r["command"], r["kind"], r["binary"]) == (command, kind, binary)
    assert r["reason"]


def test_every_refused_command_is_listed_not_only_the_first():
    plan = ["systemctl restart x", "docker restart y", "rm -rf /z"]
    refusals = plan_refusals(plan, ALLOW)
    assert [r["command"] for r in refusals] == plan[1:]
    # validate_plan's message is still the first refusal's reason.
    assert validate_plan(plan, ALLOW) == (False, refusals[0]["reason"])


def test_plan_level_refusals():
    assert [r["kind"] for r in plan_refusals([], ALLOW)] == ["no_commands"]
    too_many = ["systemctl restart x"] * 4
    assert [r["kind"] for r in plan_refusals(too_many, ALLOW)] == ["too_many"]
    unconfigured = AllowList(frozenset(), frozenset(), 3)
    assert [r["kind"] for r in plan_refusals(["chmod 600 /x"], unconfigured)] == ["no_allowlist"]


def test_a_clean_plan_has_no_refusals():
    assert plan_refusals(["systemctl restart x", "chmod 600 /y"], ALLOW) == []
    assert validate_plan(["systemctl restart x"], ALLOW) == (True, "ok")


# ---- the agent's gate keeps, logs and counts them -----------------------------


def _refused_count(binary, reason):
    return REGISTRY.get_sample_value(
        'cfoperator_node_action_refused_total', {'binary': binary, 'reason': reason}) or 0.0


def _gate_op(reply_plan):
    op = MagicMock()
    op.config = {"remediation": {"executor": {"node_action": {
        "enabled": True, "change_record": {"url": "http://changerecord:8091"},
        "allow_binaries": "systemctl,chmod", "allow_systemctl_verbs": "restart,is-active",
        "max_commands": 6}}}}
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._change_record_url = lambda: "http://changerecord:8091"
    op._node_action_setting = lambda key: ''
    op._node_action_allowlist = lambda: CFOperator._node_action_allowlist(op)
    op._node_action_refusal_label = lambda b: CFOperator._node_action_refusal_label(op, b)
    op._generate_node_action_plan = lambda work: CFOperator._generate_node_action_plan(op, work)
    op._complete_node_action_plan = lambda prompt: json.dumps(reply_plan)
    return op


def test_a_refused_plan_is_kept_on_the_row_logged_and_counted(caplog):
    reply = {"host": "pi2", "commands": ["systemctl restart x", "docker restart y",
                                         "frobnicate", "rm -rf /z", "systemctl stop x"],
             "explanation": "x"}
    op = _gate_op(reply)
    work = {"id": 41, "remediation_class": "node-action", "risk": "med",
            "payload": {"recommendation": "fix", "target": {"host": "pi2"}}, "result": {}}
    before = {k: _refused_count(*k) for k in
              [("other", "not_allowlisted"), ("rm", "denied_binary"),
               ("systemctl", "systemctl_verb")]}

    with caplog.at_level("WARNING"):
        assert CFOperator._prepare_node_action_change_record(op, work) is None

    op.kb.fail_remediation.assert_called_once()
    args, kwargs = op.kb.fail_remediation.call_args
    # The row's message is unchanged: the first refusal, as before.
    assert args == (41, "change record plan: command plan failed safety gate: "
                        "binary not in allowlist: docker")
    stored = kwargs["result"]
    assert stored["proposed_commands"] == reply["commands"]
    assert [b["command"] for b in stored["blocked_commands"]] == reply["commands"][1:]

    # docker and frobnicate are on neither the ceiling nor the deny list: "other".
    assert _refused_count("other", "not_allowlisted") - before[("other", "not_allowlisted")] == 2
    assert _refused_count("rm", "denied_binary") - before[("rm", "denied_binary")] == 1
    # A ceiling program keeps its own name as the label.
    assert _refused_count("systemctl", "systemctl_verb") - before[("systemctl", "systemctl_verb")] == 1
    assert sum("Node-action command refused for remediation #41" in r.getMessage()
               for r in caplog.records) == 4


def test_the_binary_label_names_ceiling_and_deny_programs_only():
    op = _gate_op({})
    assert CFOperator._node_action_refusal_label(op, "chmod") == "chmod"   # on the ceiling
    assert CFOperator._node_action_refusal_label(op, "dd") == "dd"         # on the deny list
    assert CFOperator._node_action_refusal_label(op, "x" * 40) == "other"  # anything else
    assert CFOperator._node_action_refusal_label(op, "") == "other"


def test_fail_remediation_merges_result_into_the_row():
    from knowledge_base import KnowledgeBase
    item = MagicMock(attempts=0, result={"change_record": {"ref": "r"}})
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = item
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.session_scope = MagicMock()
    kb.session_scope.return_value.__enter__.return_value = session
    kb.remediation_policy = lambda: MagicMock(max_attempts=3)
    KnowledgeBase.fail_remediation(kb, 7, "boom", result={"blocked_commands": [1]})
    assert item.result == {"change_record": {"ref": "r"}, "blocked_commands": [1]}
    assert item.last_error == "boom"


# ---- the executor's gate reports every refusal too ----------------------------


def test_the_executor_copy_lists_the_same_refusals():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "_executor_nodeaction_for_refusals", os.path.join(root, "executor", "nodeaction.py"))
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    allow = ex.AllowList(ALLOW.binaries, ALLOW.systemctl_verbs, ALLOW.max_commands)
    plan = ["systemctl restart x", "docker restart y", "rm -rf /z"]
    # The two copies agree, entry for entry (the parity suite covers the rest;
    # executor/test_nodeaction.py drives the executor's completion itself).
    assert ex.plan_refusals(plan, allow) == plan_refusals(plan, ALLOW)


def test_a_later_attempt_clears_an_earlier_refusal():
    """Every result write merges, so a refusal stored on attempt 1 would sit in
    the drawer after attempt 2 passed the gate, or failed some other way
    (claude-review on #318). Only a refusal sets the keys; the rest clear them."""
    cleared = {"blocked_commands": None, "proposed_commands": None}
    work = {"id": 42, "remediation_class": "node-action", "risk": "med",
            "payload": {"recommendation": "fix", "target": {"host": "pi2"}},
            "result": {"blocked_commands": [{"command": "docker restart y"}]}}

    # The plan now passes and the record awaits its merge.
    op = _gate_op({"host": "pi2", "commands": ["systemctl restart x"], "explanation": "x"})
    with patch.object(agent_mod, "change_record_open", return_value={"ref": "r", "url": "u"}), \
         patch.object(agent_mod, "change_record_approval", return_value=None):
        assert CFOperator._prepare_node_action_change_record(op, dict(work)) is None
    stored = op.kb.release_remediation_claim.call_args.kwargs["result"]
    assert {k: stored[k] for k in cleared} == cleared

    # The model answers with no parseable plan: a failure, but not a refusal.
    op = _gate_op({})
    op._complete_node_action_plan = lambda prompt: "no plan here"
    assert CFOperator._prepare_node_action_change_record(op, dict(work)) is None
    assert op.kb.fail_remediation.call_args.kwargs["result"] == cleared


def test_the_row_is_written_before_the_log_and_metric():
    """The log and the metric are decoration; a failure there must not leave
    the row claimed with no attempt burned (claude-review on #318)."""
    op = _gate_op({"host": "pi2", "commands": ["docker restart y"], "explanation": "x"})

    def broken(binary):
        raise RuntimeError("label lookup broke")
    op._node_action_refusal_label = broken
    work = {"id": 43, "remediation_class": "node-action", "risk": "med",
            "payload": {"recommendation": "fix", "target": {"host": "pi2"}}, "result": {}}
    with pytest.raises(RuntimeError):
        CFOperator._prepare_node_action_change_record(op, work)
    op.kb.fail_remediation.assert_called_once()


def test_the_label_lookup_itself_never_raises():
    op = _gate_op({})

    def gone():
        raise RuntimeError("config gone")
    op._executor_config = gone
    assert CFOperator._node_action_refusal_label(op, "docker") == "other"
    assert CFOperator._node_action_refusal_label(op, "rm") == "rm"  # the deny list needs no config


def test_a_huge_refused_plan_is_capped_on_the_row():
    commands = ["docker restart " + "y" * 2000] * 100
    op = _gate_op({"host": "pi2", "commands": commands, "explanation": "x"})
    work = {"id": 44, "remediation_class": "node-action", "risk": "med",
            "payload": {"recommendation": "fix", "target": {"host": "pi2"}}, "result": {}}
    assert CFOperator._prepare_node_action_change_record(op, work) is None
    stored = op.kb.fail_remediation.call_args.kwargs["result"]
    assert len(stored["proposed_commands"]) == 20 and len(stored["blocked_commands"]) == 20
    assert max(len(c) for c in stored["proposed_commands"]) == 500
    assert max(len(b["command"]) for b in stored["blocked_commands"]) <= 500
