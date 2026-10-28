#!/usr/bin/env python3
"""The dedupe model step (CFOP-545): the prompt and parser, and the funnel
applying the model's answer.

What has to hold: the model is asked only when the tiers said new, never
under ``_remediation_lock``, through the ``dedupe`` seat; in shadow mode its
answer is recorded and changes nothing; in fold mode it folds only onto a row
that is still open; and every failure inserts the row. The repo-wide guard
that no site asks under the lock is tests/test_dedupe_funnel.py.
"""

import json
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import CFOperator, LLM_SCOPES  # noqa: E402
import dedupe_model as dm  # noqa: E402
import dedupe_client as dc  # noqa: E402
import dedupe_reference  # noqa: E402
from tools import UNATTENDED  # noqa: E402

# Remediations #400 and #401, as the queue held them on 2026-10-28: one fix,
# two target names, nothing the tiers can match.
ROW_400 = {"id": 400, "status": "filed", "remediation_class": "data-fix", "host_id": "default",
           "payload": {"dedupe_key": "tgt-0bb3c2e7002133a7",
                       "recommendation": "Update the database schema to add `ON DELETE CASCADE` to the "
                                         "`issue_activities_issue_id_180e5662_fk_issues_id` constraint.",
                       "targets": [{"id": "plane-pgdb, issue_activities", "kind": "database-row"}]}}
CAND_401 = {"dedupe_key": "tgt-bc6eaeae368ee43f", "remediation_class": "k8s-imperative",
            "recommendation": "Update the foreign key constraint in the `issue_activities` table "
                              "referencing `issues` to include `ON DELETE CASCADE`.",
            "targets": [{"id": "plane-pgdb-wl-0", "kind": "k8s-imperative"}],
            "trigger": "Plane Postgres (plane-pgdb-wl-0) logged a data-integrity error"}


def _model(mode, outcome):
    return dm.DEDUPE_MODEL.labels(mode=mode, outcome=outcome)._value.get()


def _decisions(path, decision, tier, source):
    return dc.DEDUPE_DECISIONS.labels(path=path, decision=decision, tier=tier,
                                      source=source)._value.get()


# ---------------------------------------------------------------------------
# Prompt and parser
# ---------------------------------------------------------------------------

def test_the_prompt_shows_the_candidate_and_every_row_it_may_name():
    rows = [ROW_400, {"id": 9, "status": "needs-human", "payload": {"recommendation": "x" * 5000,
                                                                     "target": {"host": "pi5"}}}]
    text = dm.build_messages(CAND_401, rows)[0]["content"]
    assert "ON DELETE CASCADE" in text and "k8s-imperative plane-pgdb-wl-0" in text
    assert "#400 (filed)" in text and "database-row plane-pgdb, issue_activities" in text
    assert "#9 (needs-human)" in text and "host: pi5" in text
    assert "x" * (dm.REC_CHARS + 1) not in text, "a recommendation is cut to REC_CHARS"


def test_the_prompt_is_capped_at_max_rows():
    rows = [{"id": i, "status": "queued", "payload": {}} for i in range(dm.MAX_ROWS + 5)]
    text = dm.build_messages(CAND_401, rows)[0]["content"]
    assert f"#{dm.MAX_ROWS - 1} (" in text and f"#{dm.MAX_ROWS} (" not in text


@pytest.mark.parametrize("reply, want", [
    ('{"row": 400, "reason": "same FK change"}', {"row_id": 400, "reason": "same FK change"}),
    ('{"row": null, "reason": "different fix"}', {"row_id": None, "reason": "different fix"}),
    ('```json\n{"row": "#400", "reason": "r"}\n```', {"row_id": 400, "reason": "r"}),
    ('Thinking {not json} then {"row": 400, "reason": "a {brace} inside"}',
     {"row_id": 400, "reason": "a {brace} inside"}),
    ('{"row": "none", "reason": "no"}', {"row_id": None, "reason": "no"}),
])
def test_the_parser_reads_an_answer(reply, want):
    assert dm.parse_answer(reply, [400, 9]) == want


@pytest.mark.parametrize("reply", [
    "", "no json here", '{"reason": "no row key"}',
    '{"row": 401, "reason": "a row it was not shown"}',
    '{"row": true, "reason": "a bool is not an id"}',
    '{"row": "four hundred", "reason": "words"}',
])
def test_the_parser_refuses_anything_it_cannot_act_on(reply):
    assert dm.parse_answer(reply, [400, 9]) is None


