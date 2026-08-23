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
#: drawer. Both should be linkable; a third one added later should have to
#: think about it rather than quietly not be.
DRAWER_PAGES = ["investigations.html", "remediations.html"]

#: Pages offering a one-click copy of something. The console is normally
#: reached over plain HTTP on the LAN, where ``navigator.clipboard`` does not
#: exist, so every one of these needs the fallback.
COPY_PAGES = ["investigations.html", "account.html", "admin.html"]


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
    server.sock = None
    server.ws_clients = []
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
# the drawer offers it, and does not spell it itself
# --------------------------------------------------------------------------

def test_the_drawer_renders_the_servers_command():
    assert "attach_command" in inline_script("investigations.html"), (
        "the drawer no longer reads the command off the investigation payload")


def test_the_drawer_does_not_write_the_command_itself():
    """A literal here would be a third copy of the handoff line — one the
    cross-artifact contract test cannot see, and the first to drift."""
    assert "cfassist attach" not in read("investigations.html"), (
        "investigations.html spells the attach command itself; render the "
        "server's attach_command instead so the contract test covers it")


@pytest.mark.parametrize("page", COPY_PAGES)
def test_copy_does_not_assume_a_secure_context(page):
    """``navigator.clipboard`` is undefined over plain HTTP, which is how this
    console is normally reached on a LAN. A copy button that only tries the
    async API is a button that does nothing for most operators here."""
    html = read(page)
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
    """Guards the next page that grows a drawer: if you can open a row, you
    should be able to hand someone the URL of the row you opened."""
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

const detailFetches=[];
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
  window:{location:loc,history:hist,addEventListener(){}},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},addEventListener(){},
    createElement:()=>el(),
    getElementById:id=>(els[id]=els[id]||el())},
  navigator:{},
  fetch:(url)=>{
    if(url.indexOf('/api/investigations/')===0){
      detailFetches.push(url);
      return Promise.resolve({json:()=>Promise.resolve(
        {id:2272,trigger:'backup failed',outcome:'monitoring',
         attach_command:'cfassist attach 2272',findings:{}})});
    }
    if(url.indexOf('/api/investigations')===0){
      return Promise.resolve({json:()=>Promise.resolve({investigations:[]})});
    }
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);

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
