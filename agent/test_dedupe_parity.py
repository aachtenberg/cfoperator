#!/usr/bin/env python3
"""The dedupe service's shared cases, run against the Python tiers (CFOP-510).

``dedupe/`` is a Go port of the dedupe tiers in this directory (CFOP-509):
the CFOP-71 node collapse, the CFOP-453/494 target fold, the CFOP-78
identifier fold and the knowledge base's exact-key check. Its cases live in
``dedupe/testdata/`` and its Go tests run them. This file runs the same files
against the code being ported, so a case that passes on one side and fails on
the other is a parity break, not an opinion about what the right answer is.

The Python side is driven through the real choke point,
``_maybe_queue_remediation``, and through ``_feed_remediations_from_summary``
for the cases marked ``"via": "summary"`` (the node tier reading the title,
CFOP-130). Only the knowledge base is faked, and its two queries are the
SQL's filters: non-terminal rows, newest first, the exact check over every
open row and the fold and alias scan over the newest 100.
"""

import ast
import json
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402
from agent.knowledge_base import payload_carries_dedupe_key  # noqa: E402
from test_remediation_queue import _confirming_judge, _feed_op, _wire_flags  # noqa: E402

TESTDATA = Path(__file__).resolve().parents[1] / "dedupe" / "testdata"
DECIDE_FILES = sorted((TESTDATA / "decide").glob("*.json"))
FUNCTIONS = json.loads((TESTDATA / "functions.json").read_text(encoding="utf-8"))

_TERMINAL = ("resolved", "rejected")
_NEW_ID = 999_999
_FOLD_LOG = re.compile(r"Folding (target|repeat) remediation onto open #(\d+) \(shared: (.*)\)$")


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


class _KB:
    """The queries the tiers make, over one case's rows."""

    def __init__(self, rows):
        self.rows = [r for r in rows if r.get("status") not in _TERMINAL]
        self.absorbed, self.aliases = [], []
        self.queued = self.refused = None

    def _own(self, key):
        for r in self.rows:
            if (r.get("payload") or {}).get("dedupe_key") == key:
                return r
        return None

    def list_open_remediations(self, limit=100):
        return [{"id": r["id"], "payload": r["payload"]} for r in self.rows][:limit]

    def find_open_remediation_by_dedupe_key(self, key):
        if not key:
            return None
        row = self._own(key)
        if row is None:
            row = next((r for r in self.rows[:100]
                        if payload_carries_dedupe_key(r.get("payload"), key)), None)
        return dict(row) if row else None

    def queue_remediation(self, **kw):
        row = self._own(kw.get("dedupe_key")) if kw.get("dedupe_key") else None
        if row:
            self.refused = (row["id"], kw)
            return None
        self.queued = kw
        return _NEW_ID

    def record_remediation_absorbed(self, rid, summary):
        self.absorbed.append((rid, summary))
        return True

    def record_remediation_alias(self, rid, key):
        self.aliases.append((rid, key))
        return True


def _wire_nodes(op, request):
    notready = (request.get("vocabulary") or {}).get("notready_nodes") or []
    op.tools.k8s_tools.get_nodes.return_value = {
        "success": True, "nodes": [{"name": n, "ready": "False"} for n in notready]}
    op._normalize_host = CFOperator._normalize_host
    op._notready_nodes = lambda: CFOperator._notready_nodes(op)
    op._node_incident_dedupe_key = CFOperator._node_incident_dedupe_key
    op._collapse_key_for_node_incident = (
        lambda d: CFOperator._collapse_key_for_node_incident(op, d))
    node_folds = []

    def record(key, details):
        rid = CFOperator._record_absorbed_symptom(op, key, details)
        if rid:
            node_folds.append((rid, key))
        return rid
    op._record_absorbed_symptom = record
    op.kb = _KB(request.get("open_rows") or [])
    return node_folds


def _via_investigation(c, request):
    assert "title" not in c, (
        "the investigation path never reads a title; mark the case via=summary")
    op = _confirming_judge(_wire_flags(MagicMock()))
    op.config = {"remediation": {"queue_feed": True}}
    op._count_enqueued = MagicMock()
    node_folds = _wire_nodes(op, request)
    details = {"remediation_class": c.get("remediation_class") or "manual",
               "risk": "high", "confidence": None,
               "recommendation": c.get("recommendation"), "host": c.get("host"),
               "dedupe_key": c.get("dedupe_key"), "trigger": c.get("trigger"),
               "source": c.get("source")}
    for key in ("targets", "fold_targets"):
        if c.get(key) is not None:
            details[key] = c[key]
    rid = CFOperator._maybe_queue_remediation(op, None, details)
    return op, node_folds, rid


