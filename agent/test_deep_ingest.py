"""Tests for deep-investigation ingest: KB storage + diff→PR via existing gates."""

from __future__ import annotations

import base64
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from remediation import (
    RemediationProposer,
    apply_unified_diff,
    parse_unified_diff,
)

MANIFEST = """apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-exporter
spec:
  replicas: 1
  foo: bar
"""

DIFF = """--- a/k3s/base/daemonsets/node-exporter.yml
+++ b/k3s/base/daemonsets/node-exporter.yml
@@ -5,3 +5,3 @@
 spec:
   replicas: 1
-  foo: bar
+  foo: baz
"""


class _FakeGH:
    """Canned GitHub client (mirrors test_remediation.py's fake)."""

    def __init__(self, files=None, branch_exists=False, open_pr_branches=()):
        self.calls = []
        self.files = files or {}
        self.branch_exists = branch_exists
        self.open_pr_branches = list(open_pr_branches)
        self.created_pull = None

    def request(self, method, path, *, body=None, params=None, cache_ttl=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/pulls"):
            return {"success": True,
                    "data": [{"head": {"ref": b}} for b in self.open_pr_branches]}
        if method == "GET" and "/contents/" in path:
            p = path.split("/contents/", 1)[1]
            if p in self.files:
                return {"success": True, "data": {"encoding": "base64", "sha": "filesha",
                        "content": base64.b64encode(self.files[p].encode()).decode()}}
            return {"success": False, "status": 404}
        if method == "GET" and "/git/ref/heads/cfop/" in path:
            return {"success": self.branch_exists, "status": 200 if self.branch_exists else 404}
        if method == "GET" and "/git/ref/heads/main" in path:
            return {"success": True, "data": {"object": {"sha": "basesha"}}}
        if method == "POST" and path.endswith("/git/refs"):
            return {"success": True, "data": {}}
        if method == "PUT" and "/contents/" in path:
            return {"success": True, "data": {"commit": {"sha": "newsha"}}}
        if method == "POST" and path.endswith("/pulls"):
            self.created_pull = body
            return {"success": True, "data": {"number": 7, "html_url": "https://github.com/x/y/pull/7"}}
        return {"success": False, "status": 404}


def _proposer(gh, **kw):
    return RemediationProposer(
        None,
        repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}],
        open_prs=True, github=gh, **kw,
    )


# ---- unified diff parsing/applying ------------------------------------------


def test_parse_unified_diff_single_file():
    parsed = parse_unified_diff(DIFF)
    assert parsed is not None
    path, hunks = parsed
    assert path == "k3s/base/daemonsets/node-exporter.yml"
    assert len(hunks) == 1
    old_start, old, new = hunks[0]
    assert old_start == 5
    assert old == ["spec:", "  replicas: 1", "  foo: bar"]
    assert new == ["spec:", "  replicas: 1", "  foo: baz"]


def test_parse_unified_diff_rejects_multi_file():
    multi = DIFF + "--- a/other.yml\n+++ b/other.yml\n@@ -1 +1 @@\n-a\n+b\n"
    assert parse_unified_diff(multi) is None


def test_parse_unified_diff_rejects_garbage():
    assert parse_unified_diff("") is None
    assert parse_unified_diff("just some prose") is None
    assert parse_unified_diff("+++ b/x.yml\n@@ bad hunk @@\n") is None


def test_apply_unified_diff_at_stated_position():
    _path, hunks = parse_unified_diff(DIFF)
    patched = apply_unified_diff(MANIFEST, hunks)
    assert patched is not None
    assert "foo: baz" in patched
    assert "foo: bar" not in patched


def test_apply_unified_diff_finds_unique_drifted_block():
    # Two extra lines at the top shift everything; the block is still unique.
    drifted = "# comment\n# comment2\n" + MANIFEST
    _path, hunks = parse_unified_diff(DIFF)
    patched = apply_unified_diff(drifted, hunks)
    assert patched is not None
    assert "foo: baz" in patched


