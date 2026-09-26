"""The Events page's own behaviour, under node (CFOP-215).

The list-page guards (a11y, common, nav) already hold events.html to the
console's shared contract. This drives what is particular to it: filters are
a URL you can paste and become the API query; "load older" follows the
server's cursor instead of re-reading; the drawer links an alert to the
investigation it caused; an alert id from an alert source cannot become
script; and each way /api/events fails reads as what to do about it.

Fails closed without node, like test_console_js: a skip would be a green run
that tested nothing.
"""
import json
import re
import shutil
import subprocess

import pytest

from repo_paths import REPO_ROOT

PAGE = REPO_ROOT / "ui" / "events.html"

_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const scenario=process.argv[3];
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(process.argv[2]),'common.js'),'utf8');

const HOSTILE='x"><img src=x onerror=alert(1)>';
function row(i, extra){ return Object.assign({alert_id:'alert-'+i, source:i%2?'cfoperator-sweep':'alertmanager',
  severity:'warning', summary:'thing '+i, status:i===2?'failed':'completed', action:'investigate',
  namespace:'apps', resource_name:'pod-'+i,
  latest_event_at:new Date(Date.UTC(2026,8,26,13,0,0)-i*60000).toISOString().replace('.000Z','+00:00'),
  event_count:3, result:{details:{investigation_id:i===2?7:null}}}, extra||{}); }
const N=scenario==='paging'?60:5;
const ALL=Array.from({length:N},(_,k)=>row(k+1));
if(scenario==='hostile') ALL.unshift(row(0,{alert_id:HOSTILE, summary:HOSTILE}));

const fetched=[];
let listAnswer=null;   // overrides the list response for the error scenarios
function listResponse(url){
  if(listAnswer) return listAnswer;
  const q={}; url.split('?')[1].split('&').forEach(p=>{ const [k,v]=p.split('='); q[k]=decodeURIComponent(v||''); });
  let rows=ALL.filter(r=>(!q.status||r.status===q.status)&&(!q.source||r.source===q.source));
  const start=q.cursor?Number(q.cursor):0, limit=Number(q.limit||50);
  const page=rows.slice(start,start+limit);
  return {ok:true,status:200,json:()=>Promise.resolve({alerts:page,
    next_cursor:start+limit<rows.length?String(start+limit):null,store:'postgres',lagging:false})};
}
const events=[
  {event_id:'e1',event_type:'alert_received',created_at:'2026-09-26T13:07:00+00:00',payload:{alert:{alert_id:'alert-2',details:{category:'disk'}}}},
  {event_id:'e2',event_type:'decision_made',created_at:'2026-09-26T13:07:01+00:00',payload:{}},
  {event_id:'e3',event_type:'action_completed',created_at:'2026-09-26T13:07:02+00:00',payload:{}}];

const doc={documentElement:{},body:{appendChild(){}},addEventListener(){},activeElement:null,hidden:false};
function el(id){ return {id:id||'',className:'',innerHTML:'',textContent:'',value:'',hidden:false,inert:true,
  style:{},classList:{add(){},remove(){}},setAttribute(){},getAttribute(){return null;},remove(){},
  appendChild(){},addEventListener(){},focus(){ doc.activeElement=this; },isConnected:true}; }
const els={};
doc.createElement=()=>el('');
doc.getElementById=id=>(els[id]=els[id]||el(id));
const initialSearch=scenario==='from-url'?'?source=cfoperator-sweep&window=24h&q=disk':'';
const loc={pathname:'/events',search:initialSearch,hash:'',href:'http://cfop/events'+initialSearch};
const urls=[];
const hist={replaceState(s,t,url){ urls.push(url);
  const hashAt=url.indexOf('#'); loc.hash=hashAt>=0?url.slice(hashAt):'';
  if(!url.startsWith('#')){ const q=url.indexOf('?'); loc.search=q>=0?url.slice(q,hashAt>=0?hashAt:undefined):''; } }};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,
  setTimeout,clearTimeout,setInterval:()=>0,clearInterval:()=>{},
  location:loc,history:hist,document:doc,navigator:{},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  fetch:(url)=>{ fetched.push(url);
    if(url.indexOf('/api/events/')===0){
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({
        alert:Object.assign(row(2),{first_event_at:'2026-09-26T13:07:00+00:00',
          decision:{action:'investigate',confidence:0.9,reasoning:'disk filling on node-2'},
          message:'Resolved: freed space',
          timeline:events.map(e=>({created_at:e.created_at,event_type:e.event_type,note:null}))}),
        events:events})});
    }
    return Promise.resolve(listResponse(url));
  }};
