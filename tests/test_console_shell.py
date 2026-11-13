"""Tests for the console's shared shell (CFOP-696): console.css and the rail.

Six pages each carried a copy of the tokens, the header, the chips, the badge,
the table and the drawer, and the copies drifted. console.css is the one copy
now, and nav.js draws the rail, the status card and the Ask button on every
page. These tests are about that staying true: a new page that skips the
stylesheet, or an old one that grows its own tokens back, should fail here
rather than be noticed as "why is this page a slightly different grey".

Like test_console_nav.py they are about wiring, not rendering.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
CSS = UI / "console.css"
NAV_JS = UI / "nav.js"

# Every page an authenticated browser sees. Globbed, so a page added later is
# held to this without anyone remembering to list it. login.html is what an
# unauthenticated browser sees and cannot load an authenticated stylesheet.
CONSOLE_PAGES = sorted(p.name for p in UI.glob("*.html") if p.name != "login.html")

# The palette. A page may add tokens of its own (the chat has its dims and its
# bar heights); it may not restate one of these, because that is the copy that
# drifts — --text-secondary was three different greys' worth of contrast away
# from the design before it lived in one place.
SHARED_TOKENS = ["--bg-primary", "--bg-secondary", "--bg-tertiary", "--bg-hover", "--border",
                 "--border-subtle", "--text-primary", "--text-secondary", "--text-muted",
                 "--accent", "--cyan", "--orange", "--red", "--purple", "--font-ui", "--font-mono"]


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def test_there_are_console_pages_to_check():
    """A wrong root makes the glob empty and every parametrized test vacuous."""
    assert len(CONSOLE_PAGES) >= 7 and "inbox.html" in CONSOLE_PAGES


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_links_the_stylesheet_before_its_own_style(page):
    """Before, so the page's own rules win where it has any."""
    html = read(page)
    link = html.find('<link rel="stylesheet" href="/console.css">')
    assert link >= 0, f"{page} does not link /console.css"
    style = html.find("<style>")
    assert style < 0 or link < style, f"{page} links /console.css after its own <style>"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_does_not_restate_a_shared_token(page):
    styles = "\n".join(re.findall(r"<style>(.*?)</style>", read(page), re.S))
    for token in SHARED_TOKENS:
        assert not re.search(re.escape(token) + r"\s*:", styles), (
            f"{page} defines {token} itself; it is console.css's to define")


def test_the_stylesheet_defines_every_shared_token():
    css = CSS.read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    for token in SHARED_TOKENS:
        assert re.search(re.escape(token) + r"\s*:", root), f"console.css :root lacks {token}"


def test_the_stylesheet_fetches_nothing():
    """This box has no outbound network: a webfont or an @import hangs."""
    css = CSS.read_text(encoding="utf-8")
    assert "@import" not in css
    for url in re.findall(r"url\(\s*[\"']?([^\"')]+)", css):
        assert url.startswith("data:"), f"console.css loads {url[:60]}"
    assert "@font-face" not in css, "a webfont has to be vendored through scripts/vendor_ui.py"


def test_the_hidden_attribute_still_hides():
    """The kit sets display on its classes (.chip, .badge, .cfop-logout), and
    an author display rule beats the browser's [hidden]. The pages and nav.js
    hide things with the attribute — the logout before identity lands, the
    Inbox count at zero, Admin's Cancel edit — so the stylesheet has to say
    that hidden wins. Found by review on #374: all three were showing."""
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\[hidden\]\s*\{\s*display\s*:\s*none\s*!important", css), (
        "console.css must carry [hidden]{display:none!important}; its display rules override the attribute")
    assert ("hidden" in NAV_JS.read_text(encoding="utf-8")
            and re.search(r"<button[^>]*class=\"chip[^>]*\bhidden\b", read("admin.html"))), (
        "nothing relies on [hidden] any more; this guard can go with it")


def test_login_does_not_depend_on_the_authenticated_stylesheet():
    assert "/console.css" not in read("login.html"), (
        "login.html is painted before a session exists; /console.css is behind the login")


def test_web_server_serves_the_stylesheet_and_the_inbox():
    source = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "@self.app.route('/console.css')" in source
    assert "send_from_directory('ui', 'console.css', mimetype='text/css')" in source
    assert "@self.app.route('/inbox')" in source
    from web_auth import EXEMPT_PATHS
    assert "/console.css" not in EXEMPT_PATHS and "/inbox" not in EXEMPT_PATHS


