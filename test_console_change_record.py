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
      // No TERMINAL here on purpose. The first version of this harness invented
      // one as {{resolved,rejected,failed}} while the page defines
      // {{resolved,rejected}} -- the same "test environment is not the page" trap
      // this suite exists to close. The render now keys off r.last_error, so
      // there is no set to get wrong.
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


def test_a_waiting_row_reads_as_waiting_and_a_failed_one_does_not():
    out = _render(_row())
    assert "waiting on a human" in out["block"]

    # A row the operator CLOSED without merging is failed by the 409 path -- and
    # `failed` is deliberately NOT terminal on this page, so it stays in the
    # active table. Telling that row to go merge the PR that was just closed is
    # exactly wrong, which is why `waiting` keys off the last_error sentinel
    # rather than off status.
    closed = _render(_row(status="failed", last_error="change record gate: closed without merge"))
    assert "waiting on a human" not in closed["block"]
    assert _URL in closed["block"], "a failed row should still show the record that failed it"

    # Same for a row already running: claimed is not waiting.
    running = _render(_row(status="claimed", last_error=None))
    assert "waiting on a human" not in running["block"]

    resolved = _render(_row(status="resolved", last_error=None))
    assert "waiting on a human" not in resolved["block"]
    assert _URL in resolved["block"], "a resolved row should still show its record"


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
    assert "changeRecordChip(r)" in src, \
        "nothing renders the change-record chip -- the PR is unreachable again"
    # It must REPLACE Approve while a record is pending, not sit beside it:
    # Approve on a waiting row is a no-op that contradicts the copy telling the
    # operator to go merge. Same shape as the gitops `pr_url ? Review PR : Approve`.
    approve_branch = re.search(r"changeRecordChip\(r\)\s*\n\s*\?\s*changeRecordChip\(r\)\s*\n\s*:\s*`<button class=\"chip\" onclick=\"approve", src)
    assert approve_branch, "the change-record chip must stand in for Approve while a record is pending"
    # And the queue LIST must LINK it -- the operator who found this had to open
    # the drawer because the row showed an em dash. Asserted on the href
    # expression itself: a first version checked only that changeRecord(r) was
    # mentioned, which survived dropping it from the link.
    assert re.search(r"const prHref = [^;]*crHref", src), \
        "the row list computes a change-record href but does not link it"
    assert "${changeRecordHtml(r)}" in src, \
        "nothing renders the change-record block — the commands are hidden again"
    # And the block must sit after last_error, so "awaiting change-record
    # approval" is immediately followed by the record it refers to.
    assert src.index("${r.last_error?") < src.index("${changeRecordHtml(r)}"), \
        "the record should follow the last_error line it explains"


def _signature(rows):
    """Run the page's own signature() over a row set, under node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    src = _inline_script()
    m = re.search(r"^function signature\(\).*?^}", src, re.S | re.M)
    assert m, "signature() is gone from remediations.html"
    harness = f"""
      const ALL = {json.dumps(rows)};
      {m.group(0)}
      console.log(JSON.stringify(signature()));
    """
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_attaching_a_change_record_rebuilds_the_list():
    """signature() must notice the record arriving, or the column never paints.

    load() only calls paint() when the signature changes. Attaching a record is
    routinely the ONLY thing that changes on a poll -- the row is queued before
    the gate opens the PR and queued after it -- so a signature blind to
    result.change_record leaves the list showing an em dash until something
    unrelated happens to the row. The fix that adds the column is worth nothing
    without this, and no source grep would catch it.
    """
    before = _row(result={})
    after = _row()  # same row, now carrying the record
    assert _signature([before]) != _signature([after]), \
        "signature() ignores result.change_record — the list will not repaint when a record arrives"


def test_the_signature_still_reacts_to_the_fields_it_always_did():
    """Guard against 'fixing' the above by making the signature indiscriminate."""
    base = _row(result={})
    assert _signature([base]) != _signature([_row(result={}, status="failed")])
    assert _signature([base]) == _signature([_row(result={})]), \
        "signature() is unstable for an unchanged row — the list would repaint every poll"
