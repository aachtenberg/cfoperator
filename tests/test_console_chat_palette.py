"""The web chat in cfassist's layout (CFOP-719).

A flat transcript, the input in a rounded box, the menu under it, one footer
line with what is true right now: the hints, the model, how full the context
is and what the last turn cost. The page's own script is run under node with
a recording DOM and driven the way a turn drives it, so what is asserted is
what the operator would see change, not that a class name appears in a file.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
PAGE = UI / "index.html"

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mode=process.argv[3]||'';

function el(){ const e={className:'',innerHTML:'',textContent:'',value:'',rows:1,title:'',
  disabled:false,hidden:false,scrollTop:0,scrollHeight:0,children:[],style:{},dataset:{},attrs:{},on:{},
  parentElement:null,
  classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
             contains(c){return this._s.has(c);},
             toggle(c,f){ if(f===undefined) f=!this._s.has(c); if(f) this._s.add(c); else this._s.delete(c); return f; }},
  setAttribute(k,v){this.attrs[k]=String(v);},removeAttribute(k){delete this.attrs[k];},
  getAttribute(k){return k in this.attrs?this.attrs[k]:null;},
  select(){},remove(){},focus(){},blur(){},scrollIntoView(){},
  appendChild(n){ n.parentElement=this; this.children.push(n); return n; },
  insertBefore(n){ n.parentElement=this; this.children.push(n); return n; },
  querySelector(sel){ const c=sel.replace(/^\./,''); return this.kids[c]||(this.kids[c]=el()); },
  querySelectorAll(){return [];},
  addEventListener(t,f){ this.on[t]=f; },removeEventListener(){},closest(){return null;}};
  // Setting innerHTML replaces the children, as it does in a browser: a
  // reloaded transcript must not be read together with the one before it.
  let markup='';
  Object.defineProperty(e,'innerHTML',{get(){ return markup; }, set(v){ markup=String(v); e.children.length=0; }});
  e.kids={}; return e; }

const els={}, docOn={};
const doc={readyState:'complete',documentElement:el(),body:el(),activeElement:null,
  addEventListener(t,f){ (docOn[t]=docOn[t]||[]).push(f); },removeEventListener(){},
  createElement:()=>el(),
  querySelector(){return el();},querySelectorAll(){return [];},
  getElementById:id=>(els[id]=els[id]||el())};
if(mode==='embed') doc.documentElement.classList.add('embed');

const stops=[];
const session={id:7, llm:null, messages:[
  {role:'user', content:'first'},
  {role:'assistant', content:'one', backend:'ollama', model:'gemma4:26b', tool_calls:3, duration_s:9.6,
   usage:{input_tokens:1840, output_tokens:412, last_prompt_tokens:11000, context_window:32768}},
  {role:'user', content:'second'},
  {role:'assistant', content:'two', backend:'ollama', model:'gemma4:26b', tool_calls:1, duration_s:3.1,
   usage:{input_tokens:2310, output_tokens:96, last_prompt_tokens:12300, context_window:32768}},
  {role:'assistant', content:'old reply, stored before any of this'},
  {role:'error', content:'the database refused'}]};

const box={console:{log:console.log, warn(){}, error(){}}, JSON, Math, Date, Number, String, Array,
  Object, Boolean, RegExp, Error, Promise, URLSearchParams, Set, Map, encodeURIComponent,
  decodeURIComponent, parseInt, parseFloat, isNaN, isFinite,
  setTimeout:(fn)=>{ setImmediate(fn); return 0; }, clearTimeout(){},
  setInterval:()=>0, clearInterval(){},
  location:{pathname:'/console', search:'', hash:'', href:'http://cfop/console'},
  history:{replaceState(){}},
  localStorage:{getItem:()=>null, setItem(){}, removeItem(){}},
  document:doc, navigator:{},
  marked:{parse:s=>s, setOptions(){}},
  renderMarkdown:s=>s, sweepDoc:()=>'', toast(){},
  CFOP:{me:()=>Promise.resolve({username:'a', role:'admin'})},
  alert(){}, confirm:()=>true,
  fetch:(url, opts)=>{
    url=String(url);
    if (/\/api\/chat\/[^/]+\/stop$/.test(url)) stops.push(url);
    if (url.indexOf('/api/chat-sessions/7')===0) return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(session)});
    if (url==='/api/skills') return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({skills:[
      {command:'/investigate-pod',args:'<pod>',description:'Why a pod is failing'},
      {command:'/why-restart',args:'<target>',description:'Root cause of a restart'}]})});
    return Promise.resolve({ok:true, status:200, json:()=>Promise.resolve({})});
  }};
box.window=box; box.globalThis=box; box.self=box;
vm.createContext(box);
vm.runInContext(src, box);

const g=id=>doc.getElementById(id);
const text=id=>g(id).textContent;
const stats=n=>(n.children.find(c=>c.className==='turn-stats')||{}).textContent||null;
const out={};
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
  for(let i=0;i<8;i++) await tick();
  out.restHints=text('footer-hints');
  out.restGaugeHidden=g('ctx-gauge').hidden; out.restTurnHidden=g('turn-stats').hidden;
  out.welcome=g('chat-container').innerHTML;

  // A turn starts: the button is Stop, the box shows it, the hints change.
  box.setChatInFlight('c0ffee');
  out.busyStop=g('send-button').classList.contains('stop');
  out.busyLabel=g('send-button').attrs['aria-label'];
  out.busyBox=g('palette-box').classList.contains('busy');
  out.busyHints=text('footer-hints');

  // Escape in the composer stops it.
  g('message-input').on.keydown({key:'Escape', preventDefault(){}});
  out.escStopped=stops.length;
  out.afterStopBox=g('palette-box').classList.contains('busy');
  out.afterStopLabel=g('send-button').attrs['aria-label'];
  // ...and anywhere else on the page, unless the model picker is open.
  box.setChatInFlight('beef');
  (docOn.keydown||[]).forEach(f=>f({key:'Escape', target:{tagName:'DIV'}, preventDefault(){}}));
  out.escElsewhere=stops.length;
  // Escape with nothing running stops nothing.
  g('message-input').on.keydown({key:'Escape', preventDefault(){}});
  out.escIdle=stops.length;

  // Tool rows: the call, then its result under it.
  const before=g('chat-container').children.length;
  box.setChatInFlight('cafe');
  box.handleChatEvent({event:'tool_call', data:{tool:'loki_query', args:{q:'{host="node-a"}'}, iteration:1, max:3}});
  const log=g('chat-container').children[g('chat-container').children.length-1];
  const body=log.kids['tool-log-body'];
  const card=body.children[0];
  out.callRow=card.innerHTML;
  out.callPending=!card.classList.contains('done');
  box.handleChatEvent({event:'tool_result', data:{tool:'loki_query', result:'0 lines'}});
  out.resultRow=(card.children.find(c=>/tool-card-result/.test(c.className))||{}).innerHTML;
  out.callDone=card.classList.contains('done');
  box.handleChatEvent({event:'tool_call', data:{tool:'get_investigation', args:{id:5458}}});
  box.handleChatEvent({event:'tool_result', data:{tool:'get_investigation', result:{error:'timed out'}}});
  const card2=body.children[1];
  out.errorRowClass=(card2.children.find(c=>/tool-card-result/.test(c.className))||{}).className;
  out.errorRow=(card2.children.find(c=>/tool-card-result/.test(c.className))||{}).innerHTML;

  // The answer arrives with what it cost.
  box.handleChatEvent({event:'done', data:{response:'All good.', backend:'ollama', model:'gemma4:26b',
    tool_calls:2, duration_s:9.63,
    usage:{input_tokens:1840, output_tokens:412, last_prompt_tokens:12300, context_window:32768}}});
  const kids=g('chat-container').children;
  const answer=kids[kids.length-1];
  out.answerClass=answer.className; out.answerStats=stats(answer);
  out.footerTurn=text('turn-stats'); out.footerTurnHidden=g('turn-stats').hidden;
  out.gaugeHidden=g('ctx-gauge').hidden; out.gaugeText=text('ctx-text'); out.gaugeFill=g('ctx-fill').style.width;
  out.gaugeHigh=g('ctx-gauge').classList.contains('high');
  out.doneBox=g('palette-box').classList.contains('busy');
  out.doneHints=text('footer-hints');
  out.session1=text('session-stats');

  // A hosted model reports no window: no gauge, and the turn's cost still shows.
  box.setChatInFlight('f00d');
  box.handleChatEvent({event:'done', data:{response:'From a hosted model.', backend:'openrouter', model:'some/model',
    tool_calls:0, duration_s:2, usage:{input_tokens:900, output_tokens:30, last_prompt_tokens:900}}});
  out.hostedGaugeHidden=g('ctx-gauge').hidden; out.hostedTurn=text('turn-stats');
  const hosted=g('chat-container').children[g('chat-container').children.length-1];
  out.hostedStats=stats(hosted);

  // Nearly full: the gauge says so.
  box.setChatInFlight('feed');
  box.handleChatEvent({event:'done', data:{response:'x', backend:'ollama', model:'m', duration_s:1,
    usage:{input_tokens:5, output_tokens:5, last_prompt_tokens:30000, context_window:32768}}});
  out.fullGaugeHigh=g('ctx-gauge').classList.contains('high'); out.fullFill=g('ctx-fill').style.width;

  // A turn that reported nothing draws nothing: absent is not zero.
  box.setChatInFlight('dead');
  box.handleChatEvent({event:'done', data:{response:'No numbers.', backend:'N/A', model:'N/A'}});
  const bare=g('chat-container').children[g('chat-container').children.length-1];
  out.bareStats=stats(bare); out.bareTurnHidden=g('turn-stats').hidden; out.bareGaugeHidden=g('ctx-gauge').hidden;

  // The console's own lines are not answers.
  box.setChatInFlight('aaaa');
  box.handleChatEvent({event:'error', data:{error:'boom'}});
  const err=g('chat-container').children[g('chat-container').children.length-1];
  out.errorClass=err.className; out.errorStats=stats(err);

  // A hand-off names its row in the box's top edge.
  box.setBoxTitle('#5458 · No journal   logs\nfrom node-a');
  out.title=text('box-title'); out.titleHidden=g('box-title').hidden;

  // The menu opens: the hints say how to use it.
  g('message-input').value='/inv';
  g('message-input').on.input({target:g('message-input')});
  out.menuActive=g('slash-menu').classList.contains('active');
  out.menuHints=text('footer-hints');

  // A reload rebuilds the totals and the footer from what was stored.
  box.loadChatSession(7);
  for(let i=0;i<8;i++) await tick();
  out.reloadSession=text('session-stats'); out.reloadTurn=text('turn-stats');
  out.reloadTitleHidden=g('box-title').hidden;
  const rk=g('chat-container').children;
  out.reloadStats=rk.filter(c=>/message agent/.test(c.className)).map(stats);

  // New chat starts over.
  box.startNewChat();
  out.newSession=text('session-stats'); out.newTurnHidden=g('turn-stats').hidden;
  out.newWelcome=g('chat-container').innerHTML;
  console.log(JSON.stringify(out));
})();
"""


