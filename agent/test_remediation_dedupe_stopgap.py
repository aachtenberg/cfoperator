#!/usr/bin/env python3
"""The CFOP-78 stopgap: repeat folding and fork commitment.

On 2026-08-23 the live queue held six needs-human rows for three problems —
the same CFOP_RUNTIME_TOKEN fix three times (grok-4.6 twice, gemma4 once), the
same stale Cloudflare rule twice, and one genuinely new row whose
recommendation was a fork nothing could execute. The dedupe key's fallback
tier SHA-1s the recommendation sentence, and two models never produce the same
sentence.

The fixtures below are those six rows' actual recommendation texts, verbatim.
That is deliberate: the guard being tested is "the rewordings real models
actually produce fold onto each other", and paraphrasing the fixtures would
test my rewording instead.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from agent.agent import _FORK_SHAPED  # noqa: E402

# --- the six live rows, verbatim -------------------------------------------

REC_67 = ("Add `CFOP_RUNTIME_TOKEN` to sealed `cfoperator-secrets` and mount it "
          "on `k3s/base/apps/cfoperator-event-runtime.yml` (`secretKeyRef: "
          "{name: cfoperator-secrets, key: CFOP_RUNTIME_TOKEN}`), then "
          "ArgoCD-sync/rollout so the startup WARNING is gone.")
REC_69 = ("Add `CFOP_RUNTIME_TOKEN` to sealed "
          "`k3s/base/apps/sealed-secrets/cfoperator-secrets.yml` and mount it on "
          "the event-runtime Deployment (`secretKeyRef: {name: "
          "cfoperator-secrets, key: CFOP_RUNTIME_TOKEN}`), then "
          "ArgoCD-sync/rollout until the startup WARNING is gone.")
REC_71 = ("Add `CFOP_RUNTIME_TOKEN` to `cfoperator-secrets` and update the "
          "`cfoperator-event-runtime` Deployment to inject it, then restart "
          "the deployment.")
REC_68 = ("In Cloudflare Zero Trust → Networks → Tunnels → this tunnel, delete "
          "or retarget Public Hostname ingressRule=1 away from "
          "http://192.168.0.131:80; keep only reachable origins (e.g. freshet "
          "via Traefik).")
REC_70 = ("Remove or retarget the Cloudflare Zero Trust ingress rule for "
          "`192.168.0.131:80` in the Cloudflare Dashboard.")
REC_72 = ("Truncate or chunk the text content for `learning_id 2368` in the "
          "underlying database to fit within the model's context window, or "
          "update the `embedding_service` configuration to use a model with a "
          "larger context window.")

extract = CFOperator._extract_remediation_identifiers


# ---------------------------------------------------------------------------
# identifier extraction
# ---------------------------------------------------------------------------

def test_env_names_ips_and_row_ids_are_extracted():
    assert "CFOP_RUNTIME_TOKEN" in extract(REC_67)
    # The port STAYS: this cluster runs many services per address, so the
    # stale tunnel rule for :80 and a broken service on :22 of the same box
    # are different problems. (PR #169 review — the first version stripped
    # it, and the test pinned the weaker choice.)
    assert "192.168.0.131:80" in extract(REC_68)
    assert "learning_id:2368" in extract(REC_72)


def test_the_same_address_on_different_ports_is_two_problems():
    a = extract("Retarget the tunnel rule for 192.168.0.131:80")
    b = extract("Fix the dead ssh origin 192.168.0.131:22")
    assert not (a <= b or b <= a)


def test_prose_and_dash_words_extract_nothing():
    """The reason hostnames/workloads are excluded: dash-words collide with
    ordinary prose, and a false fold hides a real incident."""
    assert extract("Restart the needs-human worker on grok-4.6 after the "
                   "HTTP 500 STATUS ERROR; see RECOMMENDATION above") == frozenset()
    assert extract("") == frozenset()
    assert extract(None) == frozenset()


def test_the_live_token_rows_share_identifiers():
    """#67/#69/#71: two models, three wordings, one fix. Containment either
    way is the fold rule, so the tightest row must still match the fullest."""
    a, b, c = extract(REC_67), extract(REC_69), extract(REC_71)
    for x, y in [(a, b), (a, c), (b, c)]:
        assert x <= y or y <= x, f"{sorted(x)} vs {sorted(y)}"


def test_the_live_tunnel_rows_share_identifiers():
    """Both live rows spell the port, so keeping it still folds them."""
    a, b = extract(REC_68), extract(REC_70)
    assert a & b == {"192.168.0.131:80"}
    assert a <= b or b <= a


def test_distinct_problems_do_not_contain_each_other():
    """#72 must never fold onto the token rows or the tunnel rows."""
    embedding = extract(REC_72)
    for other in (REC_67, REC_68):
        ids = extract(other)
        assert not (embedding <= ids or ids <= embedding)


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------

