"""The dedupe model eval (CFOP-545) labels its cases from the operator's own
notes, and scores through dedupe_model.ask.

The labels are the whole value of that harness, and they are parsed from
free text written over months, so the shapes it reads are pinned here, as is
each row's moment of arrival: a case shows the model only the rows that were
open then, never one closed before it arrived and never how a row ended.
"""

from repo_paths import REPO_ROOT
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

_spec = importlib.util.spec_from_file_location("dedupe_model_eval", REPO_ROOT / "benchmarks" / "dedupe_model_eval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _row(rid, created, completed=None, note=None, rec="r", **extra):
    return {"id": rid, "created_at": created, "completed_at": completed, "status": "rejected",
            "remediation_class": "manual", "host_id": "default", "last_error": note, "result": None,
            "payload": {"recommendation": f"{rec} {rid}", "dedupe_key": f"k{rid}"}, **extra}


def test_the_note_shapes_the_operator_wrote_are_all_read():
    rows = [_row(67, "01"), _row(69, "02", note="Duplicate of #67 - same fix, reworded"),
            _row(70, "03", note="Duplicates #67 and #69 already rejected as same fix."),
            _row(334, "04"), _row(341, "05"),
            _row(347, "06", note="This is one of 13 duplicate rows (#334-#347) for that one fix"),
            _row(220, "07", note="Resolved: duplicate of CFOP-304. Fixed by homelab-infra #180"),
            _row(222, "08", note="Resolved: duplicate of CFOP-304. Fixed by homelab-infra #180"),
            _row(315, "09", note='Duplicate of remediation #314 (CFOP-451)'), _row(314, "10"),
            _row(400, "11"), _row(401, "12")]
    root = ev.clusters(rows)
    assert root[69] == root[67] == root[70]
    assert root[347] == root[334]
    assert 341 not in root, "a range names the cluster's first row, not every id in it"
    assert root[220] == root[222], "rows closed against one tracker issue are one fix"
    assert root[220] != root[67]
    assert root[315] == root[314]
    assert root[401] == root[400], "KNOWN: cancelled in Plane, no note on the row"


def test_a_case_shows_only_the_rows_open_when_it_arrived():
    rows = [_row(1, "2026-10-01", completed="2026-10-02"),  # closed before #4 arrived
            _row(2, "2026-10-01", completed="2026-10-09"),  # closed after
            _row(3, "2026-10-02", completed=None, status="filed"),
            _row(4, "2026-10-05"), _row(5, "2026-10-06")]   # #5 arrived after
    assert [r["id"] for r in ev.open_at(rows, rows[3])] == [3, 2]


def test_a_duplicate_expects_its_open_mates_and_one_whose_mates_closed_is_a_negative():
    rows = [_row(10, "2026-10-01", completed="2026-10-20"),
            _row(11, "2026-10-02", note="Duplicate of #10"),
            _row(20, "2026-10-03", completed="2026-10-04"),
            _row(21, "2026-10-05", note="Duplicate of #20")]
    cases = {c["id"]: c for c in ev.build_cases(rows, negatives=10)}
    assert cases[11]["kind"] == "duplicate" and cases[11]["expected"] == [10]
    assert cases[21]["kind"] == "distinct" and cases[21]["cluster_open_gone"], (
        "the model is only shown open rows; #20 had closed")
    assert {r["status"] for c in cases.values() for r in c["rows"]} == {"open"}, (
        "how a row ended is not something the agent knew when it asked")


def test_negatives_are_capped_hardest_first():
    rows = [_row(1, "01", rec="raise the probe timeout on plane-api"),
            _row(2, "02", rec="raise the probe timeout on plane-api now"),
            _row(3, "03", rec="something else entirely")]
    cases = ev.build_cases(rows, negatives=1)
    assert [c["id"] for c in cases] == [2]


@pytest.mark.parametrize("kind, expected, outcome, row, want", [
    ("duplicate", [10], "fold", 10, "caught"),
    ("duplicate", [10], "fold", 12, "wrong-row"),
    ("duplicate", [10], "none", None, "missed"),
    ("distinct", [], "none", None, "correct"),
    ("distinct", [], "fold", 12, "false-fold"),
    ("distinct", [], "malformed", None, "malformed"),
    ("duplicate", [10], "error", None, "error"),
])
def test_grading(kind, expected, outcome, row, want):
    assert ev.grade({"kind": kind, "expected": expected}, {"outcome": outcome, "row_id": row}) == want


def test_seats_name_any_backend_and_an_ollama_seat_names_its_host():
    assert ev.parse_seat("ollama:gemma4:26b@http://h:11434/") == {
        "backend": "ollama", "model": "gemma4:26b", "url": "http://h:11434", "name": "ollama:gemma4:26b@http://h:11434/"}
    assert ev.parse_seat("openrouter:anthropic/claude-haiku-5.5")["model"] == "anthropic/claude-haiku-5.5"
    with pytest.raises(SystemExit):
        ev.parse_seat("ollama:gemma4:26b")
    with pytest.raises(SystemExit):
        ev.parse_seat("nosuch:model")


def test_a_seat_is_scored_through_the_production_prompt_and_parser(monkeypatch):
    rows = [_row(10, "2026-10-01"), _row(11, "2026-10-02", note="Duplicate of #10"), _row(12, "2026-10-03")]
    cases = ev.build_cases(rows, negatives=10)
    seen = []

    def fake_caller(seat, timeout, num_ctx=None):
        def call(messages, system):
            seen.append((system, messages[0]["content"]))
            return {"response": '{"row": 10, "reason": "same"}', "backend": "x", "model": "m"}
        return call
    monkeypatch.setattr(ev, "caller", fake_caller)
    out = ev.run_seat({"name": "x:m"}, cases, runs=1, workers=1, timeout=1, num_ctx=None)
    assert {r["case"]: r["grade"] for r in out["results"]} == {11: "caught", 12: "false-fold"}
    assert all(system == ev.dedupe_model.SYSTEM_PROMPT for system, _ in seen)
    assert all("Queued rows:" in text for _, text in seen)
    assert out["summary"]["wrong_folds"] == 1 and out["summary"]["recall"] == 1.0


@pytest.mark.parametrize("kind, row, verdict, want", [
    ("distinct", 12, "S", "unmarked-dup"),
    ("distinct", 12, "B", "borderline"),
    ("distinct", 12, "D", "false-fold"),
    ("distinct", 13, "S", "false-fold"),       # the verdict is for one pair, not the case
    ("duplicate", 12, "S", "unmarked-dup"),    # a mate the notes missed
])
def test_a_hand_verdict_regrades_only_the_pair_it_names(kind, row, verdict, want):
    case = {"id": 20, "kind": kind, "expected": [10] if kind == "duplicate" else []}
    assert ev.grade(case, {"outcome": "fold", "row_id": row}, {"20-12": verdict}) == want


def test_replay_regrades_a_saved_run_without_calling_anything(tmp_path, monkeypatch, capsys):
    import json
    rows = [_row(10, "2026-10-01"), _row(11, "2026-10-02", note="Duplicate of #10"), _row(12, "2026-10-03")]
    (tmp_path / "rows.json").write_text(json.dumps({"remediations": rows}))
    saved = {"seats": [{"seat": "x:m", "results": [
        {"case": 11, "run": 0, "kind": "duplicate", "expected": [10], "row_id": 10, "reason": "", "grade": "caught", "seconds": 1},
        {"case": 12, "run": 0, "kind": "distinct", "expected": [], "row_id": 11, "reason": "", "grade": "false-fold", "seconds": 1}]}]}
    (tmp_path / "run.json").write_text(json.dumps(saved))
    (tmp_path / "adj.json").write_text(json.dumps({"_note": "x", "12-11": "S"}))
    monkeypatch.setattr(ev, "caller", lambda *a, **k: pytest.fail("replay called a model"))
    assert ev.main(["--rows", str(tmp_path / "rows.json"), "--replay", str(tmp_path / "run.json"),
                    "--adjudication", str(tmp_path / "adj.json")]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("x:m"))
    assert line.split()[1:6] == ["1/1", "0", "1", "0", "0"], line
