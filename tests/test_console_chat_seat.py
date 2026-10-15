"""The console chat's model is a per-session seat, not a global switch (CFOP-417).

Before this, the header chip wrote ``selected_backend`` — the default for
every future chat — and the page always posted ``backend: 'auto'``. Switch to
a frontier for one question, forget, and every later chat paid for it; open
a row with Ask console and get the cheap model until you stopped it and
switched. Now a session starts on a seat (``chat`` for a plain session,
``handoff`` for an Ask-console row, both set in Admin → LLM), the seat is
stored on the session row, every turn carries it, and the chip changes that
one session only.

Two halves. The page runs under node the way ``test_console_chat_poll.py``
runs it, with every ``fetch`` recorded, through the four flows the issue
names: a plain load, a handed row, a chip change, a new chat after a change.
The routes run over the ``test_console_provider_registry`` fixture with the
agent's real seat resolver bound on, the way ``test_console_llm_scopes.py``
does it.
"""

from repo_paths import REPO_ROOT
import json
import os
import shutil
import subprocess
import sys
import time
from types import MethodType

import pytest

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

from test_console_provider_registry import server  # noqa: E402,F401 -- the fixture

UI = REPO_ROOT / "ui"
PAGE = UI / "index.html"

# What the stub server hands back for each seat. Distinct on purpose: the
# assertions below are that the right one reaches /api/chat, so the two must
# be tellable apart.
CHAT_SEAT = {"seat": "chat", "backend": "ollama", "model": "gemma4:26b", "source": "console"}
HANDOFF_SEAT = {"seat": "handoff", "backend": "anthropic", "model": "claude-opus-5-5", "source": "console"}

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mode=process.argv[3];
const SEATS=JSON.parse(process.argv[4]);

const out={calls:[], badges:[]};

