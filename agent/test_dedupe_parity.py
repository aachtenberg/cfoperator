#!/usr/bin/env python3
"""The dedupe service's shared cases, from the agent's side (CFOP-510, CFOP-538).

cfop-dedupe (``dedupe/``) decides every remediation enqueue. Its cases live in
``dedupe/testdata/`` and its Go tests run them through ``Decide``. This file
runs the same files twice over:

1. Through ``dedupe_reference.decide``, the Python model of the service that
   every other agent test is answered by (agent/conftest.py). It must give
   each case's answer exactly as the Go tests require it, reason aside, or a
   test passing against the model would prove nothing about the service.
2. Through the agent's real enqueue sites, ``_maybe_queue_remediation`` and
   ``_feed_remediations_from_summary`` for the cases marked
   ``"via": "summary"`` (the node tier reading the title, CFOP-130), with the
   case's answer standing in for the service. The request the agent sends
   must be the case's request, and applying the answer must write what it
   says: a new row under its key and host, a fold's absorbed line and alias
   on its row, nothing for a repeat. The Go tests prove the service gives
   that answer for that request, so the two files together cover the funnel
   end to end without a Go binary here.

Only the knowledge base's queries are faked, as the SQL's filters. The insert
itself is the real ``KnowledgeBase.queue_remediation`` over a fake session, so
its exact-key backstop runs too.

With CFOP_DEDUPE_LIVE_URL set, a running cfop-dedupe answers part 2 instead
of the case (CFOP_DEDUPE_SHARED_SECRET is sent as the token, if set):
  (cd dedupe && CFOP_DEDUPE_PORT=18093 go run ./cmd/cfop-dedupe) &
  CFOP_DEDUPE_LIVE_URL=http://127.0.0.1:18093 pytest agent/test_dedupe_parity.py
"""

import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402
import dedupe_client  # noqa: E402
import dedupe_reference  # noqa: E402
from knowledge_base import KnowledgeBase  # noqa: E402
from test_remediation_queue import _confirming_judge, _feed_op, _wire_flags  # noqa: E402

# Taken before agent/conftest.py swaps in the model, for the live-service run.
_REAL_DECIDE = dedupe_client.decide

TESTDATA = Path(__file__).resolve().parents[1] / "dedupe" / "testdata"
DECIDE_FILES = sorted((TESTDATA / "decide").glob("*.json"))
FUNCTIONS = json.loads((TESTDATA / "functions.json").read_text(encoding="utf-8"))

_TERMINAL = ("resolved", "rejected")
_NEW_ID = 999_999


def _decide_cases():
    out = []
    for path in DECIDE_FILES:
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            out.append(pytest.param(case, id=f"{path.stem}: {case['name']}"))
    return out


DECIDE_CASES = _decide_cases()


def test_the_shared_cases_are_there():
    """A moved or emptied testdata/ must not pass as "every case agreed"."""
    assert len(DECIDE_FILES) >= 8, f"found {len(DECIDE_FILES)} case files under {TESTDATA}"
    assert len(DECIDE_CASES) >= 50


# ---------------------------------------------------------------------------
# 1. The model answers as the service does
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", DECIDE_CASES)
def test_the_model_decides_each_shared_case_as_the_service_does(case):
    got = dedupe_reference.decide(case["request"])
    assert got.pop("reason"), "a decision always carries a reason"
    assert got == case["want"]


# ---------------------------------------------------------------------------
# 2. The agent asks the case's question and applies its answer
# ---------------------------------------------------------------------------

