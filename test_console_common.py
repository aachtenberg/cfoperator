"""The shared console helpers (ui/common.js) and the quiet poll (CFOP-95).

Every console page carried its own ``esc``/``badge``/``color``/``toast`` —
five copies of the escaper, five toasts with three different durations — and
the list pages rebuilt their rows by innerHTML on every poll whether or not
anything had changed, which shifted rows under the pointer and dropped focus
mid-Tab. ``nav.js`` ended the same drift one layer up; ``common.js`` is the
same fix for the helpers.

These guards are about wiring rather than rendering, in the style of
``test_console_nav.py``: that every page loads the one copy, in an order that
works, and that no page grows a local copy again. The behavioural half runs
the list pages under node, the way ``test_console_a11y.py`` does, because
"the same data twice paints once" is a count a grep cannot take.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_console_nav import CONSOLE_PAGES

REPO_ROOT = Path(__file__).resolve().parent
UI = REPO_ROOT / "ui"
COMMON_JS = UI / "common.js"

#: What common.js owns. A page defining any of these locally is the drift
#: this file exists to stop; add to the list when a helper moves in.
SHARED_HELPERS = ["esc", "color", "badge", "age", "refreshAges", "toast", "trapFocus"]

#: Pages whose main content is a polled row list. Same set as
#: test_console_a11y.LIST_PAGES; repeated rather than imported so this file
#: reads on its own.
LIST_PAGES = ["investigations.html", "remediations.html"]


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def inline_scripts(name):
    return re.findall(r"<script>(.*?)</script>", read(name), re.S)


# --------------------------------------------------------------------------
# the one copy exists and defines what the pages expect
# --------------------------------------------------------------------------

def test_common_js_defines_every_shared_helper():
    js = COMMON_JS.read_text(encoding="utf-8")
    for name in SHARED_HELPERS:
        assert re.search(r"^function %s\(" % re.escape(name), js, re.M), \
            f"common.js does not define {name}()"


def test_common_js_has_no_external_dependencies():
    """Same rule as nav.js: this box has no outbound network."""
    js = COMMON_JS.read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js


def test_one_toast_duration():
    """The drift being ended: 3200 here, 3800 there, 6000 on the chat page.
    One constant, and no page carries a toast timer of its own."""
    js = COMMON_JS.read_text(encoding="utf-8")
    assert re.search(r"^const TOAST_MS = \d+;", js, re.M), "common.js has no TOAST_MS"
    assert len(re.findall(r"\bTOAST_MS\b", js)) >= 2, "TOAST_MS is defined but not used"
    for page in CONSOLE_PAGES:
        for block in inline_scripts(page):
            assert "'toast '" not in block and '"toast "' not in block, \
                f"{page} builds a toast element itself — call toast() from common.js"


# --------------------------------------------------------------------------
# every page loads it, before it needs it, and does not redefine it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_loads_common_js_before_its_own_script(page):
    """Not deferred, and ahead of the first inline script.

    nav.js is deferred because it needs its mount to exist. common.js needs no
    DOM at load, and the pages' inline scripts are not deferred — they call
    trapFocus() at top level and paint() whenever the first fetch resolves —
    so a deferred common.js would be a race that is usually won. The tag is
    the guarantee; this pins it.
    """
    html = read(page)
    m = re.search(r'<script src="/common\.js"([^>]*)>', html)
    assert m, f"{page} does not load /common.js"
    assert "defer" not in m.group(1) and "async" not in m.group(1), \
        f"{page} loads /common.js deferred/async — the page script calls it at top level"
    first_inline = html.find("<script>")
    assert first_inline == -1 or m.start() < first_inline, \
        f"{page} loads /common.js after its own script has already run"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_does_not_redefine_a_shared_helper(page):
    """The nav.js lesson one layer down: the copies drift the day they exist."""
    for block in inline_scripts(page):
        for name in SHARED_HELPERS:
            assert not re.search(r"\bfunction\s+%s\s*\(" % re.escape(name), block), \
                f"{page} defines its own {name}() — common.js owns that now"


def test_login_page_is_not_in_the_helper_set():
    """login.html is what an unauthenticated browser sees; /common.js sits
    behind the auth gate like /nav.js, so the login page must not need it."""
    assert "common.js" not in read("login.html")


# --------------------------------------------------------------------------
# no browser dialogs
# --------------------------------------------------------------------------

def test_no_console_page_calls_alert():
    """The console reports through toasts; alert() blocks the tab and is how
    triage failures used to surface on /investigations."""
    for page in sorted(UI.glob("*.html")):
        for block in re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S):
            assert not re.search(r"(?<!\w)alert\(", block), \
                f"{page.name} calls alert() — use toast(msg, 'err')"


# --------------------------------------------------------------------------
# served like nav.js
# --------------------------------------------------------------------------

def test_common_js_is_served():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__, root_path=str(REPO_ROOT))

    @app.route("/common.js")
    def common_js():
        return flask.send_from_directory("ui", "common.js", mimetype="application/javascript")

    res = app.test_client().get("/common.js")
    assert res.status_code == 200
    assert res.headers["Content-Type"].startswith("application/javascript")
    assert b"function toast(" in res.data


def test_web_server_registers_the_route():
    source = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "@self.app.route('/common.js')" in source, \
        "the pages request /common.js — web_server.py must serve it like /nav.js"


def test_common_js_is_not_exempt_from_auth():
    """It is loaded only by authenticated pages, so it stays behind the gate —
    a helper file is not secret, but the exempt list is the console's attack
    surface and grows only with a reason."""
    from web_auth import EXEMPT_PATHS
    assert "/common.js" not in EXEMPT_PATHS
    assert "/nav.js" not in EXEMPT_PATHS


# --------------------------------------------------------------------------
# the helpers themselves, under node
# --------------------------------------------------------------------------

_HELPER_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
const appended=[];
const box={console,JSON,Array,Object,String,Number,Date,Math,
  setTimeout:(fn,ms)=>{ appended.push({timer:ms}); return 0; },
  getComputedStyle:()=>({getPropertyValue:v=>v==='--accent'?' #00ff00 ':''}),
  document:{documentElement:{},activeElement:null,
    getElementById:id=>id==='toasts'?{appendChild(t){ appended.push(t); }}:null,
    createElement:()=>({className:'',textContent:'',style:{}})}};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
box.toast('hello','err');
console.log(JSON.stringify({
  esc: box.esc(`<a href="x" title='y'>&</a>`),
  escNull: box.esc(null),
  badge: box.badge('<b>', '--accent'),
  badgeEmpty: box.badge('', '--nope'),
  toast: appended,
}));
"""


