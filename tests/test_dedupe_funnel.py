"""Every remediation enqueue goes through the dedupe funnel (CFOP-509, CFOP-538).

Before CFOP-509 there were five enqueue sites and each ran its own subset of
the dedupe tiers, so every new site picked a different one and the duplicates
came through whichever door was weakest. Now cfop-dedupe decides for all of
them, and this file fails if a site stops asking:

- every ``.queue_remediation(`` call outside the tests sits under the enqueue
  lock (a ``with ..._remediation_lock:`` block, or a method decorated
  ``@_under_remediation_lock``), after a ``_dedupe_decide`` call in the same
  function, so the insert applies the answer to the rows that answer was
  about;
- nothing builds a ``RemediationQueue`` row except
  ``KnowledgeBase.queue_remediation`` and ``queue_confirmed_host_command``.
  The second is outside the funnel on purpose (CFOP-442): it records a
  command an admin has already confirmed and claimed, not a proposal.

It also pins the console's POST, the one site with no other test of its
wiring here.
"""

from repo_paths import REPO_ROOT
import ast
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

# agent.py imports its siblings bare, so the package directory goes on the path
# after the repo root, the same arrangement as test_console_provider_registry.
sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

_SKIP_DIRS = {".venv", ".claude", "node_modules", "demo", "tests", ".git"}
_ROW_BUILDERS = {("agent/knowledge_base.py", "queue_remediation"),
                 ("agent/knowledge_base.py", "queue_confirmed_host_command")}


def _sources():
    for p in REPO_ROOT.rglob("*.py"):
        rel = p.relative_to(REPO_ROOT)
        if _SKIP_DIRS & set(rel.parts) or p.name.startswith("test_") or p.name == "conftest.py":
            continue
        yield rel.as_posix(), p.read_text(errors="replace")


def _is_lock(expr):
    return isinstance(expr, ast.Attribute) and expr.attr == "_remediation_lock"


def _calls(node, attr):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == attr]


def enqueues_outside_the_funnel(src):
    """(line, why) for each ``.queue_remediation(`` call the funnel does not
    cover in this module's source."""
    out = []

    def visit(node, fn, locked):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node
            locked = any(getattr(d, "id", getattr(d, "attr", "")) == "_under_remediation_lock"
                         for d in node.decorator_list)
        if isinstance(node, ast.With) and any(_is_lock(i.context_expr) for i in node.items):
            locked = True
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "queue_remediation":
            if not locked:
                out.append((node.lineno, "not under _remediation_lock"))
            elif fn is None or not any(c.lineno < node.lineno for c in _calls(fn, "_dedupe_decide")):
                out.append((node.lineno, "no _dedupe_decide before it"))
        for child in ast.iter_child_nodes(node):
            visit(child, fn, locked)
    visit(ast.parse(src), None, False)
    return out


