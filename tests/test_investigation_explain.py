"""The console's Explain view: the route and the drawer (CFOP-324).

The recorder's own tests live in agent/test_investigation_trace.py. What is
guarded here is the read side: the route shapes the stored rows for the
timeline, says *why* there is nothing to show, never hands out a cockpit
session as a trace step and sits behind the console's login like every other
read; the prune deletes only trace rows; and the drawer escapes what it
renders, because a tool result is text from the infrastructure, not from us.
"""

from repo_paths import REPO_ROOT
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from flask import Flask

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent"))

UI = REPO_ROOT / "ui"
RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=90)).replace(tzinfo=None).isoformat()


class FakeKB:
    def __init__(self, started_at=RECENT, events=None):
        self.inv = {'id': 2871, 'trigger': 'KubeJobFailed', 'outcome': 'needs_action',
                    'started_at': started_at, 'findings': {}}
        self.events = events if events is not None else [
            {'event_type': 'reasoning', 'action_type': 'prompt', 'reasoning_text': '--- system ---\nYou are…',
             'event_at': '2026-10-15T09:41:07'},
            {'event_type': 'tool_call', 'tool_name': 'k8s_logs', 'tool_input': {'pod': 'x7k2p'},
             'tool_output': {'status': 'success', 'length': 2311, 'cached': False},
             'reasoning_text': 'npm ERR! code ECONNRESET', 'duration_ms': 1200, 'success': True,
             'event_at': '2026-10-15T09:41:20'},
            {'event_type': 'cockpit_session', 'reasoning_text': 'a human verdict',
             'event_at': '2026-10-15T10:00:00'},
            {'event_type': 'reasoning', 'action_type': 'answer', 'reasoning_text': 'Raise BOOT_TIMEOUT_MS.',
             'action_target': 'groq/llama', 'event_at': '2026-10-15T09:42:41'},
        ]

    def get_investigation(self, investigation_id):
        return self.inv if investigation_id == self.inv['id'] else None

    def get_investigation_events(self, investigation_id):
        return self.events


def _client(kb, auth_disabled=True, retention=30):
    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb = kb
    operator.config = {}
    operator._trace_retention_days = lambda: retention
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._setup_routes()
    keys = ("CFOP_AUTH_DISABLED", "CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")
    prior = {k: os.environ.pop(k, None) for k in keys}
    if auth_disabled:
        os.environ["CFOP_AUTH_DISABLED"] = "1"
    try:
        install_auth(server.app, store=None)
    finally:
        os.environ.pop("CFOP_AUTH_DISABLED", None)
        for k, v in prior.items():
            if v is not None:
                os.environ[k] = v
    return server.app.test_client()


def test_the_trace_comes_back_as_timeline_steps_without_cockpit_sessions():
    body = _client(FakeKB()).get('/api/investigations/2871/explain').get_json()
    assert body['recorded'] is True
    assert [s['kind'] for s in body['steps']] == ['prompt', 'tool', 'answer']
    tool = body['steps'][1]
    assert tool['tool'] == 'k8s_logs' and tool['status'] == 'success'
    assert tool['result'] == 'npm ERR! code ECONNRESET' and tool['length'] == 2311
    assert 'a human verdict' not in json.dumps(body), \
        "a cockpit session is a verdict with its own block in the drawer, not a trace step"


def test_an_unknown_investigation_is_a_404():
    assert _client(FakeKB()).get('/api/investigations/9/explain').status_code == 404


def test_no_trace_says_not_recorded_for_an_investigation_inside_the_window():
    body = _client(FakeKB(events=[])).get('/api/investigations/2871/explain').get_json()
    assert body['recorded'] is False and body['reason'] == 'not_recorded'


def test_no_trace_says_pruned_for_an_investigation_past_the_window():
    body = _client(FakeKB(started_at=OLD, events=[])).get('/api/investigations/2871/explain').get_json()
    assert body['reason'] == 'pruned' and body['retention_days'] == 30


def test_retention_off_never_claims_pruned():
    body = _client(FakeKB(started_at=OLD, events=[]), retention=0) \
        .get('/api/investigations/2871/explain').get_json()
    assert body['reason'] == 'not_recorded'


def test_the_route_sits_behind_the_console_login():
    """Any console user may read it, as with the drill-in; nobody else."""
    res = _client(FakeKB(), auth_disabled=False).get('/api/investigations/2871/explain')
    assert res.status_code in (401, 503), res.status_code


# -- retention ----------------------------------------------------------------

def test_the_prune_deletes_trace_rows_and_never_cockpit_sessions():
    """Mutation check: add cockpit_session to TRACE_EVENT_TYPES and this goes red."""
    from knowledge_base import KnowledgeBase, COCKPIT_SESSION_EVENT
    assert set(KnowledgeBase.TRACE_EVENT_TYPES) == {'tool_call', 'reasoning'}
    assert COCKPIT_SESSION_EVENT not in KnowledgeBase.TRACE_EVENT_TYPES
    src = inspect.getsource(KnowledgeBase.prune_investigation_traces)
    assert 'event_type.in_(self.TRACE_EVENT_TYPES)' in src


def test_the_agent_prunes_with_its_configured_window_and_not_when_off():
    from agent import CFOperator
    op = CFOperator.__new__(CFOperator)
    op.kb = MagicMock()
    op.kb.prune_investigation_traces.return_value = 3
    op.config = {'investigations': {'trace_retention_days': 14}}
    assert op._prune_investigation_traces() == 3
    op.kb.prune_investigation_traces.assert_called_once_with(14)
    op.kb.reset_mock()
    op.config = {'investigations': {'trace_retention_days': 0}}
    assert op._prune_investigation_traces() == 0
    op.kb.prune_investigation_traces.assert_not_called()


