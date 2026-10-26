#!/usr/bin/env python3
"""The dedupe shadow (CFOP-514): what it compares, when it asks, what it never breaks.

The end-to-end check lives in test_dedupe_parity.py, where every shared case
runs with the shadow on and must send the case's request and agree with the
case's answer. This file pins the pieces that suite cannot reach:
- the comparison rule;
- the scope's lifecycle (off, no decision, a raising decision, a failed
  snapshot);
- the client's auth and failure modes;
- the background thread;
- a source guard that no enqueue site escapes the shadow.
"""

import ast
import http.server
import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import dedupe_shadow as ds  # noqa: E402
from test_remediation_queue import _feed_op  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _count(path, result, python, service):
    v = ds.DEDUPE_SHADOW.labels(path=path, result=result, python=python, service=service)
    return v._value.get()


@pytest.fixture
def inline(monkeypatch):
    """Run comparisons on the calling thread and collect their results."""
    results = []
    monkeypatch.setattr(ds, "_submit", lambda fn, *a: results.append(fn(*a)))
    return results


def _scope(path="investigation", candidate=None, rows=None, url="http://dedupe.test"):
    return ds.scope(path, candidate or {"dedupe_key": "k"}, url=url,
                    open_rows=lambda: rows or [], notready=lambda: set())


# ---------------------------------------------------------------------------
# matches(): the shared cases' strictness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("python, service, agree", [
    ({"decision": "fold", "row_id": 3, "tier": "target"},
     {"decision": "fold", "row_id": 3, "tier": "target"}, True),
    ({"decision": "fold", "row_id": 3, "tier": "target"},
     {"decision": "fold", "row_id": 4, "tier": "target"}, False),
    # Same row by a different tier is a parity break, not the same answer.
    ({"decision": "fold", "row_id": 3, "tier": "identifier"},
     {"decision": "fold", "row_id": 3, "tier": "target"}, False),
    ({"decision": "suppress", "row_id": 9, "tier": "key"},
     {"decision": "suppress", "row_id": 9, "tier": "key"}, True),
    ({"decision": "new", "dedupe_key": "k", "host": "default"},
     {"decision": "new", "dedupe_key": "k"}, True),
    ({"decision": "new", "dedupe_key": "k"}, {"decision": "new", "dedupe_key": "j"}, False),
    # The service names the node only when its node tier rewrote the row.
    ({"decision": "new", "dedupe_key": "node-down-pi5", "host": "raspberrypi5"},
     {"decision": "new", "dedupe_key": "node-down-pi5", "host": "pi5"}, False),
    ({"decision": "new", "dedupe_key": "", "host": "default"}, {"decision": "new"}, True),
    ({"decision": "new", "dedupe_key": "k"}, {"decision": "fold", "row_id": 1, "tier": "node"}, False),
])
def test_matches(python, service, agree):
    assert ds.matches(python, service) is agree


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------

def test_unconfigured_scope_does_nothing_at_all(inline):
    rows = MagicMock(side_effect=AssertionError("no snapshot when the service is not configured"))
    with ds.scope("investigation", {"dedupe_key": "k"}, url="", open_rows=rows,
                  notready=lambda: set()) as s:
        ds.note("new", 1, dedupe_key="k")
    assert s is None and inline == []


def test_a_scope_with_no_decision_asks_nothing(inline):
    with _scope():
        pass
    assert inline == []


def test_a_decision_that_raised_asks_nothing_and_the_error_is_the_sites(inline):
    with pytest.raises(RuntimeError, match="db down"):
        with _scope():
            ds.note("new", 1, dedupe_key="k")
            raise RuntimeError("db down")
    assert inline == []
    ds.note("new", 2)  # the scope is gone: a no-op, not an error


def test_a_snapshot_that_fails_costs_the_comparison_not_the_enqueue(inline):
    def broken():
        raise RuntimeError("db down")
    ran = []
    with ds.scope("investigation", {"dedupe_key": "k"}, url="http://dedupe.test",
                  open_rows=broken, notready=lambda: set()) as s:
        ran.append(True)
        ds.note("new", 1, dedupe_key="k")
    assert s is None and ran == [True] and inline == []