def _op(open_rows):
    op = MagicMock()
    op._extract_remediation_identifiers = extract
    op.kb.list_open_remediations.return_value = open_rows
    op.kb.record_remediation_absorbed.return_value = True
    return op


def _row(rid, rec, key="inv-aaaa"):
    return {"id": rid, "payload": {"dedupe_key": key, "recommendation": rec}}


def _details(rec, key="inv-bbbb", trigger="event-runtime fail-open"):
    return {"recommendation": rec, "dedupe_key": key, "trigger": trigger}


def test_a_reworded_repeat_folds_onto_the_open_row():
    op = _op([_row(67, REC_67)])
    rid = CFOperator._absorb_repeat_remediation(op, _details(REC_69))
    assert rid == 67
    op.kb.record_remediation_absorbed.assert_called_once()
    summary = op.kb.record_remediation_absorbed.call_args[0][1]
    assert "event-runtime fail-open" in summary, (
        "the fold must be visible on the surviving row, not silent")


def test_a_third_model_rewording_still_folds():
    """gemma4's #71 against grok's #67 — the cross-model case that actually
    happened."""
    op = _op([_row(67, REC_67)])
    assert CFOperator._absorb_repeat_remediation(op, _details(REC_71)) == 67


def test_a_distinct_problem_does_not_fold():
    op = _op([_row(67, REC_67), _row(68, REC_68)])
    assert CFOperator._absorb_repeat_remediation(op, _details(REC_72)) is None
    op.kb.record_remediation_absorbed.assert_not_called()


def test_no_identifiers_means_no_fold_not_fold_onto_everything():
    """An empty set is a subset of every set. If the guard on the NEW side
    ever goes, a vague recommendation folds onto whatever row happens to be
    newest — silently discarding a real finding."""
    op = _op([_row(67, REC_67)])
    assert CFOperator._absorb_repeat_remediation(
        op, _details("Investigate the elevated latency further")) is None
    op.kb.list_open_remediations.assert_not_called()


def test_an_open_row_with_no_identifiers_is_never_a_fold_target():
    """Same trap from the other side: the OPEN row's empty set is a subset of
    the new row's identifiers."""
    op = _op([_row(50, "Investigate the elevated latency further")])
    assert CFOperator._absorb_repeat_remediation(op, _details(REC_67)) is None


def test_a_shared_identifier_alone_is_not_the_same_problem():
    """The conservative line: containment, not intersection. Two rows that
    each name something the other does not are two problems that happen to
    touch the same object — folding them hides one. This is the test that
    makes loosening `<=` to `&` fail."""
    op = _op([_row(60, "Rotate `CFOP_RUNTIME_TOKEN` and `CFOP_API_TOKEN` "
                      "after the leak on 10.0.0.14")])
    rid = CFOperator._absorb_repeat_remediation(
        op, _details("Add `CFOP_RUNTIME_TOKEN` to sealed secrets and mount it "
                     "at 192.168.0.150"))
    assert rid is None
    op.kb.record_remediation_absorbed.assert_not_called()


