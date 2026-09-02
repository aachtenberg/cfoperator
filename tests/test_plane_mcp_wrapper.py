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

The stdout tests matter more than they look: stdout is the MCP JSON-RPC channel.
The wrapper's own logging is easy to keep on stderr; the real hazard is the npm
child on first launch, which is chatty and whose dependency postinstalls print.
Hand that child fd 1 and every response the host parses is corrupted. So the
channel is guarded twice -- once with the package already present (no spawn),
and once through ``install()`` with a deliberately noisy stub ``npm``.
"""

from repo_paths import REPO_ROOT
import json
import shutil
import subprocess

import pytest

ROOT = REPO_ROOT
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


@needs_node
def test_install_output_never_reaches_stdout(tmp_path):
    """The install child is the process that can actually corrupt the channel.

    The test above never reaches ``install()`` -- the stub tree already exists,
    so npm is never spawned. But npm is the only child here, and it is chatty:
    ``@scarf/scarf`` is a dependency of 0.1.4 and its postinstall ``console.log``s.
    Handing that child fd 1 puts it on the JSON-RPC stream the host is parsing.

    So: no package tree, and a stub ``npm`` on PATH that writes to stdout the way
    a postinstall or an npm notice would. Nothing may reach our stdout.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pkg = (tmp_path / "cfoperator" / "plane-mcp" / "0.1.4" / "node_modules"
           / "@makeplane" / "plane-mcp-server")
    npm = bindir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        "echo 'npm notice: a new version of npm is available'\n"
        "echo 'scarf: analytics postinstall chatter'\n"
        f"mkdir -p '{pkg}/build/tools'\n"
        f"echo 'process.exit(0);' > '{pkg}/build/index.js'\n"
        f"cat > '{pkg}/build/tools/issues.js' <<'EOF'\n"
        f"{UPSTREAM_GET}{UPSTREAM_POST}"
        "EOF\n"
    )
    npm.chmod(0o755)

    result = subprocess.run(
        ["node", str(WRAPPER)],
        cwd=ROOT,
        env={"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin",
             "XDG_CACHE_HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60,
    )

    assert result.stdout == "", f"install output reached stdout: {result.stdout!r}"
    # The chatter still has to be visible somewhere, and stderr is the safe place.
    assert "npm notice" in result.stderr
    # And the install path must still end with a patched package.
    assert "/comments/`);" in (pkg / "build" / "tools" / "issues.js").read_text()


@needs_node
def test_install_skips_dependency_scripts(tmp_path):
    """Belt and braces: postinstalls never run, so scarf cannot print at all."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv = tmp_path / "argv.txt"
    npm = bindir / "npm"
    npm.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv}'\n")
    npm.chmod(0o755)

    subprocess.run(
        ["node", str(WRAPPER)],
        cwd=ROOT,
        env={"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin",
             "XDG_CACHE_HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60,
    )

    assert "--ignore-scripts" in argv.read_text().splitlines()


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