def test_the_first_note_is_the_decision(inline, monkeypatch):
    sent = []
    monkeypatch.setattr(ds, "decide", lambda url, req, **kw: sent.append(req) or
                        {"decision": "fold", "row_id": 4, "tier": "node"})
    with _scope():
        ds.note("fold", 4, tier="node")
        ds.note("new", 5, dedupe_key="later")
    assert inline == ["agree"]


def test_the_snapshot_is_taken_before_the_decision_writes(inline, monkeypatch):
    """The service must be asked about the rows Python saw, not the rows Python
    left: an enqueue inside the scope must not appear in the request."""
    rows = [{"id": 1, "status": "needs-human", "payload": {"dedupe_key": "a"}}]
    sent = []
    monkeypatch.setattr(ds, "decide", lambda url, req, **kw: sent.append(req) or
                        {"decision": "new", "dedupe_key": "b"})
    with ds.scope("investigation", {"dedupe_key": "b"}, url="http://dedupe.test",
                  open_rows=lambda: list(rows), notready=lambda: {"Pi4"}):
        rows.insert(0, {"id": 2, "status": "needs-human", "payload": {"dedupe_key": "b"}})
        ds.note("new", 2, dedupe_key="b")
    assert [r["id"] for r in sent[0]["open_rows"]] == [1]
    assert sent[0]["vocabulary"] == {"notready_nodes": ["pi4"]}
    assert inline == ["agree"]


def test_the_candidate_is_copied_before_the_tiers_rewrite_it(inline, monkeypatch):
    """The node collapse renames details['host'] and details['dedupe_key'] in
    place; the service has to be asked about the candidate as it arrived."""
    sent = []
    monkeypatch.setattr(ds, "decide", lambda url, req, **kw: sent.append(req) or
                        {"decision": "new", "dedupe_key": "node-down-pi5", "host": "pi5"})
    details = {"dedupe_key": "alert-1", "host": "pi5:9100", "recommendation": "r",
               "report": "not sent", "risk": "high"}
    with ds.scope("investigation", details, url="http://dedupe.test",
                  open_rows=lambda: [], notready=lambda: {"pi5"}):
        details.update(dedupe_key="node-down-pi5", host="pi5")
        ds.note("new", 7, dedupe_key="node-down-pi5", host="pi5")
    assert sent[0]["candidate"] == {"dedupe_key": "alert-1", "host": "pi5:9100",
                                    "recommendation": "r"}
    assert inline == ["agree"]


def test_rows_are_trimmed_to_what_the_tiers_read():
    rows = [{"id": 3, "status": "filed", "payload": {
        "dedupe_key": "k", "dedupe_aliases": ["a"], "recommendation": "r",
        "targets": [{"kind": "k", "id": "x"}], "rendered_context": "x" * 5000,
        "proposed_diff": "diff", "fold_targets": None}}]
    assert ds.trim_rows(rows) == [{"id": 3, "status": "filed", "payload": {
        "dedupe_key": "k", "dedupe_aliases": ["a"], "recommendation": "r",
        "targets": [{"kind": "k", "id": "x"}]}}]


def test_a_disagreement_is_counted_and_logged_with_both_answers(inline, monkeypatch, caplog):
    monkeypatch.setattr(ds, "decide", lambda url, req, **kw: {
        "decision": "fold", "row_id": 362, "tier": "target", "reason": "same FIX target"})
    before = _count("summary", "disagree", "new", "fold")
    with _scope(path="summary", candidate={"dedupe_key": "summary-x"}):
        ds.note("new", 365, dedupe_key="summary-x")
    assert inline == ["disagree"]
    assert _count("summary", "disagree", "new", "fold") == before + 1
    msg = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "python new #365" in msg and "service fold #362" in msg and "same FIX target" in msg