box.window={location:loc,history:hist,addEventListener(){},removeEventListener(){}};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={firstFetch:fetched[0], rows:els['rows'].innerHTML};
  if(scenario==='from-url'){
    out.sourceSelect=els['f-source'].value; out.windowSelect=els['f-window'].value; out.searchBox=els['f-q'].value;
  }
  if(scenario==='flow'){
    box.setFilter('status','failed'); await tick(); await tick();
    out.afterFilterFetch=fetched[fetched.length-1]; out.afterFilterUrl=urls[urls.length-1];
    out.afterFilterRows=(els['rows'].innerHTML.match(/<tr /g)||[]).length;
    box.setFilter('status',''); await tick(); await tick();
    out.moreHiddenWithoutCursor=els['more'].hidden;
    await box.detail('alert-2');
    const drawer=els['detail'].innerHTML;
    out.hash=loc.hash;
    out.linksInvestigation=drawer.indexOf('href="/investigations#7"')>=0;
    out.showsReasoning=drawer.indexOf('disk filling on node-2')>=0;
    out.timelineItems=(drawer.match(/<li>/g)||[]).length;
    out.rawFolded=/<details><summary>Raw events \(3\)/.test(drawer);
    out.alertDetails=drawer.indexOf('&quot;category&quot;: &quot;disk&quot;')>=0;
    box.closeDetail();
    out.hashAfterClose=loc.hash;
  }
  if(scenario==='paging'){
    out.firstPageRows=(els['rows'].innerHTML.match(/<tr /g)||[]).length;
    out.moreShown=!els['more'].hidden;
    await box.loadMore(); await tick();
    out.moreFetch=fetched[fetched.length-1];
    out.rowsAfterMore=(els['rows'].innerHTML.match(/<tr /g)||[]).length;
    out.distinctRows=new Set((els['rows'].innerHTML.match(/data-id="([^"]+)"/g)||[])).size;
    out.moreHiddenAtEnd=els['more'].hidden;
  }
  if(scenario==='unauthorized'){
    listAnswer={ok:false,status:502,json:()=>Promise.resolve({reason:'unauthorized',error:'event runtime refused the agent (401)'})};
    await box.load();
    out.banner=els['banner'].textContent; out.bannerHidden=els['banner'].hidden; out.bannerClass=els['banner'].className;
  }
  if(scenario==='outbox'){
    listAnswer={ok:true,status:200,json:()=>Promise.resolve({alerts:[row(1)],next_cursor:null,store:'outbox',lagging:false})};
    await box.load();
    out.banner=els['banner'].textContent; out.bannerClass=els['banner'].className;
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run(scenario, tmp_path):
    node = shutil.which("node")
    assert node, "node is required for the console behaviour suites (ubuntu-latest ships it)"
    stub = tmp_path / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(PAGE), scenario],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_filters_in_the_url_become_the_query(tmp_path):
    b = _run("from-url", tmp_path)
    assert "source=cfoperator-sweep" in b["firstFetch"]
    assert "q=disk" in b["firstFetch"]
    # The window is page state; the API gets a since bound instead.
    assert "since=" in b["firstFetch"] and "window=" not in b["firstFetch"]
    assert (b["sourceSelect"], b["windowSelect"], b["searchBox"]) == ("cfoperator-sweep", "24h", "disk")


def test_a_filter_change_queries_and_names_itself_in_the_url(tmp_path):
    b = _run("flow", tmp_path)
    assert "status=failed" in b["afterFilterFetch"]
    assert b["afterFilterUrl"] == "/events?status=failed"
    assert b["afterFilterRows"] == 1


def test_the_drawer_opens_by_hash_and_links_the_investigation(tmp_path):
    b = _run("flow", tmp_path)
    assert b["hash"] == "#alert-2"
    assert b["linksInvestigation"], "an alert whose completion names investigation 7 must link to it"
    assert b["showsReasoning"], "the triage decision's reasoning is not in the drawer"
    assert b["timelineItems"] == 3
    assert b["rawFolded"] and b["alertDetails"]
    assert b["hashAfterClose"] == ""


def test_a_hostile_alert_id_stays_text(tmp_path):
    """Alert ids come from alert sources. The row hands its id to the handler
    through an escaped data-id, never through inline JS."""
    rows = _run("hostile", tmp_path)["rows"]
    assert "<img src=x" not in rows
    tr = re.search(r'<tr id="row-x&quot;[^>]*>', rows)
    assert tr, "the hostile row's tag is not escaped as one tag"
    tag = tr.group(0)
    assert 'data-id="x&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"' in tag
    # The handlers are fixed text: the id never enters inline JS.
    assert 'onclick="openRow(this)"' in tag and 'onkeydown="rowKey(event,this)"' in tag


def test_a_refused_token_says_what_to_fix(tmp_path):
    b = _run("unauthorized", tmp_path)
    assert not b["bannerHidden"] and b["bannerClass"] == "err"
    assert "CFOP_RUNTIME_TOKEN" in b["banner"]


def test_the_outbox_fallback_is_said_not_hidden(tmp_path):
    b = _run("outbox", tmp_path)
    assert "outbox" in b["banner"] and b["bannerClass"] == ""


def test_load_older_follows_the_servers_cursor(tmp_path):
    b = _run("paging", tmp_path)
    assert b["firstPageRows"] == 50 and b["moreShown"]
    assert "cursor=50" in b["moreFetch"], "load older must continue from the server's cursor"
    assert b["rowsAfterMore"] == b["distinctRows"] == 60
    assert b["moreHiddenAtEnd"]


def test_no_cursor_no_load_older_button(tmp_path):
    assert _run("flow", tmp_path)["moreHiddenWithoutCursor"]
