"""Console-confirmed host writes, agent side (CFOP-442).

The class of regression guarded here: a confirmed command that runs without
a record, runs twice, waits on a merge, or runs after the admin declined; and
a turn that cannot confirm being handed a confirmer anyway.
"""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402
import host_write  # noqa: E402
from host_write import HostWriteConfirmer, PendingConfirms, wait_for_row  # noqa: E402
from knowledge_base import CONSOLE_CONFIRMED_SOURCE, KnowledgeBase  # noqa: E402


# ---- the question and its answer ---------------------------------------------

def _confirmer(run=None, timeout_s=5.0):
    events = []
    pending = PendingConfirms()
    ran = []

    def run_confirmed(**kw):
        ran.append(kw)
        return run or {"success": True, "exit_code": 0}

    c = HostWriteConfirmer(lambda t, d: events.append((t, d)), run_confirmed,
                           timeout_s=timeout_s, pending=pending)
    return c, events, pending, ran


def _ask_in_thread(c):
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=c(tool="ssh_execute", host="box1", command="sudo -n docker stop x", reason="stops a container")))
    t.start()
    return t, out


def _wait_for_question(events):
    for _ in range(200):
        if events and events[0][0] == "confirm":
            return events[0][1]["id"]
        time.sleep(0.01)
    raise AssertionError("no confirm event")


def test_run_executes_with_the_approver_and_reports_the_result():
    c, events, pending, ran = _confirmer(run={"success": True, "exit_code": 0, "stdout": "x"})
    t, out = _ask_in_thread(c)
    cid = _wait_for_question(events)
    assert c.busy  # the stream must not time the turn out while it waits
    q = events[0][1]
    assert (q["host"], q["command"], q["reason"]) == ("box1", "sudo -n docker stop x", "stops a container")
    assert pending.resolve(cid, True, "aachten") == "ok"
    t.join(2)
    assert out["r"]["stdout"] == "x"
    (call,) = ran
    assert call["approver"] == "aachten" and call["command"] == "sudo -n docker stop x"
    assert call["decided_at"]
    assert ("confirm_result", {"id": cid, "decision": "approved", "by": "aachten"}) in events
    assert not c.busy


def test_decline_runs_nothing_and_tells_the_model_not_to_find_another_way():
    c, events, pending, ran = _confirmer()
    t, out = _ask_in_thread(c)
    cid = _wait_for_question(events)
    pending.resolve(cid, False, "aachten")
    t.join(2)
    assert ran == []
    assert out["r"]["refused"] is True and out["r"]["declined"] is True
    assert "declined" in out["r"]["error"] and "another way" in out["r"]["error"]


def test_stop_reads_as_stopped_not_declined():
    c, events, pending, ran = _confirmer()
    t, out = _ask_in_thread(c)
    cid = _wait_for_question(events)
    pending.resolve(cid, False, "aachten", why="stopped")
    t.join(2)
    assert ran == []
    assert events[-1] == ("confirm_result", {"id": cid, "decision": "stopped", "by": "aachten"})


def test_no_answer_in_time_runs_nothing():
    c, events, pending, ran = _confirmer(timeout_s=0.05)
    out = c(tool="ssh_execute", host="box1", command="reboot", reason="reboots")
    assert ran == []
    assert out["refused"] is True and "nothing ran" in out["error"]
    assert events[-1][1]["decision"] == "timeout"
    # The question is gone: a late click finds nothing to approve.
    assert pending.resolve(events[0][1]["id"], True, "aachten") == "unknown"


def test_the_first_answer_wins():
    pending = PendingConfirms()
    cid, _ = pending.open()
    assert pending.resolve(cid, False, "alice") == "ok"
    assert pending.resolve(cid, True, "bob") == "answered"
    assert pending.resolve("nope", True, "bob") == "unknown"


def test_wait_for_row_returns_the_outcome_or_none_at_the_deadline():
    rows = iter([{"status": "claimed"}, None, {"status": "resolved", "id": 3}])
    clock = iter(range(100))
    got = wait_for_row(lambda rid: next(rows), 3, timeout_s=50, sleep=lambda s: None,
                       clock=lambda: next(clock))
    assert got == {"status": "resolved", "id": 3}
    clock = iter(range(0, 1000, 10))
    assert wait_for_row(lambda rid: {"status": "claimed"}, 3, timeout_s=25,
                        sleep=lambda s: None, clock=lambda: next(clock)) is None


# ---- the confirmed path -------------------------------------------------------

