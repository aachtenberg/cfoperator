"""A sweep report that is a document is shown as one (CFOP-708).

A morning summary is stored as a sweep report. Its only "finding" is the first
500 characters of a markdown document; the document is ``sweep_meta.full_text``.
Both pages that show the latest sweep printed the finding as text, so the
operator saw markdown source on one line, cut mid-word ("### Current System
Heal").

What is guarded is the class, not the two pages: any console page that reads
``/api/sweep-reports`` to show findings has to ask ``sweepDoc`` first and
render what it gets, and the rules that make rendered markdown readable live
in one place.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
COMMON_JS = UI / "common.js"

#: Pages that fetch the sweep reports AND print a finding. nav.js reads the
#: same endpoint for an id and a clock time and prints no finding, and it is
#: not a page.
SWEEP_PAGES = sorted(p.name for p in UI.glob("*.html")
                     if "/api/sweep-reports" in p.read_text(encoding="utf-8"))


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def test_the_pages_that_show_a_sweep_were_found():
    """A wrong root, or a renamed endpoint, would make every per-page test
    below pass over nothing."""
    assert {"inbox.html", "index.html"} <= set(SWEEP_PAGES), SWEEP_PAGES


@pytest.mark.parametrize("page", SWEEP_PAGES)
def test_a_page_showing_the_sweep_renders_the_document(page):
    html = read(page)
    assert "sweepDoc(" in html, (
        f"{page} shows the latest sweep without asking sweepDoc(): a morning summary "
        "will print as a 500-character fragment of markdown source")
    assert "renderMarkdown(doc)" in html, f"{page} has the document and does not render it"
    assert re.search(r'class="[^"]*\bmd\b', html), (
        f"{page} renders the document outside a .md block, so it gets no markdown styling")


@pytest.mark.parametrize("page", SWEEP_PAGES)
def test_a_page_showing_the_sweep_can_parse_markdown(page):
    """renderMarkdown escapes when marked is absent: safe, and exactly the
    wall of source this issue is about. common.js must also be there first."""
    html = read(page)
    assert 'src="/vendor/marked.min.js"' in html, f"{page} does not load the vendored marked"


def test_the_markdown_rules_are_the_stylesheets():
    css = read("console.css")
    assert re.search(r"^\.md \{", css, re.M), "console.css lost the shared .md block"
    for page in sorted(p.name for p in UI.glob("*.html")):
        assert not re.search(r"^\s*\.md\s*\{", read(page), re.M), (
            f"{page} restates .md; the markdown rules are one copy in console.css")


def test_the_findings_list_rules_do_not_reach_the_document():
    """Inbox styles a finding as a flex row with a dot. Written as ``.sweep
    li`` that rule also caught the bullets inside the rendered summary, which
    sits in the same card, and squeezed each bold lead-in into a narrow
    column. The findings list is addressed by its id."""
    html = read("inbox.html")
    assert not re.search(r"\.sweep\s+(ul|li)\b", html), (
        "a .sweep ul/li rule styles the summary's own lists as finding rows")
    assert re.search(r"#sweep li\s*\{", html)


# --------------------------------------------------------------------------
# the same document, as an alert on the Events page
# --------------------------------------------------------------------------

def _summary_line(tmp_path, values):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    fn = re.search(r"^function summaryLine\(s\)\{.*?\n\}", read("events.html"), re.S | re.M)
    assert fn, "events.html has no summaryLine()"
    stub = tmp_path / "stub.js"
    stub.write_text(fn.group(0) + "\nconsole.log(JSON.stringify(" + json.dumps(values) + ".map(summaryLine)));",
                    encoding="utf-8")
    out = subprocess.run([node, str(stub)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_alert_that_is_a_document_shows_its_first_line(tmp_path):
    """A sweep posts the morning summary as an alert whose summary is the
    markdown itself; 9 of the last 50 sweep alerts on the live console were.
    Run together in a table row that read "# Morning Infrastructure Summary …
    ### Overnight Activity Highlights - **`api-server`…"."""
    got = _summary_line(tmp_path, [
        "# Morning Infrastructure Summary - 2026-11-15 11:00\n\n### Overnight Activity Highlights\n- **`api-server`**",
        "\n\n## 1. Overnight Activity Highlights\r\n* **AppArmor Denial**",
        "CronJob controller errors due to object modification conflicts on node-a",
        "disk #3 is failing on node-b", "### Heading ###", None, "",
    ])
    assert got == [
        "Morning Infrastructure Summary - 2026-11-15 11:00",
        "1. Overnight Activity Highlights",
        # An ordinary one-line summary is untouched, a '#' inside it included.
        "CronJob controller errors due to object modification conflicts on node-a",
        "disk #3 is failing on node-b", "Heading", "", "",
    ]


def test_the_events_row_and_drawer_title_use_it():
    page = read("events.html")
    assert "esc(summaryLine(r.summary).slice(0,160))" in page, "the row prints the summary as it came"
    assert "const title=summaryLine(a.summary)||a.alert_id;" in page, "the drawer's title is the whole document"


_STUB = r"""
const fs=require('fs'), vm=require('vm');
const box={console,JSON,Array,Object,String,Number,Date,Math,document:{documentElement:{}}};
box.globalThis=box; vm.createContext(box); vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),box);
const d=box.sweepDoc;
console.log(JSON.stringify({
  summary: d({sweep_meta:{type:'morning_summary', full_text:'# Morning\n\n- **a**\n'}}),
  plain: d({findings:[{finding:'pod x restarted'}], sweep_meta:{sweep_backend:'ollama'}}),
  noMeta: d({findings:[]}), nothing: d(null), blank: d({sweep_meta:{full_text:'  \n '}}),
  notText: d({sweep_meta:{full_text:{a:1}}}),
}));
"""


@pytest.fixture(scope="module")
def sweep_doc(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("sweepdoc") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(COMMON_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_report_carrying_a_document_gives_it_back(sweep_doc):
    assert sweep_doc["summary"] == "# Morning\n\n- **a**"


def test_an_ordinary_sweep_has_no_document(sweep_doc):
    """Its findings are short one-line strings and keep their list."""
    assert sweep_doc["plain"] == "" and sweep_doc["noMeta"] == "" and sweep_doc["nothing"] == ""


def test_a_blank_or_non_text_document_is_not_one(sweep_doc):
    assert sweep_doc["blank"] == "" and sweep_doc["notText"] == ""
