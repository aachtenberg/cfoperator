"""The Plane MCP server config, and the one rule that fails silently.

The npm `@makeplane/plane-mcp-server` is a superseded generation whose
``get_issue_comments`` 500s on every call and will never be fixed. CFOP-157
moved us to upstream's Python server. Two things can quietly undo that:

* ``.mcp.json`` drifting back to the npm package (the 500 returns, and with it
  the local wrapper we deleted), and
* the PQL warning falling out of CLAUDE.md.

The second is the one worth a test. Every other defect in this stack announces
itself -- a 500, a validation error, a 404. PQL filters on Community Edition do
not: the server returns HTTP 200 and the *unfiltered* set, so a caller that
trusts ``pql`` gets a confident wrong answer with no signal at all. Measured
against the live instance: ``state = "In Progress"`` returned 100 rows, 97 of
which were not In Progress. That is only survivable while it stays written
down, which makes the note load-bearing rather than decorative.

These are static checks by necessity -- the real behaviour lives in a Plane
instance CI cannot reach. They guard the documentation of a hazard, not the
hazard itself, and that is the honest description of what they are worth.
"""

from repo_paths import REPO_ROOT
import json
import re

MCP_JSON = REPO_ROOT / ".mcp.json"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def plane_server():
    return json.loads(MCP_JSON.read_text())["mcpServers"]["plane"]


def test_plane_server_is_the_python_one():
    plane = plane_server()

    assert plane["command"] == "uvx", "the npm server's get_issue_comments 500s"
    assert plane["args"][-1] == "stdio"
    assert any("plane-mcp-server" in a for a in plane["args"])


def test_upstream_version_is_pinned():
    """An unpinned uvx spec picks up whatever shipped this morning."""
    spec = next(a for a in plane_server()["args"] if "plane-mcp-server" in a)

    assert re.fullmatch(r"plane-mcp-server@\d+\.\d+\.\d+", spec), (
        f"pin the version explicitly, got {spec!r}"
    )


def test_npm_server_is_gone_for_good():
    """The npm package and the wrapper that patched it must not come back."""
    raw = MCP_JSON.read_text()

    assert "@makeplane/plane-mcp-server" not in raw
    assert "npx" not in raw
    assert not (REPO_ROOT / "scripts" / "plane-mcp-server.mjs").exists(), (
        "the Node wrapper only existed to patch the npm server's broken URL"
    )


def test_self_hosted_base_url_is_configured():
    """Without PLANE_BASE_URL the server talks to api.plane.so, not our instance."""
    env = plane_server()["env"]

    assert env["PLANE_BASE_URL"].startswith("https://")
    assert "plane.so" not in env["PLANE_BASE_URL"]
    assert env["PLANE_WORKSPACE_SLUG"]
    assert env["PLANE_API_KEY"].startswith("${"), "never commit the key itself"


def test_claude_md_warns_that_pql_is_silently_ignored():
    """The only failure here that gives no signal at all, so it must stay written."""
    doc = CLAUDE_MD.read_text()

    assert "pql" in doc.lower(), "the PQL trap is undocumented"
    para = next(
        (p for p in re.split(r"\n\s*\n", doc) if "pql" in p.lower()), ""
    )
    assert "silent" in para.lower() or "lies" in para.lower(), (
        "CLAUDE.md mentions pql but not that it fails silently -- that is the "
        "whole point of the warning"
    )


def test_claude_md_does_not_describe_the_dead_npm_tools():
    """Stale tool names send a reader looking for tools that no longer exist."""
    doc = CLAUDE_MD.read_text()

    for dead in ("get_issue_using_readable_identifier", "add_issue_comment",
                 "get_projects", "list_states"):
        # Naming them as gone is fine; describing them as the way to work is not.
        # Checked per paragraph, not per line: the disclaimer routinely wraps
        # onto a different line from the tool name it disclaims.
        for para in re.split(r"\n\s*\n", doc):
            if dead in para:
                assert re.search(
                    r"predates|dead|no longer|do not exist|none of those", para, re.I
                ), f"{dead} is referenced as if current: {para.strip()[:120]}"
