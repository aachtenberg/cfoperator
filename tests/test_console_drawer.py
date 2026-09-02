"""The investigation drawer as a handoff, not a dead end (CFOP-73).

Reading an investigation in the console used to leave you holding an integer:
the drawer showed ``Investigation #2272`` as text, so working it meant carrying
"2272" to a terminal in your head. Slack, Discord and ntfy had all solved this
already — every notification carrying an investigation ends with
``Take over: cfassist attach <id>``.

These guard the two halves of closing that gap, and both are about a *class* of
regression rather than today's markup:

* the drawer offers the handoff line, and gets the string from the server so it
  cannot drift from the command the shipped binary implements (the drift itself
  is guarded across artifacts in ``test_cockpit_attach_contract.py``);
* a page with a detail drawer is linkable — the open row is in the URL, so it
  survives a reload and can be pasted to someone.

The behavioural half runs the page's own inline script under node, the same way
``test_console_nav.py`` runs nav.js, because the interesting parts are a
round-trip (open writes the URL, close clears it) and a re-entry guard that a
grep cannot see.
"""

from repo_paths import REPO_ROOT
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from flask import Flask

from event_runtime.notifications import ATTACH_COMMAND

REPO_ROOT = REPO_ROOT
UI = REPO_ROOT / "ui"

#: Console pages whose main content is a row list plus a slide-in detail
#: drawer. What is asserted below is the *inbound* half only — a pasted URL
#: opens the row it names. investigations.html also writes the hash as you
#: click, so its URL always names what you are looking at; remediations.html
#: does not yet, and this does not claim otherwise. A third drawer page added
#: later should have to think about linkability rather than quietly not have
#: it.
DRAWER_PAGES = ["investigations.html", "remediations.html"]

#: Pages offering a one-click copy of something. The console is normally
#: reached over plain HTTP on the LAN, where ``navigator.clipboard`` does not
#: exist, so every one of these needs the fallback.
COPY_PAGES = ["investigations.html", "remediations.html", "account.html", "admin.html"]


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def inline_script(name):
    """The page's own script block (the nav.js tag carries attributes, so a
    bare ``<script>`` matches only the inline one)."""
    blocks = re.findall(r"<script>(.*?)</script>", read(name), re.S)
    assert len(blocks) == 1, f"{name} has {len(blocks)} inline scripts, expected 1"
    return blocks[0]


# --------------------------------------------------------------------------
# the server hands the console the line
# --------------------------------------------------------------------------

class FakeKB:
    """One investigation, returned as the *same* dict every time.

    Deliberately not a copy: that is what lets the mutation test below notice
    if the route ever starts decorating the knowledge base's own row instead of
    a copy of it.
    """

    def __init__(self):
        self.row = {'id': 2272, 'trigger': 'Per-host backup failed on raspberrypi4',
                    'outcome': 'monitoring', 'findings': {'response': 'transient'}}

    def get_investigation(self, investigation_id):
        return self.row if investigation_id == self.row['id'] else None

    # Two remediation rows (CFOP-109): one linked to the investigation above,
    # one with no investigation at all — there is nothing to attach to there.
    remediations = {
        80: {'id': 80, 'investigation_id': 2272, 'status': 'needs-human',
             'remediation_class': 'node-action', 'risk': 'low', 'host_id': 'raspberrypi4',
             'payload': {'recommendation': 'verify the backup mount'}},
        81: {'id': 81, 'investigation_id': None, 'status': 'needs-human',
             'remediation_class': 'manual', 'risk': 'low', 'host_id': None, 'payload': {}},
    }

    def get_remediation(self, remediation_id):
        return self.remediations.get(remediation_id)


def _client(kb):
    from unittest.mock import MagicMock

    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb = kb
    operator.config = {}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._setup_routes()

    prior = os.environ.get("CFOP_AUTH_DISABLED")
    os.environ["CFOP_AUTH_DISABLED"] = "1"
    try:
        install_auth(server.app, store=None)
    finally:
        if prior is None:
            os.environ.pop("CFOP_AUTH_DISABLED", None)
        else:
            os.environ["CFOP_AUTH_DISABLED"] = prior
    return server.app.test_client()