def test_apply_unified_diff_rejects_on_context_mismatch():
    _path, hunks = parse_unified_diff(DIFF)
    assert apply_unified_diff("totally: different\nfile: here\n", hunks) is None


def test_apply_unified_diff_rejects_ambiguous_match():
    _path, hunks = parse_unified_diff(DIFF)
    block = "spec:\n  replicas: 1\n  foo: bar\n"
    two_copies = "a: 1\n" + block + "b: 2\n" + block
    assert apply_unified_diff(two_copies, hunks) is None


# ---- open_pr_from_diff gates -------------------------------------------------


def _open(gh, **kw):
    return _proposer(gh, **kw).open_pr_from_diff(
        diff_text=DIFF, title="fix", body="body", dedupe_key="raspberrypi3-NodeUnreachable")


def test_open_pr_from_diff_end_to_end():
    gh = _FakeGH(files={"k3s/base/daemonsets/node-exporter.yml": MANIFEST})
    res = _open(gh)
    assert res["status"] == "opened"
    assert res["pr_number"] == 7
    assert res["branch"] == "cfop/remediate-deep-raspberrypi3-nodeunreachable"
    put = [c for c in gh.calls if c[0] == "PUT"][0]
    committed = base64.b64decode(put[2]["content"]).decode()
    assert "foo: baz" in committed


def test_open_pr_from_diff_noop_when_flag_off():
    gh = _FakeGH(files={"k3s/base/daemonsets/node-exporter.yml": MANIFEST})
    proposer = RemediationProposer(
        None, repos=[{"name": "homelab-infra", "github": "aachtenberg/homelab-infra"}],
        open_prs=False, github=gh)
    assert proposer.open_pr_from_diff(
        diff_text=DIFF, title="t", body="b", dedupe_key="k") is None


def test_open_pr_from_diff_refuses_secret_paths():
    secret_diff = DIFF.replace("k3s/base/daemonsets/node-exporter.yml",
                               "k3s/base/apps/sealed-secrets/cfoperator-secrets.yml")
    gh = _FakeGH()
    res = _proposer(gh).open_pr_from_diff(
        diff_text=secret_diff, title="t", body="b", dedupe_key="k")
    assert res["status"] == "refused"
    assert gh.created_pull is None


def test_open_pr_from_diff_declines_multi_file():
    multi = DIFF + "--- a/other.yml\n+++ b/other.yml\n@@ -1 +1 @@\n-a\n+b\n"
    res = _open(_FakeGH())
    gh = _FakeGH()
    res = _proposer(gh).open_pr_from_diff(diff_text=multi, title="t", body="b", dedupe_key="k")
    assert res["status"] == "declined"


def test_open_pr_from_diff_skips_existing_branch():
    gh = _FakeGH(files={"k3s/base/daemonsets/node-exporter.yml": MANIFEST}, branch_exists=True)
    res = _open(gh)
    assert res["status"] == "skipped"
    assert gh.created_pull is None


def test_open_pr_from_diff_respects_shared_cap():
    gh = _FakeGH(
        files={"k3s/base/daemonsets/node-exporter.yml": MANIFEST},
        open_pr_branches=["cfop/remediate-a", "cfop/remediate-b", "cfop/remediate-deep-c"],
    )
    res = _open(gh, max_open_prs=3)
    assert res["status"] == "capped"
    assert gh.created_pull is None


def test_open_pr_from_diff_declines_when_diff_does_not_apply():
    gh = _FakeGH(files={"k3s/base/daemonsets/node-exporter.yml": "changed: completely\n"})
    res = _open(gh)
    assert res["status"] == "declined"
    assert gh.created_pull is None


# ---- store_deep_investigation -------------------------------------------------


def _operator(config=None):
    op = CFOperator.__new__(CFOperator)
    op.config = config or {}
    op.embeddings = MagicMock()
    op.embeddings.is_available.return_value = False
    op.kb = MagicMock()
    op.kb.start_investigation.return_value = 11
    op.tools = MagicMock()
    return op