def _run(tmp_path_factory, mode=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("palette" + mode) / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    proc = subprocess.run([node, str(stub), str(PAGE), mode], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    return _run(tmp_path_factory)


@pytest.fixture(scope="module")
def panel(tmp_path_factory):
    return _run(tmp_path_factory, "embed")


def html():
    return PAGE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the footer says what is true right now
# --------------------------------------------------------------------------

def test_at_rest_the_footer_says_how_to_start(page):
    assert page["restHints"] == "/ skills · enter send · shift+enter new line"
    assert page["restGaugeHidden"] and page["restTurnHidden"], "stats before any turn"


def test_while_a_turn_runs_the_box_and_the_button_show_it(page):
    """The ants are the box's 'busy' class; the button is two icons, so which
    one shows is its 'stop' class and what it is called is its label."""
    assert page["busyBox"] and page["busyStop"] and page["busyLabel"] == "Stop"
    assert page["busyHints"] == "esc stop · typing is kept"
    assert page["doneBox"] is False, "the box still shows a turn running after it finished"
    assert page["doneHints"].startswith("/ skills")


def test_escape_stops_the_turn_from_the_composer_and_from_the_page(page):
    """The footer promises it. One stop per running turn, none when idle."""
    assert page["escStopped"] == 1
    assert page["afterStopBox"] is False and page["afterStopLabel"] == "Send"
    assert page["escElsewhere"] == 2
    assert page["escIdle"] == 2, "Escape stopped a chat that was not running"


def test_the_open_menu_changes_the_hints(page):
    assert page["menuActive"]
    assert page["menuHints"] == "↑↓ choose · tab accept · enter run · esc close"


def test_the_panel_sheds_hints_first(panel):
    """cfassist's footer sheds in a fixed order when narrow; the hints go
    before the model and the gauge."""
    assert panel["restHints"] == "/ skills"
    assert panel["busyHints"] == "esc stop"
    assert panel["footerTurn"] == "1,840↑ 412↓ 9.6s 2t", "the panel dropped the stats instead"


# --------------------------------------------------------------------------
# what a turn cost
# --------------------------------------------------------------------------

def test_an_answer_says_what_it_cost_and_which_model_wrote_it(page):
    assert page["answerClass"] == "message agent"
    assert page["answerStats"] == "1,840↑ 412↓ · 9.6s · 2 tools · ollama/gemma4:26b"
    assert page["footerTurn"] == "1,840↑ 412↓ 9.6s 2t" and page["footerTurnHidden"] is False


def test_the_gauge_is_the_last_prompt_over_a_measured_window(page):
    assert page["gaugeHidden"] is False
    assert page["gaugeText"] == "12.3k/33k"          # 32768, as cfassist prints it
    assert page["gaugeFill"] == "38%" and page["gaugeHigh"] is False
    assert page["fullGaugeHigh"] and page["fullFill"] == "92%"


def test_a_model_with_no_known_window_gets_no_gauge(page):
    """A gauge against a made-up denominator is worse than none."""
    assert page["hostedGaugeHidden"] is True
    assert page["hostedTurn"] == "900↑ 30↓ 2.0s"
    assert page["hostedStats"] == "900↑ 30↓ · 2.0s · openrouter/some/model"


def test_a_turn_that_reported_nothing_draws_nothing(page):
    """Absent is not zero: "0↑ 0↓" would be a claim."""
    assert page["bareStats"] == "N/A/N/A"
    assert page["bareTurnHidden"] is True and page["bareGaugeHidden"] is True


def test_the_consoles_own_lines_are_not_answers(page):
    assert page["errorClass"] == "message agent error"
    assert page["errorStats"] is None


def test_the_session_totals_add_up(page):
    assert page["session1"].endswith("1 turn · 1,840↑ 412↓ · 9.6s")


def test_a_reload_shows_what_the_live_view_did(page):
    """The numbers are stored with each reply (CFOP-718), so the totals, the
    footer and each answer's line survive a reload. A reply stored before
    that has no line, and is still a turn."""
    assert page["reloadSession"] == "session #7 · 3 turns · 4,150↑ 508↓ · 12.7s"
    assert page["reloadTurn"] == "", "the last stored reply has no stats, so the footer shows none"
    assert page["reloadStats"][:2] == ["1,840↑ 412↓ · 9.6s · 3 tools · ollama/gemma4:26b",
                                       "2,310↑ 96↓ · 3.1s · 1 tool · ollama/gemma4:26b"]
    assert page["reloadStats"][2] is None
    assert page["reloadTitleHidden"] is True


def test_new_chat_starts_the_count_over(page):
    assert page["newTurnHidden"] is True
    assert "turn" not in page["newSession"]
    assert "palette-banner" in page["newWelcome"]


# --------------------------------------------------------------------------
# the transcript
# --------------------------------------------------------------------------

def test_a_tool_call_is_a_row_and_its_result_the_row_under_it(page):
    assert "-&gt;" in page["callRow"] and "loki_query:" in page["callRow"]
    assert page["callPending"] is True
    assert "&lt;-" in page["resultRow"] and "loki_query:" in page["resultRow"] and "0 lines" in page["resultRow"]
    assert page["callDone"] is True
    assert "node-a" in page["callRow"], "the result overwrote the call's arguments"


def test_a_failed_tool_is_marked_as_one(page):
    assert "error" in page["errorRowClass"].split()
    assert "!!" in page["errorRow"] and "timed out" in page["errorRow"]


def test_the_row_opens_from_the_keyboard():
    """The detail is the record of what ran. A div with an onclick is not
    reachable by Tab."""
    page = html()
    assert re.search(r'<button type="button" class="tool-card-header"', page)
    assert '<div class="tool-card-header"' not in page


def test_the_welcome_is_one_copy_and_names_the_build(page):
    assert "CFOperator" in page["welcome"] and 'id="status-version"' in page["welcome"]
    assert html().count("palette-banner\"><span class=\"brand\"") == 1, "the banner is written out twice"


def test_a_hand_off_names_its_row_in_the_boxs_edge(page):
    assert page["title"] == "#5458 · No journal logs from node-a" and page["titleHidden"] is False


# --------------------------------------------------------------------------
# the markup the behaviour stands on
# --------------------------------------------------------------------------

def test_the_order_is_box_then_menu_then_footer():
    page = html()
    box, menu, footer = (page.index('id="palette-box"'), page.index('id="slash-menu"'),
                         page.index('class="palette-footer"'))
    assert box < menu < footer, "the menu opens under the input, and the footer is last"


def test_send_and_stop_are_the_icons_alone():
    """The operator asked for the arrow and the square without a circle."""
    page = html()
    button = re.search(r'<button id="send-button"[^>]*>(.*?)</button>', page, re.S)
    assert button and 'aria-label="Send"' in button.group(0)
    assert 'class="ic-send"' in button.group(1) and 'class="ic-stop"' in button.group(1)
    assert not re.sub(r"<svg.*?</svg>", "", button.group(1), flags=re.S).strip(), "the button has text again"
    rule = re.search(r"#send-button \{[^}]*\}", page).group(0)
    assert "background: transparent" in rule and "border: 0" in rule and "50%" not in rule


def test_the_ants_run_only_while_busy_and_stand_still_on_request():
    page = html()
    assert re.search(r"\.palette-box \.ants \{[^}]*display: none", page)
    assert re.search(r"\.palette-box\.busy \.ants \{ display: block; \}", page)
    # One loop of the animation is a whole number of dash periods, or it jumps.
    dash = re.search(r'stroke-dasharray="(\d+) (\d+)"', page)
    offset = int(re.search(r"@keyframes palette-ants \{ to \{ stroke-dashoffset: -(\d+);", page).group(1))
    assert offset % (int(dash.group(1)) + int(dash.group(2))) == 0
    still = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n        \}", page, re.S).group(1)
    assert ".palette-box .ants rect" in still and "animation: none" in still


def test_the_model_picker_moved_to_the_footer_and_the_status_bar_went():
    page = html()
    footer = page[page.index('class="palette-footer"'):page.index('<div id="toasts">')]
    assert 'id="llm-switcher"' in footer and 'id="llm-popover"' in footer
    assert page.count('id="llm-switcher"') == 1
    assert 'class="status-bar"' not in page
    assert 'id="connection-status"' in footer and 'id="connection-label"' in footer, (
        "the health poll's verdict has nowhere to show")


def test_the_page_does_not_borrow_the_stylesheets_notice_box():
    """console.css's .banner is an amber-edged notice. Reused for the chat's
    opening lines it drew them as two cards; they are .palette-banner."""
    page = html()
    assert 'class="banner"' not in page
    css = (UI / "console.css").read_text(encoding="utf-8")
    assert re.search(r"\.banner \{", css), "the collision this guards against is gone; drop this test"