def test_the_drill_in_carries_the_attach_command():
    """The console should not have to know how to spell the command.

    Mutation check: drop the ``attach_command`` line from the route and this
    goes red.
    """
    resp = _client(FakeKB()).get('/api/investigations/2272')
    assert resp.status_code == 200
    assert resp.get_json()['attach_command'] == ATTACH_COMMAND.format(
        investigation_id=2272)


def test_the_command_is_rendered_from_the_one_definition():
    """Not merely well-formed today: the same template Slack prints.

    ``ATTACH_COMMAND`` is pinned against the Go verb in
    ``test_cockpit_attach_contract.py``, so rendering the console's copy from
    it puts the console inside that contract instead of beside it.
    """
    payload = _client(FakeKB()).get('/api/investigations/2272').get_json()
    assert payload['attach_command'] == "cfassist attach 2272"


def test_the_knowledge_base_row_is_not_decorated_in_place():
    """The KB row is not the web layer's to grow a field on — it is handed
    out to callers that never asked for a console affordance."""
    kb = FakeKB()
    _client(kb).get('/api/investigations/2272')
    assert 'attach_command' not in kb.row


def test_a_missing_investigation_still_404s():
    """The added field must not turn a 404 into a 500."""
    assert _client(FakeKB()).get('/api/investigations/9999').status_code == 404


# --------------------------------------------------------------------------
# a remediation hands over its investigation the same way (CFOP-109)
# --------------------------------------------------------------------------

def test_the_remediation_drill_in_carries_its_investigations_attach_command():
    """Mutation check: drop the ``attach_command`` lines from the remediation
    route and this goes red."""
    payload = _client(FakeKB()).get('/api/remediations/80').get_json()
    assert payload['attach_command'] == ATTACH_COMMAND.format(investigation_id=2272)
    assert payload['attach_command'] == "cfassist attach 2272"


def test_a_remediation_with_no_investigation_offers_no_handoff():
    """``cfassist attach None`` is not a command anyone should be handed."""
    payload = _client(FakeKB()).get('/api/remediations/81').get_json()
    assert 'attach_command' not in payload


def test_the_remediation_row_is_not_decorated_in_place():
    kb = FakeKB()
    _client(kb).get('/api/remediations/80')
    assert 'attach_command' not in kb.remediations[80]


def test_a_missing_remediation_still_404s():
    assert _client(FakeKB()).get('/api/remediations/9999').status_code == 404


# --------------------------------------------------------------------------
# the drawer offers it, and does not spell it itself
# --------------------------------------------------------------------------

#: Pages whose drawer offers the take-over line.
HANDOFF_PAGES = ["investigations.html", "remediations.html"]


@pytest.mark.parametrize("page", HANDOFF_PAGES)
def test_the_drawer_renders_the_servers_command(page):
    assert "attach_command" in inline_script(page), (
        f"{page}'s drawer no longer reads the command off the payload")


@pytest.mark.parametrize("page", HANDOFF_PAGES)
def test_the_drawer_does_not_write_the_command_itself(page):
    """A literal here would be a third copy of the handoff line — one the
    cross-artifact contract test cannot see, and the first to drift."""
    assert "cfassist attach" not in read(page), (
        f"{page} spells the attach command itself; render the "
        "server's attach_command instead so the contract test covers it")


# --------------------------------------------------------------------------
# a remediation can be handed to the console chat (CFOP-109)
# --------------------------------------------------------------------------

def test_the_remediation_drawer_links_the_row_into_the_console():
    """The whole complaint: reading the number off one page to type it on
    another. The drawer carries the id in the link; the console reads it."""
    js = inline_script("remediations.html")
    assert "/?remediation=" in js, "remediations.html no longer links a row into the console"