def test_an_unreachable_service_is_counted_never_raised(inline, monkeypatch):
    def down(url, req, **kw):
        raise ds.DedupeClientError("connection refused")
    monkeypatch.setattr(ds, "decide", down)
    before = _count("chat", "unavailable", "suppress", "")
    with _scope(path="chat"):
        ds.note("suppress", 2, tier="key", dedupe_key="k")
    assert inline == ["unavailable"]
    assert _count("chat", "unavailable", "suppress", "") == before + 1


def test_an_unreadable_notready_set_still_compares(inline, monkeypatch):
    sent = []
    monkeypatch.setattr(ds, "decide", lambda url, req, **kw: sent.append(req) or
                        {"decision": "new", "dedupe_key": "k"})

    def k8s_down():
        raise RuntimeError("apiserver unreachable")
    with ds.scope("investigation", {"dedupe_key": "k"}, url="http://dedupe.test",
                  open_rows=lambda: [], notready=k8s_down):
        ds.note("new", 1, dedupe_key="k")
    assert inline == ["agree"] and sent[0]["vocabulary"] == {"notready_nodes": []}


def test_a_backlog_behind_a_hung_service_is_skipped_not_queued(monkeypatch):
    submitted = []
    monkeypatch.setattr(ds, "_submit", lambda fn, *a: submitted.append(a))
    monkeypatch.setattr(ds, "_pending", ds.MAX_PENDING)
    before = _count("console", "skipped", "new", "")
    with _scope(path="console"):
        ds.note("new", 1, dedupe_key="k")
    assert submitted == []
    assert _count("console", "skipped", "new", "") == before + 1


def test_a_comparison_that_cannot_be_scheduled_never_reaches_the_site(monkeypatch):
    """By scope exit Python has already enqueued; an executor shut down at
    process exit must not turn that into an exception at the call site."""
    def shut(fn, *a):
        raise RuntimeError("cannot schedule new futures after shutdown")
    monkeypatch.setattr(ds, "_submit", shut)
    monkeypatch.setattr(ds, "_pending", 0)
    with _scope():
        ds.note("new", 1, dedupe_key="k")
    assert ds._pending == 0


def test_comparisons_run_off_the_enqueue_thread(monkeypatch):
    done = threading.Event()
    where = []

    def slow_decide(url, req, **kw):
        where.append(threading.current_thread().name)
        done.set()
        return {"decision": "new", "dedupe_key": "k"}
    monkeypatch.setattr(ds, "decide", slow_decide)
    with _scope():
        ds.note("new", 1, dedupe_key="k")
    assert done.wait(5)
    assert where[0].startswith("dedupe-shadow") and where[0] != threading.current_thread().name


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    reply = (200, {"decision": "new", "dedupe_key": "k", "reason": "no open row covers it"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append((self.path, self.headers.get(ds.AUTH_HEADER), body))
        code, out = _Handler.reply
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def service():
    _Handler.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_client_posts_the_request_with_the_shared_secret(service, monkeypatch):
    monkeypatch.setenv(ds.SHARED_SECRET_ENV, " s3cret ")
    _Handler.reply = (200, {"decision": "new", "dedupe_key": "k", "reason": "r"})
    out = ds.decide(service, {"candidate": {"dedupe_key": "k"}})
    assert out["decision"] == "new"
    assert _Handler.seen == [("/v1/decide", "s3cret", {"candidate": {"dedupe_key": "k"}})]


def test_client_sends_no_token_when_none_is_configured(service, monkeypatch):
    monkeypatch.delenv(ds.SHARED_SECRET_ENV, raising=False)
    _Handler.reply = (200, {"decision": "new", "reason": "r"})
    ds.decide(service, {"candidate": {}})
    assert _Handler.seen[0][1] is None


@pytest.mark.parametrize("reply, status", [
    ((401, {"error": "Missing X-CFOP-Token header"}), 401),
    ((200, {"error": "not a decision"}), 0),
    ((200, {"decision": "merge"}), 0),
])
def test_client_refuses_anything_but_a_decision(service, reply, status):
    _Handler.reply = reply
    with pytest.raises(ds.DedupeClientError) as e:
        ds.decide(service, {"candidate": {}})
    assert e.value.status == status


def test_client_reports_an_unreachable_service():
    with pytest.raises(ds.DedupeClientError) as e:
        ds.decide("http://127.0.0.1:9", {"candidate": {}}, timeout=2)
    assert e.value.status == 0


# ---------------------------------------------------------------------------
# The agent's switch
# ---------------------------------------------------------------------------

def test_the_url_comes_from_env_then_config(monkeypatch):
    op = MagicMock()
    op.config = {"remediation": {"dedupe": {"url": "http://cfg:8093/"}}}
    monkeypatch.delenv("CFOP_DEDUPE_URL", raising=False)
    assert CFOperator._dedupe_url(op) == "http://cfg:8093"
    monkeypatch.setenv("CFOP_DEDUPE_URL", " http://env:8093/ ")
    assert CFOperator._dedupe_url(op) == "http://env:8093"
    monkeypatch.delenv("CFOP_DEDUPE_URL")
    op.config = {"remediation": {"queue_feed": True}}
    assert CFOperator._dedupe_url(op) == ""


# ---------------------------------------------------------------------------
# Every enqueue site is inside the shadow
# ---------------------------------------------------------------------------

def _shadowed_calls(tree):
    """(lineno, shadowed) for each .queue_remediation( call under ``tree``."""
    out = []

    def visit(node, shadowed):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed = any(isinstance(d, ast.Call) and getattr(d.func, "id", "") == "_dedupe_shadowed"
                           for d in node.decorator_list)
        if isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "attr", "") == "_dedupe_shadow_scope"
                for i in node.items):
            shadowed = True
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "queue_remediation":
            out.append((node.lineno, shadowed))
        for child in ast.iter_child_nodes(node):
            visit(child, shadowed)
    visit(tree, False)
    return out


