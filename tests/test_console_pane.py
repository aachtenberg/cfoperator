"""Two drawers, one pane (CFOP-414).

The investigations drawer was rebuilt as a pane in CFOP-696: a head, a meta
line, one action bar, cards, the long things folded. The remediations drawer
stayed a stack of label lines with the raw payload open under it, so the two
places an operator reads the same recommendation were two layouts.

They are one now, and what keeps them one is that the pane is not written
twice: its styles are ``console.css``'s and the fix renderer is
``common.js``'s. These guard that, and then run the remediations drawer for
real to hold the things the old one got wrong: raw JSON on screen, a
paragraph of ``last_error`` pushing the row off the page, and a green button
on a row that is already closed.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
PANE_PAGES = ["investigations.html", "remediations.html"]

#: What the pane is made of. A page that styles one of these itself has
#: started the second copy.
KIT = [".pane", ".p-head", ".p-meta", ".p-actions", ".pcard", ".pgrid", ".prow", ".fold"]
#: The fix renderer's output. A page that writes these itself draws its own fix.
FIX_MARKUP = ['class="tgt"', 'class="steps"', 'class="fix-meta"', 'class="ev"']


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def page_styles(name):
    return "\n".join(re.findall(r"<style>(.*?)</style>", read(name), re.S))


def inline_script(name):
    blocks = re.findall(r"<script>(.*?)</script>", read(name), re.S)
    assert len(blocks) == 1, f"{name} has {len(blocks)} inline scripts, expected 1"
    return blocks[0]


def defines(css, selector):
    """True when ``selector`` starts a rule: at a line start or after ``}``,
    not as a descendant of something else (``#detail.max .pcard`` is a page
    adjusting the kit, not defining it). Comments are dropped first: one that
    names the kit is not a rule."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.search(r"(?:^|})\s*" + re.escape(selector) + r"(?:\s*[{,>]|\s+[.\w#>*:\[])", css, re.M) is not None


# --------------------------------------------------------------------------
# one copy
# --------------------------------------------------------------------------

def test_the_selector_probe_can_tell_a_definition_from_a_use():
    """The checks below pass over nothing if this is wrong either way."""
    assert defines(".pcard { border:0; }", ".pcard")
    assert defines("  .p-head .x { float:none; }", ".p-head")
    assert defines("a{b:c}\n.pane > * { margin:0; }", ".pane")
    assert not defines("#detail.max .pcard { background:red; }", ".pcard")
    assert not defines(".pcard-other { border:0; }", ".pcard")
    assert not defines("/* the kit (.pane,\n   .fold, .rec) lives elsewhere */\n.other { a:b; }", ".fold")


@pytest.mark.parametrize("selector", KIT)
def test_the_stylesheet_owns_the_pane(selector):
    assert defines(read("console.css"), selector), f"console.css no longer defines {selector}"


@pytest.mark.parametrize("page", PANE_PAGES)
def test_a_page_does_not_restyle_the_pane(page):
    css = page_styles(page)
    assert css.strip(), f"{page} has no <style> to check"
    own = [s for s in KIT if defines(css, s)]
    assert not own, (
        f"{page} defines {own} itself. The pane's styles are console.css's: "
        "a second copy is how the two drawers stopped looking alike.")


@pytest.mark.parametrize("page", PANE_PAGES)
def test_a_page_builds_its_drawer_from_the_pane(page):
    js = inline_script(page)
    for cls in ('class="pane"', 'class="p-head"', 'class="p-meta"', 'class="p-actions"', 'class="pcard fold"'):
        assert cls in js, f"{page}'s drawer no longer uses {cls}"


@pytest.mark.parametrize("page", PANE_PAGES)
def test_a_page_draws_the_fix_with_the_shared_renderer(page):
    js = inline_script(page)
    assert "fixHtml(" in js, f"{page} no longer calls fixHtml()"
    own = [m for m in FIX_MARKUP if m in js]
    assert not own, f"{page} writes {own} itself; fixHtml() in common.js draws the fix for both drawers"


def test_the_shared_renderer_is_the_one_that_writes_the_fix():
    common = read("common.js")
    assert re.search(r"^function fixHtml\(", common, re.M)
    for markup in FIX_MARKUP:
        assert markup in common, f"common.js no longer writes {markup}"


# --------------------------------------------------------------------------
# the remediations drawer, run for real
# --------------------------------------------------------------------------

_XSS = "<img src=x onerror=alert(1)>"

_FIX = {
    "targets": [{"kind": "gitops-manifest", "id": "base/monitoring/kustomization.yaml",
                 "repo": "aachtenberg/homelab-infra"}],
    "steps": ["Locate the kustomization", "Add disableNameSuffixHash: true"],
    "verify": {"command": "kubectl get cronjob poller", "expect": "no hash suffix"},
    "observed": [{"source": "kubectl get cm", "value": "poller-7f9c"}],
    "rejected": [{"alternative": "pin the name by hand", "why_not": "drifts"}],
    "observed_check": {"status": "unverified", "reason": "could not read the file"},
}

_LONG = "Automated re-verification (deepseek/deepseek-v4-pro), 2026-11-19T23:12:02+00:00.\n\n" + \
    "The evidence contradicts the claim. " * 12

ROWS = {
    # A filed manual row: the screenshot that started this (#583).
    "1": {"id": 1, "status": "filed", "remediation_class": "manual", "risk": "low", "confidence": 0.92,
          "host_id": "headless-gpu", "investigation_id": 5525, "attempts": 0,
          "created_at": "2026-11-19T22:20:38", "attach_command": "cfassist attach 5525",
          "tracker_url": "https://plane.example/i/736", "tracker_key": "CFOP-736", "tracker_state": "filed",
          "result": {"tracker": {"key": "CFOP-736", "url": "https://plane.example/i/736",
                                 "synced_status": "filed", "synced_at": "2026-11-19T22:21:36"},
                     "reverify": {"verdict": "open", "verified_by": "deepseek/deepseek-v4-pro",
                                  "checked_at": "2026-11-20T04:29:20", "checks": 1}},
          "payload": {"recommendation": "Inspect the patch cable", "provider": "anthropic/claude-sonnet-4-6",
                      "rendered_context": "## Root cause\n\nA NIC link flap.", "proposed_diff": "",
                      "dedupe": {"source": "service", "reason": "no open row covers it",
                                 "model": {"backend": "openrouter", "model": "gpt-oss-20b", "mode": "fold",
                                           "outcome": "none", "reason": "different target and issue"}}}},
    # A live gitops row with a full fix and a judge.
    "2": {"id": 2, "status": "needs-human", "remediation_class": "gitops-patch", "risk": "low",
          "confidence": 0.8, "host_id": "default", "investigation_id": 5522, "attempts": 1,
          "created_at": "2026-11-19T22:08:01", "last_error": "judge downgraded",
          "payload": dict(_FIX, recommendation="Disable name suffix hashing",
                          decided_by={"classifier": {"backend": "groq", "model": "gpt-oss-20b"},
                                      "judge": {"backend": "deepseek", "model": "deepseek-v4-pro",
                                                "verdict": "downgrade", "reason": "could not read the target"}})},
    # Rejected by a re-check: closed, and its last_error is that re-check's note.
    "3": {"id": 3, "status": "rejected", "remediation_class": "gitops-patch", "risk": "low",
          "confidence": 0.7, "host_id": "default", "investigation_id": 5520, "attempts": 0,
          "created_at": "2026-11-19T20:00:00", "last_error": _LONG,
          "result": {"reverify": {"verdict": "rejected", "verified_by": "deepseek/deepseek-v4-pro",
                                  "checked_at": "2026-11-19T23:12:02", "checks": 1, "learning_id": 4642}},
          "payload": {"recommendation": "Raise the limit"}},
    # Everything a model wrote, hostile.
    "4": {"id": 4, "status": "needs-human", "remediation_class": "gitops-patch", "risk": "low",
          "confidence": 0.5, "host_id": _XSS, "investigation_id": None, "attempts": 0,
          "created_at": "2026-11-19T20:00:00", "last_error": _XSS,
          "payload": {"recommendation": _XSS, "provider": _XSS, "steps": [_XSS],
                      "targets": [{"kind": _XSS, "id": _XSS, "repo": _XSS}],
                      "verify": {"command": _XSS, "expect": _XSS},
                      "observed": [{"source": _XSS, "value": _XSS}],
                      "rejected": [{"alternative": _XSS, "why_not": _XSS}],
                      "observed_check": {"status": _XSS, "reason": _XSS},
                      "rendered_context": _XSS, "finding": _XSS, "evidence": _XSS, "proposed_diff": _XSS,
                      "absorbed": [_XSS],
                      "dedupe": {"reason": _XSS, "model": {"backend": _XSS, "model": _XSS, "reason": _XSS}},
                      "decided_by": {"classifier": {"backend": _XSS, "model": _XSS},
                                     "judge": {"backend": _XSS, "model": _XSS, "verdict": _XSS, "reason": _XSS,
                                               "peers": [{"backend": _XSS, "model": _XSS, "outcome": _XSS,
                                                          "detail": _XSS}, {"outcome": "verdict"}]}},
                      "pr_attempt": {"status": _XSS, "detail": _XSS, "path": _XSS, "branch": _XSS}},
          "result": {"resolution_note": _XSS, "resolved_by": _XSS,
                     "blocked_commands": [{"command": _XSS, "reason": _XSS}],
                     "reverify": {"verdict": _XSS, "verified_by": _XSS},
                     "tracker": {"key": _XSS, "synced_status": _XSS, "error": _XSS},
                     "change_record": {"url": "https://github.com/o/r/pull/1",
                                       "plan": {"commands": [_XSS]}}}},
}

_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const page=process.argv[2];
const html=fs.readFileSync(page,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(page),'common.js'),'utf8');
const ROWS=JSON.parse(process.argv[3]);
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,inert:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/remediations',search:'',hash:'',href:'http://cfop/remediations'};
const hist={replaceState(){}};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,RegExp,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  window:{location:loc,history:hist,addEventListener(){},removeEventListener(){}},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},hidden:false,addEventListener(){},
    createElement:()=>el(), activeElement:null,
    getElementById:id=>(els[id]=els[id]||el())},
  navigator:{},
  fetch:(url)=>{
    const m=url.match(/^\/api\/remediations\/(\d+)$/);
    if(m && ROWS[m[1]]) return Promise.resolve({json:()=>Promise.resolve(ROWS[m[1]])});
    if(url.indexOf('/api/remediation/flags')===0) return Promise.resolve({json:()=>Promise.resolve({})});
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={drawers:{}, fix:{}};
  for (const id of Object.keys(ROWS)) {
    await box.detail(Number(id));
    out.drawers[id]=box.document.getElementById('detail').innerHTML;
    box.closeDetail();
  }
  // The same fix through the shared renderer, as each drawer asks for it.
  out.fix.queue=box.fixHtml(ROWS['2'].payload,{risk:false});
  out.fix.withRisk=box.fixHtml(Object.assign({risk:'low'},ROWS['2'].payload));
  out.fix.empty=box.fixHtml({recommendation:'only a sentence'});
  out.fix.none=box.fixHtml(null);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("pane") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "remediations.html"), json.dumps(ROWS)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def bar(drawer):
    """The action bar's own markup: from its opening tag to the first card."""
    start = drawer.index('<div class="p-actions">')
    return drawer[start:drawer.index('<section class="pcard"', start)]