def test_ask_counts_each_outcome_and_never_raises():
    rows = [ROW_400]
    before = {o: _model("shadow", o) for o in ("fold", "none", "malformed", "error")}

    def boom(*a):
        raise RuntimeError("chain down")
    assert dm.ask(boom, CAND_401, rows, mode="shadow")["outcome"] == "error"
    assert dm.ask(lambda *a: {"response": "?"}, CAND_401, rows, mode="shadow")["outcome"] == "malformed"
    assert dm.ask(lambda *a: {"response": '{"row": null, "reason": "n"}'},
                  CAND_401, rows, mode="shadow")["outcome"] == "none"
    fold = dm.ask(lambda *a: {"response": '{"row": 400, "reason": "same"}', "backend": "xai",
                              "model": "grok"}, CAND_401, rows, mode="shadow")
    assert fold == {"mode": "shadow", "outcome": "fold", "row_id": 400, "reason": "same",
                    "backend": "xai", "model": "grok", "row_key": "tgt-0bb3c2e7002133a7"}
    for o in before:
        assert _model("shadow", o) == before[o] + 1, o


# ---------------------------------------------------------------------------
# The funnel
# ---------------------------------------------------------------------------

def _op(*, model=True, fold=False, rows=(ROW_400,), reply='{"row": 400, "reason": "same FK change"}'):
    from test_remediation_queue import _confirming_judge, _wire_flags
    # _wire_flags wires the funnel with the step off; this test wants it on.
    op = dedupe_reference.wire(_confirming_judge(_wire_flags(MagicMock())), model_step=True)
    op.config = {"remediation": {"queue_feed": True, "model_dedupe": model, "model_dedupe_fold": fold}}
    op.kb.list_open_remediations_for_dedupe.return_value = list(rows)
    op.kb.get_remediation.side_effect = lambda rid: next(
        (dict(r) for r in op.kb.list_open_remediations_for_dedupe.return_value if r["id"] == rid), None)
    op.kb.queue_remediation.return_value = 402
    op._count_enqueued = MagicMock()
    op._notready_nodes = MagicMock(return_value=set())
    op._scope_llm = MagicMock(return_value={"backend": "xai", "model": "grok-x", "source": "console"})
    op._chat_with_tools_with_fallback = MagicMock(
        return_value={"response": reply, "backend": "xai", "model": "grok-x"})
    return op


def _enqueue(op):
    details = {**CAND_401, "risk": "med", "confidence": None, "host": None}
    return CFOperator._maybe_queue_remediation(op, 4946, details)


def test_off_asks_no_model():
    op = _op(model=False)
    assert _enqueue(op) == 402
    op._chat_with_tools_with_fallback.assert_not_called()
    assert "model" not in op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe"]


def test_the_model_is_asked_through_the_dedupe_seat_one_shot_and_unattended():
    op = _op()
    _enqueue(op)
    op._scope_llm.assert_called_with("dedupe")
    kw = op._chat_with_tools_with_fallback.call_args.kwargs
    assert (kw["backend"], kw["model"]) == ("xai", "grok-x"), "the seat's backend and model, explicitly"
    assert kw["max_iterations"] == 1 and kw["tool_policy"] is UNATTENDED
    assert kw["system_context"] == dm.SYSTEM_PROMPT
    assert "#400 (filed)" in kw["messages"][0]["content"]


def test_shadow_records_the_answer_and_changes_nothing():
    op = _op(fold=False)
    news = _decisions("investigation", "new", "", "service")
    assert _enqueue(op) == 402
    note = op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe"]
    assert note["source"] == "service"
    assert note["model"] == {"mode": "shadow", "outcome": "fold", "row_id": 400,
                             "reason": "same FK change", "backend": "xai", "model": "grok-x"}
    op.kb.record_remediation_absorbed.assert_not_called()
    assert _decisions("investigation", "new", "", "service") == news + 1


def test_fold_mode_folds_onto_the_row_the_model_named():
    op = _op(fold=True)
    folds = _decisions("investigation", "fold", "model", "model")
    assert _enqueue(op) == 400
    op.kb.queue_remediation.assert_not_called()
    op.kb.record_remediation_absorbed.assert_called_once_with(400, CAND_401["trigger"][:200])
    op.kb.record_remediation_alias.assert_called_once_with(400, "tgt-bc6eaeae368ee43f")
    assert _decisions("investigation", "fold", "model", "model") == folds + 1


def test_fold_mode_inserts_when_the_named_row_closed_meanwhile():
    op = _op(fold=True)
    op.kb.get_remediation.side_effect = lambda rid: {"id": rid, "status": "rejected"}
    assert _enqueue(op) == 402
    assert op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe"]["model"]["row_id"] == 400


@pytest.mark.parametrize("reply", ['{"row": null, "reason": "different"}', "garbage",
                                   '{"row": 12, "reason": "not shown"}'])
def test_no_answer_to_act_on_inserts(reply):
    op = _op(fold=True, reply=reply)
    assert _enqueue(op) == 402


def test_a_failed_model_call_inserts():
    op = _op(fold=True)
    op._chat_with_tools_with_fallback.side_effect = RuntimeError("every rung failed")
    assert _enqueue(op) == 402
    assert op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe"]["model"]["outcome"] == "error"


def test_a_fold_the_tiers_already_see_costs_no_model_call():
    same_key = {**ROW_400, "payload": {**ROW_400["payload"], "dedupe_key": CAND_401["dedupe_key"]}}
    op = _op(fold=True, rows=(same_key,))
    assert _enqueue(op) is None, "the exact key suppresses it"
    op._chat_with_tools_with_fallback.assert_not_called()