def _via_summary(c, request):
    title, rec = c["title"], c["recommendation"]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    assert c["dedupe_key"] == f"summary-{slug}", "the summary feed keys a rec on its title"
    assert c.get("trigger") == title, "the summary feed records its title as the absorbed text"
    assert not c.get("targets") and not c.get("fold_targets"), "the summary feed has no FIX"
    assert not CFOperator._recommendation_is_investigate_shaped(rec), (
        "an investigate-shaped rec is dispatched, not enqueued")
    op = _feed_op()
    node_folds = _wire_nodes(op, request)
    block = {"recommendations": [{"title": title, "recommendation": rec, "host": c.get("host"),
                                  "remediation_class": "manual", "risk": "low",
                                  "confidence": 0.5}]}
    CFOperator._feed_remediations_from_summary(
        op, "## Summary\n```json\n" + json.dumps(block) + "\n```\n", [])
    op.enqueue_investigation.assert_not_called()
    return op, node_folds, None


@pytest.mark.parametrize("case", DECIDE_CASES)
def test_python_decides_each_shared_case_the_same_way(case, monkeypatch, caplog):
    request, want = case["request"], case["want"]
    c = request["candidate"]
    folded = MagicMock()
    monkeypatch.setattr(agent_mod, "REMEDIATION_FOLDED", folded)
    caplog.set_level(logging.INFO, logger="cfoperator")

    run = _via_summary if case.get("via") == "summary" else _via_investigation
    op, node_folds, rid = run(c, request)
    kb = op.kb

    got = {"decision": None, "row_id": None, "tier": "", "dedupe_key": "",
           "absorbed": "", "alias": "", "shared": []}
    if kb.queued is not None:
        got.update(decision="new", dedupe_key=kb.queued.get("dedupe_key") or "")
        # host_id is the node the collapse matched, or the host as given.
        expected_host = want.get("host") or str(c.get("host") or "default")[:64]
        assert kb.queued["host_id"] == expected_host
        if want.get("host"):
            assert kb.queued["payload"]["target"]["host"] == want["host"]
        assert kb.queued["payload"].get("dedupe_key") == (kb.queued.get("dedupe_key") or None)
        assert rid in (None, _NEW_ID)
    elif kb.refused is not None:
        row_id, kw = kb.refused
        got.update(decision="suppress", row_id=row_id, tier="key",
                   dedupe_key=kw.get("dedupe_key") or "")
        assert rid is None
    elif node_folds:
        row_id, node_key = node_folds[0]
        # The node the incident is keyed on; the summary feed's host rewrite
        # happens on a dict this test cannot see, so read it off the key.
        got.update(decision="fold", row_id=row_id, tier="node",
                   shared=[node_key.removeprefix("node-down-")])
    else:
        reasons = [call.kwargs.get("reason") for call in folded.labels.call_args_list]
        reasons = [r for r in reasons if r in ("target", "repeat")]
        assert len(reasons) == 1, f"no tier decided and nothing was enqueued: {reasons}"
        logged = [m for m in (_FOLD_LOG.search(r.getMessage()) for r in caplog.records) if m]
        assert len(logged) == 1
        got.update(decision="fold", row_id=rid,
                   tier="target" if reasons[0] == "target" else "identifier",
                   shared=ast.literal_eval(logged[0].group(3)))
        assert int(logged[0].group(2)) == rid

    if got["decision"] == "fold":
        absorbed = [s for r, s in kb.absorbed if r == got["row_id"]]
        got["absorbed"] = absorbed[0] if absorbed else ""
        aliases = [k for r, k in kb.aliases if r == got["row_id"]]
        got["alias"] = aliases[0] if aliases else ""
        assert len(kb.absorbed) <= 1 and len(kb.aliases) <= 1
    else:
        assert not kb.absorbed and not kb.aliases, "only a fold writes onto another row"

    full_want = {"decision": want["decision"], "row_id": want.get("row_id"),
                 "tier": want.get("tier", ""), "dedupe_key": want.get("dedupe_key", ""),
                 "absorbed": want.get("absorbed", ""), "alias": want.get("alias", ""),
                 "shared": want.get("shared", [])}
    if got["decision"] == "fold":
        # A fold does not choose a key for anything; the Go response omits it.
        got["dedupe_key"] = ""
    assert got == full_want


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
    details = {"host": case["host"]}
    key = CFOperator._collapse_key_for_node_incident(_nodes_op(case["notready"]), details)
    assert key == case["want_key"]
    assert (details["host"] if key else None) == case["want_node"]


@pytest.mark.parametrize("case", FUNCTIONS["identifiers"], ids=repr)
def test_identifiers(case):
    assert sorted(CFOperator._extract_remediation_identifiers(case["in"])) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["normalize_target_id"], ids=repr)
def test_normalize_target_id(case):
    assert agent_mod._normalize_target_id(case["in"]) == case["want"]


@pytest.mark.parametrize("case", FUNCTIONS["targets_contained"], ids=repr)
def test_targets_contained(case):
    identity = agent_mod._target_identity
    assert agent_mod._targets_contained(identity(case["a"]), identity(case["b"])) == case["want"]
