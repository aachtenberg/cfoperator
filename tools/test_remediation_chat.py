"""Chat tools for the remediation queue and investigation-by-id (CFOP-22 B)."""

from unittest.mock import MagicMock

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

    def test_rejected_is_allowed(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 5, "status": "needs-human", "claimed_at": None,
            "completed_at": None, "payload": {},
        }
        op.kb.update_remediation_status.return_value = True
        reg.execute("resolve_remediation", {
            "remediation_id": 5, "note": "wrong fix", "status": "rejected"})
        assert op.kb.update_remediation_status.call_args[0][1] == "rejected"

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

    def test_refuses_a_row_the_executor_is_running(self):
        # Closing a claimed row strands the Job that will later POST
        # /v1/remediations/<id>/complete against a row that moved on.
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 12, "status": "in-progress", "claimed_at": "2026-08-28T12:00:00",
            "completed_at": None, "payload": {},
        }
        out = reg.execute("resolve_remediation", {
            "remediation_id": 12, "note": "nope"})
        assert "claimed by the executor" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    def test_refuses_a_claimed_row_whatever_its_status(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = {
            "id": 13, "status": "queued", "claimed_at": "2026-08-28T12:00:00",
            "completed_at": None, "payload": {},
        }
        out = reg.execute("resolve_remediation", {
            "remediation_id": 13, "note": "nope"})
        assert "error" in out
        op.kb.update_remediation_status.assert_not_called()

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
