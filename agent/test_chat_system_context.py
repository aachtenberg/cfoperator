"""Guard: chat system prompt lists every registered tool (CFOP-22 C)."""

from unittest.mock import MagicMock

from agent import CFOperator


def _schemas(*names_and_descs):
    return [
        {"type": "function", "function": {"name": n, "description": d}}
        for n, d in names_and_descs
    ]


def test_chat_system_context_lists_every_registered_tool():
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {"pi": {"address": "10.0.0.1", "role": "node"}}}}
    op.current_investigation = None
    op.last_sweep = 0
    op.kb.find_learnings.return_value = []
    op.tools.get_schemas.return_value = _schemas(
        ("prometheus_query", "Query Prometheus metrics."),
        ("list_remediations", "List remediation queue rows."),
        ("get_remediation", "Fetch a remediation by id."),
        ("fake_guard_tool", "Synthetic tool for the mutation check."),
    )
    prompt = CFOperator._build_chat_system_context(op)
    for name in ("prometheus_query", "list_remediations", "get_remediation", "fake_guard_tool"):
        assert name in prompt, f"missing tool {name} in prompt"


def test_chat_system_context_mutation_fails_when_builder_ignores_registry():
    """If the builder went back to a hand-written list, a new schema would be absent."""
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {}}}
    op.current_investigation = None
    op.last_sweep = 0
    op.kb.find_learnings.return_value = []
    op.tools.get_schemas.return_value = _schemas(
        ("brand_new_tool", "Only exists in the registry."),
    )
    prompt = CFOperator._build_chat_system_context(op)
    assert "brand_new_tool" in prompt
    # The old hand-written bullets mentioned Prometheus without tool names —
    # ensure we are not still emitting that as the sole capability list.
    assert "brand_new_tool:" in prompt
