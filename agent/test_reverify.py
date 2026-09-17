#!/usr/bin/env python3
"""The filed-row re-verification tick (CFOP-185).

Four things carry the safety of this feature, and each gets its own tests:
the verifier is never the vendor that filed the row; the tool pass is
read-only by policy, not by prompt wording; anything short of a clear verdict
leaves the row filed; and a rejection teaches the knowledge base. Then the
tick's selection: what is due, what rotates, what a pass writes.

MagicMock operator, like test_tracker_sync; the chat is stubbed at the
operator, so no provider is contacted.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reverify as rv  # noqa: E402


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _row(**over):
    row = {
        "id": 117,
        "status": "filed",
        "remediation_class": "node-action",
        "risk": "low",
        "host_id": "raspberrypi4",
        "investigation_id": 2419,
        "created_at": _ago(7200),
        "last_error": None,
        "result": {"tracker": {"ref": "R1", "key": "CFOP-182"}},
        "payload": {
            "provider": "ollama/gemma4:26b",
            "recommendation": "Point K3S_URL at the control plane",
            "steps": ["Edit k3s-agent.service.env", "Restart k3s-agent"],
            "observed": [{"source": "loki_query", "value": "connection reset by peer"}],
        },
    }
    row.update(over)
    return row


def _op(*, flag=True, rows=None, response="", providers=("anthropic",),
        model="claude-opus-4-8", raises=None):
    op = MagicMock()
    op.config = {"remediation": {"queue_reverify": flag, "max_reverify_per_tick": 2,
                                 "reverify": {"min_age_seconds": 3600,
                                              "recheck_after_seconds": 86400,
                                              "max_iterations": 10}}}
    op._remediation_flag = lambda name: bool(op.config["remediation"].get(name))
    op._judge_providers = lambda: list(providers)
    op._judge_model = lambda backend: model
    op._resolve_provider = lambda backend, m=None: (backend, f"https://{backend}/v1", m or model)
    op._tracker_url = lambda: "http://tracker:8092"
    op.kb.list_remediations.return_value = list(rows if rows is not None else [_row()])
    op.kb.store_learning.return_value = 4242
    if raises:
        op._chat_with_tools.side_effect = raises
    else:
        op._chat_with_tools.return_value = {"response": response}
    return op


VERDICT_REJECT = """{"verdict": "rejected",
 "note": "K3S_URL is already https://192.168.0.167:6443.",
 "learning": {"title": "127.0.0.1:6444 is the k3s supervisor proxy",
              "description": "Not a misconfiguration.",
              "applies_when": "an investigation proposes changing K3S_URL because output mentions 127.0.0.1:6444"}}"""


# ---- the verifier must not be the model that filed the row -------------------

def test_verifier_skips_the_vendor_that_filed_the_row():
    """The failure this feature exists to catch is a model's own wrong call, so
    asking that same vendor to review it would defeat the point."""
    op = _op(providers=("ollama", "anthropic"))
    assert [b for b, _m in rv.eligible_peers(op, _row())] == ["anthropic"]


def test_no_verifier_when_every_peer_is_the_reporter():
    op = _op(providers=("ollama",))
    assert rv.eligible_peers(op, _row()) == []


def test_row_is_left_filed_when_no_peer_is_eligible():
    op = _op(providers=("ollama",))
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    op._chat_with_tools.assert_not_called()
    op.kb.update_remediation_status.assert_not_called()


def test_reporter_with_no_backend_still_matches_on_the_bare_model():
    op = _op(providers=("anthropic",), model="claude-opus-4-8")
    row = _row(payload={"provider": "claude-opus-4-8", "recommendation": "x"})
    assert rv.eligible_peers(op, row) == []


def test_failover_stays_inside_the_judge_rung():
    """_chat_with_tools_with_fallback would end this chain at the local primary
    -- the model whose judgement is under review. Failover must not leave the
    frontier peers."""
    op = _op(providers=("deepseek", "anthropic"))
    op._chat_with_tools.side_effect = [
        RuntimeError("deepseek unreachable"),
        {"response": '{"verdict": "open", "note": "still down"}'},
    ]
    assert rv.reverify_row(op, _row(), max_iterations=10) == "open"
    op._chat_with_tools_with_fallback.assert_not_called()
    tried = [call.args[0] for call in op._chat_with_tools.call_args_list]
    assert tried == ["deepseek", "anthropic"]
    assert "ollama" not in tried


def test_a_peer_that_answered_badly_does_not_advance_to_the_next():
    """Unreachable is a reason to try the next seat; a bad answer is not --
    that is the mutation judge's rule and it holds here."""
    op = _op(providers=("deepseek", "anthropic"), response="not json")
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    assert op._chat_with_tools.call_count == 1


def test_note_names_the_peer_that_actually_answered():
    op = _op(providers=("deepseek", "anthropic"))
    op._chat_with_tools.side_effect = [
        RuntimeError("down"),
        {"response": '{"verdict": "resolved", "note": "node is Ready"}'},
    ]
    rv.reverify_row(op, _row(), max_iterations=10)
    note = op.kb.update_remediation_status.call_args.kwargs["result"]["resolution_note"]
    assert "anthropic/claude-opus-4-8" in note and "deepseek" not in note