# -- the drawer ---------------------------------------------------------------

_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
function el(){return{className:'',innerHTML:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  focus(){},scrollIntoView(){},appendChild(){},addEventListener(){}};}
const els={};
const loc={pathname:'/investigations',search:'',hash:'',href:'http://cfop/investigations'};
const hist={replaceState(s,t,url){ loc.hash=(typeof url==='string'&&url.startsWith('#'))?url:''; }};
const TRACE={recorded:true, steps:[
  {kind:'prompt', text:'--- system ---\nYou are CFOperator'},
  {kind:'fallback', text:'ollama/gemma4:26b → groq/llama — timeout', model:'groq/llama'},
  {kind:'assistant', text:'Reading the logs.', model:'groq/llama'},
  {kind:'tool', tool:'k8s_logs', args:{pod:'x7k2p'}, result:'<script>alert(1)</script> POSTGRES_PASSWORD=***',
   length:44, status:'success', duration_ms:1200},
  {kind:'tool', tool:'ssh_execute', args:{command:'rocm-smi --setfan 80'}, result:'{"refused":"not run"}',
   length:21, status:'refused', duration_ms:0},
  {kind:'tool', tool:'k8s_describe', args:{kind:'cronjob'}, result:'Forbidden', length:9, status:'error', duration_ms:640},
  {kind:'answer', text:'Raise it.', model:'groq/llama'}]};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,setTimeout,clearTimeout,
  setInterval:()=>0, clearInterval:()=>{}, location:loc, history:hist,
  window:{location:loc,history:hist,addEventListener(){},removeEventListener(){}},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  document:{documentElement:{},body:{appendChild(){}},addEventListener(){},createElement:()=>el(),
    querySelectorAll:()=>[], getElementById:id=>(els[id]=els[id]||el())},
  DOMParser:function(){ this.parseFromString=h=>({body:{firstChild:{querySelectorAll:()=>[],innerHTML:h}}}); },
  navigator:{},
  fetch:(url)=>{
    const ok=(b)=>Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(b)});
    let m;
    if((m=url.match(/^\/api\/investigations\/(\d+)\/explain$/)))
      return +m[1]===2871 ? ok(TRACE) : ok({recorded:false, reason:+m[1]===5?'pruned':'not_recorded', retention_days:30, steps:[]});
    if((m=url.match(/^\/api\/investigations\/(\d+)$/)))
      return ok({id:+m[1], trigger:'KubeJobFailed', outcome:'needs_action', attach_command:'cfassist attach '+m[1], findings:{}});
    if(url.indexOf('/api/investigations')===0) return ok({investigations:[]});
    return ok({remediations:[]});
  }};
const uiDir=path.dirname(process.argv[2]);
box.globalThis=box; vm.createContext(box);
vm.runInContext(fs.readFileSync(path.join(uiDir,'common.js'),'utf8'),box);
vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
  await tick(); await tick();
  const out={};
  await box.detail(2871);
  out.drawerOffersExplain=box.document.getElementById('detail').innerHTML.indexOf('id="explain-btn"')>=0;
  out.notLoadedWithDrawer=box.document.getElementById('explain').innerHTML==='';
  await box.toggleExplain(2871);
  const x=box.document.getElementById('explain').innerHTML;
  out.hash=loc.hash;
  out.escaped=x.indexOf('&lt;script&gt;alert(1)&lt;/script&gt;')>=0 && x.indexOf('<script>alert')<0;
  out.toolRows=(x.match(/<details class="tool">/g)||[]).length;
  out.anyOpen=/<details[^>]*\sopen/.test(x);
  out.statuses=['success','refused','error'].filter(s=>x.indexOf('class="st '+s+'"')>=0);
  out.summary=(x.match(/<div class="xsum">([\s\S]*?)<\/div>/)||['',''])[1];
  out.hasAnswer=x.indexOf('class="answer"')>=0 && x.indexOf('provider switch')>=0;
  await box.toggleExplain(2871);
  out.hashAfterClose=loc.hash; out.closed=box.document.getElementById('explain').innerHTML==='';
  // A pasted /explain link opens the drawer and the trace together.
  box.closeDetail(); loc.hash='#7/explain'; box.openFromHash(); await tick(); await tick(); await tick();
  out.notRecorded=box.document.getElementById('explain').innerHTML.indexOf('Not recorded')>=0;
  box.closeDetail(); loc.hash='#5/explain'; box.openFromHash(); await tick(); await tick(); await tick();
  out.pruned=box.document.getElementById('explain').innerHTML.indexOf('Steps pruned')>=0;
  console.log(JSON.stringify(out));
})().catch(e=>{ console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def drawer(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("explain") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "investigations.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_drawer_offers_explain_and_loads_it_only_on_click(drawer):
    assert drawer["drawerOffersExplain"] and drawer["notLoadedWithDrawer"]


def test_explain_names_itself_in_the_url_and_closing_takes_it_back(drawer):
    assert drawer["hash"] == "#2871/explain"
    assert drawer["hashAfterClose"] == "#2871" and drawer["closed"]


def test_a_tool_result_is_escaped_not_rendered(drawer):
    """Tool output is text from the infrastructure. Mutation check: render
    s.result without esc() and this goes red."""
    assert drawer["escaped"]


def test_tool_rows_start_folded_with_their_status(drawer):
    assert drawer["toolRows"] == 3 and not drawer["anyOpen"]
    assert drawer["statuses"] == ["success", "refused", "error"]
    assert "3 tool calls" in drawer["summary"] and "1 refused" in drawer["summary"]
    assert "1 error" in drawer["summary"] and "1 value redacted" in drawer["summary"]
    assert drawer["hasAnswer"]


def test_an_empty_trace_says_why(drawer):
    assert drawer["notRecorded"] and drawer["pruned"]
