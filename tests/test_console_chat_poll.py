"""The console chat's event poll, and how it ends (CFOP-159).

A turn in the console chat is a POST that returns a ``chat_id`` and then a
one-second poll of ``/api/chat/events/<id>`` until the payload says ``done``.
The interesting case is not the happy path, it is what happens when the id
stops existing — which is routine here, not exotic: the agent runs under
``strategy: Recreate`` and is replaced on every merge to main, so any turn in
flight is dropped and its chat_id 404s from then on. A finished chat's session
is also reaped five minutes after it completes.

Before this, the poll read the body without looking at the status. ``fetch``
does not reject on 404 and the error body is still JSON, so ``done`` came back
``undefined``, ``!data.done`` was true, and the console polled a dead id every
second for the rest of the page's life — thinking dots up, composer disabled,
nothing coming. Observed in chat session 35 on 2026-09-02.

So these assert termination, not wording. The page's inline script runs under
node the way ``test_console_cockpit.py`` runs it, with ``setTimeout`` replaced
by a counted immediate drain: a poll that intends to run forever shows up as
the drain hitting its cap, whatever delay it asked for.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
PAGE = UI / "index.html"

#: How many re-polls the harness will honour before it stops draining. The
#: fixed code never gets near it; the broken code hits it every time, which is
#: exactly the signal — an unbounded poll cannot be distinguished from a long
#: one by waiting, only by capping.
DRAIN_CAP = 40


_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mode=process.argv[3];

const out={polls:0, drains:0, delays:[], messages:[], capped:false};

function el(){return{className:'',innerHTML:'',textContent:'',value:'',rows:1,
  disabled:false,hidden:false,scrollTop:0,scrollHeight:0,children:[],
  style:{},dataset:{},
  classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
             contains(c){return this._s.has(c);},toggle(){}},
  setAttribute(){},removeAttribute(){},getAttribute(){return null;},
  select(){},remove(){},focus(){},blur(){},scrollIntoView(){},
  appendChild(n){this.children.push(n);},insertBefore(){},
  querySelector(){return el();},querySelectorAll(){return [];},
  addEventListener(){},removeEventListener(){},closest(){return null;}};}

const els={};
const doc={readyState:'complete',documentElement:el(),body:el(),
  addEventListener(){},removeEventListener(){},
  createElement:()=>el(),
  querySelector(){return el();},querySelectorAll(){return [];},
  getElementById:id=>(els[id]=els[id]||el())};

// setTimeout is the whole instrument: every re-poll the page schedules is run
// immediately, and counted, up to a cap. A poll that never terminates hits the
// cap; one that does, does not.
const CAP = Number(process.argv[4]);
function fakeSetTimeout(fn, ms){
  out.delays.push(ms);
  if (out.drains < CAP) { out.drains++; setImmediate(fn); }
  else { out.capped = true; }
  return 0;
}

// What the events endpoint answers with, per mode.
function eventsResponse(){
  if (mode==='gone')       return {ok:false, status:404, body:{error:'Unknown chat_id'}};
  if (mode==='malformed')  return {ok:true,  status:200, body:{error:'nope'}};
  if (mode==='unreachable')return null;                     // fetch rejects
  if (mode==='stream')     return out.polls===1
      ? {ok:true, status:200, body:{events:[], cursor:0, done:false}}
      : {ok:true, status:200, body:{events:[{event:'done',data:{response:'hi'}}], cursor:1, done:true}};
  return {ok:true, status:200, body:{events:[], cursor:0, done:true}};
}

const box={console:{log:console.log, warn(){}, error(){}}, JSON, Math, Date, Number, String, Array,
  Object, Boolean, RegExp, Error, Promise, URLSearchParams, Set, Map, encodeURIComponent,
  decodeURIComponent, parseInt, parseFloat, isNaN,
  setTimeout:fakeSetTimeout, clearTimeout:()=>{},
  setInterval:()=>0, clearInterval:()=>{},
  location:{pathname:'/', search:'', hash:'', href:'http://cfop/'},
  history:{replaceState(){}},
  localStorage:{getItem:()=>null, setItem(){}, removeItem(){}},
  document:doc, navigator:{},
  marked:{parse:s=>s, setOptions(){}},
  renderMarkdown:s=>s, escapeHtmlText:s=>s, toast(){},
  CFOP:{me:()=>Promise.resolve({username:'a', role:'admin'})},
  alert(){}, confirm:()=>true,
  fetch:(url, opts)=>{
    if (String(url).indexOf('/api/chat/events/')===0){
      out.polls++;
      const r=eventsResponse();
      if (r===null) return Promise.reject(new Error('ECONNREFUSED'));
      return Promise.resolve({ok:r.ok, status:r.status, json:()=>Promise.resolve(r.body)});
    }
    // Everything else the page fires on load: answer emptily and quietly.
    return Promise.resolve({ok:true, status:200, json:()=>Promise.resolve({})});
  }};
box.window=box; box.globalThis=box; box.self=box;
vm.createContext(box);
vm.runInContext(src, box);

// Put the page into the state a live turn leaves it in, then poll.
const send=doc.getElementById('send-button');
const thinking=doc.getElementById('thinking');
const container=doc.getElementById('chat-container');
box.setChatInFlight('c0ffee');
send.disabled=true;
thinking.classList.add('active');
const before=container.children.length;

box.pollChatEvents('c0ffee', 0);

// Let every immediate the drain queued run out.
(function settle(n){
  if (n===0){
    out.thinkingActive = thinking.classList.contains('active');
    // `let` at the top level of a vm script lands in the context's global
    // lexical scope, not on the sandbox object, so this is read back by
    // evaluating the name rather than off `box`.
    out.inFlight = vm.runInContext('inFlightChatId', box);
    out.sendDisabled = send.disabled;
    out.sendLabel = send.textContent;
    out.newMessages = container.children.length - before;
    out.lastMessage = container.children.slice(-1).map(m=>m.innerHTML)[0] || '';
    console.log(JSON.stringify(out));
    return;
  }
  setImmediate(()=>settle(n-1));
})(200);
"""