_ALERT = {
    "alert_id": "abc-123",
    "summary": "NodeUnreachable raspberrypi3",
    "details": {"alertname": "NodeUnreachable"},
}


def _result(outcome="needs_action", **details):
    base = {
        "outcome": outcome,
        "report": "## Root cause\nSD card died.",
        "recommendation": "Replace the SD card.",
        "model": "claude-opus-4-8",
        "host": "raspberrypi3",
        "duration_s": 42.5,
    }
    base.update(details)
    return {"action": "deep_investigate", "success": True, "message": "m", "details": base}


def test_store_deep_investigation_stores_kb_trio():
    op = _operator()
    out = op.store_deep_investigation(_ALERT, _result())

    assert out["investigation_id"] == 11
    assert out["outcome"] == "needs_action"
    op.kb.start_investigation.assert_called_once_with(
        trigger="[deep] NodeUnreachable raspberrypi3", alert_id="abc-123")
    kwargs = op.kb.update_investigation.call_args.kwargs
    assert kwargs["investigation_id"] == 11
    assert kwargs["outcome"] == "needs_action"
    assert kwargs["findings"]["deep"] is True
    assert kwargs["findings"]["recommendation"] == "Replace the SD card."
    # Embedding attempted (is_available consulted) even though unavailable here.
    op.embeddings.is_available.assert_called()


def test_store_deep_investigation_maps_escalated_to_kb_escalate():
    op = _operator()
    out = op.store_deep_investigation(_ALERT, _result(outcome="escalated"))
    assert out["outcome"] == "escalate"


def test_store_deep_investigation_no_pr_when_gate_off():
    op = _operator(config={"remediation": {"deep_open_prs": False}})
    out = op.store_deep_investigation(_ALERT, _result(proposed_diff=DIFF))
    assert "pr_result" not in out


