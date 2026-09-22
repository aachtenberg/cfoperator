"""The console chat keeps a record of what it ran (CFOP-125).

Session 21 ran nine ``k8s_exec_pod`` calls. The pod log said only
``Executing tool: k8s_exec_pod``, and the transcript held the user text and
the final reply — and only those, and only because the browser was still on
the page to save them. Tool calls were drawn live and dropped.

The worker now writes each event into the session as it arrives, keyed by
the session id the client sends, and a reloaded session draws the tool rows
back. The browser no longer saves the assistant turn: that was the second
writer.
"""

from repo_paths import REPO_ROOT
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = REPO_ROOT
PAGE = ROOT / "ui" / "index.html"

ARGS = {
    "namespace": "sre",
    "pod_name": "sre-postgres-0",
    "command": "psql -c \"UPDATE remediation_queue SET status='resolved'\"",
}


class RecordingKB:
    """The chat-session writer, without a database behind it."""

    def __init__(self):
        self.messages = []

    def append_chat_message(self, session_id, role, content, backend="", model="", extra=None):
        row = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "backend": backend,
            "model": model,
        }
        if extra:
            row.update(extra)
        self.messages.append(row)
        return True


class BoomKB:
    def append_chat_message(self, *args, **kwargs):
        raise RuntimeError("db down")


def _client(operator):
    from flask import Flask

    from web_auth import install_auth
    from web_server import WebServer

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "1"
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN"):
        os.environ[name] = ""
    try:
        install_auth(server.app, ui_dir="ui", store=None)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return server.app.test_client()


def _operator(kb, events):
    from unittest.mock import MagicMock

    operator = MagicMock()
    operator.kb = kb

    def stream(*args, **kwargs):
        for evt in events:
            yield evt

    operator.handle_chat_message_stream.side_effect = stream
    return operator


def _wait_for(predicate, what, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(what)


def _post(client, body):
    resp = client.post("/api/chat", json=body)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["chat_id"]


def _until_done(client, chat_id):
    for _ in range(100):
        payload = client.get(f"/api/chat/events/{chat_id}").get_json()
        if payload.get("done"):
            return payload
        time.sleep(0.02)
    raise AssertionError("chat never finished")


TOOL_TURN = [
    {"event": "tool_call", "data": {
        "tool": "k8s_exec_pod", "args": ARGS, "iteration": 1, "max": 8}},
    {"event": "tool_result", "data": {
        "tool": "k8s_exec_pod", "result": "UPDATE 1"}},
    {"event": "done", "data": {
        "response": "Closed the row.", "backend": "xai", "model": "grok"}},
]


def test_a_chat_that_runs_a_tool_keeps_the_call_and_the_reply():
    """The done-when. Nobody posts to /messages: the worker writes the tool
    row, with the pod and the command, and the assistant reply, because the
    client sent its session id. A closed tab is this test — it never reads
    the events."""
    kb = RecordingKB()
    client = _client(_operator(kb, TOOL_TURN))
    _post(client, {"message": "resolve 82", "session_id": 21})
    _wait_for(lambda: any(m["role"] == "assistant" for m in kb.messages),
              f"reply was not stored; rows={kb.messages}")

    roles = [m["role"] for m in kb.messages]
    assert roles == ["tool_call", "tool_result", "assistant"]
    call = kb.messages[0]
    assert call["tool"] == "k8s_exec_pod"
    assert call["args"]["namespace"] == "sre"
    assert call["args"]["pod_name"] == "sre-postgres-0"
    assert "UPDATE remediation_queue" in call["args"]["command"]
    assert "sre-postgres-0" in call["content"]
    assert kb.messages[1]["result"] == "UPDATE 1"
    reply = kb.messages[2]
    assert reply["content"] == "Closed the row."
    assert reply["backend"] == "xai" and reply["model"] == "grok"
    assert all(m["session_id"] == 21 for m in kb.messages)


def test_a_turn_with_no_session_is_not_written_down():
    """Callers that have no transcript still chat. Nothing is appended."""
    kb = RecordingKB()
    client = _client(_operator(kb, TOOL_TURN))
    chat_id = _post(client, {"message": "hi"})
    _until_done(client, chat_id)
    time.sleep(0.1)
    assert kb.messages == []


def test_an_error_is_stored_and_a_dead_database_does_not_drop_the_turn():
    """The failure is part of the record. A transcript write that itself
    fails is logged and the live turn still finishes."""
    kb = RecordingKB()
    client = _client(_operator(kb, [
        {"event": "tool_call", "data": {"tool": "k8s_exec_pod", "args": ARGS}},
        {"event": "error", "data": {"error": "the database refused"}},
    ]))
    _post(client, {"message": "resolve 82", "session_id": 21})
    _wait_for(lambda: any(m["role"] == "error" for m in kb.messages),
              f"error was not stored; rows={kb.messages}")
    assert kb.messages[0]["args"]["pod_name"] == "sre-postgres-0"
    assert kb.messages[-1]["content"] == "the database refused"

    boom = _client(_operator(BoomKB(), TOOL_TURN))
    chat_id = _post(boom, {"message": "resolve 82", "session_id": 21})
    done = _until_done(boom, chat_id)
    assert done["done"] is True
    assert any(evt["event"] == "done" for evt in done["events"])


def test_the_page_sends_the_session_and_does_not_save_the_assistant_itself():
    """Two writers would double the reply. The user row stays on the client,
    and it is saved before the turn starts so it cannot land under the tool
    call the worker writes immediately."""
    page = PAGE.read_text(encoding="utf-8")
    assert "payload.session_id = chatSessionId" in page
    assert "saveMessage('assistant'" not in page
    assert "saveMessage('user', message).then(" in page
    assert "msg.role === 'tool_call'" in page
    assert "msg.role === 'tool_result'" in page


# --------------------------------------------------------------------------
# reloading a session draws the tool row, the way the live view did
# --------------------------------------------------------------------------

_REPLAY = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];

