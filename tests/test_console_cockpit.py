"""The browser cockpit in the investigation drawer (CFOP-59).

One click → a briefed session on the affected host, in the console. The
agent's bridge (CFOP-75) carries the bytes; ``/api/cockpit/<id>/open`` hands
the page a session, a websocket URL and a one-shot ticket. What these guard is
the page's side of that contract, which a grep cannot see:

* the auth frame is the *first* thing on the socket, and it is text; keystrokes
  are binary; resize is text — the bridge splits control from data by frame
  type, so a page that got this wrong would have someone typing the word
  "resize" into a shell;
* a refusal from /open lands beside the button with the server's reason, and
  the attach line stays — the fallback is the copy button, not an error page;
* each bridge close code maps to a different next action;
* the button is drawn for admins only, and the page never keeps the ticket.

The page's own inline script runs under node, the way ``test_console_drawer.py``
runs it, with the smallest stubs that let it load plus a fake WebSocket and a
fake Terminal that record what the page did to them.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = REPO_ROOT
UI = REPO_ROOT / "ui"
PAGE = UI / "investigations.html"


def read(name):
    return (UI / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# static: the page loads the vendored terminal, and only that
# --------------------------------------------------------------------------

def test_the_page_loads_xterm_from_the_vendor_path():
    html = read("investigations.html")
    for asset in ("/vendor/xterm.js", "/vendor/xterm-addon-fit.js", "/vendor/xterm.css"):
        assert asset in html, f"investigations.html does not load {asset}"


def test_the_page_never_puts_the_ticket_anywhere_durable():
    """The ticket is spent on verify; a copy in storage or the URL would be a
    dead credential at best and a habit at worst."""
    js = re.findall(r"<script>(.*?)</script>", read("investigations.html"), re.S)[0]
    for sink in ("localStorage", "sessionStorage", "document.cookie"):
        assert sink not in js, f"the page touches {sink}"
    # The hash carries the investigation id and nothing else.
    for call in re.findall(r"history\.replaceState\(([^)]*)\)", js):
        assert "ticket" not in call and "bridge" not in call, call


# --------------------------------------------------------------------------
# behaviour, under node
# --------------------------------------------------------------------------

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mode=process.argv[3]||'admin';

const out={fetches:[], frames:[], written:[], confirms:0, toasts:[]};
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/investigations',search:'',hash:'',href:'http://cfop/investigations'};
const hist={replaceState(state,title,url){ loc.hash=(typeof url==='string'&&url.startsWith('#'))?url:''; }};

// The bridge's end of the socket, recording every frame by type.
let sockets=[];
class FakeWS{
  constructor(url){ this.url=url; this.readyState=0; this.binaryType=''; sockets.push(this);
    setTimeout(()=>{ this.readyState=1; this.onopen&&this.onopen(); },0); }
  send(data){ out.frames.push(typeof data==='string'?{kind:'text',data}:{kind:'binary',bytes:Array.from(data)}); }
  close(code){ this.readyState=3; const ws=this; setTimeout(()=>ws.onclose&&ws.onclose({code:code||1000}),0); }
}
class FakeTerminal{
  constructor(opts){ this.opts=opts; this.cols=80; this.rows=24; this.opened=false; }
  loadAddon(a){ this.addon=a; } open(node){ this.opened=true; } focus(){} dispose(){ out.disposed=true; }
  write(bytes){ out.written.push(Array.from(bytes)); }
  onData(cb){ this._data=cb; } onResize(cb){ this._resize=cb; }
}
class FakeFit{ fit(){ out.fitted=(out.fitted||0)+1; } }

const openResponse = mode==='refused'
  ? {ok:false,status:409,body:{error:'the cockpit bridge is not enabled on this agent (cockpit.bridge_enabled)',code:'bridge_disabled',attach_command:'cfassist attach 2272'}}
  : {ok:true,status:201,body:{status:'spawned',tier:'host',host:'raspberrypi5',expires_at:Math.floor(Date.now()/1000)+227,
      bridge:{url:'ws://cfop:8084/cockpit/2272',ticket:'cfop_T1CKET',scope:'investigate',ticket_ttl_seconds:120}}};

const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Uint8Array,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist,
  TextEncoder:class{ encode(s){ return Uint8Array.from(Buffer.from(s,'utf8')); } },
  WebSocket:FakeWS, Terminal:FakeTerminal, FitAddon:{FitAddon:FakeFit},
  confirm:()=>{ out.confirms++; return true; },
  alert:()=>{},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},addEventListener(){},
    createElement:()=>{ const e=el(); return e; },
    getElementById:id=>(els[id]=els[id]||el())},
  navigator:{},
  fetch:(url,opts)=>{
    out.fetches.push({url, method:(opts&&opts.method)||'GET', body:opts&&opts.body});
    if(url.indexOf('/api/cockpit/')===0 && url.endsWith('/open')){
      const r=openResponse; return Promise.resolve({ok:r.ok,status:r.status,json:()=>Promise.resolve(r.body)});
    }
    if(url.indexOf('/api/cockpit/')===0 && url.endsWith('/close')){
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({status:'closed',host:'raspberrypi5',removed:[],tokens_revoked:2})});
    }
    if(url.indexOf('/api/investigations/')===0){
      const id=Number(url.split('/').pop());
      return Promise.resolve({ok:true,json:()=>Promise.resolve({id:id,trigger:'PodUnschedulable on headless-gpu',
        outcome:'needs_action',attach_command:'cfassist attach '+id,findings:{}})});
    }
    if(url.indexOf('/api/investigations')===0) return Promise.resolve({json:()=>Promise.resolve({investigations:[]})});
    return Promise.resolve({json:()=>Promise.resolve({remediations:[]})});
  }};
box.window={location:loc,history:hist,addEventListener(){},removeEventListener(){},
  Terminal:FakeTerminal, FitAddon:{FitAddon:FakeFit},
  CFOP:{me:()=>Promise.resolve(mode==='member'?{role:'member'}:{role:'admin'})}};
// The helpers the page calls (esc, badge, toast, trapFocus) live in ui/common.js
// since CFOP-95; the browser loads it before the page script, so the harness does.
const common=fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  await box.detail(2272);
  const drawer=box.document.getElementById('detail').innerHTML;
  out.buttonDrawn=drawer.indexOf('Open cockpit')>=0;
  out.attachLineStill=drawer.indexOf('cfassist attach 2272')>=0;

  // pure functions
  out.reasons={};
  for(const c of [4401,4403,4404,4409,4429,1000,1006,4999]) out.reasons[c]=box.closeReason(c,'');
  out.reasonWithText=box.closeReason(4403,'origin not allowed');
  out.ttl={n227:box.fmtTTL(227), zero:box.fmtTTL(0), neg:box.fmtTTL(-5), cls60:box.ttlClass(60), cls900:box.ttlClass(900), cls0:box.ttlClass(0)};
  out.authFrame=JSON.parse(box.authFrame('abc'));
  out.resizeFrame=JSON.parse(box.resizeFrame(120,40));

  if(mode==='admin'){
    await box.openCockpit(2272);
    await tick(); await tick(); await new Promise(r=>setTimeout(r,5));
    out.wsUrl=sockets.length?sockets[0].url:null;
    out.wsBinaryType=sockets.length?sockets[0].binaryType:null;
    out.framesAfterOpen=out.frames.slice();
    out.status=box.document.getElementById('cp-status').textContent;
    out.wide=true; // class toggles are no-ops in the stub; the call is what is asserted below
    // the bridge sends bytes, then an error frame, then closes
    const ws=sockets[0], term=box.COCKPIT.term;
    ws.onmessage({data:Uint8Array.from([104,105]).buffer});
    ws.onmessage({data:JSON.stringify({type:'error',code:4429,reason:'the bridge is already carrying 2 terminals (max 2)'})});
    // the operator types
    term._data('ls\n');
    term._resize({cols:100,rows:30});
    out.framesAfterTyping=out.frames.slice();
    ws.onclose({code:4429});
    out.statusAfterClose=box.document.getElementById('cp-status').textContent;
    out.wsClearedAfterClose=box.COCKPIT.ws===null;
    // kill
    await box.openCockpit(2272); await tick(); await new Promise(r=>setTimeout(r,5));
    await box.killCockpit(2272);
    out.killFetch=out.fetches.filter(f=>f.url.endsWith('/close')).length;
    out.disposedAfterKill=!!out.disposed && box.COCKPIT.term===null;
    out.toastsAfterKill=box.document.getElementById('toasts');
  }
  if(mode==='refused'){
    await box.openCockpit(2272);
    await tick();
    out.note=box.document.getElementById('cockpit-note').textContent;
    out.socketsOpened=sockets.length;
  }
  if(mode==='reentry'){
    // Open a terminal, then re-enter det() for the SAME row (a re-click, or
    // triage() ending in detail(id)). The first socket must be closed and no
    // slot left leaking against bridge_max_sessions.
    await box.openCockpit(2272);
    await tick(); await tick(); await new Promise(r=>setTimeout(r,5));
    const ws0=sockets[0];
    out.socketOpenBeforeReentry = ws0 && ws0.readyState===1;
    await box.detail(2272);
    await tick();
    out.firstSocketClosedAfterReentry = ws0 && ws0.readyState===3;
    out.wsNullAfterReentry = box.COCKPIT.ws===null;
    out.socketsTotal = sockets.length;   // no new socket opened by detail()
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def run_page(tmp_path_factory, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("cockpit") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(PAGE), mode],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def admin(tmp_path_factory):
    return run_page(tmp_path_factory, "admin")


@pytest.fixture(scope="module")
def member(tmp_path_factory):
    return run_page(tmp_path_factory, "member")


@pytest.fixture(scope="module")
def refused(tmp_path_factory):
    return run_page(tmp_path_factory, "refused")


def test_the_button_is_drawn_for_admins_and_not_for_members(admin, member):
    assert admin["buttonDrawn"]
    assert not member["buttonDrawn"]
    assert member["attachLineStill"], "members keep the attach line"


def test_the_auth_frame_is_first_and_text_then_resize(admin):
    frames = admin["framesAfterOpen"]
    assert frames, "nothing was sent on the socket"
    assert frames[0]["kind"] == "text"
    assert json.loads(frames[0]["data"]) == {"type": "auth", "token": "cfop_T1CKET"}
    assert frames[1]["kind"] == "text" and json.loads(frames[1]["data"])["type"] == "resize"
    assert admin["wsUrl"] == "ws://cfop:8084/cockpit/2272"
    assert admin["wsBinaryType"] == "arraybuffer"


def test_keystrokes_are_binary_and_resize_is_text(admin):
    after = admin["framesAfterTyping"][len(admin["framesAfterOpen"]):]
    kinds = [f["kind"] for f in after]
    assert kinds == ["binary", "text"], kinds
    assert bytes(after[0]["bytes"]) == b"ls\n"
    assert json.loads(after[1]["data"]) == {"type": "resize", "cols": 100, "rows": 30}


def test_bytes_from_the_bridge_reach_the_terminal(admin):
    assert admin["written"] and bytes(admin["written"][0]) == b"hi"


def test_a_close_says_which_wall_it_hit(admin):
    assert admin["statusAfterClose"].startswith("disconnected: the bridge is carrying its maximum terminals")
    assert "already carrying 2 terminals" in admin["statusAfterClose"], (
        "the bridge's own reason from the error frame is kept")
    assert admin["wsClearedAfterClose"]


def test_every_close_code_is_a_different_next_action(admin):
    r = admin["reasons"]
    assert "sign in" in r["4401"]
    assert "bridge_origins" in r["4403"] and "investigate" in r["4403"]
    assert "Reopen" in r["4404"]
    assert "attach line" in r["4409"]
    assert "maximum" in r["4429"]
    assert r["1006"].startswith("connection dropped")
    assert "4999" in r["4999"]
    assert len({r[k] for k in r}) == len(r), "two codes share a sentence"
    assert "origin not allowed" in admin["reasonWithText"]


def test_the_countdown_formats_and_colours(admin):
    t = admin["ttl"]
    assert t["n227"] == "3:47" and t["zero"] == "0:00" and t["neg"] == "0:00"
    assert t["cls900"] == "ttl" and t["cls60"] == "ttl low" and t["cls0"] == "ttl out"


def test_kill_posts_close_and_tears_the_terminal_down(admin):
    assert admin["confirms"] >= 1, "kill asks first"
    assert admin["killFetch"] == 1
    assert admin["disposedAfterKill"]


def test_a_refusal_lands_beside_the_button_and_opens_no_socket(refused):
    assert "bridge_enabled" in refused["note"]
    assert refused["socketsOpened"] == 0
    assert refused["attachLineStill"], "the fallback is the copy button"


@pytest.fixture(scope="module")
def reentry(tmp_path_factory):
    return run_page(tmp_path_factory, "reentry")


def test_reentering_the_open_row_does_not_leak_the_bridge_socket(reentry):
    """detail() replaces the drawer's innerHTML, orphaning the terminal node; a
    same-id re-entry (re-click, or triage() → detail(id)) must close the socket
    rather than leave it holding one of the two bridge slots. Regression from
    the #182 review."""
    assert reentry["socketOpenBeforeReentry"], "the terminal never connected"
    assert reentry["firstSocketClosedAfterReentry"], "re-entering detail() leaked the live socket"
    assert reentry["wsNullAfterReentry"], "COCKPIT.ws still points at the orphaned socket"
    assert reentry["socketsTotal"] == 1, "detail() should not open a second socket on its own"