def _op(*, enabled=True, recorder="http://changerecord:8091", final=None):
    op = MagicMock()
    op.config = {"remediation": {"executor": {"node_action": {"enabled": enabled}}}}
    op._remediation_flag = lambda name: enabled if name == "node_action_enabled" else True
    op._executor_config = lambda: op.config["remediation"]["executor"]
    op._change_record_url = lambda: recorder
    op.kb.queue_confirmed_host_command.return_value = 77
    op.kb.get_remediation.return_value = final or {
        "id": 77, "status": "resolved", "last_error": "ran 1 command(s) on box1, confirmed by aachten",
        "result": {"executed": [{"command": "c", "returncode": 0, "stdout": "stopped", "stderr": ""}]}}
    return op


def _run(op, opened=None, **over):
    kw = dict(tool="ssh_execute", host="box1", command="sudo -n docker stop x",
              reason="stops a container", approver="aachten", decided_at="2026-10-18T00:00:00+00:00",
              session_id=12)
    kw.update(over)
    opened = opened if opened is not None else {
        "ref": "tok", "url": "https://github.com/o/r/blob/main/change-records/77.json",
        "approval": {"identity": "aachten", "state": "console-confirmed"}}
    with patch.object(agent_mod, "change_record_open", return_value=opened) as cro, \
         patch.object(agent_mod, "CONFIRMED_JOB_WAIT_S", 1):
        out = CFOperator._run_confirmed_host_command(op, **kw)
    return out, cro


def test_a_confirmed_command_is_recorded_pre_approved_spawned_and_reported():
    op = _op()
    out, cro = _run(op)
    payload, job = op.kb.queue_confirmed_host_command.call_args[0][:2]
    assert payload["source"] == CONSOLE_CONFIRMED_SOURCE
    assert payload["plan"] == {"host": "box1", "commands": ["sudo -n docker stop x"],
                               "explanation": "Confirmed in the console chat by aachten"}
    assert payload["confirmed_by"] == "aachten" and payload["chat_session_id"] == 12
    intent = cro.call_args[0][1]
    assert intent["remediation_id"] == 77 and intent["commands"] == ["sudo -n docker stop x"]
    assert intent["approval"]["identity"] == "aachten"
    assert intent["approval"]["state"] == "console-confirmed"
    spawned_job, work = op._spawn_remediation_executor.call_args[0]
    assert spawned_job == job
    assert work["origin"] == CONSOLE_CONFIRMED_SOURCE
    assert work["approved_plan"]["commands"] == ["sudo -n docker stop x"]
    assert work["change_record_approval"]["identity"] == "aachten"
    assert out["success"] is True and out["exit_code"] == 0 and out["stdout"] == "stopped"
    assert out["remediation_id"] == 77 and out["confirmed_by"] == "aachten"
    assert out["change_record_url"].endswith("/77.json")


def test_the_kill_switch_still_applies_and_nothing_is_recorded():
    op = _op(enabled=False)
    out, cro = _run(op)
    cro.assert_not_called()
    op.kb.queue_confirmed_host_command.assert_not_called()
    op._spawn_remediation_executor.assert_not_called()
    assert out["success"] is False and "Nothing ran" in out["error"]


def test_no_recorder_means_no_run():
    """The audit is not optional: a confirmed command is recorded first."""
    op = _op(recorder="")
    out, cro = _run(op)
    cro.assert_not_called()
    op._spawn_remediation_executor.assert_not_called()
    assert "change recorder" in out["error"] and "Nothing ran" in out["error"]


def test_an_old_recorder_that_opened_a_review_fails_the_row_and_never_spawns():
    """A recorder from before CFOP-442 ignores the approval and opens a PR. A
    merge of that PR hours later must not run the command unwatched."""
    op = _op()
    out, _ = _run(op, opened={"ref": "tok", "url": "https://github.com/o/r/pull/9"})
    op._spawn_remediation_executor.assert_not_called()
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args == (77, "failed")
    assert "/pull/9" in kwargs["last_error"]
    assert "/pull/9" in out["error"] and "Nothing ran" in out["error"]


def test_a_spawn_failure_fails_the_row():
    op = _op()
    op._spawn_remediation_executor.side_effect = RuntimeError("forbidden")
    out, _ = _run(op)
    assert op.kb.update_remediation_status.call_args[0][:2] == (77, "failed")
    assert "forbidden" in out["error"]


def test_a_job_that_has_not_reported_is_pending_not_failed():
    op = _op(final={"id": 77, "status": "claimed"})
    with patch.object(agent_mod, "wait_for_row", return_value=None):
        out, _ = _run(op)
    assert out["pending"] is True and "do not run it again" in out["message"]