function El(){
  this.className=''; this._html=''; this.textContent=''; this.value='';
  this.children=[]; this.style={}; this.dataset={}; this.parentElement=null;
  this.disabled=false; this.scrollTop=0; this.scrollHeight=0;
  const self=this;
  this.classList={
    add(){}, remove(){}, contains(){return false;},
    toggle(c){ self.className += ' '+c; }
  };
}
El.prototype.appendChild=function(n){ n.parentElement=this; this.children.push(n); return n; };
El.prototype.addEventListener=function(){};
El.prototype.removeEventListener=function(){};
El.prototype.setAttribute=function(){};
El.prototype.removeAttribute=function(){};
El.prototype.getAttribute=function(){ return null; };
El.prototype.closest=function(){ return null; };
El.prototype.focus=function(){};
El.prototype.blur=function(){};
El.prototype.select=function(){};
El.prototype.remove=function(){};
El.prototype.scrollIntoView=function(){};
El.prototype.insertBefore=function(){};
El.prototype.querySelectorAll=function(sel){
  const cls=sel.replace(/^\./,'');
  const out=[];
  (function walk(node){
    for (const ch of node.children){
      if ((ch.className||'').split(/\s+/).indexOf(cls)>=0) out.push(ch);
      walk(ch);
    }
  })(this);
  return out;
};
El.prototype.querySelector=function(sel){ return this.querySelectorAll(sel)[0]||null; };
Object.defineProperty(El.prototype,'innerHTML',{
  get(){ return this._html; },
  set(v){
    this._html=String(v);
    this.children=[];
    const stack=[{children:this.children}];
    const re=/<\/?div\b[^>]*>/gi;
    let m;
    while ((m=re.exec(this._html))){
      if (m[0][1]==='/'){ if (stack.length>1) stack.pop(); continue; }
      const node=new El();
      const found=m[0].match(/class="([^"]*)"/);
      node.className=found?found[1]:'';
      stack[stack.length-1].children.push(node);
      stack.push(node);
    }
  }
});

const els={};
function el(){ return new El(); }
const doc={
  readyState:'complete', documentElement:el(), body:el(),
  addEventListener(){}, removeEventListener(){},
  createElement:()=>new El(),
  querySelector(){return el();}, querySelectorAll(){return [];},
  getElementById:id=>(els[id]=els[id]||new El())
};

const session={
  id:7,
  messages:[
    {role:'user', content:'resolve 82'},
    {role:'tool_call', tool:'k8s_exec_pod', args:{
      namespace:'sre', pod_name:'sre-postgres-0', command:'psql -c "select 1"'}},
    {role:'tool_result', tool:'k8s_exec_pod', result:'UPDATE 1'},
    {role:'assistant', content:'Closed the row.', backend:'xai', model:'grok'},
    {role:'error', content:'the database refused'}
  ]
};

const box={
  console:{log(){}, warn(){}, error(){}},
  JSON, Math, Date, Number, String, Array, Object, Boolean, RegExp, Error, Promise,
  URLSearchParams, Set, Map, encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  setTimeout:(fn)=>setImmediate(fn), clearTimeout(){},
  setInterval:()=>0, clearInterval(){},
  location:{pathname:'/', search:'', hash:'', href:'http://cfop/'},
  history:{replaceState(){}},
  localStorage:{getItem:()=>null, setItem(){}, removeItem(){}},
  document:doc, navigator:{},
  marked:{parse:s=>s, setOptions(){}},
  renderMarkdown:s=>s, escapeHtmlText:s=>s, toast(){},
  CFOP:{me:()=>Promise.resolve({username:'a', role:'admin'})},
  alert(){}, confirm:()=>true,
  fetch:(url)=>{
    if (String(url).indexOf('/api/chat-sessions/7')===0){
      return Promise.resolve({ok:true, status:200, json:()=>Promise.resolve(session)});
    }
    return Promise.resolve({ok:true, status:200, json:()=>Promise.resolve({})});
  }
};
box.window=box; box.globalThis=box; box.self=box;
vm.createContext(box);
vm.runInContext(src, box);
box.loadChatSession(7);

function texts(node, acc){
  if (node._html) acc.push(node._html);
  for (const ch of node.children||[]) texts(ch, acc);
}

setImmediate(()=>{
  const acc=[];
  texts(doc.getElementById('chat-container'), acc);
  const html=acc.join('\n');
  const history=vm.runInContext('chatHistory.map(m=>m.role)', box);
  console.log(JSON.stringify({html, history}));
});
"""


def test_a_reloaded_session_draws_the_tool_row(tmp_path):
    """The transcript has to read the way the live view did. The pod name is
    in a tool card, the reply is a message, and the tool row is not fed back
    to the model as history."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = Path(tmp_path) / "replay.js"
    stub.write_text(_REPLAY, encoding="utf-8")
    proc = subprocess.run([node, str(stub), str(PAGE)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "sre-postgres-0" in out["html"]
    assert "k8s_exec_pod" in out["html"]
    assert "psql" in out["html"]
    assert "UPDATE 1" in out["html"]
    assert "Closed the row." in out["html"]
    assert "Error: the database refused" in out["html"]
    assert out["history"] == ["user", "assistant"]