def test_the_drawer_is_the_pane(run):
    d = run["drawers"]["1"]
    for part in ('<div class="pane">', '<div class="p-head">', '<div class="p-meta">',
                 '<div class="p-actions">', 'aria-label="Recommendation"',
                 'aria-label="Decision"', 'aria-label="Tracking"', '<div class="pcard fold">'):
        assert part in d, f"the drawer painted without {part}"
    assert re.search(r'<h2 id="detail-title">Remediation #1<', d)


def test_nothing_is_open_and_no_json_is_on_screen(run):
    """The complaint: the payload, as JSON, was most of the drawer. It is still
    there, whole and copyable, behind a closed fold and nowhere else."""
    for rid, d in run["drawers"].items():
        assert "<details open" not in d, f"row {rid}: a fold starts open"
        shown = d[:d.index('<div class="pcard fold">')]
        assert "&quot;recommendation&quot;" not in shown and '"recommendation"' not in shown, (
            f"row {rid}: raw payload JSON is drawn outside the fold")
        folded = d[d.index('<div class="pcard fold">'):]
        assert 'id="raw-payload"' in folded and "&quot;recommendation&quot;" in folded


def test_the_fix_is_prose_chips_and_a_list(run):
    d = run["drawers"]["2"]
    assert run["fix"]["queue"] and run["fix"]["queue"] in d, "the drawer does not draw the shared fix block"
    assert '<span class="tgt">' in d and "base/monitoring/kustomization.yaml" in d
    assert '<ol class="steps"><li>Locate the kustomization</li>' in d
    assert "kubectl get cronjob poller" in d and "expect: no hash suffix" in d
    assert "Evidence" in d and "1 observed · 1 rejected" in d
    assert '<p class="rec" id="rec-text">Disable name suffix hashing</p>' in d


