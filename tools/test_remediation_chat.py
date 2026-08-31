"""Chat tools for the remediation queue and investigation-by-id (CFOP-22 B)."""

from unittest.mock import MagicMock

import pytest

import tools as tools_module
from tools import ToolRegistry


def _registry():
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {}}, "search": {}}
    # Avoid constructing real SSH/k8s/git clients — empty hosts is enough.
    reg = ToolRegistry(op)
    return op, reg


class TestRemediationChatTools:
    def test_tools_registered(self):
        _, reg = _registry()
        names = {s["function"]["name"] for s in reg.get_schemas()}
        assert {"list_remediations", "get_remediation", "get_investigation"} <= names

    def test_list_remediations(self):
        op, reg = _registry()
        op.kb.list_remediations.return_value = [
            {"id": 41, "status": "needs-human",
             "payload": {"recommendation": "x", "rendered_context": "y" * 5000}},
        ]
        out = reg.execute("list_remediations", {"limit": 5})
        assert out["success"] is True and out["count"] == 1
        op.kb.list_remediations.assert_called_once_with(status=None, limit=5)
        # bulky rendered_context is truncated for the chat loop
        assert out["remediations"][0]["payload"]["rendered_context"].endswith("…")
        assert len(out["remediations"][0]["payload"]["rendered_context"]) <= 2001

    def test_get_remediation_missing(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = None
        out = reg.execute("get_remediation", {"remediation_id": 99})
        assert "not found" in out["error"]

    def test_get_investigation(self):
        op, reg = _registry()
        op.kb.get_investigation.return_value = {
            "id": 2141,
            "trigger": "[deep] mount",
            "findings": {"response": "z" * 5000, "provider": "anthropic/claude"},
            "outcome": "needs_action",
        }
        out = reg.execute("get_investigation", {"investigation_id": 2141})
        assert out["success"] is True
        assert out["investigation"]["id"] == 2141
        assert out["investigation"]["findings"]["response"].endswith("…")


class TestResolveRemediation:
    """CFOP-123: the queue needs a WRITE tool, not just the two readers.

    Without one, an operator saying "resolve it" left the agent holding only
    update_sweep_finding — so it closed a sweep finding instead and the
    remediation stayed on /remediations.
    """

    def test_queue_has_a_write_tool(self):
        # Pins the class of regression (a missing registration), not the
        # argument list.
        _, reg = _registry()
        names = {s["function"]["name"] for s in reg.get_schemas()}
        assert "resolve_remediation" in names

    def test_resolves_and_records_the_note(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 84, "status": "needs-human", "claimed_at": None,
            "completed_at": None, "payload": {},
        }
        op.kb.update_remediation_status.return_value = True
        out = reg.execute("resolve_remediation", {
            "remediation_id": 84, "note": "device decommissioned"})
        assert out["success"] is True
        op.kb.update_remediation_status.assert_called_once_with(
            84, "resolved",
            result={"resolution_note": "device decommissioned",
                    "resolved_by": "chat-agent"})

    def test_reject_writes_last_error_like_the_http_twin(self):
        # POST /reject writes last_error; POST /resolve writes the result
        # keys. resolutionHtml() paints "resolved by <who>" whenever
        # result.resolution_note is set — whatever the status — so writing
        # the resolve keys here would label a rejected row resolved.
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 5, "status": "needs-human", "claimed_at": None,
            "completed_at": None, "payload": {},
        }
        op.kb.update_remediation_status.return_value = True
        reg.execute("resolve_remediation", {
            "remediation_id": 5, "note": "wrong fix", "status": "rejected"})
        args, kwargs = op.kb.update_remediation_status.call_args
        assert args[1] == "rejected"
        assert kwargs == {"last_error": "wrong fix"}
        assert "result" not in kwargs

    def test_note_is_capped_like_the_console(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 6, "status": "needs-human", "claimed_at": None,
            "completed_at": None, "payload": {},
        }
        op.kb.update_remediation_status.return_value = True
        reg.execute("resolve_remediation", {"remediation_id": 6, "note": "x" * 5000})
        note = op.kb.update_remediation_status.call_args[1]["result"]["resolution_note"]
        assert len(note) == 2000

    def test_reports_the_stored_row_not_the_intent(self):
        op, reg = _registry()
        op.kb.get_remediation.side_effect = [
            {"id": 84, "status": "needs-human", "claimed_at": None,
             "completed_at": None, "payload": {}},
            {"id": 84, "status": "resolved", "claimed_at": None,
             "completed_at": "2026-08-28T13:00:00", "payload": {}},
        ]
        op.kb.update_remediation_status.return_value = True
        out = reg.execute("resolve_remediation", {
            "remediation_id": 84, "note": "done"})
        assert out["remediation"]["status"] == "resolved"

    @pytest.mark.parametrize("status", ["claimed", "executing"])
    def test_refuses_a_row_the_executor_is_leasing(self, status):
        # Closing a leased row strands the Job that will later POST
        # /v1/remediations/<id>/complete against a row that moved on.
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 12, "status": status, "claimed_at": "2026-08-28T12:00:00",
            "completed_at": None, "payload": {},
        }
        out = reg.execute("resolve_remediation", {
            "remediation_id": 12, "note": "nope"})
        assert "still running" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    @pytest.mark.parametrize("status", ["pr-open", "verifying"])
    def test_closes_a_finished_row_that_still_looks_claimed(self, status):
        # update_remediation_status never clears claimed_at and only stamps
        # completed_at for resolved/rejected/needs-human, so a row whose Job
        # has finished still has claimed_at set and completed_at null. This
        # is the row an operator asks chat to close once the PR exists —
        # gating on claimed_at instead of status would refuse it.
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 14, "status": status, "claimed_at": "2026-08-28T12:00:00",
            "completed_at": None, "payload": {},
        }
        op.kb.update_remediation_status.return_value = True
        out = reg.execute("resolve_remediation", {
            "remediation_id": 14, "note": "PR merged by hand"})
        assert out["success"] is True

    def test_inflight_statuses_match_the_queue(self):
        # tools/ cannot import agent.knowledge_base (agent/__init__ pulls in
        # agent.agent, whose bare imports need agent/ on sys.path), so the
        # tuple is copied. Read the original back by parsing the source —
        # no import, no sys.path games — so the copy cannot drift.
        import ast, pathlib
        kb = pathlib.Path(__file__).resolve().parent.parent / "agent" / "knowledge_base.py"
        found = [
            ast.literal_eval(node.value)
            for node in ast.parse(kb.read_text()).body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "_REMEDIATION_INFLIGHT" for t in node.targets)
        ]
        assert found, "_REMEDIATION_INFLIGHT not found in knowledge_base.py"
        assert tuple(found[0]) == tuple(tools_module._REMEDIATION_INFLIGHT)

    def test_refused_statuses_are_real_queue_statuses(self):
        # An inflight status the CHECK constraint cannot store is a guard
        # that never fires. Parse the constraint and confirm overlap.
        import pathlib, re
        kb = (pathlib.Path(__file__).resolve().parent.parent
              / "agent" / "knowledge_base.py").read_text()
        # Anchor on the constraint NAME: several tables declare a
        # "status IN (...)" check, and the first one is not this table's.
        anchor = kb.find("name='valid_remediation_status'")
        assert anchor != -1, "valid_remediation_status constraint not found"
        window = kb[max(0, anchor - 400):anchor]
        start = window.rfind("status IN (")
        assert start != -1, "remediation status CHECK constraint not found"
        valid = set(re.findall(r"'([a-z-]+)'", window[start:]))
        assert "queued" in valid and "pr-open" in valid, valid
        assert set(tools_module._REMEDIATION_INFLIGHT) <= valid

    def test_unknown_id_reports_rather_than_silently_succeeding(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = None
        out = reg.execute("resolve_remediation", {
            "remediation_id": 999, "note": "x"})
        assert "not found" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    def test_note_is_required(self):
        op, reg = _registry()
        out = reg.execute("resolve_remediation", {"remediation_id": 84, "note": "  "})
        assert "note is required" in out["error"]
        op.kb.get_remediation.assert_not_called()

    def test_bad_status_is_refused(self):
        op, reg = _registry()
        out = reg.execute("resolve_remediation", {
            "remediation_id": 84, "note": "x", "status": "acknowledged"})
        assert "error" in out
        op.kb.update_remediation_status.assert_not_called()

    def test_missing_id_reports_cleanly(self):
        _, reg = _registry()
        out = reg.execute("resolve_remediation", {"note": "x"})
        assert "remediation_id is required" in out["error"]


class TestMorningSummaryGuard:
    """A morning summary is one synthetic finding holding the whole digest,
    so resolving index 0 closes every issue it mentions (CFOP-123)."""

    def test_refuses_a_morning_summary_report(self):
        op, reg = _registry()
        op.kb.get_sweep_report.return_value = {
            "id": 1566,
            "sweep_meta": {"type": "morning_summary", "full_text": "..."},
            "findings": [{"id": "9977c5e4", "finding": "digest text"}],
        }
        out = reg.execute("update_sweep_finding", {
            "report_id": 1566, "finding_id": "9977c5e4",
            "status": "resolved", "resolution": "Device decommissioned."})
        assert "morning summary" in out["error"]
        assert "resolve_remediation" in out["error"]
        op.kb.update_sweep_finding.assert_not_called()

    def test_normal_sweep_report_still_updates(self):
        op, reg = _registry()
        op.kb.get_sweep_report.return_value = {
            "id": 1570, "sweep_meta": {"mode": "full", "duration_seconds": 12.0},
            "findings": [{"id": "abc", "finding": "pod crashlooping"}],
        }
        op.kb.update_sweep_finding.return_value = True
        out = reg.execute("update_sweep_finding", {
            "report_id": 1570, "finding_id": "abc", "status": "resolved"})
        assert out["success"] is True

    def test_report_lookup_failure_does_not_block(self):
        # A guard that cannot read the report must not veto a legitimate update.
        op, reg = _registry()
        op.kb.get_sweep_report.side_effect = RuntimeError("db down")
        op.kb.update_sweep_finding.return_value = True
        out = reg.execute("update_sweep_finding", {
            "report_id": 1570, "finding_id": "abc", "status": "resolved"})
        assert out["success"] is True


class TestApproveFromChat:
    """CFOP-124: the hand-off question asks for "approved, resolved or
    rejected" and the tool could only do two of the three. 'approved' is the
    chat twin of POST /api/remediations/<id>/approve."""

    def _row(self, status="needs-human"):
        return {"id": 7, "status": status, "claimed_at": None, "completed_at": None,
                "payload": {}, "remediation_class": "k8s-action"}

    def test_approved_queues_the_row_like_the_console(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        op.kb.remediation_approve_conflict.return_value = None
        op.kb.update_remediation_status.return_value = True
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved"})
        assert out["success"] is True
        op.kb.update_remediation_status.assert_called_once_with(7, "queued")

    def test_the_consoles_approve_policy_applies(self):
        # manual-class and PR-already-open are refused by the same policy the
        # route uses, so chat cannot walk around the wall the API puts up.
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        op.kb.remediation_approve_conflict.return_value = "manual-class rows are human-only work"
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved", "note": "go"})
        assert "human-only" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    @pytest.mark.parametrize("status", ["claimed", "executing"])
    def test_a_leased_row_cannot_be_approved(self, status):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row(status)
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved"})
        assert "still running" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    def test_a_note_is_optional_to_approve_but_still_required_to_close(self):
        # The approve route stores no note and the executor's result overwrites
        # the row; resolve/reject keep the note as the only record of why.
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        assert "note is required" in reg.execute("resolve_remediation", {"remediation_id": 7})["error"]
        assert "note is required" in reg.execute(
            "resolve_remediation", {"remediation_id": 7, "status": "rejected"})["error"]

    def test_the_schema_advertises_approved(self):
        _, reg = _registry()
        params = reg.tools["resolve_remediation"]["schema"]["parameters"]
        assert params["properties"]["status"]["enum"] == ["resolved", "rejected", "approved"]
        assert params["required"] == ["remediation_id"]


class TestTriageInvestigation:
    """CFOP-138: the investigation surface needs a WRITE tool too.

    An operator said "clear that" about an investigation and the agent
    answered, truthfully, that it had no tool — then wrapped that in an
    invented reason (the outcome field is "an immutable historical snapshot")
    and pointed at a direct DB write, past a supported console control it did
    not know about. Same shape as CFOP-123 one object over.
    """

    ROW = {"id": 2336, "trigger": "Pod resume/resume-site-x not ready for 30m",
           "outcome": "needs_action", "triage_action": "resolved",
           "operator_notes": "node came back", "findings": {"provider": "ollama/gemma4:26b"}}

    def test_the_investigation_surface_has_a_write_tool(self):
        # Pins the class of regression (a missing registration), not the
        # argument list — same guard CFOP-123 added for the queue.
        _, reg = _registry()
        names = {s["function"]["name"] for s in reg.get_schemas()}
        assert "triage_investigation" in names

    def test_it_triages_and_records_the_note(self):
        op, reg = _registry()
        op.kb.update_investigation_triage.return_value = True
        op.kb.get_investigation.return_value = self.ROW
        out = reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": "resolved",
            "note": "raspberrypi5 came back at 17:18; the evicted pod is gone."})
        assert out["success"] is True
        op.kb.update_investigation_triage.assert_called_once_with(
            2336, "resolved",
            operator_notes="raspberrypi5 came back at 17:18; the evicted pod is gone.")
        assert out["investigation"]["triage_action"] == "resolved"

    # ---- the corpus guard: the reason this tool is narrower than the KB ----

    def test_it_never_passes_outcome_to_the_kb(self):
        """kb.update_investigation_triage takes outcome= and executes
        `inv.outcome = outcome`. `outcome` is what
        find_similar_investigations_hybrid cites as precedent into future
        triage, so an operator verdict written there edits the corpus later
        classifications reason from. Mutation-checked: adding an outcome
        passthrough to _triage_investigation fails this."""
        op, reg = _registry()
        op.kb.update_investigation_triage.return_value = True
        op.kb.get_investigation.return_value = self.ROW
        reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": "ack", "note": "seen"})
        _, kwargs = op.kb.update_investigation_triage.call_args
        assert "outcome" not in kwargs, "chat triage must not reach the outcome field"

    def test_the_schema_has_no_outcome_argument(self):
        # The model cannot even ask for it — the argument does not exist,
        # rather than existing with a safe default someone later overrides.
        _, reg = _registry()
        params = reg.tools["triage_investigation"]["schema"]["parameters"]
        assert "outcome" not in params["properties"]
        assert params["required"] == ["investigation_id", "action", "note"]

    def test_an_invented_outcome_argument_is_refused_not_applied(self):
        """A model that names outcome= anyway (the CFOP-138 agent offered to
        flip it to false_positive "for the record") gets an error, and the KB
        is never called.

        The refusal comes from execute()'s `func(**arguments)` splat, not from
        a guard in this method — assert the mechanism, so that if execute ever
        filters unknown keys to the schema, this fails loudly and names why
        rather than quietly testing something else. The sibling
        test_it_never_passes_outcome_to_the_kb is the guard that survives that
        change on its own.
        """
        op, reg = _registry()
        out = reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": "resolved", "note": "x",
            "outcome": "false_positive"})
        assert "unexpected keyword argument" in out["error"]
        assert "outcome" in out["error"]
        op.kb.update_investigation_triage.assert_not_called()

    def test_the_read_twins_point_at_the_write_tool(self):
        """The descriptions the model reads BEFORE it has a verdict to record.

        list_investigations described the surface as "triggers and outcomes /
        whether issues were resolved" and named no write tool — which is the
        sentence that taught the #2324 agent to reach for `outcome` and then
        explain why it could not have it. A read tool that describes this
        surface without naming triage_action and its write twin is the
        regression; pin that rather than today's wording.
        """
        _, reg = _registry()
        for name in ("list_investigations", "get_investigation"):
            desc = reg.tools[name]["schema"]["description"]
            assert "triage_investigation" in desc, f"{name} does not name the write tool"
            assert "triage_action" in desc, f"{name} does not name the operator's field"

    def test_it_returns_the_untouched_outcome(self):
        # The re-read puts the agent's own outcome back in front of the model
        # so it reports the split. The CFOP-138 agent told the operator the
        # row was "effectively cleared" while triage_action was still null.
        op, reg = _registry()
        op.kb.update_investigation_triage.return_value = True
        op.kb.get_investigation.return_value = self.ROW
        out = reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": "resolved", "note": "handled"})
        assert out["investigation"]["outcome"] == "needs_action"

    # ---- refusals ----

    def test_an_unknown_id_reports_rather_than_claiming_success(self):
        op, reg = _registry()
        op.kb.update_investigation_triage.return_value = False
        out = reg.execute("triage_investigation", {
            "investigation_id": 999999, "action": "resolved", "note": "x"})
        assert "not found" in out["error"]
        assert "success" not in out

    def test_a_note_is_required(self):
        op, reg = _registry()
        out = reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": "resolved"})
        assert "note is required" in out["error"]
        op.kb.update_investigation_triage.assert_not_called()

    @pytest.mark.parametrize("action", ["suppress", "retry", "context", "", "Resolved"])
    def test_the_chat_tool_reaches_no_further_than_the_console(self, action):
        # retry/context need the re-investigation path wired and suppress
        # needs a reader in the alert path; the route refuses them, so chat
        # must too rather than writing a triage_action nothing acts on.
        op, reg = _registry()
        out = reg.execute("triage_investigation", {
            "investigation_id": 2336, "action": action, "note": "x"})
        assert "action must be one of" in out["error"]
        op.kb.update_investigation_triage.assert_not_called()

    def test_the_chat_tool_offers_exactly_the_routes_actions(self):
        # web_server.py declares its tuple INSIDE setup_routes, so it cannot
        # be imported; read it back by parsing the source (same technique as
        # test_inflight_statuses_match_the_queue) so the copy cannot drift.
        import ast, pathlib
        web = pathlib.Path(__file__).resolve().parent.parent / "web_server.py"
        found = [
            ast.literal_eval(node.value)
            for node in ast.walk(ast.parse(web.read_text()))
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "_INVESTIGATION_TRIAGE_ACTIONS"
                    for t in node.targets)
        ]
        assert found, "_INVESTIGATION_TRIAGE_ACTIONS not found in web_server.py"
        assert tuple(found[0]) == tuple(tools_module._INVESTIGATION_TRIAGE_ACTIONS)
        _, reg = _registry()
        enum = reg.tools["triage_investigation"]["schema"]["parameters"]["properties"]["action"]["enum"]
        assert tuple(enum) == tuple(found[0])
