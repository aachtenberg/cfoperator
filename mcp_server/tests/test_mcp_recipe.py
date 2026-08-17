"""The committed MCP recipe must stay a working config, not a stale snippet.

`docs/examples/cfoperator.mcp.json` is copy-pasted verbatim into an MCP host —
it is executable configuration that happens to live under docs/. The failure it
rots into is quiet: a renamed environment variable leaves a file that still
parses, still looks right, and produces a server running on defaults with the
wrong scope or no agent. So the recipe is checked against `Settings.from_env`
rather than against a copy of itself.
"""

import json
from pathlib import Path

import pytest

from mcp_server.config import SCOPE_IMPLIES, Settings

RECIPE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "examples" / "cfoperator.mcp.json"
)


@pytest.fixture(scope="module")
def recipe():
    return json.loads(RECIPE_PATH.read_text())


def test_recipe_exists_and_is_valid_json(recipe):
    assert recipe["mcpServers"], "recipe must define at least one server"


def test_stdio_entry_launches_the_server_this_repo_ships(recipe):
    entry = recipe["mcpServers"]["cfoperator"]
    assert entry["type"] == "stdio"
    assert entry["args"] == ["-m", "mcp_server"]


def test_recipe_env_vars_are_ones_the_server_actually_reads(recipe):
    """Mutation check: rename any key in the recipe's env block (or in
    Settings.from_env) and this goes red.

    Verified by construction rather than by a hardcoded list: each variable is
    set to a distinctive value and must change the resulting Settings.
    """
    env = recipe["mcpServers"]["cfoperator"]["env"]
    probes = {
        "CFOP_AGENT_URL": ("http://probe.invalid:1234", "agent_url",
                           "http://probe.invalid:1234"),
        "CFOP_MCP_SCOPES": ("investigate", "scopes",
                            SCOPE_IMPLIES["investigate"]),
    }
    for name, (value, attribute, expected) in probes.items():
        assert name in env, f"{name} disappeared from the recipe"
        settings = Settings.from_env({name: value})
        assert getattr(settings, attribute) == expected, (
            f"{name} no longer reaches Settings.{attribute}"
        )

    # CFOP_API_TOKEN is read by the client, not by Settings — it is the console
    # bearer the facade presents upstream, and the recipe must keep passing it
    # through or every call answers 401.
    assert env["CFOP_API_TOKEN"] == "${CFOP_API_TOKEN}"


def test_recipe_scopes_are_valid_and_never_remediate(recipe):
    """A shared host config handing an agent the approve button is exactly the
    mistake docs/cockpit.md warns about; the example must not model it."""
    scopes = recipe["mcpServers"]["cfoperator"]["env"]["CFOP_MCP_SCOPES"]
    requested = [s.strip() for s in scopes.split(",") if s.strip()]
    assert requested, "scopes must not be empty"
    for scope in requested:
        assert scope in SCOPE_IMPLIES, f"unknown scope {scope!r}"
    assert "remediate" not in requested


def test_http_entry_carries_a_bearer(recipe):
    """The streamable-http transport refuses to start unauthenticated, so a
    recipe without an Authorization header is a broken recipe."""
    entry = recipe["mcpServers"]["cfoperator-http"]
    assert entry["url"].endswith("/mcp")
    assert entry["headers"]["Authorization"].startswith("Bearer ")


def test_cockpit_doc_points_at_the_recipe():
    """The doc and the file are useless apart; keep the link honest."""
    doc = (RECIPE_PATH.parents[1] / "cockpit.md").read_text()
    assert "examples/cfoperator.mcp.json" in doc