def test_the_chat_stays_at_the_root():
    """/?investigation= and /?remediation= hand a row to the chat. Inbox is
    first in the rail, but it does not take the chat's address."""
    source = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    root = re.search(r"@self\.app\.route\('/'\)\s*\n\s*def \w+\(\):\s*\n\s*return (.*)", source).group(1)
    assert "index.html" in root


# --- the rail, under node -------------------------------------------------

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
const pathname=process.argv[3], scenario=process.argv[4]||'';
function el(){return{className:'',innerHTML:'',textContent:'',hidden:true,attrs:{},
  addEventListener(){},setAttribute(k,v){this.attrs[k]=v;},querySelector(){return el();}};}
const mount=el(), byId={'cfop-nav':mount}, appended=[], asked=[];
const answers={
  '/api/auth/me':{username:'u',role:'admin'},
  '/api/health':{version:'main-abc1234',uptime_seconds:3600},
  '/api/settings/provider':{backend:'openrouter',model:'m'},
  '/api/sweep-reports?limit=1':{reports:[{id:2394,swept_at:'2026-11-12T19:52:00Z'}]},
  '/api/investigations?limit=80':{investigations:[
    {id:1,outcome:'needs_action'},{id:2,outcome:'needs_action',triage_action:'ack'},
    {id:3,outcome:'resolved'},{id:4,outcome:'needs_action',triage_action:null}]}
};
const box={console,window:{location:{pathname,href:''}},
  document:{readyState:'complete',hidden:false,head:{appendChild(){}},
    body:{appendChild(n){appended.push(n);}},
    getElementById:id=>byId[id]||null,
    createElement:()=>el(),addEventListener:()=>{}},
  fetch:url=>{ asked.push(url);
    if(scenario==='down' && url==='/api/health') return Promise.resolve({ok:false,status:502,json:()=>Promise.resolve({})});
    return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(answers[url]||{})}); }};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
  // The status card and the count paint into elements the rail rendered.
  byId['cfop-status']=el(); byId['cfop-inbox-count']=el();
  for(let i=0;i<6;i++) await tick();
  const status=byId['cfop-status'].innerHTML, n=byId['cfop-inbox-count'];
  const count0=n.textContent, hidden0=n.hidden;
  // A page that just triaged a row asks the rail to read again.
  const before=asked.filter(u=>u==='/api/investigations?limit=80').length;
  answers['/api/investigations?limit=80']={investigations:[{id:4,outcome:'needs_action'}]};
  box.window.CFOP.refreshStatus();
  for(let i=0;i<6;i++) await tick();
  const refetched=asked.filter(u=>u==='/api/investigations?limit=80').length-before;
  answers['/api/investigations?limit=80']={investigations:[
    {id:1,outcome:'needs_action'},{id:2,outcome:'needs_action',triage_action:'ack'},
    {id:3,outcome:'resolved'},{id:4,outcome:'needs_action',triage_action:null}]};
  console.log(JSON.stringify({
    refetched, countAfterRefresh: n.textContent,
    asks: appended.length, askHref: appended[0]?appended[0].href:null,
    askName: appended[0]?appended[0].attrs['aria-label']:null,
    count: count0, countHidden: hidden0,
    needs: box.window.CFOP.needsAction(answers['/api/investigations?limit=80'].investigations).map(r=>r.id),
    connected: status.indexOf('Connected')>=0 && status.indexOf('Disconnected')<0,
    disconnected: status.indexOf('Disconnected')>=0,
    build: status.indexOf('main-abc1234')>=0, provider: status.indexOf('openrouter')>=0,
    sweep: status.indexOf('#2394')>=0, uptime: status.indexOf('up 1h 0m')>=0,
    rail: mount.className, brandLinksInbox: mount.innerHTML.indexOf('class="cfop-brand" href="/inbox"')>=0,
    groups: (mount.innerHTML.match(/<nav class="cfop-nav"/g)||[]).length
  }));
})();
"""


def _rail(tmp_path, pathname, scenario=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(NAV_JS), pathname, scenario],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_rail_renders_two_groups_and_the_brand(tmp_path):
    r = _rail(tmp_path, "/events")
    assert r["rail"] == "cfop-rail"
    assert r["groups"] == 2, "work sections and settings are two navs"
    assert r["brandLinksInbox"]


def test_the_inbox_count_is_untriaged_needs_action(tmp_path):
    """The rail's number and the Inbox queue are one rule: an investigation the
    agent ended on needs_action that nobody has triaged. A triaged one is no
    longer waiting on anyone and must not keep the count up."""
    r = _rail(tmp_path, "/events")
    assert r["needs"] == [1, 4]
    assert r["count"] == "2" and r["countHidden"] is False


def test_a_triage_refreshes_the_rail_count(tmp_path):
    """The rail's count is its own read on a 60 s timer. Ack on Inbox (and a
    verdict in the investigations pane) changes what it should say, so both
    ask for the re-read; otherwise the number beside "Inbox" sits one ahead of
    the queue next to it. Bugbot on #374."""
    r = _rail(tmp_path, "/inbox")
    assert r["refetched"] == 1 and r["countAfterRefresh"] == "1"
    ack = re.search(r"async function ack\(id, btn\)\{.*?\n\}", read("inbox.html"), re.S).group(0)
    assert "CFOP.refreshStatus()" in ack, "Inbox's Ack does not refresh the rail"
    triage = re.search(r"async function triage\(id, action\)\{.*?\n\}", read("investigations.html"), re.S).group(0)
    assert "CFOP.refreshStatus()" in triage, "a verdict in the pane does not refresh the rail"