def test_an_exact_key_repeat_is_left_to_the_existing_path():
    """The KB's enqueue-side suppression already handles same-key rows and the
    caller links via _open_remediation_for_key; folding here would grow the
    payload for a case that is not broken."""
    op = _op([_row(67, REC_67, key="inv-same")])
    assert CFOperator._absorb_repeat_remediation(
        op, _details(REC_69, key="inv-same")) is None


def test_the_fold_fails_open():
    """A dedupe optimisation must never be the reason a real problem goes
    unrecorded — the CFOP-71 posture, inherited deliberately."""
    op = _op([])
    op.kb.list_open_remediations.side_effect = RuntimeError("db down")
    assert CFOperator._absorb_repeat_remediation(op, _details(REC_69)) is None


# ---------------------------------------------------------------------------
# fork detection
# ---------------------------------------------------------------------------

def test_the_live_fork_is_detected():
    assert _FORK_SHAPED.search(REC_72)


def test_two_verbs_one_target_is_not_a_fork():
    """#68's "delete or retarget" and #70's "Remove or retarget" are wording,
    not alternatives — same rule, either verb. Flagging them would send half
    the queue through the rewrite."""
    assert not _FORK_SHAPED.search(REC_68)
    assert not _FORK_SHAPED.search(REC_70)


def test_ordinary_recommendations_are_not_forks():
    for rec in (REC_67, REC_69, REC_71,
                "Patch the Promtail DaemonSet memory limit from 256Mi to 512Mi "
                "via GitOps."):
        assert not _FORK_SHAPED.search(rec), rec


def test_alternatively_opens_a_fork():
    assert _FORK_SHAPED.search("Restart the pod. Alternatively, raise the "
                               "memory limit.")


# ---------------------------------------------------------------------------
# the commit retry
# ---------------------------------------------------------------------------

def _commit(op, rec, response=None, raises=None):
    if raises is not None:
        op._chat_with_tools_with_fallback = MagicMock(side_effect=raises)
    else:
        op._chat_with_tools_with_fallback = MagicMock(
            return_value={'response': response})
    return CFOperator._commit_forked_recommendation(op, "trigger", rec, report="r")


def test_a_non_fork_passes_through_without_an_llm_call():
    op = MagicMock()
    op._chat_with_tools_with_fallback = MagicMock()
    text, forked = CFOperator._commit_forked_recommendation(op, "t", REC_67)
    assert (text, forked) == (REC_67, False)
    op._chat_with_tools_with_fallback.assert_not_called()


def test_a_fork_that_commits_is_replaced():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response=(
        "Truncate the text content for `learning_id 2368` in the learnings "
        "table so it fits the embedding model's context window."))
    assert forked is False
    assert "learning_id 2368" in text
    assert not _FORK_SHAPED.search(text)


def test_a_retry_that_still_forks_keeps_the_original_and_flags_it():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response=(
        "Truncate the row, or update the configuration to a larger model."))
    assert (text, forked) == (REC_72, True)


def test_an_llm_failure_keeps_the_original_and_flags_it():
    op = MagicMock()
    text, forked = _commit(op, REC_72, raises=RuntimeError("no backend"))
    assert (text, forked) == (REC_72, True)


def test_an_empty_rewrite_is_not_a_commitment():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response="")
    assert (text, forked) == (REC_72, True)


# ---------------------------------------------------------------------------
# the auto-gate cap on a stuck fork
# ---------------------------------------------------------------------------

def _feed_op(*, still_forked, confidence=0.9):
    op = MagicMock()
    op._remediation_flag = lambda name: name == 'queue_feed'
    op._commit_forked_recommendation = lambda t, r, report='': (r, still_forked)
    op._classify_needs_action_recommendation = MagicMock(return_value={
        'remediation_class': 'gitops-patch', 'risk': 'low',
        'confidence': confidence, 'host': None, 'repo': 'aachtenberg/x'})
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = MagicMock(return_value=42)
    return op