def run(tmp_path_factory, mode, cap=DRAIN_CAP):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("chatpoll") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    proc = subprocess.run([node, str(stub), str(PAGE), mode, str(cap)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def gone(tmp_path_factory):
    return run(tmp_path_factory, "gone")


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    return run(tmp_path_factory, "stream")


@pytest.fixture(scope="module")
def malformed(tmp_path_factory):
    return run(tmp_path_factory, "malformed")


@pytest.fixture(scope="module")
def unreachable(tmp_path_factory):
    return run(tmp_path_factory, "unreachable")


# --------------------------------------------------------------------------
# the poll ends
# --------------------------------------------------------------------------

def test_a_404_stops_the_poll_instead_of_repeating_it_forever(gone):
    """The regression itself. One request, no re-poll scheduled."""
    assert not gone["capped"], (
        "the poll kept going after the server said it did not know the chat_id "
        f"({gone['polls']} requests before the harness capped it)")
    assert gone["polls"] == 1, gone["polls"]


def test_a_normal_stream_still_polls_until_done(stream):
    """The fix must not turn every poll into a single request."""
    assert stream["polls"] == 2, stream["polls"]
    assert stream["delays"][:1] == [1000], stream["delays"]
    assert not stream["capped"]


def test_a_body_that_is_not_an_events_payload_ends_it_too(malformed):
    """`done` absent is not `done: false`. Reading undefined as 'keep going'
    is what made a 404 body look like an unfinished turn."""
    assert malformed["polls"] == 1
    assert not malformed["capped"]


def test_a_transport_failure_retries_but_gives_up(unreachable):
    """A rolling restart is worth riding out; a dead agent is not worth
    polling for the rest of the session."""
    assert 2 <= unreachable["polls"] <= 10, unreachable["polls"]
    assert not unreachable["capped"], "the retry is unbounded"
    assert unreachable["delays"][0] == 2000, unreachable["delays"]


# --------------------------------------------------------------------------
# ...and the operator gets the console back
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["gone", "malformed", "unreachable"])
def test_the_composer_is_returned_when_the_turn_is_lost(case, request):
    """The visible symptom: a console that looks busy forever. Whatever the
    reason the turn ended, the operator must be able to type again."""
    data = request.getfixturevalue(case)
    assert data["inFlight"] is None, "the chat is still marked in flight"
    assert data["sendDisabled"] is False, "the send button is still disabled"
    assert data["sendLabel"] == "Send", (
        f"the button still says {data['sendLabel']!r} — it is a Stop for a chat "
        "that is not running")
    assert not data["thinkingActive"], "the thinking indicator is still up"


@pytest.mark.parametrize("case", ["gone", "malformed", "unreachable"])
def test_the_lost_turn_is_announced_rather_than_swallowed(case, request):
    """Silence here reads as 'the agent is thinking', which is the state this
    replaces. It has to say something."""
    data = request.getfixturevalue(case)
    assert data["newMessages"] == 1, (
        f"{data['newMessages']} messages added; the operator is told nothing "
        "about why the answer never arrived")
    assert "ask again" in data["lastMessage"], data["lastMessage"]


def test_a_finished_turn_says_nothing_extra(stream):
    """The announcement is for a lost turn only — a chat that completed must
    not grow an apology after its answer."""
    assert "ask again" not in stream["lastMessage"], stream["lastMessage"]