def test_the_inbox_page_uses_the_rail_s_rule():
    assert "CFOP.needsAction" in read("inbox.html"), (
        "the Inbox queue must be the function the rail counts with, or the two numbers part")


def test_the_status_card_reads_what_it_shows(tmp_path):
    r = _rail(tmp_path, "/events")
    assert r["connected"] and r["build"] and r["provider"] and r["sweep"] and r["uptime"]


def test_the_status_card_says_disconnected_when_health_fails(tmp_path):
    r = _rail(tmp_path, "/events", "down")
    assert r["disconnected"] and not r["connected"]


@pytest.mark.parametrize("pathname,asks", [("/inbox", 1), ("/events", 1), ("/admin", 1), ("/", 0)])
def test_ask_is_on_every_page_but_the_chat(tmp_path, pathname, asks):
    r = _rail(tmp_path, pathname)
    assert r["asks"] == asks
    if asks:
        assert r["askHref"] == "/" and r["askName"] == "Ask CFOperator"


# --- investigations: docked pane is not modal -----------------------------

def test_a_docked_pane_lowers_aria_modal_and_trap_focus_respects_it():
    """Beside the list, the pane is not a layer over the page: Tab has to be
    able to reach the list, so the dialog must stop claiming to be modal and
    the trap must believe it. The static markup stays modal — that is what a
    narrow screen gets."""
    page = read("investigations.html")
    assert re.search(r'<div id="detail"[^>]*aria-modal="true"', page)
    assert "setAttribute('aria-modal', docked()?'false':'true')" in page
    # The script's width and the stylesheet's are one number.
    mq = re.search(r"const DOCK_MQ='\(min-width: (\d+)px\)'", page).group(1)
    assert f"@media (min-width: {mq}px)" in page, "DOCK_MQ and the dock's media query disagree"
    common = read("common.js")
    trap = re.search(r"function trapFocus\(dialog\)\{.*?\n\}", common, re.S).group(0)
    assert "getAttribute('aria-modal')==='false'" in trap


_DOCK_STUB = r"""
const fs=require('fs'), vm=require('vm');
const page=fs.readFileSync(process.argv[2],'utf8');
const a=page.indexOf("const DOCK_MQ="), b=page.indexOf("try{ const mq=window.matchMedia");
const detail={attrs:{}, setAttribute(k,v){this.attrs[k]=v;}};
const box={window:{matchMedia:()=>({matches:process.argv[3]==='wide-screen'})},
  document:{getElementById:()=>detail}, MAX:false};
vm.createContext(box);
vm.runInContext('var MAX=false;'+page.slice(a,b)+';globalThis.s=(m,w)=>{MAX=m;WIDE=w;applyModal();return document.getElementById("detail").attrs["aria-modal"];}',box);
console.log(JSON.stringify({plain:box.s(false,false), max:box.s(true,false), terminal:box.s(false,true)}));
"""


