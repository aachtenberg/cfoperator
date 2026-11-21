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

The page around the pane follows (CFOP-738): the remediations list is items
and the pane docks beside it, as on Investigations. The dock's own rules
(what is modal when, one breakpoint, no second copy) are in
``test_console_shell.py``; here the page is run to hold what docking needs
of it: the open row is marked and stays marked across a repaint, and the URL
names it.

And the pane uses a wide screen (CFOP-739): given the room, the facts cards
stand beside the recommendation instead of the right half of the screen going
empty. That is one layout too, written once, and switched by the pane's own
width.
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
KIT = [".pane", ".p-head", ".p-meta", ".p-actions", ".pcard", ".pgrid", ".prow", ".fold", ".p-cols"]
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

# Nothing decided this row and nothing tracks it: there are no facts cards.
ROWS["5"] = {"id": 5, "status": "pending", "remediation_class": "manual", "risk": "low", "confidence": 0.5,
             "attempts": 0, "created_at": "2026-11-20T10:00:00", "payload": {"recommendation": "Look at it"}}

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


# --------------------------------------------------------------------------
# the page around the pane: a list beside it (CFOP-738)
# --------------------------------------------------------------------------

_DOCK_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const page=process.argv[2];
const html=fs.readFileSync(page,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(page),'common.js'),'utf8');
const ROWS=JSON.parse(process.argv[3]);
const els={};
function el(id){return{id,attrs:{},className:'',innerHTML:'',textContent:'',value:'',hidden:false,inert:false,
  style:{},classList:{add(){},remove(){},contains:c=>id==='detail'&&c==='dock'},
  setAttribute(k,v){this.attrs[k]=String(v);},removeAttribute(k){delete this.attrs[k];},
  select(){},remove(){},focus(){},scrollIntoView(){this.revealed=(this.revealed||0)+1;},
  appendChild(){},addEventListener(){}};}
const loc={pathname:'/remediations',search:'',hash:'',href:'http://cfop/remediations'};
const urls=[];
const hist={replaceState(_s,_t,u){urls.push(u);}};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,RegExp,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  window:{location:loc,history:hist,addEventListener(){},removeEventListener(){},
    matchMedia:()=>({matches:process.argv[4]==='wide',addEventListener(){}})},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},hidden:false,addEventListener(){},
    createElement:()=>el(''), activeElement:null,
    getElementById:id=>(els[id]=els[id]||el(id)),
    querySelectorAll:sel=>Object.values(els).filter(e=>/^row-/.test(e.id)&&('aria-current' in e.attrs))},
  navigator:{},
  fetch:(url)=>{
    const m=url.match(/^\/api\/remediations\/(\d+)$/);
    if(m && ROWS[m[1]]) return Promise.resolve({json:()=>Promise.resolve(ROWS[m[1]])});
    if(url.indexOf('/api/remediation/flags')===0) return Promise.resolve({json:()=>Promise.resolve({})});
    return Promise.resolve({json:()=>Promise.resolve({remediations:Object.values(ROWS)})});
  }};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
