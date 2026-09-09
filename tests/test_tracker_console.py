"""A filed row is noted in the console, not listed as work (CFOP-170).

The whole point of the hand-off is what the operator sees: a row handed to the
issue tracker leaves the active table for its own "Filed to tracker" section,
shows its key as a link, and the drawer says where it went and what happens
when it is closed over there. These run the page's own helpers under node, the
way test_console_change_record.py does, because what matters is what the
render produces for a given row — grepping the source cannot tell you that
``signature()`` forgot a field and the link never paints until something else
changes on the row.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
PAGE = UI / "remediations.html"


def _inline_script():
    blocks = re.findall(r"<script>(.*?)</script>", PAGE.read_text("utf-8"), re.S)
    assert len(blocks) == 1
    return blocks[0]


def _extract(names):
    """Lift page helpers out verbatim: a const up to the first line ending in
    ``;``, a function either on one line or up to its unindented ``}``."""
    src = _inline_script()
    got = []
    for name in names:
        m = (re.search(rf"^const {name} = .*?;$", src, re.S | re.M)
             or re.search(rf"^function {name}\(.*\}}\s*$", src, re.M)
             or re.search(rf"^function {name}\(.*?\n\}}", src, re.S | re.M))
        assert m, f"{name} not found in remediations.html"
        got.append(m.group(0))
    return "\n".join(got)


FILED_ROW = {
    "id": 42, "status": "filed", "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.7,
    "host_id": "pi2", "investigation_id": 9, "attempts": 0, "pr_url": None, "named_pr_url": None,
    "last_error": None, "created_at": "2026-09-09T10:00:00",
    "tracker_url": "https://plane.example/ws/projects/p/issues/i", "tracker_key": "CFOP-170",
    "tracker_state": "filed",
    "result": {"tracker": {"ref": "R", "key": "CFOP-170", "url": "https://plane.example/ws/projects/p/issues/i",
                           "synced_status": "filed", "synced_at": "2026-09-09T10:01:00",
                           "error": "comment failed (0): refused", "error_count": 2}},
    "payload": {"recommendation": "bump the limit"},
}
PLAIN_ROW = {**FILED_ROW, "status": "needs-human", "tracker_url": None, "tracker_key": None,
             "tracker_state": None, "result": None}


def _node(harness):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _render(row):
    harness = f"""
      const esc = s => String(s==null?'':s).replace(/[&<>"']/g,
        c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      const safeUrl = u => {{ const s = String(u||'').trim(); return /^https?:\\/\\//i.test(s) ? s : ''; }};
      const badge = (t,c) => `<span class="badge">${{esc(t)}}</span>`;
      const age = () => 'now';
      const color = c => c;
      const changeRecord = r => null;
      {_extract(["STATUS", "SC", "RC", "TERMINAL", "FILED", "trackerInfo", "trackerChip", "trackerCopy",
                 "trackerHtml", "rowHtml"])}
      const row = {json.dumps(row)};
      console.log(JSON.stringify({{
        chip: trackerChip(row), copy: trackerCopy(row), block: trackerHtml(row), row: rowHtml(row),
        filedInStatus: STATUS.includes('filed'), filedColour: SC.filed || null,
      }}));
    """
    return _node(harness)


def test_filed_row_paints_its_key_as_a_link_everywhere():
    out = _render(FILED_ROW)
    assert "CFOP-170" in out["chip"] and 'href="https://plane.example/ws/projects/p/issues/i"' in out["chip"]
    assert "CFOP-170" in out["row"] and "plane.example" in out["row"]
    assert "synced filed" in out["block"] and "2026-09-09T10:01:00" in out["block"]
    assert "last tracker error: comment failed (0): refused (2×)" in out["block"]
    assert "groom or discard it there" in out["copy"] and "CFOP-170" in out["copy"]
    assert out["filedInStatus"] is True and out["filedColour"]


def test_row_without_a_tracker_item_paints_nothing_for_it():
    out = _render(PLAIN_ROW)
    assert out["chip"] == "" and out["block"] == ""
    assert "tracker" not in out["row"].lower()


def test_tracker_link_is_href_safe():
    row = {**FILED_ROW, "tracker_url": "javascript:alert(1)", "result": {"tracker": {"url": "javascript:alert(1)", "key": "X"}}}
    out = _render(row)
    assert "javascript:" not in out["chip"] and "javascript:" not in out["row"] and "javascript:" not in out["block"]


def test_render_buckets_filed_rows_apart_from_active_and_closed():
    """The whole point: a filed row is not in the active table."""
    harness = f"""
      const esc = s => String(s==null?'':s);
      const safeUrl = u => String(u||'');
      const badge = (t,c) => t; const age = () => ''; const color = c => c; const changeRecord = r => null;
      const store = {{}};
      const el = id => (store[id] = store[id] || {{innerHTML:'', hidden:null, textContent:''}});
      global.document = {{ getElementById: el, activeElement: null }};
      {_extract(["STATUS", "SC", "RC", "TERMINAL", "FILED", "focusedRow", "refocusRow", "rowHtml", "render"])}
      render([
        {{...{json.dumps(PLAIN_ROW)}, id: 1, status: 'needs-human'}},
        {{...{json.dumps(FILED_ROW)}, id: 2}},
        {{...{json.dumps(PLAIN_ROW)}, id: 3, status: 'resolved'}},
        {{...{json.dumps(PLAIN_ROW)}, id: 4, status: 'queued'}},
      ]);
      const ids = html => (html.match(/id="row-(\\d+)"/g) || []).map(s => Number(s.match(/\\d+/)[0]));
      console.log(JSON.stringify({{
        active: ids(el('rows').innerHTML), filed: ids(el('rows-filed').innerHTML), done: ids(el('rows-done').innerHTML),
        filedHidden: el('filed-section').hidden, filedCount: el('filed-count').textContent,
      }}));
    """
    out = _node(harness)
    assert out["active"] == [1, 4]
    assert out["filed"] == [2] and out["filedHidden"] is False and out["filedCount"] == "(1)"
    assert out["done"] == [3]


def test_page_wiring_for_the_hand_off():
    src = _inline_script()
    page = PAGE.read_text("utf-8")
    # The poll signature must see the tracker fields, or the link never paints
    # until something unrelated changes on the row (the CFOP-139 trap).
    sig = re.search(r"function signature\(\)\{.*?\n\}", src, re.S).group(0)
    assert "r.tracker_url" in sig and "r.tracker_state" in sig
    assert "queue_tracker:'tracker'" in src
    assert "confirmTracker" in src and "queue_tracker" in re.search(r"function toggleFlag.*?\n\}", src, re.S).group(0)
    assert 'id="filed-section"' in page and 'id="rows-filed"' in page and 'id="filed-count"' in page
    assert "'filed'" in re.search(r"const show = \[.*?\];", src).group(0)


def test_remediation_row_dict_exposes_the_tracker_fields_and_tolerates_no_result():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "agent"))
    from knowledge_base import remediation_row_dict

    class Row:
        id = 1; status = "filed"; remediation_class = "manual"; risk = "low"; confidence = None
        host_id = "h"; investigation_id = None; priority = 0; attempts = 0; pr_url = None
        last_error = None; created_at = None; claimed_at = None; completed_at = None; payload = {}
        result = {"tracker": {"url": "https://t/x", "key": "CFOP-1", "synced_status": "filed"}}

    d = remediation_row_dict(Row())
    assert (d["tracker_url"], d["tracker_key"], d["tracker_state"]) == ("https://t/x", "CFOP-1", "filed")
    Row.result = None
    d = remediation_row_dict(Row())
    assert (d["tracker_url"], d["tracker_key"], d["tracker_state"]) == (None, None, None)
    Row.result = {"tracker": "garbage"}
    assert remediation_row_dict(Row())["tracker_url"] is None