def test_the_fix_renderer_draws_only_what_it_was_given(run):
    f = run["fix"]
    assert "risk low" in f["withRisk"], "an investigation's fix shows its risk"
    assert "risk low" not in f["queue"], "a queue row shows its risk in the head, not twice"
    assert f["empty"] == "" and f["none"] == ""


def test_a_long_error_folds_and_a_short_one_does_not(run):
    long_, short = run["drawers"]["3"], run["drawers"]["2"]
    assert '<details class="more">' in long_
    assert "The evidence contradicts the claim." in long_
    assert '<details class="more">' not in short and "judge downgraded" in short


def test_a_recheck_is_a_decision_and_its_note_is_not_an_error(run):
    """CFOP-414 asked for this by name: the verdict gets its own heading. The
    note still travels as last_error; the label says what it is."""
    rejected, live, filed = run["drawers"]["3"], run["drawers"]["2"], run["drawers"]["1"]
    assert ">re-check<" in rejected and "learning #4642" in rejected
    assert ">re-check note<" in rejected and ">last error<" not in rejected
    assert ">last error<" in live and ">re-check note<" not in live
    assert "still open" in filed
    # The fold shows what was found, not the line that says who looked.
    summary = re.search(r'<details class="more"><summary>(.*?)<span class="cut">', rejected, re.S).group(1)
    assert summary.startswith("The evidence contradicts the claim.")