class _KB:
    """The queries the funnel makes, over one case's rows."""

    def __init__(self, rows):
        self.rows = [r for r in rows if r.get("status") not in _TERMINAL]
        self.absorbed, self.aliases = [], []
        self.queued = self.refused = None

    def _own(self, key):
        for r in self.rows:
            if (r.get("payload") or {}).get("dedupe_key") == key:
                return r
        return None

    def list_open_remediations_for_dedupe(self):
        return [{"id": r["id"], "status": r["status"], "payload": r["payload"]} for r in self.rows]

    def get_remediation(self, rid):
        return next((dict(r) for r in self.rows if r["id"] == rid), None)

    def queue_remediation(self, **kw):
        """The real enqueue, over a session whose one query is the own-key
        check, so the backstop behind the service is the real code."""
        row = self._own(kw.get("dedupe_key")) if kw.get("dedupe_key") else None

        class _Query:
            def filter(self, *a):
                return self

            def first(self):
                return (row["id"],) if row else None

        class _Session:
            item = None

            def query(self, *a):
                return _Query()

            def add(self, item):
                self.item = item

            def flush(self):
                self.item.id = _NEW_ID

        @contextmanager
        def session_scope():
            yield _Session()

        real = KnowledgeBase.__new__(KnowledgeBase)
        real.session_scope = session_scope
        rid = real.queue_remediation(**kw)
        if row:
            assert rid is None
            self.refused = (row["id"], kw)
            return None
        assert rid == _NEW_ID
        self.queued = kw
        return rid

    def record_remediation_absorbed(self, rid, summary):
        self.absorbed.append((rid, summary))
        return True

    def record_remediation_alias(self, rid, key):
        self.aliases.append((rid, key))
        return True


def _wire(op, request):
    notready = (request.get("vocabulary") or {}).get("notready_nodes") or []
    op._notready_nodes = lambda: set(notready)
    op.kb = _KB(request.get("open_rows") or [])
    dedupe_reference.wire(op)


def _via_investigation(c, request):
    assert "title" not in c, (
        "the investigation path never reads a title; mark the case via=summary")
    op = _confirming_judge(_wire_flags(MagicMock()))
    op.config = {"remediation": {"queue_feed": True}}
    op._count_enqueued = MagicMock()
    op._stamp_opened_pr = MagicMock()
    _wire(op, request)
    details = {"remediation_class": c.get("remediation_class") or "manual",
               "risk": "high", "confidence": None,
               "recommendation": c.get("recommendation"), "host": c.get("host"),
               "dedupe_key": c.get("dedupe_key"), "trigger": c.get("trigger"),
               "source": c.get("source")}
    for key in ("targets", "fold_targets"):
        if c.get(key) is not None:
            details[key] = c[key]
    rid = CFOperator._maybe_queue_remediation(op, None, details)
    return op, rid


def _via_summary(c, request):
    title, rec = c["title"], c["recommendation"]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    assert c["dedupe_key"] == f"summary-{slug}", "the summary feed keys a rec on its title"
    assert c.get("trigger") == title, "the summary feed records its title as the absorbed text"
    assert not c.get("targets") and not c.get("fold_targets"), "the summary feed has no FIX"
    assert not CFOperator._recommendation_is_investigate_shaped(rec), (
        "an investigate-shaped rec is dispatched, not enqueued")
    op = _feed_op()
    _wire(op, request)
    block = {"recommendations": [{"title": title, "recommendation": rec, "host": c.get("host"),
                                  "remediation_class": "manual", "risk": "low",
                                  "confidence": 0.5}]}
    CFOperator._feed_remediations_from_summary(
        op, "## Summary\n```json\n" + json.dumps(block) + "\n```\n", [])
    op.enqueue_investigation.assert_not_called()
    return op, None


# The candidate fields Decide reads (dedupe/decide.go); remediation_class and
# source ride along for the logs and decide nothing.
_DECIDED_ON = ("dedupe_key", "recommendation", "targets", "fold_targets", "host", "title", "trigger")


def _decided_on(candidate):
    return {k: candidate.get(k) or None for k in _DECIDED_ON}