def test_a_failed_command_reports_its_exit_and_stderr():
    op = _op(final={"id": 77, "status": "failed", "last_error": "command exited 1: No such container",
                    "result": {"executed": [{"returncode": 1, "stdout": "", "stderr": "No such container"}]}})
    out, _ = _run(op)
    assert out["success"] is False and out["exit_code"] == 1
    assert out["stderr"] == "No such container"


# ---- the queue never runs it twice ---------------------------------------------

def _kb_with(item):
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = item
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.session_scope = MagicMock()
    kb.session_scope.return_value.__enter__.return_value = session
    kb.remediation_policy = lambda: MagicMock(max_attempts=3)
    return kb, session


def test_a_confirmed_row_that_fails_is_never_requeued():
    """The reaper reaches fail_remediation for a Job that died mid-run.
    Re-queueing would hand a confirmed `docker rm` to the drainer to run again.

    Mutation-check: drop the CONSOLE_CONFIRMED_SOURCE branch in
    fail_remediation and this row goes back to 'queued'.
    """
    item = MagicMock(attempts=0, result=None, payload={"source": CONSOLE_CONFIRMED_SOURCE})
    kb, _ = _kb_with(item)
    assert KnowledgeBase.fail_remediation(kb, 7, "lease expired") == "failed"
    assert item.status == "failed" and item.completed_at is not None


def test_an_ordinary_row_still_retries():
    item = MagicMock(attempts=0, result=None, payload={"source": "investigation"})
    kb, _ = _kb_with(item)
    assert KnowledgeBase.fail_remediation(kb, 7, "boom") == "queued"


def test_a_confirmed_row_enters_claimed_so_the_drainer_never_sees_it():
    kb, session = _kb_with(None)
    session.flush.side_effect = lambda: setattr(session.add.call_args[0][0], "id", 91)
    rid = KnowledgeBase.queue_confirmed_host_command(
        kb, {"source": CONSOLE_CONFIRMED_SOURCE, "plan": {}}, "cfop-executor-abc", host_id="box1")
    row = session.add.call_args[0][0]
    assert rid == 91
    assert row.status == "claimed" and row.executor_job_name == "cfop-executor-abc"
    assert row.claimed_at is not None and row.remediation_class == "node-action"


def test_only_a_confirmed_payload_may_enter_that_way():
    kb, _ = _kb_with(None)
    with pytest.raises(ValueError):
        KnowledgeBase.queue_confirmed_host_command(kb, {"source": "investigation"}, "job")


# ---- who gets a confirmer -----------------------------------------------------

def _stream_policy(**kw):
    """The ToolPolicy handle_chat_message_stream builds for a turn."""
    op = MagicMock()
    op._expand_slash_shortcut = lambda m: m
    seen = {}

    def handle(message, history, backend, model, event_callback, tool_policy=None):
        seen["policy"] = tool_policy
        return {"response": "ok"}
    op._handle_chat_with_stream = handle
    list(CFOperator.handle_chat_message_stream(op, "hi", [], **kw))
    return seen["policy"]


def test_a_console_admin_session_turn_gets_a_confirmer():
    assert _stream_policy(actor_role="admin", can_confirm_host_writes=True).confirm_host_write is not None


@pytest.mark.parametrize("kw", [
    {"actor_role": "admin", "can_confirm_host_writes": False},  # a token: nobody to ask
    {"actor_role": "member", "can_confirm_host_writes": True},
    {"actor_role": "admin", "can_confirm_host_writes": True, "verify_only": True},
    {"actor_role": "admin"},  # the default is no
])
def test_every_other_turn_gets_none(kw):
    assert _stream_policy(**kw).confirm_host_write is None


def test_the_prompt_tells_the_model_only_when_it_can_confirm():
    from tools import ToolPolicy
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {}}}
    op.tools.get_schemas.return_value = []
    op.current_investigation = None
    op.last_sweep = time.time()
    op.kb.find_learnings.return_value = []
    with_c = CFOperator._build_chat_system_context(
        op, tool_policy=ToolPolicy(actor_role="admin", confirm_host_write=lambda **k: {}))
    without = CFOperator._build_chat_system_context(op, tool_policy=ToolPolicy(actor_role="admin"))
    assert agent_mod.SSH_WRITE_CONFIRM_CONTRACT in with_c
    assert agent_mod.SSH_WRITE_CONFIRM_CONTRACT not in without