def test_the_console_reads_a_handed_row_and_asks_about_it():
    """index.html has several script blocks; this is about the console's own
    bootstrap, so read the page rather than the single-inline-script helper."""
    html = read("index.html")
    assert "get('remediation')" in html, "index.html no longer reads ?remediation="
    assert "askAboutRemediation(" in html
    # Consumed once the question is on its way — not before. A reload must
    # not ask twice, but a failed session create or row fetch must leave the
    # URL intact so a reload retries rather than landing on an empty chat.
    ask = html[html.index("function askAboutRemediation("):]
    ask = ask[:ask.index("\n        }\n")]
    assert "history.replaceState(null, '', location.pathname" in ask, (
        "the remediation param is not stripped from the URL once consumed")
    assert ask.index("sendMessage(") < ask.index("history.replaceState("), (
        "the param is stripped before the question is sent")
    boot = html[html.index("const handedRemediation"):]
    boot = boot[:boot.index("} else if (savedSession)")]
    assert "history.replaceState" not in boot, "the bootstrap strips the param before anything is sent"
    assert "reload to retry" in boot, "a failed session create for a handed row is silent"


# --------------------------------------------------------------------------
# ...and so can an investigation (CFOP-113), the same way
# --------------------------------------------------------------------------

def test_the_investigation_drawer_links_the_row_into_the_console(drawer_behaviour):
    js = inline_script("investigations.html")
    assert "/?investigation=" in js, "investigations.html no longer links a row into the console"
    assert drawer_behaviour["asksConsole"], "the open drawer carries no Ask console link for its row"


def test_the_drawer_can_be_maximized(drawer_behaviour):
    """The drawer is 620px so the table stays beside it; a report or the raw
    findings want the room. The toggle is drawn in the drawer and flips the
    page's MAX state (the class rides on #detail, which is never replaced)."""
    assert drawer_behaviour["maxDrawnOff"], "no maximize toggle in the drawer, or it does not start restored"
    assert drawer_behaviour["maxFlips"], "toggleMax() did not flip MAX"
    assert drawer_behaviour["maxLabelFollows"], (
        "the toggle's accessible name does not follow its state — a screen reader "
        "hears 'maximize' on a control that restores")


def test_the_console_reads_a_handed_investigation_and_asks_about_it():
    """Mirror of the remediation test above: the console must consume
    ?investigation= with the same consumed-only-once-sent rule."""
    html = read("index.html")
    assert "get('investigation')" in html, "index.html no longer reads ?investigation="
    assert "askAboutInvestigation(" in html
    ask = html[html.index("function askAboutInvestigation("):]
    ask = ask[:ask.index("\n        }\n")]
    assert "history.replaceState(null, '', location.pathname" in ask, (
        "the investigation param is not stripped from the URL once consumed")
    assert ask.index("sendMessage(") < ask.index("history.replaceState("), (
        "the param is stripped before the question is sent")
    # The question carries what the drawer showed — the recommendation and the
    # steps — and asks for the checks to be run, not recited.
    q = html[html.index("function investigationQuestion("):]
    q = q[:q.index("\n        }\n")]
    for field in ("f.recommendation", "steps", "v.command", "run the checks yourself"):
        assert field in q, f"investigationQuestion() no longer carries {field}"
    boot = html[html.index("const handedInvestigation"):]
    boot = boot[:boot.index("} else if (savedSession)")]
    assert "askAboutInvestigation(handedInvestigation)" in boot
    assert "reload to retry" in boot, "a failed session create for a handed investigation is silent"


@pytest.mark.parametrize("page", COPY_PAGES)
def test_copy_does_not_assume_a_secure_context(page):
    """``navigator.clipboard`` is undefined over plain HTTP, which is how this
    console is normally reached on a LAN. A copy button that only tries the
    async API is a button that does nothing for most operators here."""
    html = read(page)
    if "copyElementText(" in html or "copyIcon(" in html:
        # The copy lives in common.js since CFOP-109 (and the icon path since
        # CFOP-113); each of the two entry points must carry the fallback.
        common = read("common.js")
        for fn in ("copyElementText", "copyText"):
            body = common[common.index("function %s(" % fn):]
            body = body[:body.index("\n}\n")]
            assert "navigator.clipboard" in body and "execCommand('copy')" in body, (
                "common.js %s lost its plain-HTTP fallback" % fn)
        return
    if "navigator.clipboard" not in html:
        pytest.skip(f"{page} has no copy control")
    assert "execCommand('copy')" in html, (
        f"{page} copies only via navigator.clipboard — no fallback for the "
        "plain-HTTP LAN case")


