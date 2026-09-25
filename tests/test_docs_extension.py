"""The extension docs must describe code that exists (CFOP-209).

The "Writing your own backend" section described a contract the code never
had: a query_metric() call that did not exist, a registry the config loader
did not read, and a startup failure that never happened. Nothing checked it.
These tests do, for the class rather than today's wording:

- the backend matrix's Shipped names are exactly the names the agent accepts;
- every documented plugin example with a register() loads through the real
  loader and registers something.
"""

from __future__ import annotations

import ast
import re
import textwrap

import pytest

from repo_paths import REPO_ROOT

INFRA_DOC = REPO_ROOT / "docs" / "infrastructure-config.md"
PLUGIN_DOCS = [REPO_ROOT / "docs" / "event-runtime-quickstart.md", INFRA_DOC]
# The matrix rows whose backends only the agent wires. The Notifications row is
# shared with the event runtime (ntfy lives only there), so it is used for the
# "nothing accepted is undocumented" direction, not the other.
AGENT_ROWS = ("Metrics", "Logs", "Containers", "Alerts (ingest)")


def _accepted_backend_names() -> set[str]:
    """Every string _init_observability_backends compares a backend name to."""
    tree = ast.parse((REPO_ROOT / "agent" / "agent.py").read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_init_observability_backends"
    )

    def is_backend_name(expr: ast.AST) -> bool:
        # x.get('backend')  or  backend_type
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get":
            return bool(expr.args) and isinstance(expr.args[0], ast.Constant) and expr.args[0].value == "backend"
        return isinstance(expr, ast.Name) and expr.id == "backend_type"

    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            if is_backend_name(node.left) and isinstance(node.comparators[0], ast.Constant):
                names.add(node.comparators[0].value)
    return names


def _shipped_names_in_matrix(rows=None) -> set[str]:
    names = set()
    for line in INFRA_DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] and (rows is None or cells[0] in rows) and cells[0] != "Capability":
            names.update(re.findall(r"`([a-z0-9_-]+)`", cells[1]))
    return names


def test_the_matrix_ships_exactly_what_the_agent_accepts():
    """Both directions: no documented backend the agent lacks, no accepted backend the table omits."""
    accepted = _accepted_backend_names()
    assert accepted, "found no backend comparisons; the AST walk no longer matches the function"
    claimed = _shipped_names_in_matrix(AGENT_ROWS)
    assert claimed <= accepted, f"the table ships {sorted(claimed - accepted)}, which the agent does not accept"
    undocumented = accepted - _shipped_names_in_matrix()
    assert not undocumented, f"the agent accepts {sorted(undocumented)}, which the table's Shipped column omits"


def _plugin_examples():
    for doc in PLUGIN_DOCS:
        text = doc.read_text(encoding="utf-8")
        for i, block in enumerate(re.findall(r"```python\n(.*?)```", text, flags=re.S)):
            if "def register(plugins, context)" in block:
                yield pytest.param(textwrap.dedent(block), id=f"{doc.name}#{i}")


EXAMPLES = list(_plugin_examples())


def test_there_is_a_documented_plugin_example():
    assert EXAMPLES, "no python block with register(plugins, context) in the plugin docs"


@pytest.mark.parametrize("source", EXAMPLES)
def test_every_documented_plugin_example_loads_and_registers(source, tmp_path, monkeypatch):
    from event_runtime.external_plugins import PluginContext, load_external_plugins
    from event_runtime.plugin_manager import PluginManager

    (tmp_path / "cfop209_doc_example.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    plugins = PluginManager()
    load_external_plugins(plugins, PluginContext(), raw="cfop209_doc_example")
    registered = (
        plugins.alert_sources + plugins.context_providers + plugins.notification_sinks
        + plugins.completion_observers + plugins.alert_policies + list(plugins.action_handlers.values())
    )
    assert registered, "the documented register() ran but registered nothing"
