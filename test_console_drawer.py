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

REPO_ROOT = Path(__file__).resolve().parent
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
    assert ask.index("sendMessage();") < ask.index("history.replaceState("), (
        "the param is stripped before the question is sent")
    boot = html[html.index("const handedRemediation"):]
    boot = boot[:boot.index("} else if (savedSession)")]
    assert "history.replaceState" not in boot, "the bootstrap strips the param before anything is sent"
    assert "reload to retry" in boot, "a failed session create for a handed row is silent"


@pytest.mark.parametrize("page", COPY_PAGES)
def test_copy_does_not_assume_a_secure_context(page):
    """``navigator.clipboard`` is undefined over plain HTTP, which is how this
    console is normally reached on a LAN. A copy button that only tries the
    async API is a button that does nothing for most operators here."""
    html = read(page)
    if "copyElementText(" in html:
        # The copy lives in common.js since CFOP-109; the fallback must be there.
        common = read("common.js")
        assert "navigator.clipboard" in common and "execCommand('copy')" in common, (
            "common.js copyElementText lost its plain-HTTP fallback")
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
  navigator:{},
  fetch:(url)=>{
    if(url.indexOf('/api/investigations/')===0){
      detailFetches.push(url);
      const id=Number(url.split('/').pop());
      const res={json:()=>Promise.resolve({id:id,trigger:'backup failed',
        outcome:'monitoring',attach_command:'cfassist attach '+id,findings:{}})};
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
const common=fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();          // let the initial load() settle
  const out={};

  await box.detail(2272);
  out.hashAfterOpen=loc.hash;
  out.drawerHasCommand=box.document.getElementById('detail').innerHTML
    .indexOf('cfassist attach 2272')>=0;

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
  console.log(JSON.stringify({finalDrawerId:(drawn.match(/#(\d+)<\/h2>/)||[])[1]}));
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