def rows_built(src):
    """(function, line) for each ``RemediationQueue(`` construction."""
    out = []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "RemediationQueue":
            out.append((fn, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)
    visit(ast.parse(src), None)
    return out


def test_every_enqueue_goes_through_the_funnel():
    calls, escaped = [], []
    for rel, src in _sources():
        if ".queue_remediation(" not in src:
            continue
        tree_calls = [n.lineno for n in _calls(ast.parse(src), "queue_remediation")]
        calls += [f"{rel}:{line}" for line in tree_calls]
        escaped += [f"{rel}:{line} ({why})" for line, why in enqueues_outside_the_funnel(src)]
    # The five CFOP-509 sites: if this drops, the scan is broken, not the code.
    assert len(calls) >= 5, calls
    assert not escaped, f"enqueue outside the dedupe funnel: {escaped}"


def test_only_the_knowledge_base_builds_a_row():
    built = []
    for rel, src in _sources():
        if "RemediationQueue(" in src:
            built += [(rel, fn) for fn, _ in rows_built(src)]
    assert set(built) == _ROW_BUILDERS, built


@pytest.mark.parametrize("src, line, why", [
    ("def f(self):\n    self.kb.queue_remediation('manual', {})\n", 2, "not under _remediation_lock"),
    ("def f(self):\n    with self._remediation_lock:\n        self.kb.queue_remediation('manual', {})\n",
     3, "no _dedupe_decide before it"),
    # Asked after the insert is not asked: the answer was not applied.
    ("def f(self):\n    with self._remediation_lock:\n        self.kb.queue_remediation('manual', {})\n"
     "        self._dedupe_decide('x', {})\n", 3, "no _dedupe_decide before it"),
])
def test_the_guard_sees_an_escaped_enqueue(src, line, why):
    assert enqueues_outside_the_funnel(src) == [(line, why)]


def test_the_guard_passes_a_funnelled_enqueue():
    assert enqueues_outside_the_funnel(
        "def f(self):\n    with self._remediation_lock:\n        d = self._dedupe_decide('x', {})\n"
        "        self.kb.queue_remediation('manual', {})\n") == []
    assert enqueues_outside_the_funnel(
        "@_under_remediation_lock\ndef f(self):\n    d = self._dedupe_decide('x', {})\n"
        "    self.kb.queue_remediation('manual', {})\n") == []


def test_the_guard_sees_another_row_builder():
    assert rows_built("def sneak(s):\n    s.add(RemediationQueue(status='queued'))\n") == [("sneak", 2)]


# ---------------------------------------------------------------------------
# The console's POST /api/remediations
# ---------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("CFOP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CFOP_AUTH_DB_DISABLED", "true")
    op = MagicMock()
    op.config = {}
    op._remediation_lock = threading.RLock()
    from web_server import WebServer
    ws = WebServer(op)
    ws.app.config["TESTING"] = True
    return ws, op


_BODY = {"recommendation": "Replace the SD card in raspberrypi3", "title": "rpi3 SD card",
         "host_id": "raspberrypi3", "dedupe_key": "manual-rpi3-sd"}


def test_the_console_asks_under_the_lock_and_gives_the_node_tier_no_host(server):
    """An operator's row is a request, not a symptom of a dead node: folding
    it into a node incident, or keying it node-down-<host> where the recovery
    sweep closes it, would lose what the operator asked for."""
    ws, op = server
    log = []

    class _Lock:
        def __enter__(self):
            log.append("lock")

        def __exit__(self, *a):
            log.append("unlock")
    op._remediation_lock = _Lock()

    def decide(path, cand, **kw):
        log.append(("decide", path, cand))
        return {"decision": "new", "dedupe_key": cand["dedupe_key"], "source": "service",
                "reason": "no open row covers it"}
    op._dedupe_decide = decide
    op.kb.queue_remediation.side_effect = lambda **kw: log.append("queue") or 12
    op.kb.get_remediation.return_value = {"id": 12}

    resp = ws.app.test_client().post("/api/remediations", json=_BODY)
    assert resp.status_code == 201, resp.get_json()
    assert [e if isinstance(e, str) else e[0] for e in log] == ["lock", "decide", "queue", "unlock"]
    assert log[1][1:] == ("console", {
        "dedupe_key": "manual-rpi3-sd", "recommendation": "Replace the SD card in raspberrypi3",
        "remediation_class": "manual", "source": "manual"})
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["host_id"] == "raspberrypi3" and kw["dedupe_key"] == "manual-rpi3-sd"
    assert kw["payload"]["dedupe"] == {"source": "service", "reason": "no open row covers it"}


@pytest.mark.parametrize("answer", [
    {"decision": "fold", "row_id": 40, "tier": "identifier", "absorbed": "rpi3 SD card",
     "reason": "same identifiers as open row #40", "source": "service"},
    {"decision": "suppress", "row_id": 40, "tier": "key",
     "reason": "open row #40 already carries manual-rpi3-sd", "source": "service"},
])
def test_a_console_row_the_service_says_is_queued_says_which(server, answer):
    ws, op = server
    op._dedupe_decide = lambda path, cand, **kw: answer
    resp = ws.app.test_client().post("/api/remediations", json=_BODY)
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "deduped", "decision": answer["decision"],
                               "remediation_id": 40, "reason": answer["reason"]}
    op.kb.queue_remediation.assert_not_called()
    if answer["decision"] == "fold":
        op._apply_dedupe_fold.assert_called_once_with(answer)
    else:
        op._apply_dedupe_fold.assert_not_called()
