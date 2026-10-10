"""The drawer shows every refused node-action command (CFOP-319).

``last_error`` names the first command the allowlist refused; the row now
carries all of them in ``result.blocked_commands``, from either gate.

Run under node like ``test_console_change_record.py``: what matters is what
the page's own helpers render for a given row.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"


def _render(name, value):
    """Run one of the page's own helpers, lifted verbatim, over one value."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    src = re.findall(r"<script>(.*?)</script>", (UI / "remediations.html").read_text("utf-8"), re.S)
    assert len(src) == 1
    m = re.search(rf"^function {name}\(.*?^}}", src[0], re.S | re.M)
    assert m, f"{name}() is gone from remediations.html"
    harness = f"""
      const esc = s => String(s==null?'':s).replace(/[&<>"']/g,
        c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      {m.group(0)}
      const v = {json.dumps(value)};
      console.log(JSON.stringify({name}(v)));
    """
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_every_blocked_command_is_listed_with_its_reason():
    row = {"result": {"blocked_commands": [
        {"command": "docker restart y", "binary": "docker", "kind": "not_allowlisted",
         "reason": "binary not in allowlist: docker"},
        {"command": "rm -rf /z", "binary": "rm", "kind": "denied_binary",
         "reason": "binary is explicitly denied: rm"},
        {"command": "", "binary": "", "kind": "too_many", "reason": "plan has too many commands (5 > 4)"},
    ]}}
    html = _render("blockedCommandsHtml", row)
    assert "3 refused" in html
    for text in ("docker restart y", "binary not in allowlist: docker",
                 "rm -rf /z", "explicitly denied", "(plan)", "too many commands"):
        assert text in html, text


def test_blocked_commands_are_escaped():
    row = {"result": {"blocked_commands": [{"command": "<img src=x onerror=alert(1)>",
                                            "reason": "shell metacharacter"}]}}
    html = _render("blockedCommandsHtml", row)
    assert "<img" not in html and "&lt;img" in html


def test_rows_without_refusals_render_nothing():
    assert _render("blockedCommandsHtml", {"result": {}}) == ""
    assert _render("blockedCommandsHtml", {}) == ""


def test_the_drawer_shows_the_blocked_commands():
    assert "${blockedCommandsHtml(r)}" in (UI / "remediations.html").read_text("utf-8")