def _sources():
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO)
        if rel.parts[0] in (".venv", ".claude", "node_modules", "demo") or p.name.startswith("test_") \
                or "tests" in rel.parts:
            continue
        yield rel, p.read_text(errors="replace")


def test_every_enqueue_site_is_inside_a_shadow_scope():
    """A sixth enqueue site added without the scope would never be compared,
    and phase 3 would cut it over on no evidence at all."""
    calls, unshadowed = [], []
    for rel, src in _sources():
        if ".queue_remediation(" not in src:
            continue
        for line, shadowed in _shadowed_calls(ast.parse(src)):
            calls.append(f"{rel}:{line}")
            if not shadowed:
                unshadowed.append(f"{rel}:{line}")
    # The five CFOP-509 names: if this drops, the scan is broken, not the code.
    assert len(calls) >= 5, calls
    assert not unshadowed, f"enqueue outside a dedupe shadow scope: {unshadowed}"


def test_the_guard_sees_an_unshadowed_call():
    tree = ast.parse("def f(kb):\n    kb.queue_remediation('manual', {})\n")
    assert _shadowed_calls(tree) == [(2, False)]


# ---------------------------------------------------------------------------
# The sites outside the parity cases: what each asks
# ---------------------------------------------------------------------------

def _recording(log):
    @contextmanager
    def scope(path, details):
        log.append(("enter", path, ds.candidate_from(details)))
        yield None
        log.append(("exit", path))
    return scope


def test_the_sweep_fallback_asks_before_its_node_tier_and_enqueue():
    op = _feed_op()
    log = []
    op._dedupe_shadow_scope = _recording(log)
    op.kb.queue_remediation.side_effect = lambda **kw: log.append(("queue", kw["dedupe_key"])) or 5
    reports = [{"findings": [
        {"id": "f1", "finding": "Pi down", "remediation": "Physically inspect power strip",
         "severity": "critical", "resource_name": "raspberrypi4"},
    ]}]
    assert CFOperator._feed_remediations_from_sweeps(op, reports) == 1
    assert [e[0] for e in log] == ["enter", "queue", "exit"]
    (_, path, cand) = log[0]
    assert path == "sweep-fallback"
    assert cand == {"dedupe_key": "sweep-f1", "recommendation": "Physically inspect power strip",
                    "host": "raspberrypi4", "title": "Pi down", "trigger": "Pi down",
                    "remediation_class": "manual", "source": "morning-summary/sweep"}
