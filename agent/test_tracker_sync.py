#!/usr/bin/env python3
"""The tracker hand-off tick (CFOP-170).

Every line of the lifecycle table in tracker_sync.decide() as its own test,
then the tick's writes: a filed row leaves the active list in ONE write right
after the create, a tracker-closed item closes the row without bouncing a
transition back, a dead service isolates per row, and nothing is ever filed
post-hoc. MagicMock operator; the HTTP client functions are patched at the
module that calls them.
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tracker_sync as ts  # noqa: E402
from tracker_client import TrackerClientError  # noqa: E402

CREATED = {"ref": "R1", "url": "https://plane/x/1", "key": "CFOP-1", "backend": "plane"}


def _op(*, flag=True, url="http://tracker:8092", console="http://c", rows=None, per_tick=10):
    op = MagicMock()
    op.config = {"remediation": {"queue_tracker": flag, "max_tracker_per_tick": per_tick}}
    op._remediation_flag = lambda name: bool(op.config["remediation"].get(name))
    op._tracker_url = lambda: url
    op._console_base_url = lambda: console
    op._tracker_url_warned = False
    op.kb.list_remediations_for_tracker.return_value = list(rows or [])
    return op


def _row(**over):
    row = {"id": 42, "status": "needs-human", "remediation_class": "gitops-patch", "risk": "low",
           "confidence": 0.7, "host_id": "pi2", "investigation_id": 9, "attempts": 0, "pr_url": None,
           "named_pr_url": None, "last_error": None, "result": {}, "payload": {"recommendation": "fix it"}}
    row.update(over)
    return row


def _with_ref(status="filed", synced="filed", **over):
    return _row(status=status, result={"tracker": {"ref": "R1", "key": "CFOP-1", "url": "https://plane/x/1",
                                                    "synced_status": synced, "synced_at": "t0"}}, **over)


def _tracker_written(kb_method):
    """The result.tracker dict a KB write received."""
    assert kb_method.called, "no KB write"
    kwargs = kb_method.call_args.kwargs
    return kwargs["result"]["tracker"] if "result" in kwargs else kb_method.call_args.args[1]["tracker"]


# --- the decision table --------------------------------------------------

@pytest.mark.parametrize("row, want", [
    (_row(status="needs-human"), "create"),
    (_row(status="pr-open", pr_url="https://x/pull/1"), "create"),
    (_row(status="needs-human", pr_url="https://x/pull/1"), "create"),
    (_row(status="queued"), None),
    (_row(status="claimed"), None),
    (_row(status="resolved"), None),          # never post-hoc
    (_row(status="rejected"), None),
    (_row(status="failed"), None),
    (_with_ref("filed"), "reconcile"),
    (_with_ref("needs-human", synced="filed"), "repark"),
    (_with_ref("needs-human", synced="filed", pr_url="https://x/pull/1"), "comment"),
    (_with_ref("needs-human", synced="needs-human", pr_url="https://x/pull/1"), None),
    (_with_ref("pr-open", synced="filed", pr_url="https://x/pull/1"), "comment"),
    (_with_ref("pr-open", synced="pr-open", pr_url="https://x/pull/1"), None),
    (_with_ref("queued", synced="filed"), "comment"),
    (_with_ref("queued", synced="queued"), None),
    (_with_ref("resolved", synced="filed"), "transition"),
    (_with_ref("rejected", synced="pr-open"), "transition"),
    (_with_ref("resolved", synced="resolved"), None),
    (_with_ref("executing", synced="queued"), None),   # mid-run: leave it alone
])
def test_decide(row, want):
    assert ts.decide(row) == want


def test_decide_never_bounces_a_transition_the_tracker_caused():
    row = _with_ref("resolved", synced="filed")
    row["result"]["resolved_by"] = "tracker"
    assert ts.decide(row) is None


# --- create --------------------------------------------------------------

def test_create_hands_off_a_parked_row_in_one_write():
    op = _op()
    with patch.object(ts, "tracker_create", return_value=CREATED) as create:
        assert ts.sync_tracker_row(op, _row(), "http://tracker:8092") == 1
    item = create.call_args.args[1]
    assert create.call_args.args[0] == "http://tracker:8092"
    assert item["priority"] == "low" and item["title"].startswith("[cfop #42] ")
    assert item["links"]["console_url"] == "http://c/remediations#42"
    op.kb.update_remediation_status.assert_called_once()
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args == (42, "filed")
    tr = kwargs["result"]["tracker"]
    assert tr["ref"] == "R1" and tr["key"] == "CFOP-1" and tr["synced_status"] == "filed"
    assert tr["error"] is None and tr["error_count"] == 0
    op.kb.merge_remediation_result.assert_not_called()


def test_create_for_a_row_with_a_pr_keeps_its_status_and_files_high():
    for row in (_row(status="pr-open", pr_url="https://x/pull/1"),
                _row(status="needs-human", pr_url="https://x/pull/1")):
        op = _op()
        with patch.object(ts, "tracker_create", return_value=CREATED) as create:
            assert ts.sync_tracker_row(op, row, "http://t") == 1
        assert create.call_args.args[1]["priority"] == "high"
        op.kb.update_remediation_status.assert_not_called()
        tr = _tracker_written(op.kb.merge_remediation_result)
        assert tr["synced_status"] == row["status"] and tr["ref"] == "R1"


def test_a_row_that_already_has_a_ref_is_never_created_again():
    op = _op()
    with patch.object(ts, "tracker_create") as create, \
            patch.object(ts, "tracker_get", return_value={"state": "open", "key": "CFOP-1"}):
        assert ts.sync_tracker_row(op, _with_ref("filed"), "http://t") == 0
    create.assert_not_called()


# --- repark / comment / transition --------------------------------------

def test_repark_tells_the_item_why_and_refiles():
    op = _op()
    row = _with_ref("needs-human", synced="queued", last_error="executor declined: multi-file diff")
    with patch.object(ts, "tracker_comment") as comment:
        assert ts.sync_tracker_row(op, row, "http://t") == 1
    assert comment.call_args.args[:2] == ("http://t", "R1")
    assert "multi-file diff" in comment.call_args.args[2]
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args == (42, "filed") and kwargs["result"]["tracker"]["synced_status"] == "filed"


def test_comment_on_pr_open_carries_the_pr_link_and_on_queued_says_so():
    op = _op()
    with patch.object(ts, "tracker_comment") as comment:
        ts.sync_tracker_row(op, _with_ref("pr-open", synced="filed", pr_url="https://x/pull/7"), "http://t")
        assert "https://x/pull/7" in comment.call_args.args[2]
        ts.sync_tracker_row(op, _with_ref("queued", synced="filed", attempts=1), "http://t")
        assert "executor" in comment.call_args.args[2] and "attempt 2" in comment.call_args.args[2]
    assert [c.args[1]["tracker"]["synced_status"] for c in op.kb.merge_remediation_result.call_args_list] \
        == ["pr-open", "queued"]
    op.kb.update_remediation_status.assert_not_called()


def test_transition_carries_the_note_and_marks_synced():
    op = _op()
    row = _with_ref("resolved", synced="filed", result_extra=None)
    row["result"]["resolved_by"] = "operator"
    row["result"]["resolution_note"] = "fixed by hand"
    with patch.object(ts, "tracker_transition") as transition:
        assert ts.sync_tracker_row(op, row, "http://t") == 1
    assert transition.call_args.args == ("http://t", "R1", "resolved", "Resolved by operator. fixed by hand")
    assert _tracker_written(op.kb.merge_remediation_result)["synced_status"] == "resolved"

    op = _op()
    with patch.object(ts, "tracker_transition") as transition:
        ts.sync_tracker_row(op, _with_ref("rejected", synced="filed", last_error="PR closed without merge"), "http://t")
    assert transition.call_args.args[2:] == ("rejected", "Rejected. PR closed without merge")


# --- reconcile: the tracker closes the row -------------------------------

def test_reconcile_resolves_the_row_when_the_item_is_done():
    op = _op()
    with patch.object(ts, "tracker_get", return_value={"state": "resolved", "key": "CFOP-1"}):
        assert ts.sync_tracker_row(op, _with_ref("filed"), "http://t") == 1
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args == (42, "resolved")
    assert kwargs["result"]["resolved_by"] == "tracker"
    assert "CFOP-1" in kwargs["result"]["resolution_note"]
    # synced in the same write, so the next tick sees no drift to bounce back
    assert kwargs["result"]["tracker"]["synced_status"] == "resolved"


def test_reconcile_rejects_the_row_when_the_item_is_cancelled_or_deleted():
    for got, needle in (({"state": "rejected", "key": "CFOP-1"}, "cancelled"), (None, "deleted")):
        op = _op()
        with patch.object(ts, "tracker_get", return_value=got):
            assert ts.sync_tracker_row(op, _with_ref("filed"), "http://t") == 1
        args, kwargs = op.kb.update_remediation_status.call_args
        assert args == (42, "rejected") and needle in kwargs["last_error"]
        assert kwargs["result"]["tracker"]["synced_status"] == "rejected"


def test_reconcile_of_a_still_open_item_only_stamps_the_poll():
    op = _op()
    with patch.object(ts, "tracker_get", return_value={"state": "open", "key": "CFOP-1"}):
        assert ts.sync_tracker_row(op, _with_ref("filed"), "http://t") == 0
    op.kb.update_remediation_status.assert_not_called()
    tr = _tracker_written(op.kb.merge_remediation_result)
    assert tr["checked_at"] and tr["synced_status"] == "filed"


# --- the tick ------------------------------------------------------------

def test_flag_off_lists_nothing():
    op = _op(flag=False)
    assert ts.sync_tracker(op) == 0
    op.kb.list_remediations_for_tracker.assert_not_called()


def test_url_unset_warns_once_and_lists_nothing(caplog):
    op = _op(url="")
    with caplog.at_level(logging.WARNING):
        assert ts.sync_tracker(op) == 0
        assert ts.sync_tracker(op) == 0
    assert sum("no tracker URL" in r.message for r in caplog.records) == 1
    op.kb.list_remediations_for_tracker.assert_not_called()


def test_tick_isolates_a_failing_row_and_records_its_error():
    rows = [_row(id=1), _row(id=2), _row(id=3)]
    op = _op(rows=rows, per_tick=3)
    with patch.object(ts, "tracker_create",
                      side_effect=[CREATED, TrackerClientError("create failed (0): refused", status=0), CREATED]):
        assert ts.sync_tracker(op) == 2
    op.kb.list_remediations_for_tracker.assert_called_once_with(limit=3)
    filed = [c.args[0] for c in op.kb.update_remediation_status.call_args_list]
    assert filed == [1, 3]
    (rid, patch_), = [c.args for c in op.kb.merge_remediation_result.call_args_list]
    assert rid == 2
    assert "refused" in patch_["tracker"]["error"] and patch_["tracker"]["error_count"] == 1
    assert patch_["tracker"].get("ref") is None


def test_error_count_accumulates_and_a_kb_failure_after_create_is_logged_not_raised(caplog):
    op = _op()
    row = _with_ref("pr-open", synced="filed", pr_url="https://x/pull/1")
    row["result"]["tracker"]["error_count"] = 2
    with patch.object(ts, "tracker_comment", side_effect=RuntimeError("boom")):
        assert ts.sync_tracker_row(op, row, "http://t") == 0
    assert _tracker_written(op.kb.merge_remediation_result)["error_count"] == 3

    op = _op()
    op.kb.update_remediation_status.side_effect = RuntimeError("db offline")
    op.kb.merge_remediation_result.side_effect = RuntimeError("db offline")
    with patch.object(ts, "tracker_create", return_value=CREATED), caplog.at_level(logging.WARNING):
        assert ts.sync_tracker_row(op, _row(), "http://t") == 0
    assert any("db offline" in r.message for r in caplog.records)


def test_counters_count_ok_and_error():
    ok = ts.REMEDIATION_TRACKER.labels(action="create", outcome="ok")
    err = ts.REMEDIATION_TRACKER.labels(action="create", outcome="error")
    before_ok, before_err = ok._value.get(), err._value.get()
    with patch.object(ts, "tracker_create", side_effect=[CREATED, RuntimeError("x")]):
        ts.sync_tracker_row(_op(), _row(), "http://t")
        ts.sync_tracker_row(_op(), _row(), "http://t")
    assert ok._value.get() == before_ok + 1 and err._value.get() == before_err + 1


# --- the operator's side of it ------------------------------------------

def test_operator_wiring_flag_interval_and_urls(monkeypatch):
    from agent import CFOperator
    assert "queue_tracker" in CFOperator._REMEDIATION_FLAGS
    assert "filed" in CFOperator._REMEDIATION_STATUSES
    op = MagicMock()
    op.config = {"ooda": {"remediation_tracker_interval_seconds": 120},
                 "remediation": {"tracker": {"url": "http://cfg:8092/", "console_url": "http://console/"}}}
    op.kb.get_setting.return_value = ""
    assert CFOperator._get_tracker_interval(op) == 120
    op.kb.get_setting.return_value = "5"
    assert CFOperator._get_tracker_interval(op) == 30            # clamped
    op._tracker_config = lambda: CFOperator._tracker_config(op)
    monkeypatch.delenv("CFOP_TRACKER_URL", raising=False)
    monkeypatch.delenv("CFOP_CONSOLE_URL", raising=False)
    assert CFOperator._tracker_url(op) == "http://cfg:8092"
    assert CFOperator._console_base_url(op) == "http://console"
    monkeypatch.setenv("CFOP_TRACKER_URL", "http://env:8092/")
    monkeypatch.setenv("CFOP_CONSOLE_URL", "http://envc")
    assert CFOperator._tracker_url(op) == "http://env:8092"
    assert CFOperator._console_base_url(op) == "http://envc"
    op.config = {}
    monkeypatch.delenv("CFOP_TRACKER_URL")
    assert CFOperator._tracker_url(op) == ""