def test_a_stuck_fork_cannot_clear_the_auto_gate():
    """Live row #72 was gitops-patch at 0.80 — one 'low' risk away from the
    executor opening a PR against a repo unrelated to the fix. A fork that
    will not commit must never be executable, whatever the classifier said."""
    op = _feed_op(still_forked=True)
    rid = CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_72, "report", "ollama/gemma4:26b")
    assert rid == 42
    details = op._maybe_queue_remediation.call_args[0][1]
    assert details['confidence'] is None
    assert details['remediation_class'] == 'gitops-patch', (
        "the class stays honest; only the gate is barred")


def test_a_committed_recommendation_keeps_its_confidence():
    op = _feed_op(still_forked=False)
    CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_67, "report", "xai/grok-4.6")
    details = op._maybe_queue_remediation.call_args[0][1]
    assert details['confidence'] == 0.9


def test_the_committed_text_is_what_everything_downstream_sees():
    """The classifier and the dedupe key must both read the committed action —
    the whole point is that nothing downstream reverse-engineers the fork."""
    committed = "Truncate the text for `learning_id 2368` in learnings."
    op = _feed_op(still_forked=False)
    op._commit_forked_recommendation = lambda t, r, report='': (committed, False)
    CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_72, "report", "ollama/gemma4:26b")
    classified = op._classify_needs_action_recommendation.call_args[0][1]
    details = op._maybe_queue_remediation.call_args[0][1]
    assert classified == committed
    assert details['recommendation'] == committed
    assert details['dedupe_key'] == CFOperator._investigation_dedupe_key(
        {}, committed)


# ---------------------------------------------------------------------------
# the choke point (PR #169 review)
# ---------------------------------------------------------------------------

from test_remediation_queue import (  # noqa: E402
    _confirming_judge, _no_node_incident, _wire_flags)


def _choke_op(**flags):
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": True, **flags}}
    op.kb.queue_remediation.return_value = 99
    op.kb.list_open_remediations.return_value = []
    return op


def test_the_fold_is_skipped_while_the_node_collapse_owns_the_row():
    """The first NotReady symptom arrives before any incident row exists. If
    the identifier fold runs on it, a rec naming 192.168.0.131 while the
    tunnel row is open is absorbed THERE — and the node-incident row is never
    created. The node tier owns the row outright."""
    op = _choke_op()
    op._collapse_key_for_node_incident = lambda details: "node-down-raspberrypi4"
    op._record_absorbed_symptom = lambda key, details: None   # no incident row yet
    op.kb.list_open_remediations.return_value = [
        _row(68, REC_68)]  # an open row sharing the IP
    fold_calls = []
    real = CFOperator._absorb_repeat_remediation
    op._absorb_repeat_remediation = (
        lambda details: fold_calls.append(1) or real(op, details))
    rid = CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "recommendation": "Restore connectivity to 192.168.0.131:80",
        "host": "raspberrypi4", "dedupe_key": "inv-fresh"})
    assert fold_calls == [], "the identifier fold ran on a node-incident row"
    assert rid == 99, "the incident row must still be created"


def test_a_fork_reaching_the_choke_point_cannot_carry_confidence():
    """The rewrite lives in the investigation feed; the CAP lives here, so a
    feed that never called the rewrite — sweep, deep-investigation, whatever
    comes third — still cannot hand the executor a fork."""
    op = _choke_op()
    CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "recommendation": REC_72, "host": None, "dedupe_key": "inv-fork"})
    assert op.kb.queue_remediation.call_args.kwargs["confidence"] is None


def test_a_committed_recommendation_keeps_confidence_at_the_choke_point():
    op = _choke_op()
    CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "recommendation": REC_67, "host": None, "dedupe_key": "inv-ok"})
    assert op.kb.queue_remediation.call_args.kwargs["confidence"] == 0.9