# --------------------------------------------------------------------------
# a drawer you can link to
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", DRAWER_PAGES)
def test_a_drawer_page_opens_from_the_url(page):
    """Guards the next page that grows a drawer: a URL naming a row should
    open that row. Writing the hash back as you click is the other half, and
    only investigations.html does it — see the behavioural tests below."""
    js = inline_script(page)
    assert "location.hash" in js, f"{page} never reads location.hash"
    assert "'hashchange'" in js, f"{page} does not react to the hash changing"


# The page's inline script, run for real. Stubs are the smallest thing that
# lets it load: it fetches its rows and paints them on eval, so the harness has
# to answer those before any of the interesting calls happen.
_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];

const detailFetches=[], slow=new Set();
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/investigations',search:'',hash:'',href:'http://cfop/investigations'};
const hist={replaceState(state,title,url){
  if(typeof url==='string' && url.startsWith('#')) loc.hash=url; else loc.hash=''; }};

const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,
  setTimeout,clearTimeout,
  setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  window:{location:loc,history:hist,addEventListener(){},removeEventListener(){}},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},addEventListener(){},
    createElement:()=>el(),
    getElementById:id=>(els[id]=els[id]||el())},
  // The sanitizer walks a parsed tree; node has no DOM, so this hands the
  // HTML back untouched. What is asserted is that marked ran, not the walk.
  DOMParser:function(){ this.parseFromString=h=>({body:{firstChild:{querySelectorAll:()=>[],
    innerHTML:h.replace(/^<div>/,'').replace(/<\/div>$/,'')}}}); },
  navigator:{},
  fetch:(url)=>{
    if(url.indexOf('/api/investigations/')===0){
      detailFetches.push(url);
      const id=Number(url.split('/').pop());
      // 2272 carries a structured fix and a report (CFOP-113); the other
      // ids are bare so the drawer's empty-findings path is exercised too.
      const findings=id===2272?{recommendation:'Power-cycle the node.',provider:'ollama/x',
        fix:{risk:'low',targets:[{kind:'host',id:'pi4'}],steps:['check the PSU','reboot pi4'],
             verify:{command:'ping pi4',expect:'0% loss'},
             observed:[{source:'ping pi4',value:'100% loss'}],
             rejected:[{alternative:'delete the pod',why_not:'node is gone'}]},
        // The protocol tail the agent parses; TAILJSON is the tell.
        response:'# Report\n\nThe node is **gone**.\n\nSTATUS: needs_action\nRECOMMENDATION: Power-cycle the node.\nFIX: {"marker":"TAILJSON"}\n\nNotes: kubectl describe showed\nStatus: Running'}:{};
      const res={ok:true,json:()=>Promise.resolve({id:id,trigger:'backup failed',
        outcome:'monitoring',attach_command:'cfassist attach '+id,findings:findings})};
      return slow.has(id) ? new Promise(r=>setTimeout(()=>r(res),40))
                          : Promise.resolve(res);
    }
    if(url.indexOf('/api/investigations')===0){
      return Promise.resolve({json:()=>Promise.resolve({investigations:[]})});
    }
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
// The helpers the page calls (esc, badge, toast, trapFocus) live in ui/common.js
// since CFOP-95; the browser loads it before the page script, so the harness does.
const uiDir=require('path').dirname(process.argv[2]);
const common=fs.readFileSync(require('path').join(uiDir,'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box);
// Vendored scripts the page itself references (marked, for the full report).
// Loaded from the page's own tags, so a page that drops the tag loses the
// library here too — the way it would in the browser. xterm needs a DOM.
for(const m of html.matchAll(/<script src="\/vendor\/([^"]+)"/g)){
  if(/marked/.test(m[1])) vm.runInContext(fs.readFileSync(require('path').join(uiDir,'vendor',m[1]),'utf8'),box);
}
vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();          // let the initial load() settle
  const out={};

  await box.detail(2272);
  out.hashAfterOpen=loc.hash;
  const opened=box.document.getElementById('detail').innerHTML;
  out.drawerHasCommand=opened.indexOf('cfassist attach 2272')>=0;
  // The fix as a list, not a JSON dump (CFOP-113).
  out.fixStepsAsList=/<ol class="steps"><li>check the PSU<\/li><li>reboot pi4<\/li><\/ol>/.test(opened);
  out.fixDumpedAsJson=opened.indexOf('"steps"')>=0 && opened.indexOf('<h3>FIX</h3>')>=0;
  out.reportFolded=/<details><summary>Full report/.test(opened);
  out.rawFolded=/<details><summary>Raw findings/.test(opened);
  out.copyIcons=(opened.match(/class="cp"/g)||[]).length;
  out.titleCopiesTrigger=opened.indexOf('data-copy="Investigation #2272 — backup failed"')>=0;
  // The tail survives only in the raw findings (once), not in the rendered
  // report or its copy text.
  out.tailMentions=(opened.match(/TAILJSON/g)||[]).length;
  // Markdown actually rendered: in the report's own div, **gone** is <strong>,
  // not asterisks. (The summary's copy icon carries the markdown source in
  // data-copy on purpose, so the div is what is inspected, not the section.)
  const report=(opened.match(/<summary>Full report[\s\S]*?<div class="md">([\s\S]*?)<\/div><\/details>/)||['',''])[1];
  out.reportRendered=/<strong>gone<\/strong>/.test(report) && !/\*\*gone\*\*/.test(report);
  out.asksConsole=opened.indexOf('href="/?investigation=2272"')>=0;
  // The maximize toggle (CFOP-113): drawn off, flips the page's state, and
  // its accessible name follows the state.
  out.maxDrawnOff=opened.indexOf('id="detail-max"')>=0 && opened.indexOf('aria-pressed="false"')>=0
    && opened.indexOf('aria-label="Maximize panel"')>=0;
  const maxBefore=box.MAX; box.toggleMax(); out.maxFlips=(maxBefore===false && box.MAX===true);
  const pressedLabel=box.maxButton(); box.toggleMax();
  out.maxLabelFollows=pressedLabel.indexOf('aria-label="Restore panel width"')>=0 && pressedLabel.indexOf('aria-pressed="true"')>=0;

  // A hashchange for the row already open must not refetch it.
  const before=detailFetches.length;
  box.openFromHash();
  await tick();
  out.refetchedOpenRow=detailFetches.length>before;

  box.closeDetail();
  out.hashAfterClose=loc.hash;

  // ...but a hash naming a different row does open it.
  loc.hash='#1889';
  box.openFromHash();
  await tick();
  out.openedFromHash=detailFetches[detailFetches.length-1];

  // Two clicks, the first one slow: the row you left must not paint over the
  // row you moved to.
  slow.add(2272);
  const a=box.detail(2272), b=box.detail(1889);
  await a; await b;
  await new Promise(r=>setTimeout(r,80));
  const html=box.document.getElementById('detail').innerHTML;
  out.finalDrawerId=(html.match(/Investigation #(\d+)/)||[])[1];
  out.finalHash=loc.hash;

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def drawer_behaviour(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("drawer") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "investigations.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_opening_a_row_names_it_in_the_url(drawer_behaviour):
    assert drawer_behaviour["hashAfterOpen"] == "#2272"


def test_the_open_drawer_shows_the_handoff_line(drawer_behaviour):
    assert drawer_behaviour["drawerHasCommand"], (
        "the drawer rendered without the attach command the payload carried")


def test_the_fix_is_rendered_as_steps_not_json(drawer_behaviour):
    """CFOP-113. findings.fix is agent.py's structured fix (targets / observed /
    steps / verify / rejected / risk). The drawer used to JSON.stringify it
    into a <pre>; an operator reads steps, not braces. Mutation check: put
    the JSON dump back and fixDumpedAsJson flips."""
    assert drawer_behaviour["fixStepsAsList"], "the fix's steps are not an <ol>"
    assert not drawer_behaviour["fixDumpedAsJson"], "the fix is still dumped as JSON"


def test_the_long_sections_fold_and_the_short_ones_copy(drawer_behaviour):
    """The report and the raw findings stay complete but closed; what remains
    open carries a copy icon — title, trigger, attach line, recommendation,
    steps, verify command, and the folded sections' summaries."""
    assert drawer_behaviour["reportFolded"], "the full report is not a <details>"
    assert drawer_behaviour["rawFolded"], "the raw findings are not a <details>"
    assert drawer_behaviour["copyIcons"] >= 7, (
        f"only {drawer_behaviour['copyIcons']} copy icons in a drawer with a full fix")
    assert drawer_behaviour["titleCopiesTrigger"], (
        "the title's copy icon does not carry the id and the trigger together")


def test_the_report_drops_the_protocol_tail_it_already_renders(drawer_behaviour):
    """The response ends with STATUS: / RECOMMENDATION: / FIX: {json} — the
    lines agent.py parses, and the drawer paints as the badge, the sentence
    and the steps. Showing them again under Full report is the JSON dump
    coming back through the side door. Raw findings keep the text verbatim,
    so the tell appears exactly once."""
    assert drawer_behaviour["tailMentions"] == 1, (
        f"the protocol tail appears {drawer_behaviour['tailMentions']} times; "
        "want once, in the raw findings only")


def test_the_report_is_rendered_markdown_not_escaped_text(drawer_behaviour):
    """The harness loads marked from the page's own vendor tag, so this fails
    the way the browser would if the tag went: the report degrades to a
    <pre> of asterisks. test_console_vendor's marked guard is chat-only;
    this is the one for the second consumer."""
    assert drawer_behaviour["reportRendered"], (
        "the full report is not rendered markdown — is /vendor/marked.min.js still loaded?")


def test_closing_the_drawer_clears_the_url(drawer_behaviour):
    """Otherwise the link you copy after closing points at a drawer you are no
    longer looking at."""
    assert drawer_behaviour["hashAfterClose"] == ""


def test_the_row_already_open_is_not_refetched(drawer_behaviour):
    """The re-entry guard. Writing the hash and listening for hashchange is a
    loop unless one side checks; without it every open costs two round trips
    and the drawer repaints under the reader."""
    assert not drawer_behaviour["refetchedOpenRow"]


def test_a_pasted_url_opens_that_row(drawer_behaviour):
    """The point of the whole exercise: someone hands you
    /investigations#1889 and you land on 1889."""
    assert drawer_behaviour["openedFromHash"] == "/api/investigations/1889"


def test_a_slow_response_cannot_paint_over_the_row_you_moved_to(drawer_behaviour):
    """Click one row, change your mind, click another: a late response for the
    first must not replace the second. The URL already names the newer row, so
    without the guard the drawer and the address bar disagree — and the one
    you would paste is the one you are not looking at."""
    assert drawer_behaviour["finalDrawerId"] == "1889"
    assert drawer_behaviour["finalHash"] == "#1889"


# --------------------------------------------------------------------------
# the stale-response guard, on every drawer page (CFOP-95)
# --------------------------------------------------------------------------
#
# investigations.html carried the guard from CFOP-73; remediations.html did
# not, and the same two quick clicks painted the row you left over the row
# you moved to. Run both pages, and any drawer page added later, through the
# same race.

_RACE_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(process.argv[2]),'common.js'),'utf8');
const api=process.argv[3];

const slow=new Set();
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/x',search:'',hash:'',href:'http://cfop/x'};
const hist={replaceState(){}};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},addEventListener(){},activeElement:null,
    createElement:()=>el(), getElementById:id=>(els[id]=els[id]||el())},
  navigator:{},
  fetch:(url)=>{
    if(url.indexOf(api+'/')===0){
      const id=Number(url.split('/').pop());
      const res={ok:true,json:()=>Promise.resolve({id:id,trigger:'t',outcome:'monitoring',
        status:'queued',risk:'low',remediation_class:'gitops-patch',host_id:'h',payload:{},
        attach_command:'cfassist attach '+id,findings:{}})};
      return slow.has(id) ? new Promise(r=>setTimeout(()=>r(res),40)) : Promise.resolve(res);
    }
    return Promise.resolve({ok:true,json:()=>Promise.resolve({investigations:[],remediations:[]})});
  }};