@pytest.mark.parametrize("case", DECIDE_CASES)
def test_the_agent_asks_each_shared_case_and_applies_its_answer(case, monkeypatch):
    request, want = case["request"], case["want"]
    c = request["candidate"]
    sent = []
    live = os.environ.get("CFOP_DEDUPE_LIVE_URL")

    def service(url, req, **kw):
        sent.append(json.loads(json.dumps(req)))
        return _REAL_DECIDE(live, req) if live else {**want, "reason": "the case's answer"}

    monkeypatch.setattr(dedupe_client, "decide", service)
    run = _via_summary if case.get("via") == "summary" else _via_investigation
    op, rid = run(c, request)
    kb = op.kb

    # The question: the case's candidate, the open rows as the SQL returns
    # them, and the NotReady set whenever the node tier has a host to read.
    (asked,) = sent
    assert _decided_on(asked["candidate"]) == _decided_on(c)
    assert asked["open_rows"] == dedupe_client.trim_rows(
        r for r in request.get("open_rows") or [] if r.get("status") not in _TERMINAL)
    notready = sorted({n.lower() for n in (request.get("vocabulary") or {}).get("notready_nodes") or []})
    reads_nodes = bool(agent_mod._host_candidates(c.get("host") or c.get("title")))
    assert asked["vocabulary"]["notready_nodes"] == (notready if reads_nodes else [])

    # The answer, applied.
    if want["decision"] == "new":
        assert kb.queued is not None and kb.refused is None
        assert (kb.queued.get("dedupe_key") or "") == want.get("dedupe_key", "")
        assert kb.queued["payload"].get("dedupe_key") == (want.get("dedupe_key") or None)
        # host_id is the node the collapse matched, or the host as given.
        assert kb.queued["host_id"] == (want.get("host") or str(c.get("host") or "default")[:64])
        if want.get("host"):
            assert kb.queued["payload"]["target"]["host"] == want["host"]
        assert kb.queued["payload"]["dedupe"]["source"] == "service"
        assert rid in (None, _NEW_ID)
    else:
        assert kb.queued is None and kb.refused is None, "a fold or a repeat inserts nothing"
    if want["decision"] == "fold":
        assert kb.absorbed == ([(want["row_id"], want["absorbed"])] if want.get("absorbed") else [])
        assert kb.aliases == ([(want["row_id"], want["alias"])] if want.get("alias") else [])
        if case.get("via") != "summary":
            assert rid == want["row_id"], "the caller links to the surviving row"
    else:
        assert not kb.absorbed and not kb.aliases, "only a fold writes onto another row"
    if want["decision"] == "suppress":
        assert rid is None, "a repeat answers None, as queue_remediation always has"


# ---------------------------------------------------------------------------
# functions.json: the helpers each tier is built from
# ---------------------------------------------------------------------------

def _nodes_op(notready):
    op = MagicMock()
    op.tools.k8s_tools.get_nodes.return_value = {
        "success": True, "nodes": [{"name": n, "ready": "False"} for n in notready]}
    op._normalize_host = CFOperator._normalize_host
    op._notready_nodes = lambda: CFOperator._notready_nodes(op)
    op._node_incident_dedupe_key = CFOperator._node_incident_dedupe_key
    return op


def test_every_function_section_has_cases():
    for section in ("normalize_host", "host_candidates", "match_known_host", "collapse_key",
                    "identifiers", "normalize_target_id", "targets_contained"):
        assert FUNCTIONS.get(section), f"functions.json has no {section} cases"


@pytest.mark.parametrize("case", FUNCTIONS["normalize_host"], ids=repr)
def test_normalize_host(case):
    assert CFOperator._normalize_host(case["in"]) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["host_candidates"], ids=repr)
def test_host_candidates(case):
    assert agent_mod._host_candidates(case["in"]) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["match_known_host"], ids=repr)
def test_match_known_host(case):
    assert agent_mod._match_known_host(case["token"], case["known"]) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["collapse_key"], ids=repr)
def test_collapse_key(case):
    """The agent's own copy, kept for the CFOP-108 checklist veto, still
    agrees with the service's node tier."""
    details = {"host": case["host"]}
    key = CFOperator._collapse_key_for_node_incident(_nodes_op(case["notready"]), details)
    assert key == case["want_key"]
    assert (details["host"] if key else None) == case["want_node"]


@pytest.mark.parametrize("case", FUNCTIONS["identifiers"], ids=repr)
def test_identifiers(case):
    assert sorted(dedupe_reference.extract_identifiers(case["in"])) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["normalize_target_id"], ids=repr)
def test_normalize_target_id(case):
    assert dedupe_reference.normalize_target_id(case["in"]) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["targets_contained"], ids=repr)
def test_targets_contained(case):
    identity = dedupe_reference.target_identity
    assert dedupe_reference.targets_contained(identity(case["a"]), identity(case["b"])) == case["want"]