# ---- the pass is read-only by policy ----------------------------------------

def test_pass_runs_under_a_verify_only_tool_policy():
    """The read-only guarantee is the registry's, not the prompt's: mutating
    tools are withheld from the schema and refused at execute."""
    op = _op(response=VERDICT_REJECT)
    rv.reverify_row(op, _row(), max_iterations=10)
    policy = op._chat_with_tools.call_args.kwargs["tool_policy"]
    assert policy.verify_only is True
    assert policy.allows_mutation() is False


def test_verify_policy_still_offers_ssh_for_read_only_checks():
    """Withholding ssh_execute outright would leave the pass narrating checks
    it could not run; the command is classified at execute time instead."""
    from tools import ToolPolicy
    policy = ToolPolicy(verify_only=True)
    assert policy.allows_tool("ssh_execute", True) is True
    assert policy.allows_tool("k8s_get_nodes", False) is True


# ---- anything short of a clear verdict leaves the row filed ------------------

@pytest.mark.parametrize("response", [
    "", "not json at all", "{}", '{"verdict": "maybe"}',
    '{"verdict": "resolved"}',           # a close with no note
    '{"note": "looks fine"}',            # no verdict
])
def test_unusable_answers_leave_the_row_alone(response):
    op = _op(response=response)
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    op.kb.update_remediation_status.assert_not_called()


def test_a_raising_chat_leaves_the_row_alone():
    op = _op(raises=RuntimeError("provider down"))
    assert rv.reverify_row(op, _row(), max_iterations=10) is None
    op.kb.update_remediation_status.assert_not_called()


def test_verdict_survives_a_fenced_block_and_surrounding_prose():
    parsed = rv.parse_verdict(
        'Here is my answer.\n```json\n{"verdict": "open", "note": "still down"}\n```\nDone.')
    assert parsed["verdict"] == "open" and parsed["note"] == "still down"


def test_a_single_object_array_is_unwrapped():
    """The tool loop's last-iteration nudge tells OpenAI-compatible peers to
    answer 'as the JSON array described above' -- wording inherited from the
    sweep. Unwrap rather than throw away a spent frontier pass."""
    parsed = rv.parse_verdict('[{"verdict": "rejected", "note": "already correct"}]')
    assert parsed["verdict"] == "rejected"


def test_a_multi_object_array_is_still_refused():
    assert rv.parse_verdict('[{"verdict": "open"}, {"verdict": "resolved"}]') is None


# ---- what each verdict writes -----------------------------------------------

def test_resolved_closes_the_row_and_names_the_verifier():
    op = _op(response='{"verdict": "resolved", "note": "node is Ready, 2/2 pings"}')
    assert rv.reverify_row(op, _row(), max_iterations=10) == "resolved"
    args, kwargs = op.kb.update_remediation_status.call_args
    assert args[0] == 117 and args[1] == "resolved"
    assert kwargs["result"]["resolved_by"] == "reverify"
    note = kwargs["result"]["resolution_note"]
    assert "Automated re-verification (anthropic/claude-opus-4-8)" in note
    assert "node is Ready" in note


def test_rejected_closes_the_row_and_teaches_the_kb():
    op = _op(response=VERDICT_REJECT)
    assert rv.reverify_row(op, _row(), max_iterations=10) == "rejected"
    assert op.kb.update_remediation_status.call_args.args[1] == "rejected"
    learning = op.kb.store_learning.call_args.args[0]
    assert learning["learning_type"] == "antipattern"
    assert "127.0.0.1:6444" in learning["applies_when"]
    assert "filed_by:ollama/gemma4:26b" in learning["tags"]
    assert "remediation:117" in learning["tags"]


def test_a_rejection_without_a_trigger_condition_stores_nothing():
    """store_learning() deprecates a learning with no applies_when on arrival,
    so inventing one would report success while seeding something unreachable."""
    op = _op(response='{"verdict": "rejected", "note": "wrong", '
                      '"learning": {"title": "t", "description": "d"}}')
    assert rv.reverify_row(op, _row(), max_iterations=10) == "rejected"
    op.kb.store_learning.assert_not_called()
    assert op.kb.update_remediation_status.call_args.args[1] == "rejected"


def test_open_leaves_the_status_and_records_the_check():
    op = _op(response='{"verdict": "open", "note": "still unreachable"}')
    assert rv.reverify_row(op, _row(), max_iterations=10) == "open"
    op.kb.update_remediation_status.assert_not_called()
    state = op.kb.merge_remediation_result.call_args.args[1]["reverify"]
    assert state["verdict"] == "open" and state["checks"] == 1


# ---- which rows are due ------------------------------------------------------

def test_a_freshly_filed_row_is_not_due():
    assert rv.is_due(_row(created_at=_ago(60)), min_age=3600, recheck_after=86400) is False


def test_only_filed_rows_are_due():
    assert rv.is_due(_row(status="needs-human"), min_age=0, recheck_after=0) is False