box.window={location:loc,history:hist,addEventListener(){},removeEventListener(){},
  CFOP:{me:()=>Promise.resolve({role:'admin'})}};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  slow.add(2272);
  const a=box.detail(2272), b=box.detail(1889);
  await a; await b;
  await new Promise(r=>setTimeout(r,80));
  const drawn=els['detail'].innerHTML;
  // The heading names the row; a control after the number (the copy icon,
  // CFOP-113) is not part of the name.
  console.log(JSON.stringify({finalDrawerId:(drawn.match(/<h2[^>]*>[^<]*#(\d+)/)||[])[1]}));
})().catch(e => { console.error(e); process.exit(1); });
"""

_RACE_API = {"investigations.html": "/api/investigations",
             "remediations.html": "/api/remediations"}


@pytest.mark.parametrize("page", DRAWER_PAGES)
def test_a_slow_response_cannot_paint_over_the_newer_row_on_any_drawer_page(tmp_path, page):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "race.js"
    stub.write_text(_RACE_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / page), _RACE_API[page]],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"{page}: {out.stderr}"
    assert json.loads(out.stdout)["finalDrawerId"] == "1889", (
        f"{page}: the slow response for the row you left painted over the row you moved to")


# The remediations drawer, run for real: open a row and look at what it
# painted. Same harness shape as the investigations one above, with the
# fetches that page makes on load answered.
_REM_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,inert:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/remediations',search:'',hash:'',href:'http://cfop/remediations'};
const hist={replaceState(){}};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  window:{location:loc,history:hist,addEventListener(){},removeEventListener(){}},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},hidden:false,addEventListener(){},
    createElement:()=>el(), activeElement:null,
    getElementById:id=>(els[id]=els[id]||el())},
  navigator:{},
  fetch:(url)=>{
    if(url==='/api/remediations/80'){
      return Promise.resolve({json:()=>Promise.resolve({id:80,investigation_id:2272,
        status:'needs-human',remediation_class:'node-action',risk:'low',host_id:'raspberrypi4',
        attempts:0,created_at:'2026-08-26T12:44:12',attach_command:'cfassist attach 2272',
        payload:{recommendation:'verify the mount'}})});
    }
    if(url==='/api/remediations/81'){
      return Promise.resolve({json:()=>Promise.resolve({id:81,investigation_id:null,
        status:'needs-human',remediation_class:'manual',risk:'low',host_id:null,attempts:0,
        created_at:'2026-08-26T12:44:12',payload:{}})});
    }
    if(url.indexOf('/api/remediation/flags')===0){
      return Promise.resolve({json:()=>Promise.resolve({queue_feed:true,queue_reap:true,queue_drain:true,queue_verify:true})});
    }
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
const common=fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={};
  await box.detail(80);
  const linked=box.document.getElementById('detail').innerHTML;
  out.linkedHasCommand=linked.indexOf('cfassist attach 2272')>=0;
  out.linkedConsoleHref=(linked.match(/href="([^"]*remediation=[^"]*)"/)||[])[1];
  box.closeDetail();
  await box.detail(81);
  const orphan=box.document.getElementById('detail').innerHTML;
  out.orphanHasCommand=orphan.indexOf('cfassist attach')>=0;
  out.orphanHasTakeOver=orphan.indexOf('take over')>=0;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def remediation_drawer(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("remdrawer") / "stub.js"
    stub.write_text(_REM_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "remediations.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_remediation_drawer_shows_its_investigations_handoff(remediation_drawer):
    assert remediation_drawer["linkedHasCommand"], (
        "the remediation drawer rendered without the attach command the payload carried")


def test_the_remediation_drawer_links_this_row_into_the_console(remediation_drawer):
    assert remediation_drawer["linkedConsoleHref"] == "/?remediation=80"


def test_a_remediation_with_no_investigation_shows_no_handoff(remediation_drawer):
    """No investigation, no attach line — not a line with a blank in it."""
    assert not remediation_drawer["orphanHasCommand"]
    assert not remediation_drawer["orphanHasTakeOver"]


# --------------------------------------------------------------------------
# a PR the row already has (CFOP-116)
# --------------------------------------------------------------------------
#
# Row #85: the investigation opened the PR itself and the drawer still offered
# Approve, which hands the row to an executor that opens a second PR. The API
# 409s that now; this is the control the operator actually clicks. Two kinds of
# link: pr_url is a PR the row tracks (Review PR replaces Approve), named_pr_url
# is one the recommendation merely names (linked, Approve stays, the note says
# why). Same harness as the drawer above; the rows are fed to detail() and to
# rowHtml() for the list.

_PR_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,inert:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/remediations',search:'',hash:'',href:'http://cfop/remediations'};
const hist={replaceState(){}};
const TRACKED='https://github.com/aachtenberg/homelab-infra/pull/116';
const NAMED='https://github.com/aachtenberg/homelab-infra/pull/114';
const base={investigation_id:2312,remediation_class:'gitops-patch',risk:'low',host_id:'default',
  attempts:0,created_at:'2026-08-28T11:10:04',payload:{recommendation:'merge the PR'}};
const ROWS={
  85:Object.assign({},base,{id:85,status:'needs-human',pr_url:null,named_pr_url:NAMED}),
  86:Object.assign({},base,{id:86,status:'pr-open',pr_url:TRACKED,named_pr_url:null}),
  87:Object.assign({},base,{id:87,status:'needs-human',pr_url:null,named_pr_url:null})};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,
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
    if(url.indexOf('/api/remediation/flags')===0){
      return Promise.resolve({json:()=>Promise.resolve({queue_feed:true,queue_reap:true,queue_drain:true,queue_verify:true})});
    }
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
const common=fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={};
  for (const id of [85,86,87]) {
    await box.detail(id);
    out['drawer'+id]=box.document.getElementById('detail').innerHTML;
    out['row'+id]=box.rowHtml(ROWS[id]);
    box.closeDetail();
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""

_TRACKED = "https://github.com/aachtenberg/homelab-infra/pull/116"
_NAMED = "https://github.com/aachtenberg/homelab-infra/pull/114"


@pytest.fixture(scope="module")
def pr_drawer(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("prdrawer") / "stub.js"
    stub.write_text(_PR_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "remediations.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_row_with_a_tracked_pr_offers_review_not_approve(pr_drawer):
    d = pr_drawer["drawer86"]
    assert "approve(86)" not in d, "Approve on a row with an open PR opens a second PR"
    assert f'href="{_TRACKED}"' in d and "Review PR" in d
    assert "second PR" in d


def test_a_row_that_only_names_a_pr_keeps_approve_and_links_it(pr_drawer):
    """A #85-shaped row: the link for the human, the decision stays theirs."""
    d = pr_drawer["drawer85"]
    assert "approve(85)" in d
    assert f'href="{_NAMED}"' in d and "Named PR" in d
    assert "no row tracks" in d


def test_a_row_with_no_pr_is_unchanged(pr_drawer):
    d = pr_drawer["drawer87"]
    assert "approve(87)" in d
    assert "Review PR" not in d and "Named PR" not in d and "github.com" not in d


def test_the_list_links_both_kinds_of_pr(pr_drawer):
    assert f'href="{_TRACKED}"' in pr_drawer["row86"] and "PR ↗" in pr_drawer["row86"]
    assert f'href="{_NAMED}"' in pr_drawer["row85"] and "not tracked" in pr_drawer["row85"]
    assert "github.com" not in pr_drawer["row87"]