def test_only_a_live_row_has_a_green_button_and_it_has_one(run):
    for rid in ("1", "2"):
        assert bar(run["drawers"][rid]).count(" primary") == 1, f"row {rid}: expected one next step"
    assert " primary" not in bar(run["drawers"]["3"]), "a closed row was drawn with a next step"
    assert "approve(3)" in bar(run["drawers"]["3"]), "Approve must stay reachable on a closed row"


def test_the_bar_offers_what_the_row_needs(run):
    manual, gitops = bar(run["drawers"]["1"]), bar(run["drawers"]["2"])
    assert "approve(1)" not in manual and manual.count("showReclassify(1)") == 1
    assert "approve(2)" in gitops and "showReclassify(2)" in gitops
    for b in (manual, gitops):
        assert "showResolve(" in b and "showReject(" in b and 'id="actionform"' in b
    assert 'href="/console?remediation=1"' in manual
    assert 'href="https://plane.example/i/736"' in manual and "CFOP-736" in manual
    assert "Filed as CFOP-736" in manual and "nothing for the executor to run" in manual


def test_the_investigation_is_a_link_not_a_json_dump(run):
    """The chip used to fetch the investigation and append its findings to the
    drawer as JSON. The investigation has a pane of its own."""
    d = run["drawers"]["1"]
    assert bar(d).count('href="/investigations#5525"') == 1
    assert "viewInvestigation" not in read("remediations.html")
    assert "/investigations#" not in run["drawers"]["4"], "a row with no investigation linked one"


def test_what_decided_and_where_it_went_are_on_the_cards(run):
    filed, live = run["drawers"]["1"], run["drawers"]["2"]
    assert "no open row covers it" in filed and "openrouter/gpt-oss-20b" in filed
    assert "cfassist attach 5525" in filed and "synced filed" in filed
    assert "groq/gpt-oss-20b" in live and "deepseek/deepseek-v4-pro" in live
    assert "downgrade" in live and "could not read the target" in live


def test_model_text_is_escaped_everywhere_it_is_drawn(run):
    """Every field in row 4 is the same hostile string. None of it may reach
    the drawer as markup, in a card, a fold, a tooltip or the copy icon."""
    d = run["drawers"]["4"]
    assert "<img" not in d, "an unescaped model-authored value reached the drawer"
    assert d.count("&lt;img") >= 30, f"only {d.count('&lt;img')} of the hostile values were drawn at all"