function el(){return{className:'',innerHTML:'',textContent:'',value:'',rows:1,
  disabled:false,hidden:false,scrollTop:0,scrollHeight:0,children:[],options:[],
  style:{},dataset:{},title:'',
  classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
             contains(c){return this._s.has(c);},
             toggle(c,force){ if(force===undefined) force=!this._s.has(c); if(force) this._s.add(c); else this._s.delete(c); return force; }},
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

let nextSession=41;
function respond(body, ok){ return Promise.resolve({ok:ok!==false, status:ok===false?500:200, json:()=>Promise.resolve(body)}); }

// Every request the page makes is recorded; the ones the flows depend on
// are answered the way the server would.
function fetch(url, opts){
  url=String(url); opts=opts||{};
  const method=(opts.method||'GET').toUpperCase();
  let body=null; try{ body=opts.body?JSON.parse(opts.body):null; }catch(e){ body=opts.body; }
  out.calls.push({url, method, body});
  if (url==='/api/chat-sessions' && method==='POST'){
    const seat=(body&&body.seat)||'chat';
    return respond({id: nextSession++, llm: SEATS[seat]||null});
  }
  const m=url.match(/^\/api\/chat-sessions\/(\d+)\/llm$/);
  if (m && method==='PUT'){
    const cur=vm.runInContext('chatLlm', box)||{};
    const seat=cur.seat||'chat';
    if (!body.backend || body.backend==='auto') return respond({success:true, llm: SEATS[seat]});
    return respond({success:true, llm:{seat, backend:body.backend, model:body.model||'', source:'session'}});
  }
  if (/^\/api\/chat-sessions\/\d+\/messages$/.test(url)) return respond({success:true});
  if (/^\/api\/chat-sessions\/\d+$/.test(url)) return respond({id:Number(url.split('/').pop()), messages:[], llm:null});
  if (url.indexOf('/api/chat-sessions?')===0) return respond({sessions:[]});
  if (/^\/api\/investigations\/\d+$/.test(url)) return respond({id:7, outcome:'resolved', host_id:'raspberrypi5', findings:{recommendation:'look again'}});
  if (/^\/api\/remediations\/\d+$/.test(url)) return respond({id:9, status:'needs_human', remediation_class:'manual', risk:'low', payload:{recommendation:'restart it'}});
  if (url==='/api/chat' && method==='POST') return respond({chat_id:'c1'});
  if (url.indexOf('/api/chat/events/')===0) return respond({events:[{event:'done', data:{response:'ok', backend:'x', model:'y'}}], cursor:1, done:true});
  if (url==='/api/settings/provider') return respond({backend:'ollama', model:'gemma4:26b'});
  if (url==='/api/providers') return respond({providers:[{id:'auto',name:'Auto',available:true},{id:'ollama',name:'Ollama',available:true},{id:'xai',name:'xAI',available:true}]});
  if (url.indexOf('/api/models/')===0) return respond({models:['grok-4'], selected:''});
  return respond({});
}

const box={console:{log:console.log, warn(){}, error(){}}, JSON, Math, Date, Number, String, Array,
  Object, Boolean, RegExp, Error, Promise, URLSearchParams, Set, Map, encodeURIComponent,
  decodeURIComponent, parseInt, parseFloat, isNaN,
  setTimeout:(fn)=>{ setImmediate(fn); return 0; }, clearTimeout(){},
  setInterval:()=>0, clearInterval(){},
  location:{pathname:'/', search: mode==='handed' ? '?investigation=7' : mode==='handed_rem' ? '?remediation=9' : '', hash:'', href:'http://cfop/'},
  history:{replaceState(){}},
  localStorage:{getItem:()=>null, setItem(){}, removeItem(){}},
  document:doc, navigator:{},
  marked:{parse:s=>s, setOptions(){}},
  renderMarkdown:s=>s, escapeHtmlText:s=>s, toast(){},
  CFOP:{me:()=>Promise.resolve({username:'a', role:'admin'})},
  alert(){}, confirm:()=>true, fetch};
box.window=box; box.globalThis=box; box.self=box;
vm.createContext(box);
vm.runInContext(src, box);

function settle(){ return new Promise(res=>{ (function tick(n){ if(n===0) return res(); setImmediate(()=>tick(n-1)); })(200); }); }
function badge(){ return doc.getElementById('llm-chip-badge').textContent; }
function send(text){ doc.getElementById('message-input').value=text; box.sendMessage(); return settle(); }

(async ()=>{
  await settle();                      // the load: session created (and, when handed, the first question sent)
  out.badges.push(badge());
  if (mode==='plain'){ await send('hi'); }
  if (mode==='override' || mode==='newchat'){
    await send('hi');                  // on the seat
    doc.getElementById('llm-backend').value='xai';
    box.commitLlmModel('grok-4');      // the chip, as a model pick does it
    await settle();
    out.badges.push(badge());
    await send('again');               // on the override
  }
  if (mode==='newchat'){
    box.startNewChat();
    await settle();
    out.badges.push(badge());
    await send('fresh');
  }
  out.chatLlm = vm.runInContext('chatLlm', box);
  console.log(JSON.stringify(out));
})();
"""


def run(tmp_path_factory, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("chatseat") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    seats = json.dumps({"chat": CHAT_SEAT, "handoff": HANDOFF_SEAT})
    proc = subprocess.run([node, str(stub), str(PAGE), mode, seats],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _calls(out, url_prefix, method=None):
    return [c for c in out["calls"]
            if c["url"].startswith(url_prefix) and (method is None or c["method"] == method)]


def _turns(out):
    return [c["body"] for c in _calls(out, "/api/chat", "POST") if c["url"] == "/api/chat"]


def _global_writes(out):
    """The writes the old chip made. None of them may happen on this page now."""
    return [c for c in out["calls"]
            if (c["url"] == "/api/settings/provider" and c["method"] == "POST")
            or (c["url"].startswith("/api/models/") and c["url"].endswith("/select"))]


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    return run(tmp_path_factory, "plain")


@pytest.fixture(scope="module")
def handed(tmp_path_factory):
    return run(tmp_path_factory, "handed")


@pytest.fixture(scope="module")
def handed_rem(tmp_path_factory):
    return run(tmp_path_factory, "handed_rem")


@pytest.fixture(scope="module")
def override(tmp_path_factory):
    return run(tmp_path_factory, "override")


@pytest.fixture(scope="module")
def newchat(tmp_path_factory):
    return run(tmp_path_factory, "newchat")


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def test_a_plain_session_starts_on_the_chat_seat_and_the_turn_carries_it(plain):
    created = _calls(plain, "/api/chat-sessions", "POST")
    assert [c["body"] for c in created if c["url"] == "/api/chat-sessions"] == [{"seat": "chat"}]
    turns = _turns(plain)
    assert len(turns) == 1, turns
    assert (turns[0]["backend"], turns[0].get("model")) == (CHAT_SEAT["backend"], CHAT_SEAT["model"])
    assert "mode" not in turns[0]
    assert plain["badges"] == [""], "a plain session on its seat is not badged"
    assert _global_writes(plain) == []


def test_a_handed_row_starts_on_the_handoff_seat(handed):
    """Ask console from a row: the session is created on the handed-rows
    seat and the first question — asked by the page itself — runs there,
    read-only as before."""
    created = [c["body"] for c in _calls(handed, "/api/chat-sessions", "POST") if c["url"] == "/api/chat-sessions"]
    assert created == [{"seat": "handoff"}]
    turns = _turns(handed)
    assert len(turns) == 1, turns
    assert (turns[0]["backend"], turns[0].get("model")) == (HANDOFF_SEAT["backend"], HANDOFF_SEAT["model"])
    assert turns[0]["mode"] == "verify"
    assert "Investigation #7" in turns[0]["message"]
    assert handed["badges"] == ["handed row"]
    assert _global_writes(handed) == []


def test_a_handed_remediation_starts_on_the_handoff_seat_too(handed_rem):
    """The other hand-off (CFOP-109). Separate because the two branches are
    separate code: the first mutation check broke only the remediation one
    and the investigation flow did not notice."""
    created = [c["body"] for c in _calls(handed_rem, "/api/chat-sessions", "POST") if c["url"] == "/api/chat-sessions"]
    assert created == [{"seat": "handoff"}]
    turns = _turns(handed_rem)
    assert len(turns) == 1, turns
    assert (turns[0]["backend"], turns[0].get("model")) == (HANDOFF_SEAT["backend"], HANDOFF_SEAT["model"])
    assert turns[0]["mode"] == "verify" and "Remediation #9" in turns[0]["message"]
    assert handed_rem["badges"] == ["handed row"]


def test_a_chip_change_is_this_session_only_and_rides_the_next_turn(override):
    puts = _calls(override, "/api/chat-sessions/41/llm", "PUT")
    assert [p["body"] for p in puts] == [{"backend": "xai", "model": "grok-4"}]
    turns = _turns(override)
    assert len(turns) == 2, turns
    assert (turns[0]["backend"], turns[0]["model"]) == (CHAT_SEAT["backend"], CHAT_SEAT["model"])
    assert (turns[1]["backend"], turns[1]["model"]) == ("xai", "grok-4")
    assert override["badges"] == ["", "this session"]
    assert _global_writes(override) == [], "the chip wrote the global selection"


def test_a_new_chat_after_an_override_starts_from_the_default(newchat):
    created = [c["body"] for c in _calls(newchat, "/api/chat-sessions", "POST") if c["url"] == "/api/chat-sessions"]
    assert created == [{"seat": "chat"}, {"seat": "chat"}]
    turns = _turns(newchat)
    assert len(turns) == 3, turns
    assert (turns[1]["backend"], turns[1]["model"]) == ("xai", "grok-4")
    assert (turns[2]["backend"], turns[2]["model"]) == (CHAT_SEAT["backend"], CHAT_SEAT["model"])
    assert turns[2]["session_id"] == 42 and turns[1]["session_id"] == 41
    assert newchat["badges"] == ["", "this session", ""]
    assert newchat["chatLlm"] == CHAT_SEAT
    assert _global_writes(newchat) == []


def test_the_turn_is_built_from_the_session_not_the_chip_dom():
    """The class of regression: sendMessage reading the popover's selects
    (or hard-coding 'auto') again. The row is the truth; the chip is a view."""
    html = PAGE.read_text(encoding="utf-8")
    start = html.index("function sendMessage(")
    end = html.index("function stopChat()", start)
    body = html[start:end]
    assert "chatLlm" in body
    assert "backend: 'auto'" not in body
    assert "getElementById('llm-backend')" not in body
    assert "getElementById('llm-model')" not in body


# --------------------------------------------------------------------------
# the routes
# --------------------------------------------------------------------------

@pytest.fixture
def api(server):
    """The registry fixture's operator is a MagicMock; the seat is resolved
    through the agent's real resolver, so bind it (and a dict config) on."""
    from agent import CFOperator
    op = server.operator
    op.config = {'llm': {}, 'remediation': {}}
    for name in ('_scope_llm', '_scope_llm_config', '_triage_model',
                 '_judge_model', '_judge_model_setting', '_judge_model_config', '_executor_config'):
        setattr(op, name, MethodType(getattr(CFOperator, name), op))
    op.kb.create_chat_session.return_value = 5
    op.kb.set_chat_session_llm.return_value = True
    op.kb.get_chat_session_llm.return_value = None
    return server


def test_the_two_chat_seats_are_scopes_with_provenance(api):
    rows = {r['scope']: r for r in api.app.test_client().get('/api/settings/llm_scopes').get_json()['scopes']}
    for seat in ('chat', 'handoff'):
        assert rows[seat]['editable'] is True and rows[seat]['kind'] == 'backend_model'
        assert rows[seat]['source'] == 'default' and rows[seat]['effective'] == {'backend': 'auto', 'model': ''}


def test_a_session_is_created_on_its_seat_resolved_once(api):
    c = api.app.test_client()
    api.settings.update({'handoff_backend': 'anthropic', 'handoff_model': 'claude-opus-5-5'})
    # Plain: the chat seat, unset, is 'auto' — the global selection, as before.
    r = c.post('/api/chat-sessions', json={})
    assert r.status_code == 200
    chat_llm = {'seat': 'chat', 'backend': 'auto', 'model': '', 'source': 'default'}
    assert r.get_json() == {'id': 5, 'llm': chat_llm}
    assert api.operator.kb.create_chat_session.call_args.kwargs == {'title': None, 'llm': chat_llm}
    # Handed: the handoff seat, as Admin → LLM set it, with where it came from.
    r = c.post('/api/chat-sessions', json={'seat': 'handoff'})
    handed_llm = {'seat': 'handoff', 'backend': 'anthropic', 'model': 'claude-opus-5-5', 'source': 'console'}
    assert r.status_code == 200 and r.get_json()['llm'] == handed_llm
    assert api.operator.kb.create_chat_session.call_args.kwargs['llm'] == handed_llm
    assert c.post('/api/chat-sessions', json={'seat': 'judge'}).status_code == 400


def test_the_chip_writes_the_session_row_and_nothing_global(api):
    c = api.app.test_client()
    api.operator.kb.get_chat_session_llm.return_value = {
        'seat': 'handoff', 'backend': 'anthropic', 'model': 'claude-opus-5-5', 'source': 'console'}
    r = c.put('/api/chat-sessions/5/llm', json={'backend': 'xai', 'model': 'grok-4'})
    assert r.status_code == 200
    want = {'seat': 'handoff', 'backend': 'xai', 'model': 'grok-4', 'source': 'session'}
    assert r.get_json() == {'success': True, 'llm': want}
    assert api.operator.kb.set_chat_session_llm.call_args.args == (5, want)
    assert api.settings == {}, 'the chip wrote a global setting'
    # Backend alone: that backend's selected model, left to the server at chat time.
    assert c.put('/api/chat-sessions/5/llm', json={'backend': 'groq'}).get_json()['llm'] == \
        {'seat': 'handoff', 'backend': 'groq', 'model': '', 'source': 'session'}
    # 'auto' with no model: back on the seat the session was created on, resolved now.
    api.settings.update({'handoff_backend': 'deepseek'})
    r = c.put('/api/chat-sessions/5/llm', json={'backend': 'auto'})
    assert r.get_json()['llm'] == {'seat': 'handoff', 'backend': 'deepseek', 'model': '', 'source': 'console'}
    assert c.put('/api/chat-sessions/5/llm', json={'backend': 'nope'}).status_code == 400
    api.operator.kb.set_chat_session_llm.return_value = False
    assert c.put('/api/chat-sessions/5/llm', json={'backend': 'xai'}).status_code == 404
    api.operator.kb.set_chat_session_llm.side_effect = RuntimeError('could not connect to db-host:5432')
    r = c.put('/api/chat-sessions/5/llm', json={'backend': 'xai'})
    assert r.status_code == 503 and 'db-host' not in r.get_data(as_text=True)


def test_the_chip_write_is_admin_gated():
    src = (REPO_ROOT / 'web_server.py').read_text(encoding='utf-8')
    idx = src.index('def set_chat_session_llm')
    preceding = src[:idx].rsplit('@self.app.route', 1)[1]
    assert 'require_role(ROLE_ADMIN)' in preceding, 'set_chat_session_llm lost its ROLE_ADMIN gate'


def _chat_call(api, payload):
    """POST /api/chat and return the (args, kwargs) the operator was called with."""
    op = api.operator
    op.handle_chat_message_stream.reset_mock()

    def stream(*_a, **_k):
        yield {'event': 'done', 'data': {'response': 'ok'}}
    op.handle_chat_message_stream.side_effect = stream
    r = api.app.test_client().post('/api/chat', json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    deadline = time.time() + 2
    while time.time() < deadline and not op.handle_chat_message_stream.called:
        time.sleep(0.01)
    assert op.handle_chat_message_stream.called, 'the chat never started'
    call = op.handle_chat_message_stream.call_args
    return call.args[2], call.kwargs.get('model')


def test_the_chat_route_runs_on_the_row_seat_unless_the_payload_names_one(api):
    api.operator.kb.get_chat_session_llm.return_value = {
        'seat': 'handoff', 'backend': 'anthropic', 'model': 'claude-opus-5-5', 'source': 'console'}
    # The page's default payload: the row decides.
    assert _chat_call(api, {'message': 'hi', 'session_id': 5}) == ('anthropic', 'claude-opus-5-5')
    assert _chat_call(api, {'message': 'hi', 'session_id': 5, 'backend': 'auto'}) == ('anthropic', 'claude-opus-5-5')
    # A payload that names a backend wins (the MCP server chooses per call).
    assert _chat_call(api, {'message': 'hi', 'session_id': 5, 'backend': 'groq'}) == ('groq', None)
    # No session: 'auto', as before.
    assert _chat_call(api, {'message': 'hi'}) == ('auto', None)
    # A row from before the column, or a row read that fails: still a turn, on 'auto'.
    api.operator.kb.get_chat_session_llm.return_value = None
    assert _chat_call(api, {'message': 'hi', 'session_id': 5}) == ('auto', None)
    api.operator.kb.get_chat_session_llm.side_effect = RuntimeError('offline')
    assert _chat_call(api, {'message': 'hi', 'session_id': 5}) == ('auto', None)