const marked=()=>Object.values(els).filter(e=>e.attrs['aria-current']==='true').map(e=>e.id);
(async () => {
  await tick(); await tick();
  const out={};
  await box.detail(1);
  out.afterOpen={marked:marked(), url:urls[urls.length-1], modal:els['detail'].attrs['aria-modal'],
    revealed:els['row-1'].revealed||0, hasMax:els['detail'].innerHTML.indexOf('id="detail-max"')>=0};
  await box.detail(2);
  out.afterSecond={marked:marked(), url:urls[urls.length-1]};
  // A poll that changed something replaces the rows; the new <tr> has no mark.
  delete els['row-2'].attrs['aria-current'];
  box.paint();
  out.afterRepaint={marked:marked()};
  box.toggleMax();
  out.maximized={modal:els['detail'].attrs['aria-modal'], max:box.MAX};
  box.toggleMax();
  box.closeDetail();
  out.afterClose={marked:marked(), url:urls[urls.length-1]};
  out.rows={filed:els['rows-filed'].innerHTML, active:els['rows'].innerHTML, done:els['rows-done'].innerHTML};
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _dock(tmp_path_factory, screen):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("dock") / "stub.js"
    stub.write_text(_DOCK_STUB, encoding="utf-8")
    rows = {k: v for k, v in ROWS.items() if k in ("1", "2", "3")}
    out = subprocess.run([node, str(stub), str(UI / "remediations.html"), json.dumps(rows), screen],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def wide(tmp_path_factory):
    return _dock(tmp_path_factory, "wide")


@pytest.fixture(scope="module")
def narrow(tmp_path_factory):
    return _dock(tmp_path_factory, "narrow")


def test_the_open_row_is_marked_and_only_that_one(wide):
    assert wide["afterOpen"]["marked"] == ["row-1"]
    assert wide["afterSecond"]["marked"] == ["row-2"], "the row you left is still marked, or the new one is not"
    assert wide["afterClose"]["marked"] == []


def test_a_repaint_does_not_lose_the_open_row(wide):
    """The list repaints whenever a poll brings a change, and the pane beside
    it stays open. The mark has to come back with the rows."""
    assert wide["afterRepaint"]["marked"] == ["row-2"]


def test_the_url_names_the_open_row(wide):
    assert wide["afterOpen"]["url"] == "#1" and wide["afterSecond"]["url"] == "#2"
    assert wide["afterClose"]["url"] == "/remediations", "closing left the row's id in the URL"


def test_docked_is_not_modal_and_a_drawer_or_a_maximized_pane_is(wide, narrow):
    assert wide["afterOpen"]["modal"] == "false", "beside the list, Tab has to be able to reach the list"
    assert narrow["afterOpen"]["modal"] == "true"
    assert wide["maximized"] == {"modal": "true", "max": True}
    assert wide["afterOpen"]["hasMax"], "the pane has no maximize button"


def test_docking_brings_the_clicked_row_back_into_view(wide, narrow):
    """Opening a row narrows the page and folds the pipeline away, so the row
    is no longer under the pointer. Over the list (narrow) nothing moves."""
    assert wide["afterOpen"]["revealed"] == 1
    assert narrow["afterOpen"]["revealed"] == 0


def test_the_list_rows_are_items_in_their_buckets(wide):
    rows = wide["rows"]
    assert 'id="row-1"' in rows["filed"] and 'id="row-2"' in rows["active"] and 'id="row-3"' in rows["done"]
    row = re.search(r'<tr id="row-1".*?</tr>', rows["filed"], re.S).group(0)
    cells = re.findall(r'<td class="([a-z]+)', row)
    assert cells == ["id", "oc", "when", "trig", "meta"], cells
    for text in ("#1", "filed", "low risk", "Inspect the patch cable", "manual", "headless-gpu",
                 "anthropic/claude-sonnet-4-6", "conf 0.92", "CFOP-736"):
        assert text in row, f"the row lost {text}"


def test_what_folds_beside_a_docked_pane_is_marked_and_only_folds_there():
    page = read("remediations.html")
    assert re.search(r'<div class="card-body dock-fold">\s*<div class="switches" id="flags">', page)
    assert re.search(r'<section class="[^"]*\bdock-fold\b[^"]*" id="tiles"', page)
    css = re.sub(r"/\*.*?\*/", "", read("console.css"), flags=re.S)
    rule = "body:has(#detail.dock.open) .dock-fold { display:none; }"
    assert css.count(rule) == 1
    media = css.index("@media (min-width: 1280px)")
    assert media < css.index(rule) < css.index("#detail.dock.max"), (
        "the fold is not inside the dock's media query: the pipeline would vanish under a plain drawer too")
    assert css.count(".dock-fold") == 1, "something else hides .dock-fold"


# --------------------------------------------------------------------------
# the pane on a wide screen (CFOP-739)
# --------------------------------------------------------------------------

MAIN, SIDE, REST = '<div class="p-main">', '<div class="pgrid p-side">', '<div class="p-rest">'


def _block(css, opener):
    """``(inside, outside)`` of the one at-rule that starts with ``opener``, by brace count."""
    assert css.count(opener) == 1, f"expected one {opener!r} in console.css, found {css.count(opener)}"
    start = css.index("{", css.index(opener)) + 1
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[start:i - 1], css[:css.index(opener)] + css[i:]


def _css():
    return re.sub(r"/\*.*?\*/", "", read("console.css"), flags=re.S)


@pytest.mark.parametrize("page,main,side,rest", [
    ("investigations.html", ["actionBar(r)", 'id="explain"', 'id="cockpit"', "recommendationBlock("],
     ["triageBlock(r)", "similarBlock(f)"], ["sessionsBlock(r)", 'class="pcard fold"']),
    ("remediations.html", ["actionBar(r)", 'aria-label="Recommendation"'],
     ['aria-label="Decision"', 'aria-label="Tracking"'],
     ["changeRecordHtml(r)", "blockedCommandsHtml(r)", "foldsHtml(p)"]),
])
def test_a_pane_is_three_groups_in_reading_order(page, main, side, rest):
    """Stacked, the groups show in the order they are written, so the order
    here is the drawer's. A facts card written into the first group would
    not move to the side; one written after the folds would sit under them."""
    js = inline_script(page)
    tpl = js[js.index('<div class="p-cols">'):]
    tpl = tpl[:tpl.index("</div></div>`;")]
    at = [tpl.index(m) for m in (MAIN, SIDE, REST)]
    assert at == sorted(at), f"{page}: the groups are not main, facts, rest"
    for group, lo, hi in ((main, at[0], at[1]), (side, at[1], at[2]), (rest, at[2], len(tpl))):
        for part in group:
            assert tpl.count(part) == 1, f"{page}: {part} is written {tpl.count(part)} times under the head"
            assert lo < tpl.index(part) < hi, f"{page}: {part} is not in the group it belongs to"
    head = js[js.index('<div class="pane">'):js.index('<div class="p-cols">')]
    assert 'class="p-head"' in head and 'class="p-meta"' in head, (
        f"{page}: the head and the meta line span the pane, so they are written before the columns")


def test_the_painted_drawer_keeps_the_groups(run):
    d = run["drawers"]["1"]
    at = [d.index(m) for m in ('<div class="p-cols">', MAIN, '<div class="p-actions">', 'aria-label="Recommendation"',
                               SIDE, 'aria-label="Decision"', 'aria-label="Tracking"', REST, '<div class="pcard fold">')]
    assert at == sorted(at)
    assert d.count(SIDE) == 1 and d.count('class="pgrid') == 1, "the facts are one grid, and it is the side group"


def test_a_row_with_no_facts_has_no_side_group(run):
    """An empty side group would still take its column. The stylesheet only
    makes two columns when the group is there, so it must not be written
    empty."""
    d = run["drawers"]["5"]
    assert MAIN in d and REST in d
    assert "p-side" not in d and 'aria-label="Decision"' not in d and 'aria-label="Tracking"' not in d


def test_two_columns_are_decided_by_the_panes_own_width():
    """Not the viewport's: the pane is docked, maximized, or over the list
    while a terminal is open, three widths on one screen."""
    css = _css()
    assert re.search(r"#detail\.dock\s*\{\s*container:\s*pane\s*/\s*inline-size;\s*\}", css), (
        "the docking drawer no longer names itself the pane's container")
    inside, outside = _block(css, "@container pane")
    assert re.search(r"\.p-cols:has\(> \.p-side\)\s*\{[^}]*display:\s*grid", inside), (
        "the two columns are gone, or no longer wait for a side group to be there")
    assert re.search(r"\.p-cols > \.p-side\s*\{[^}]*grid-column:\s*2", inside)
    for selector, body in re.findall(r"([^{}]*)\{([^}]*)\}", outside):
        if re.search(r"\.p-(cols|main|rest|side)\b", selector):
            assert "grid" not in body, (
                f"{selector.strip()} lays out columns outside the container query: a narrow drawer would get them")


def test_one_column_ends_where_two_begin():
    """The stacked pane is capped at the width where it becomes two columns.
    A lower cap is the empty band this was written to remove; a higher one
    never applies."""
    css = _css()
    cap = re.search(r"(?:^|})\s*\.pane\s*\{[^}]*max-width:\s*(\d+)px", css, re.M)
    at = re.search(r"@container pane \(min-width:\s*(\d+)px\)", css)
    assert cap and at, "the pane's cap or the container query was not found"
    assert cap.group(1) == at.group(1), f"one column is capped at {cap.group(1)}px, two start at {at.group(1)}px"
    inside, _ = _block(css, "@container pane")
    wide = re.search(r"\.pane:has\(\.p-side\)\s*\{[^}]*max-width:\s*(\d+)px", inside)
    assert wide and int(wide.group(1)) > int(cap.group(1)), "two columns are no wider than one"


@pytest.mark.parametrize("page", PANE_PAGES)
def test_a_page_does_not_lay_out_the_columns_itself(page):
    css = re.sub(r"/\*.*?\*/", "", page_styles(page), flags=re.S)
    assert "@container" not in css, f"{page} has its own container query"
    own = re.findall(r"\.p-(?:cols|main|rest|side)\b", css)
    assert not own, f"{page} styles {sorted(set(own))}; the pane's columns are console.css's"