def test_no_open_rows_costs_no_model_call():
    op = _op(fold=True, rows=())
    assert _enqueue(op) == 402
    op._chat_with_tools_with_fallback.assert_not_called()


def test_the_model_is_asked_outside_the_lock():
    """The console, the chat tool and the summary feed wait on this lock; a
    model call failing over between providers must not hold them."""
    op = _op(fold=True)
    free = []

    def chat(**kw):
        t = threading.Thread(target=lambda: free.append(
            op._remediation_lock.acquire(blocking=False) and (op._remediation_lock.release() or True)))
        t.start()
        t.join()
        return {"response": '{"row": 400, "reason": "same"}', "backend": "xai", "model": "grok-x"}
    op._chat_with_tools_with_fallback.side_effect = chat
    assert _enqueue(op) == 400
    assert free == [True], "the lock was held while the model ran"


def test_the_seat_is_a_console_scope():
    assert "dedupe" in LLM_SCOPES
    op = MagicMock()
    op.kb.get_setting.side_effect = lambda k, d='': {"dedupe_backend": "openrouter",
                                                      "dedupe_model": "anthropic/claude-haiku-5.5"}.get(k, d)
    op.config = {}
    assert CFOperator._scope_llm(op, "dedupe") == {"backend": "openrouter", "model": "anthropic/claude-haiku-5.5",
                                                  "source": "console"}


def test_both_flags_are_wired_everywhere_a_flag_has_to_be():
    import pathlib
    from cfshared.config import REMEDIATION_FLAGS
    page = (pathlib.Path(__file__).resolve().parents[1] / "ui" / "remediations.html").read_text("utf-8")
    for name, chip in (("model_dedupe", "model-dedupe"), ("model_dedupe_fold", "model-folds")):
        assert name in REMEDIATION_FLAGS and name in CFOperator._REMEDIATION_FLAGS
        assert f"{name}:'{chip}'" in page


def test_the_payload_note_is_json():
    op = _op(fold=False)
    _enqueue(op)
    json.dumps(op.kb.queue_remediation.call_args.kwargs["payload"])


def test_the_summary_asks_only_for_a_rec_it_would_enqueue():
    """A rec the summary dispatches becomes an investigation, which asks when
    it enqueues; asking here would spend a call on a row this feed never makes."""
    from test_remediation_queue import (_ALL_READY, _summary_investigate_shaped, _summary_nodes_op,
                                        _summary_with_host)
    op = dedupe_reference.wire(_summary_nodes_op(_ALL_READY, open_rows={"other-key": {"id": 93}}),
                               model_step=True)
    flag = op._remediation_flag
    op._remediation_flag = lambda name: True if name == "model_dedupe" else flag(name)
    op._scope_llm = MagicMock(return_value={"backend": "auto", "model": None, "source": "default"})
    op._chat_with_tools_with_fallback = MagicMock(
        return_value={"response": '{"row": null, "reason": "different"}', "backend": "ollama", "model": "g"})
    CFOperator._feed_remediations_from_summary(op, _summary_investigate_shaped("raspberrypi5"), [])
    op.enqueue_investigation.assert_called_once()
    op._chat_with_tools_with_fallback.assert_not_called()
    CFOperator._feed_remediations_from_summary(op, _summary_with_host("raspberrypi5"), [])
    op._chat_with_tools_with_fallback.assert_called_once()
    assert op.kb.queue_remediation.call_args.kwargs["payload"]["dedupe"]["model"]["outcome"] == "none"


def test_the_sweep_fallback_folds_on_the_models_answer():
    from test_remediation_queue import _feed_op
    op = dedupe_reference.wire(_feed_op(), model_step=True)
    flag = op._remediation_flag
    op._remediation_flag = lambda name: True if name in ("model_dedupe", "model_dedupe_fold") else flag(name)
    op.kb.list_open_remediations_for_dedupe.return_value = [ROW_400]
    op.kb.get_remediation.side_effect = lambda rid: dict(ROW_400) if rid == 400 else None
    op._notready_nodes = MagicMock(return_value=set())
    op._scope_llm = MagicMock(return_value={"backend": "auto", "model": None, "source": "default"})
    op._chat_with_tools_with_fallback = MagicMock(
        return_value={"response": '{"row": 400, "reason": "same FK change"}', "backend": "ollama", "model": "g"})
    finding = {"finding": "plane-pgdb FK violation", "severity": "warning", "resource_type": "pod",
               "resource_name": "plane-pgdb-wl-0", "namespace": "plane",
               "remediation": "Physically replace the issue_activities FK with ON DELETE CASCADE by hand"}
    handled = CFOperator._feed_remediations_from_sweeps(
        op, [{"id": 1, "findings": [finding], "report": "", "provider": "ollama/g"}])
    assert handled == 1
    op.kb.queue_remediation.assert_not_called()
    assert op.kb.record_remediation_absorbed.call_args.args[0] == 400
