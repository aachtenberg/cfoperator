"""The Plane MCP wrapper has to keep fixing upstream's broken comments URL.

``get_issue_comments`` 500s in every published version of
``@makeplane/plane-mcp-server`` because the GET path is built without a
trailing slash. That is expensive rather than merely annoying: an agent that
calls the tool gets an opaque 500, retries, and burns context. The wrapper in
``scripts/plane-mcp-server.mjs`` patches the installed package before running
it, so this guards the two ways that silently stops working:

* ``.mcp.json`` drifts back to launching upstream directly (the bug returns
  wholesale), and
* the patch stops being applied, or is applied to the wrong statement.

The patch is exercised by **running the real wrapper** against a stub package
tree, not by grepping the source for a slash -- a wrapper that computed the
right replacement and never wrote it would pass that. ``XDG_CACHE_HOME`` points
the wrapper at the stub, so no network and no npm install is involved.

The stdout test matters more than it looks: stdout is the MCP JSON-RPC channel,
so a stray ``console.log`` anywhere in the wrapper corrupts every response.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parent
WRAPPER = ROOT / "scripts" / "plane-mcp-server.mjs"
MCP_JSON = ROOT / ".mcp.json"

# The real upstream statements, copied verbatim from build/tools/issues.js.
UPSTREAM_GET = (
    "        const comments = await makePlaneRequest(\"GET\", "
    "`workspaces/${process.env.PLANE_WORKSPACE_SLUG}/projects/${project_id}"
    "/issues/${issue_id}/comments`);\n"
)
UPSTREAM_POST = (
    "        const response = await makePlaneRequest(\"POST\", "
    "`workspaces/${process.env.PLANE_WORKSPACE_SLUG}/projects/${project_id}"
    "/issues/${issue_id}/comments/`, {\n"
)

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def build_stub(tmp_path, issues_src):
    """A minimal package tree where the wrapper expects the installed server."""
    pkg = (tmp_path / "cfoperator" / "plane-mcp" / "0.1.4" / "node_modules"
           / "@makeplane" / "plane-mcp-server")
    (pkg / "build" / "tools").mkdir(parents=True)
    # Stands in for the real server: exits immediately so the wrapper's
    # `await import(ENTRY)` has something to load.
    (pkg / "build" / "index.js").write_text("process.exit(0);\n")
    (pkg / "build" / "tools" / "issues.js").write_text(issues_src)
    return pkg / "build" / "tools" / "issues.js"


def run_wrapper(tmp_path):
    return subprocess.run(
        ["node", str(WRAPPER)],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "XDG_CACHE_HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60,
    )


@needs_node
def test_patch_adds_the_missing_trailing_slash(tmp_path):
    target = build_stub(tmp_path, UPSTREAM_GET + UPSTREAM_POST)

    run_wrapper(tmp_path)

    patched = target.read_text()
    assert "/comments/`);" in patched, "the GET path was not given its slash"
    assert "/comments`);" not in patched
    # The POST statement was already correct and must come through untouched.
    assert UPSTREAM_POST in patched


@needs_node
def test_patch_is_idempotent(tmp_path):
    target = build_stub(tmp_path, UPSTREAM_GET + UPSTREAM_POST)

    run_wrapper(tmp_path)
    once = target.read_text()
    run_wrapper(tmp_path)

    assert target.read_text() == once
    assert once.count("/comments/`);") == 1


@needs_node
def test_unrecognised_upstream_is_left_alone_and_warns(tmp_path):
    """If upstream restructures, run unpatched rather than corrupt the file."""
    novel = "const x = await makePlaneRequest(\"GET\", `something/else/`);\n"
    target = build_stub(tmp_path, novel)

    result = run_wrapper(tmp_path)

    assert target.read_text() == novel
    assert "WARNING" in result.stderr


@needs_node
def test_wrapper_never_writes_to_stdout(tmp_path):
    """stdout is the MCP channel; anything written there corrupts responses."""
    build_stub(tmp_path, UPSTREAM_GET + UPSTREAM_POST)

    result = run_wrapper(tmp_path)

    assert result.stdout == "", f"wrapper wrote to stdout: {result.stdout!r}"
    # It should still be reporting what it did -- on stderr.
    assert "patched" in result.stderr


def test_mcp_json_launches_the_wrapper():
    """Guards a revert to `npx @makeplane/plane-mcp-server`, which reinstates the bug."""
    plane = json.loads(MCP_JSON.read_text())["mcpServers"]["plane"]

    assert plane["command"] == "node"
    assert plane["args"] == ["scripts/plane-mcp-server.mjs"]
    assert WRAPPER.exists()


def test_upstream_version_is_pinned_exactly():
    """A floating range would silently pick up 0.1.5 and its axios 1.12.0 pin."""
    src = WRAPPER.read_text()

    assert 'const VERSION = "0.1.4";' in src, "the pin moved -- see commit 9e55c55"
    assert "^" not in src.split("const SPEC")[1].split("\n")[0]