@pytest.fixture(scope="module")
def helpers(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("common") / "stub.js"
    stub.write_text(_HELPER_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(COMMON_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_esc_escapes_both_quote_kinds(helpers):
    """These values land inside attributes as well as text."""
    assert helpers["esc"] == "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;"
    assert helpers["escNull"] == ""


def test_badge_escapes_and_falls_back(helpers):
    assert "&lt;b&gt;" in helpers["badge"] and "#00ff00" in helpers["badge"]
    assert "—" in helpers["badgeEmpty"] and "#888" in helpers["badgeEmpty"]


def test_toast_appends_a_typed_element_on_one_timer(helpers):
    t, timer = helpers["toast"]
    assert t["className"] == "toast err" and t["textContent"] == "hello"
    assert timer == {"timer": 5000}


# --------------------------------------------------------------------------
# the quiet poll, under node
# --------------------------------------------------------------------------

# Each list page's inline script, with common.js loaded first (as the browser
# does). The rows element counts innerHTML writes; fetch answers from a
# mutable dataset so the harness can poll twice with the same rows, then
# change one, then hide the document.
_POLL_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(process.argv[2]),'common.js'),'utf8');
const api=process.argv[3], key=process.argv[4];

let data=[{id:7,status:'queued',outcome:'monitoring',risk:'low',remediation_class:'gitops-patch',
           host_id:'h',payload:{},created_at:'2026-08-25T10:00:00Z',started_at:'2026-08-25T10:00:00Z'},
          {id:6,status:'resolved',outcome:'resolved',risk:'low',remediation_class:'gitops-patch',
           host_id:'h',payload:{},created_at:'2026-08-25T09:00:00Z',started_at:'2026-08-25T09:00:00Z'}];
let fetches=0, paints=0;
const doc={documentElement:{},body:{appendChild(){}},addEventListener(){},activeElement:null,hidden:false};
function el(id){const e={id:id||'',className:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},remove(){},
  appendChild(){},addEventListener(){},focus(){},isConnected:true,_html:''};
  Object.defineProperty(e,'innerHTML',{get(){return e._html;},set(v){ e._html=v; if(id==='rows') paints++; }});
  return e;}
