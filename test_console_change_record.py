"""The node-action change-record gate has to be visible in the console (CFOP-139).

A ``node-action`` is the one remediation class that runs shell on a host, and
its second gate is a PR whose merge authorises exactly that. The first live one
(row #95) showed the console rendering only::

    status      queued
    last error  awaiting change-record approval

with no link to the PR the operator is asked to merge and no view of the
commands it authorises. The gate existed and was invisible: you would have had
to know to go look in the infra repo.

The data was always on the row -- ``result.change_record.url`` and
``.plan.commands`` -- it simply was not read. It deliberately does NOT live in
``pr_url``: that column is the executor-opened gitops PR, and putting the record
there would trip the "a PR is already open, Approve is off" note and the
CFOP-116 reconciler, neither of which applies here. So these guard that the
separate path stays rendered, and that it stays separate.

Run under node like ``test_console_drawer.py``, because what matters is what the
render *produces* for a given row, which grepping the source cannot tell you.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
UI = REPO_ROOT / "ui"

_URL = "https://github.com/aachtenberg/homelab-infra/pull/119"
_CMD = "systemctl is-active sshd"


def _inline_script():
    blocks = re.findall(r"<script>(.*?)</script>", (UI / "remediations.html").read_text("utf-8"), re.S)
    assert len(blocks) == 1
    return blocks[0]


def _render(row):
    """Run the page's own changeRecord* helpers over one row, under node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    harness = f"""
      const TERMINAL = new Set(['resolved','rejected','failed']);
      const esc = s => String(s==null?'':s).replace(/[&<>"']/g,
        c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      const safeUrl = u => {{
        const s = String(u||'').trim();
        return /^https?:\\/\\//i.test(s) ? s : '';
      }};
      {_extract_helpers()}
      const row = {json.dumps(row)};
      console.log(JSON.stringify({{
        chip: changeRecordChip(row),
        block: changeRecordHtml(row),
      }}));
    """
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _extract_helpers():
    """Lift the three changeRecord* functions out of the page verbatim."""
    src = _inline_script()
    got = []
    for name in ("changeRecord", "changeRecordChip", "changeRecordHtml"):
        m = re.search(rf"^function {name}\(.*?^}}", src, re.S | re.M)
        assert m, f"{name}() is gone from remediations.html — the gate render was removed"
        got.append(m.group(0))
    return "\n".join(got)


def _row(**over):
    row = {
        "id": 95, "status": "queued", "host_id": "raspberrypi2",
        "remediation_class": "node-action", "pr_url": None, "named_pr_url": None,
        "last_error": "awaiting change-record approval",
        "result": {"change_record": {"url": _URL, "ref": "opaque",
                                     "plan": {"host": "raspberrypi2", "commands": [_CMD]}}},
    }
    row.update(over)
    return row


def test_the_record_pr_is_reachable_from_the_row():
    """Without this an operator sees 'awaiting approval' and no way to approve."""
    out = _render(_row())
    assert _URL in out["chip"], "no chip linking to the change-record PR"
    assert _URL in out["block"], "the drawer body does not name the record PR"


def test_the_commands_are_shown_before_they_run():
    """The single most useful thing while deciding: what will execute on the host.

    The plan is generated BEFORE the record opens precisely so what is approved
    is byte-for-byte what runs; showing it is the point of that ordering.
    """
    out = _render(_row())
    assert _CMD in out["block"]
    assert "raspberrypi2" in out["block"]


def test_a_waiting_row_reads_as_waiting_not_broken():
    out = _render(_row())
    assert "waiting on a human" in out["block"]
    # and a finished row drops the call to action rather than still nagging
    done = _render(_row(status="resolved"))
    assert "waiting on a human" not in done["block"]
    assert _URL in done["block"], "a resolved row should still show its record"


def test_rows_without_a_change_record_render_nothing():
    """gitops-patch and manual rows must not grow an empty section."""
    out = _render(_row(result={}))
    assert out["chip"] == ""
    assert out["block"] == ""
    assert _render(_row(result={"change_record": None}))["block"] == ""


def test_a_record_without_a_plan_says_so_rather_than_showing_an_empty_box():
    out = _render(_row(result={"change_record": {"url": _URL}}))
    assert "no command plan recorded yet" in out["block"]
    assert _URL in out["block"]


def test_the_record_url_is_sanitised_like_every_other_link():
    """safeUrl is the console's rule; a record URL is not exempt from it."""
    out = _render(_row(result={"change_record": {"url": "javascript:alert(1)",
                                                 "plan": {"commands": [_CMD]}}}))
    assert "javascript:" not in out["chip"]
    assert "javascript:" not in out["block"]
    assert out["chip"] == "", "an unsafe URL must not produce a chip"


def test_the_change_record_does_not_ride_on_pr_url():
    """It must stay off the tracked column.

    pr_url drives 'a PR is already open ... Approve is off' and the CFOP-116
    reconciler. A change record is neither, so if some future change starts
    stamping it there, this fails and the author has to think about both.
    """
    src = _inline_script()
    m = re.search(r"^function changeRecord\(.*?^}", src, re.S | re.M)
    assert "pr_url" not in m.group(0), \
        "changeRecord() must read result.change_record, not the tracked pr_url column"


def test_the_helpers_are_actually_wired_into_the_drawer():
    """Rendering correctly is worth nothing if nothing calls it.

    The behavioural tests above lift the helpers out and run them in isolation,
    which means deleting the call sites leaves them all green while the console
    goes straight back to hiding the gate. Mutation-checking caught that; this
    closes it. Cheap and source-level on purpose -- the question is only
    "is it invoked", which a render harness cannot answer about itself.
    """
    src = _inline_script()
    # The chip belongs in the action row, the block in the drawer body.
    assert "${changeRecordChip(r)}" in src, \
        "nothing renders the change-record chip — the PR is unreachable again"
    assert "${changeRecordHtml(r)}" in src, \
        "nothing renders the change-record block — the commands are hidden again"
    # And the block must sit after last_error, so "awaiting change-record
    # approval" is immediately followed by the record it refers to.
    assert src.index("${r.last_error?") < src.index("${changeRecordHtml(r)}"), \
        "the record should follow the last_error line it explains"