@pytest.mark.parametrize("screen,expected", [
    # Beside the list: not modal. Maximized, or run over the list by an open
    # terminal: nothing beside it is visible, so it is modal again.
    ("wide-screen", {"plain": "false", "max": "true", "terminal": "true"}),
    ("narrow-screen", {"plain": "true", "max": "true", "terminal": "true"}),
])
def test_the_pane_is_modal_whenever_it_covers_the_list(tmp_path, screen, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "dock.js"
    stub.write_text(_DOCK_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "investigations.html"), screen],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == expected


def test_opening_and_closing_a_terminal_re_decides_modality():
    """#detail.wide runs the pane over the list at any width. The class and
    the flag docked() reads move together, and each move calls applyModal(),
    or aria-modal keeps saying "false" over a list nobody can see and Tab
    walks its 80 hidden rows. Bugbot on #374."""
    page = read("investigations.html")
    mount = re.search(r"function mountCockpit\(id, s\)\{.*?\n\}", page, re.S).group(0)
    assert re.search(r"classList\.add\('wide'\);\s*WIDE=true;\s*applyModal\(\)", mount)
    drop = re.search(r"function disconnectCockpit\(\)\{.*?\n\}", page, re.S).group(0)
    assert re.search(r"classList\.remove\('wide'\);\s*WIDE=false;\s*applyModal\(\)", drop)


# --- events: grouping a sweep's burst -------------------------------------

_GROUP_STUB = r"""
const fs=require('fs'), vm=require('vm');
const common=fs.readFileSync(process.argv[2],'utf8'), page=fs.readFileSync(process.argv[3],'utf8');
const a=page.indexOf('const GROUP_GAP_MS'), b=page.indexOf('function hhmm(');
const box={console,document:{},localStorage:{getItem(){return null;},setItem(){}}};
vm.createContext(box); vm.runInContext(common,box); vm.runInContext(page.slice(a,b)+';globalThis.groupRuns=groupRuns;',box);
const row=(id,source,at)=>({alert_id:id,source,latest_event_at:at});
const show=rows=>box.groupRuns(rows).map(g=>[g.group, g.rows.map(r=>r.alert_id).join('')]);
const S='cfoperator-sweep';
console.log(JSON.stringify({
  basic: show([
    row('a',S,'2026-11-12T19:52:10Z'), row('b',S,'2026-11-12T19:52:00Z'), row('c',S,'2026-11-12T19:51:30Z'),
    row('d','alertmanager','2026-11-12T19:14:00Z'),
    row('e',S,'2026-11-12T13:55:00Z'),
    row('f',S,'2026-11-12T13:40:00Z'), row('g',S,'2026-11-12T13:39:00Z')]),
  // Another source firing while the sweep is still posting.
  interleaved: show([
    row('a',S,'2026-11-12T19:52:10Z'), row('x','alertmanager','2026-11-12T19:52:05Z'),
    row('b',S,'2026-11-12T19:52:00Z'), row('c',S,'2026-11-12T19:51:30Z')]),
  // A sweep every four minutes: each step is inside the gap, the span is not.
  chained: show([
    row('a',S,'2026-11-12T12:00:00Z'), row('b',S,'2026-11-12T11:56:00Z'),
    row('c',S,'2026-11-12T11:52:00Z'), row('d',S,'2026-11-12T11:48:00Z')])
}));
"""


def test_a_sweep_s_burst_folds_and_a_lone_finding_does_not(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "group.js"
    stub.write_text(_GROUP_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "common.js"), str(UI / "events.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["basic"] == [
        [True, "abc"],    # three findings seconds apart: one sweep
        [False, "d"],     # another source is never grouped
        [False, "e"],     # a sweep finding with nothing near it is just a row
        [True, "fg"],     # 15 minutes later is a different sweep
    ]
    # An alert from elsewhere inside the burst does not cut the sweep in two.
    assert got["interleaved"] == [[True, "abc"], [False, "x"]]
    # The gap is measured from the run's first row, so a group cannot span
    # more than it: a short sweep interval must not fold the whole history.
    assert got["chained"] == [[True, "ab"], [True, "cd"]]