const els={};
doc.createElement=()=>el('');
doc.getElementById=id=>(els[id]=els[id]||el(id));
const loc={pathname:'/x',search:'',hash:'',href:'http://cfop/x'};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:{replaceState(){}}, document:doc, navigator:{},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  fetch:(url)=>{
    if(url.indexOf(api)===0 && url.indexOf(api+'/')!==0){ fetches++;
      return Promise.resolve({json:()=>Promise.resolve({[key]:data,investigations:key==='investigations'?data:[],remediations:key==='remediations'?data:[]})}); }
    return Promise.resolve({ok:true,json:()=>Promise.resolve({})});
  }};
box.window={location:loc,history:box.history,addEventListener(){},removeEventListener(){},
  CFOP:{me:()=>Promise.resolve({role:'admin'})}};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={};
  out.paintsAfterFirstLoad=paints;
  await box.load(); await tick();
  out.paintsAfterSameData=paints;
  data=data.map(r=>r.id===7?Object.assign({},r,{status:'pr-open',outcome:'needs_action'}):r);
  await box.load(); await tick();
  out.paintsAfterChangedData=paints;
  const before=fetches;
  doc.hidden=true;
  await box.load(); await tick();
  out.fetchedWhileHidden=fetches>before;
  out.rowsStillPainted=els['rows']._html.indexOf('row-7')>=0;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""

_API = {"investigations.html": ("/api/investigations", "investigations"),
        "remediations.html": ("/api/remediations", "remediations")}


@pytest.fixture(scope="module", params=LIST_PAGES)
def poll_behaviour(request, tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    page = request.param
    stub = tmp_path_factory.mktemp("poll") / "stub.js"
    stub.write_text(_POLL_STUB, encoding="utf-8")
    api, key = _API[page]
    out = subprocess.run([node, str(stub), str(UI / page), api, key],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"{page}: {out.stderr}"
    return page, json.loads(out.stdout)


def test_the_same_data_twice_paints_once(poll_behaviour):
    """The defect: a poll that brought nothing new still replaced every row."""
    page, b = poll_behaviour
    assert b["paintsAfterFirstLoad"] == 1, f"{page}: first load painted {b['paintsAfterFirstLoad']} times"
    assert b["paintsAfterSameData"] == 1, f"{page}: an unchanged poll repainted the rows"


def test_changed_data_repaints(poll_behaviour):
    """The guard must not be so quiet that a status change never lands."""
    page, b = poll_behaviour
    assert b["paintsAfterChangedData"] == 2, f"{page}: a status change did not repaint"
    assert b["rowsStillPainted"], f"{page}: the rows vanished"


def test_a_hidden_tab_does_not_poll(poll_behaviour):
    page, b = poll_behaviour
    assert not b["fetchedWhileHidden"], f"{page}: load() fetched with the document hidden"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_a_hidden_tab_catches_up_when_shown(page):
    """Skipping polls while hidden is only acceptable if coming back does not
    mean staring at a list up to 20 s stale."""
    js = "".join(inline_scripts(page))
    assert "'visibilitychange'" in js, f"{page} never reloads when the tab becomes visible"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_age_column_is_refreshed_in_place(page):
    """Fewer repaints must not mean a page that reads "3m" for an hour."""
    js = "".join(inline_scripts(page))
    assert "data-age=" in js, f"{page} rows carry no data-age for the in-place refresh"
    assert "refreshAges()" in js, f"{page} never refreshes ages on a quiet poll"
