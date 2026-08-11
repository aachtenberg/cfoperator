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