def test_a_recently_checked_row_is_not_due_again():
    row = _row(result={"reverify": {"checked_at": _ago(600)}})
    assert rv.is_due(row, min_age=3600, recheck_after=86400) is False


def test_a_row_checked_long_ago_is_due_again():
    row = _row(result={"reverify": {"checked_at": _ago(200000)}})
    assert rv.is_due(row, min_age=3600, recheck_after=86400) is True


def test_an_unparseable_stamp_does_not_pin_a_row_as_fresh():
    """Reading a bad stamp as age-zero would retire the row permanently."""
    row = _row(created_at="not a date", result={"reverify": {"checked_at": "also not"}})
    assert rv.is_due(row, min_age=3600, recheck_after=86400) is True


# ---- the tick ---------------------------------------------------------------

def test_tick_is_a_no_op_when_the_flag_is_off():
    op = _op(flag=False)
    assert rv.reverify_filed_rows(op) == 0
    op.kb.list_remediations.assert_not_called()


def test_the_tick_reads_a_list_that_actually_carries_its_clocks():
    """Guards the shape, not a fabricated dict.

    The first cut listed rows with list_remediations_by_status, which was built
    for the PR reconciler and returns six fields -- no `status`, `created_at`
    or `result`. Every clock and state read here lives in those three, so the
    tick was a silent no-op that the hand-built rows in this file could never
    catch. Assert against the real row dict instead.
    """
    from knowledge_base import remediation_row_dict

    class Row:
        id = 1; status = "filed"; remediation_class = "manual"; risk = "low"
        confidence = None; host_id = "h"; investigation_id = None; priority = 0
        attempts = 0; pr_url = None; last_error = None
        created_at = None; claimed_at = None; completed_at = None
        payload = {}; result = {"tracker": {"ref": "R1"}}

    real = remediation_row_dict(Row())
    for field in ("status", "created_at", "result"):
        assert field in real, f"{field} is missing from the row the tick lists"
    assert rv.is_due(real, min_age=3600, recheck_after=86400) is True
    assert rv.reverify_state(real) == {}


def test_tick_respects_max_per_tick():
    rows = [_row(id=i, result={}) for i in range(5)]
    op = _op(rows=rows, response='{"verdict": "open", "note": "n"}')
    assert rv.reverify_filed_rows(op) == 2
    assert op._chat_with_tools.call_count == 2


def test_tick_takes_the_least_recently_checked_first():
    old = _row(id=1, result={"reverify": {"checked_at": _ago(500000)}})
    newer = _row(id=2, result={"reverify": {"checked_at": _ago(200000)}})
    op = _op(rows=[newer, old], response='{"verdict": "open", "note": "n"}')
    op.config["remediation"]["max_reverify_per_tick"] = 1
    rv.reverify_filed_rows(op)
    assert "#1," in rv.build_question(old)
    assert op.kb.merge_remediation_result.call_args.args[0] == 1


def test_one_raising_row_does_not_stop_the_tick():
    op = _op(rows=[_row(id=1, result={}), _row(id=2, result={})],
             response='{"verdict": "open", "note": "n"}')
    op.kb.merge_remediation_result.side_effect = [RuntimeError("db"), None]
    assert rv.reverify_filed_rows(op) == 1


def test_the_flag_is_wired_everywhere_a_flag_has_to_be():
    """Three places, not one. The profile clamp keeps it off under
    `investigate`; CFOperator._REMEDIATION_FLAGS is what the console's
    GET/POST /api/remediation/flags will accept; FLAG_LABEL is the chip an
    operator actually clicks. CFOP-170 wired queue_tracker through all three,
    and a flag in only the first is one nobody can turn on from the console.
    """
    import pathlib
    from agent import CFOperator
    from cfshared.config import REMEDIATION_FLAGS

    assert "queue_reverify" in REMEDIATION_FLAGS
    assert "queue_reverify" in CFOperator._REMEDIATION_FLAGS
    page = pathlib.Path(__file__).resolve().parents[1] / "ui" / "remediations.html"
    assert "queue_reverify:'re-verify'" in page.read_text("utf-8")


def test_node_action_enabled_is_wired_everywhere_a_flag_has_to_be():
    """Console kill-switch (CFOP-131). Not a top-level REMEDIATION_FLAGS key
    — those get zeroed as ``remediation.<name>`` — but it must be on the
    operator tuple the flags API accepts, and on FLAG_LABEL so a chip exists.
    """
    import pathlib
    from agent import CFOperator
    from cfshared.config import REMEDIATION_FLAGS

    assert "node_action_enabled" not in REMEDIATION_FLAGS
    assert "node_action_enabled" in CFOperator._REMEDIATION_FLAGS
    page = pathlib.Path(__file__).resolve().parents[1] / "ui" / "remediations.html"
    text = page.read_text("utf-8")
    assert "node_action_enabled:'node-action'" in text
    assert "confirmNodeAction" in text


def test_question_carries_the_claim_and_frames_steps_as_checks():
    text = rv.build_question(_row())
    assert "Point K3S_URL at the control plane" in text
    assert "what you CHECK, not what you run" in text
    assert "connection reset by peer" in text