def test_store_deep_investigation_routes_diff_through_gates():
    op = _operator(config={
        "remediation": {"deep_open_prs": True, "default_repo": "homelab-infra"},
        "git": {"repos": [{"name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "main"}]},
    })
    gh = _FakeGH(files={"k3s/base/daemonsets/node-exporter.yml": MANIFEST})
    op._github_write_client = lambda: gh

    out = op.store_deep_investigation(_ALERT, _result(proposed_diff=DIFF))

    assert out["pr_result"]["status"] == "opened"
    assert out["pr_result"]["branch"].startswith("cfop/remediate-deep-")
    assert gh.created_pull["title"].startswith("cfoperator deep-investigation fix")


def test_store_deep_investigation_counts_the_outcome_and_observes_the_duration():
    """The deep worker's result is a terminal outcome: it must move the same
    started/outcome/duration metrics as an in-process investigation, or the
    three stop reconciling for every alert the deep tier handled (review of
    CFOP-163)."""
    import sys as _sys
    M = _sys.modules[CFOperator.__module__]
    started = M.INVESTIGATIONS_STARTED._value.get()
    outcome = M.INVESTIGATIONS.labels(outcome="needs_action")._value.get()
    dur = M.INVESTIGATION_DURATION.labels(outcome="needs_action")
    dur_n = sum(b.get() for b in dur._buckets); dur_sum = dur._sum.get()
    op = _operator()
    op.store_deep_investigation(_ALERT, _result(duration_s=42.5))
    assert M.INVESTIGATIONS_STARTED._value.get() == started + 1
    assert M.INVESTIGATIONS.labels(outcome="needs_action")._value.get() == outcome + 1
    assert sum(b.get() for b in dur._buckets) == dur_n + 1
    assert dur._sum.get() == dur_sum + 42.5


# ---- CFOP-216: the row exists before the worker's completion goes ------------


def test_begin_creates_the_row_with_its_alert_and_store_reuses_it():
    """The route creates the row up front and hands its id to the background
    storage, which must not create a second one."""
    op = _operator()
    inv_id = op.begin_deep_investigation(_ALERT)
    assert inv_id == 11
    op.store_deep_investigation(_ALERT, _result(), inv_id=inv_id)
    op.kb.start_investigation.assert_called_once_with(
        trigger="[deep] NodeUnreachable raspberrypi3", alert_id="abc-123")
    assert op.kb.update_investigation.call_args.kwargs["investigation_id"] == 11


def test_a_report_that_never_landed_marks_the_published_row_failed():
    """Event_runtime already holds this id (Events link, attach line), so a
    storage failure before the report is written must not leave the row
    in_progress forever. Mutation check: drop the finally and this fails."""
    op = _operator()
    op.kb.update_investigation.side_effect = [RuntimeError("db hiccup"), True]
    try:
        op.store_deep_investigation(_ALERT, _result(), inv_id=11)
    except RuntimeError:
        pass
    last = op.kb.update_investigation.call_args.kwargs
    assert last["investigation_id"] == 11 and last["outcome"] == "failed"


def test_a_failure_after_the_report_landed_keeps_its_outcome():
    """The queue or PR gate failing afterwards does not make the
    investigation wrong; overwriting needs_action with failed would."""
    op = _operator()
    op._maybe_queue_remediation = MagicMock(side_effect=RuntimeError("queue down"))
    try:
        op.store_deep_investigation(_ALERT, _result(), inv_id=11)
    except RuntimeError:
        pass
    outcomes = [c.kwargs.get("outcome") for c in op.kb.update_investigation.call_args_list]
    assert outcomes == ["needs_action"], outcomes


def test_an_offline_placeholder_is_never_marked():
    """A negative id is ResilientKB's local placeholder: no row to mark."""
    op = _operator()
    op.kb.update_investigation.side_effect = RuntimeError("offline")
    try:
        op.store_deep_investigation(_ALERT, _result(), inv_id=-4)
    except RuntimeError:
        pass
    assert op.kb.update_investigation.call_count == 1


# ---- the route answers with the id -------------------------------------------


def _deep_client(monkeypatch, begin, existing=lambda alert: None):
    import threading
    from types import SimpleNamespace

    from flask import Flask
    from web_server import WebServer

    monkeypatch.delenv("CFOP_COMPLETION_SHARED_SECRET", raising=False)
    stored = threading.Event()
    op = SimpleNamespace(current_investigation=None, start_time=0.0, store_calls=[])
    op.begin_deep_investigation = begin
    op.existing_deep_investigation = existing

    def store(alert, result, inv_id=None):
        op.store_calls.append(inv_id)
        stored.set()

    op.store_deep_investigation = store
    server = WebServer.__new__(WebServer)
    server.operator, server.host, server.port = op, "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server._setup_routes()
    return server.app.test_client(), op, stored


def _ingest(client):
    return client.post("/v1/deep-investigations", json={"alert": _ALERT, "result": _result()})


def test_the_ingest_route_answers_with_the_investigation_id(monkeypatch):
    client, op, stored = _deep_client(monkeypatch, lambda alert: 2301)
    resp = _ingest(client)
    assert resp.status_code == 202
    assert resp.get_json()["investigation_id"] == 2301
    assert stored.wait(2) and op.store_calls == [2301]


def test_the_ingest_route_never_publishes_an_offline_placeholder(monkeypatch):
    """Mutation check: drop the > 0 guard and the -7 goes out, which the
    worker would stamp onto Slack as an attach line to no row."""
    client, op, stored = _deep_client(monkeypatch, lambda alert: -7)
    resp = _ingest(client)
    assert resp.status_code == 202
    assert "investigation_id" not in resp.get_json()
    assert stored.wait(2)


def test_the_ingest_route_still_stores_when_the_row_could_not_be_made(monkeypatch):
    def boom(alert):
        raise RuntimeError("db down")

    client, op, stored = _deep_client(monkeypatch, boom)
    resp = _ingest(client)
    assert resp.status_code == 202 and "investigation_id" not in resp.get_json()
    assert stored.wait(2) and op.store_calls == [None]


# ---- the in-process path links its alert too ---------------------------------


def test_act_records_the_alert_it_answers():
    """_act creates the row for HTTP-triggered investigations; it carries
    the event_runtime alert_id so the drawer can link to /events."""
    seen = {}

    class _Stop(Exception):
        pass

    def start(trigger, alert_id=None):
        seen["alert_id"] = alert_id
        raise _Stop()

    op = _operator()
    op.kb.start_investigation.side_effect = start
    try:
        op._act({"trigger": "Pod foo not ready", "alert": {"alert_id": "aid-77"}})
    except _Stop:
        pass
    assert seen["alert_id"] == "aid-77"


def test_an_offline_start_keeps_its_alert_through_the_buffer():
    """Offline, ResilientKB buffers the start and replays it later; the
    alert link must survive that round trip, or every investigation begun
    during an outage loses its Events link for good."""
    import threading
    from types import SimpleNamespace

    from knowledge_base import ResilientKnowledgeBase

    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._health_monitor = SimpleNamespace(is_healthy=lambda: False)
    buffered = []
    rkb._buffer = SimpleNamespace(buffer_event=lambda kind, data: buffered.append((kind, data)))
    rkb._local_id_lock = threading.Lock()
    rkb._local_inv_id_counter = 0
    rkb._local_to_db_id_map = {}

    local = rkb.start_investigation("[deep] node gone", alert_id="aid-9")
    assert local < 0, "offline ids are negative placeholders"
    kind, data = buffered[0]
    assert kind == "start_investigation" and data["alert_id"] == "aid-9"

    rkb._kb = MagicMock()
    rkb._kb.start_investigation.return_value = 501
    rkb._replay_event(SimpleNamespace(event_type=kind, data=data))
    rkb._kb.start_investigation.assert_called_once_with("[deep] node gone", alert_id="aid-9")
    assert rkb._local_to_db_id_map[local] == 501



# ---- review of #303: a retried ingest reuses the row, not a second one --------


def test_a_retry_while_storage_runs_gets_the_same_row_and_no_second_writer(monkeypatch):
    """The worker retries an ingest whose answer took longer than its 10s
    timeout; the first attempt may have landed and still be storing. The
    retry gets that row and starts no second storage thread. Mutation check:
    treat an in_progress row as abandoned regardless of the set and a second
    store starts."""
    import threading

    release = threading.Event()
    lookups = iter([None, (2301, "in_progress")])
    client, op, stored = _deep_client(monkeypatch, lambda a: 2301,
                                      existing=lambda a: next(lookups))
    def slow_store(alert, result, inv_id=None):
        op.store_calls.append(inv_id)
        stored.set()
        release.wait(2)
    op.store_deep_investigation = slow_store

    first = _ingest(client)
    assert stored.wait(2)
    retry = _ingest(client)
    release.set()
    assert first.get_json()["investigation_id"] == 2301
    assert retry.get_json() == {"status": "duplicate", "investigation_id": 2301}
    assert op.store_calls == [2301], "a duplicate must not start a second writer"


def test_a_retry_after_storage_finished_gets_the_same_row(monkeypatch):
    """The common retry: the first answer was slow, but its storage finished
    before the retry arrived, so the row already has an outcome. Matching
    only in_progress rows missed this and made a second row (second review
    of #303). Mutation check: only match in_progress and begin is called."""
    began = []
    client, op, stored = _deep_client(monkeypatch, lambda a: began.append(a) or 999,
                                      existing=lambda a: (2301, "needs_action"))
    resp = _ingest(client)
    assert resp.get_json() == {"status": "duplicate", "investigation_id": 2301}
    assert began == [] and not stored.wait(0.2)


def test_a_retry_resumes_a_row_whose_storage_was_lost(monkeypatch):
    """An in_progress row with no storage running in this process was left
    by a restart between its INSERT and its report. Returning it as a
    duplicate would link the completion to a row that stays empty forever;
    the retry resumes storage on it instead (review of #303)."""
    began = []
    client, op, stored = _deep_client(monkeypatch, lambda a: began.append(a) or 999,
                                      existing=lambda a: (2301, "in_progress"))
    resp = _ingest(client)
    assert resp.get_json() == {"status": "resumed", "investigation_id": 2301}
    assert stored.wait(2) and op.store_calls == [2301]
    assert began == [], "resuming must not create a second row"


def test_a_retry_after_storage_failed_tries_storage_again(monkeypatch):
    """A row storage marked failed holds no report. Answering duplicate
    would link the completion to it and never retry; the retry resumes
    storage on that row instead (third review of #303). Mutation check:
    resume only in_progress and this is answered duplicate."""
    began = []
    client, op, stored = _deep_client(monkeypatch, lambda a: began.append(a) or 999,
                                      existing=lambda a: (2301, "failed"))
    resp = _ingest(client)
    assert resp.get_json() == {"status": "resumed", "investigation_id": 2301}
    assert stored.wait(2) and op.store_calls == [2301]
    assert began == []


def test_concurrent_ingests_for_one_alert_make_one_row(monkeypatch):
    """Lookup, decision and INSERT share one lock, so two overlapping
    ingests cannot both see no row and both insert. Mutation check: release
    the lock before begin and two rows are made."""
    import threading
    import time

    rows = {}
    def existing(alert):
        return (rows["id"], "in_progress") if rows else None
    def begin(alert):
        time.sleep(0.05)  # widen the window between lookup and insert
        rows["id"] = 2400 + len(rows)
        return rows["id"]
    client, op, stored = _deep_client(monkeypatch, begin, existing=existing)
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(_ingest(client).get_json()))
               for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert sorted(a["investigation_id"] for a in answers) == [2400, 2400], answers
    assert sorted(a["status"] for a in answers) == ["accepted", "duplicate"], answers


def test_a_failed_duplicate_check_falls_through_to_a_new_row(monkeypatch):
    def boom(alert):
        raise RuntimeError("db down")

    client, op, stored = _deep_client(monkeypatch, lambda a: 2302, existing=boom)
    resp = _ingest(client)
    assert resp.get_json()["investigation_id"] == 2302
    assert stored.wait(2)


def test_the_duplicate_lookup_matches_on_alert_and_deep_trigger():
    op = _operator()
    op.kb.find_recent_investigation_for_alert.return_value = (77, "in_progress")
    assert op.existing_deep_investigation(_ALERT) == (77, "in_progress")
    op.kb.find_recent_investigation_for_alert.assert_called_once_with(
        "abc-123", "[deep] NodeUnreachable raspberrypi3")
    op.kb.find_recent_investigation_for_alert.reset_mock()
    assert op.existing_deep_investigation({"summary": "no id"}) is None
    op.kb.find_recent_investigation_for_alert.assert_not_called()


# ---- review of #303: buffered starts wait for the schema ---------------------


def _sync_kb(schema_ok):
    import threading
    from types import SimpleNamespace

    from knowledge_base import ResilientKnowledgeBase

    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._health_monitor = SimpleNamespace(is_healthy=lambda: True)
    rkb._buffer = SimpleNamespace(has_pending_events=lambda: True)
    rkb._schema_initialized = False
    rkb._kb = MagicMock()
    rkb._kb.initialize_schema.return_value = schema_ok
    rkb._schema_lock = threading.Lock()
    replayed = []
    rkb._sync_buffered_events = lambda: replayed.append(1)
    return rkb, replayed


def test_buffered_events_are_held_while_the_schema_is_incomplete():
    """A failed replay is skipped and marked synced, so replaying a buffered
    start into a table still missing alert_id would lose it for good.
    Mutation check: drop the _schema_initialized gate and this replays."""
    rkb, replayed = _sync_kb(schema_ok=False)
    rkb._sync_tick()
    assert replayed == []


def test_buffered_events_replay_once_the_schema_is_complete():
    rkb, replayed = _sync_kb(schema_ok=True)
    rkb._sync_tick()
    assert replayed == [1]
