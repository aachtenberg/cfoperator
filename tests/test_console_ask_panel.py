"""Ask CFOperator opens the chat over the page instead of leaving it (CFOP-711).

The round Ask button and the drawers' "Ask about this" links were plain links
to the chat. Each one replaced the page being read, which is the page the
question is about. A plain click now opens the chat in a floating panel: the
chat page itself in a same-origin frame, in an embed mode.

nav.js runs here under node with a small DOM that records what was appended,
which listeners were bound and what a click did, so the behaviour is driven
rather than read out of the source.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
NAV_JS = UI / "nav.js"

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
const pathname=process.argv[3], search=process.argv[4]||'', remembered=process.argv[5]==='remembered';
function el(tag){ const e={tag:tag||'', className:'', id:'', innerHTML:'', textContent:'', hidden:false, href:'',
  attrs:{}, on:{}, focused:0, kids:{},
  addEventListener(t,f){ this.on[t]=f; }, setAttribute(k,v){ this.attrs[k]=String(v); },
  getAttribute(k){ return k in this.attrs?this.attrs[k]:null; }, focus(){ this.focused++; },
  querySelector(sel){ return this.kids[sel]||(this.kids[sel]=el(sel)); }};
  return e; }
const mount=el('div'), appended=[], asked=[], docOn={};
const store={}; if(remembered) store['cfop.ask.open']='1';
const box={console,
  sessionStorage:{getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v);}, removeItem:k=>{delete store[k];}},
  window:{location:{pathname, search, href:''}},
  document:{readyState:'complete',hidden:false,head:{appendChild(){}},
    body:{appendChild(n){appended.push(n);}},
    getElementById:id=>id==='cfop-nav'?mount:null,
    createElement:t=>el(t), addEventListener:(t,f)=>{ (docOn[t]=docOn[t]||[]).push(f); }},
  fetch:url=>{ asked.push(url); return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})}); }};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
const click=(over)=>Object.assign({button:0, prevented:false, preventDefault(){ this.prevented=true; }}, over||{});
const ask=()=>appended.find(n=>n.className==='cfop-ask');
const panel=()=>appended.find(n=>n.className==='cfop-chat');
const frame=()=>panel()?panel().kids['iframe']:null;
const linkTo=href=>({closest:sel=>sel==='a[href^="/console?"]'&&href.indexOf('/console?')===0
  ?{getAttribute:()=>href}:null});
const docClick=(href,over)=>{ const e=click(Object.assign({target:linkTo(href)},over||{}));
  (docOn.click||[]).forEach(f=>f(e)); return e; };
const out={rail:mount.className, fetched:asked.length, hasCFOP:typeof box.window.CFOP.me==='function',
  askDrawn:!!ask(), panelAtLoad:!!panel(), frameAtLoad:frame()?frame().attrs.src:null,
  rememberedAfterLoad:store['cfop.ask.open']||null};
// The chat's composer, inside the frame. The frame's document is reachable
// (same origin) once it has loaded; loadFrame() is that moment.
const composer=el('textarea');
const loadFrame=()=>{ frame().contentDocument={getElementById:id=>id==='message-input'?composer:null};
  if(frame().on.load) frame().on.load({}); };
if(panel()){
  // Restored from the page before: shown, and focus left where this page put it.
  out.restoredFrameFocus=frame().focused; loadFrame(); out.restoredComposerFocus=composer.focused;
}
if(ask()){
  out.askHref=ask().href; out.askControls=ask().attrs['aria-controls']; out.expanded0=ask().attrs['aria-expanded'];
  // A modified click is the browser's: the link opens the full page.
  const mod=click({ctrlKey:true}); ask().on.click(mod);
  out.modifiedPrevented=mod.prevented; out.panelAfterModified=!!panel() && !remembered;
  // A plain click opens the panel.
  const c1=click(); ask().on.click(c1);
  out.plainPrevented=c1.prevented; out.panelId=panel().id; out.panelRole=panel().attrs.role;
  out.openHidden=panel().hidden; out.frameSrc=frame().attrs.src; out.expanded1=ask().attrs['aria-expanded'];
  out.remembered1=store['cfop.ask.open']||null; out.frameFocused=frame().focused>0;
  out.fullPageLink=/class="cfop-chat-full" href="\/console"/.test(panel().innerHTML);
  // The operator opened it, so the composer gets the keyboard once the frame has loaded.
  const before0=composer.focused; loadFrame(); out.composerOnLoad=composer.focused-before0;
  // A later load of the frame (a reload inside it) is not a reason to take focus again.
  const before1=composer.focused; frame().on.load({}); out.composerOnLaterLoad=composer.focused-before1;
  // Again closes it, and leaves the loaded frame alone.
  ask().on.click(click());
  out.closedHidden=panel().hidden; out.expanded2=ask().attrs['aria-expanded']; out.remembered2=store['cfop.ask.open']||null;
  frame().attrs.src='(loaded)';
  const before2=composer.focused;
  ask().on.click(click());
  out.reopenKeptFrame=frame().attrs.src==='(loaded)'; out.composerOnReopen=composer.focused-before2;
  // The header's close button, and Escape there, close it and hand focus back.
  const before=ask().focused;
  panel().kids['.cfop-chat-close'].on.click(click());
  out.closeButtonHides=panel().hidden; out.focusReturned=ask().focused===before+1;
  ask().on.click(click());
  panel().on.keydown({key:'Escape'});
  out.escapeHides=panel().hidden;
  panel().on.keydown({key:'a'});
  // A drawer's hand-off link opens the panel on that row.
  const h=docClick('/console?investigation=2272');
  out.handoffPrevented=h.prevented; out.handoffSrc=frame().attrs.src; out.handoffOpen=!panel().hidden;
  const r=docClick('/console?remediation=80');
  out.remediationSrc=frame().attrs.src;
  // ...and only that: anything else on the link is left to the browser.
  frame().attrs.src='(untouched)';
  out.strangePrevented=[docClick('/console?next=//evil.example').prevented,
    docClick('/console?investigation=1&x=2').prevented, docClick('/console?investigation=abc').prevented,
    docClick('/console?investigation=5',{metaKey:true}).prevented, docClick('/admin?tab=llm').prevented];
  out.strangeLeftFrame=frame().attrs.src==='(untouched)';
}
console.log(JSON.stringify(out));
"""


def _run(tmp_path, pathname, search="", remembered=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(NAV_JS), pathname, search, "remembered" if remembered else ""],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_plain_click_opens_the_chat_over_the_page(tmp_path):
    r = _run(tmp_path, "/events")
    assert r["askDrawn"] and not r["panelAtLoad"], "the panel is built on demand, not on every page load"
    assert r["plainPrevented"], "the click still navigated away"
    assert r["panelRole"] == "dialog" and r["openHidden"] is False
    assert r["frameSrc"] == "/console?embed=1"
    assert r["frameFocused"], "typing after opening should reach the chat"


def test_the_button_says_whether_it_is_open(tmp_path):
    r = _run(tmp_path, "/events")
    assert r["askControls"] == r["panelId"] == "cfop-chat"
    assert (r["expanded0"], r["expanded1"], r["expanded2"]) == ("false", "true", "false")


def test_the_button_is_still_a_link_to_the_full_page(tmp_path):
    """A middle or modified click, and a browser where the script did not run,
    get the chat page as before."""
    r = _run(tmp_path, "/events")
    assert r["askHref"] == "/console"
    assert not r["modifiedPrevented"] and not r["panelAfterModified"]
    assert r["fullPageLink"], "the panel offers no way to the full page"


def test_closing_keeps_the_conversation(tmp_path):
    """Close hides the panel; it does not unload the frame. Reopening must not
    reload a chat that may be mid-answer."""
    r = _run(tmp_path, "/events")
    assert r["closedHidden"] is True
    assert r["reopenKeptFrame"], "reopening reloaded the frame"


def test_close_and_escape_hand_focus_back(tmp_path):
    r = _run(tmp_path, "/events")
    assert r["closeButtonHides"] and r["focusReturned"]
    assert r["escapeHides"]


def test_a_drawer_hand_off_opens_the_panel_on_its_row(tmp_path):
    r = _run(tmp_path, "/investigations")
    assert r["handoffPrevented"] and r["handoffOpen"]
    assert r["handoffSrc"] == "/console?embed=1&investigation=2272"
    assert r["remediationSrc"] == "/console?embed=1&remediation=80"


def test_only_a_row_hand_off_is_intercepted(tmp_path):
    """One key, digits only, a plain click. Anything else on a /console link
    is the browser's, and none of it is copied into the frame's address."""
    r = _run(tmp_path, "/investigations")
    assert r["strangePrevented"] == [False, False, False, False, False]
    assert r["strangeLeftFrame"]


def test_the_panel_follows_the_operator_between_pages(tmp_path):
    r = _run(tmp_path, "/events")
    assert r["remembered1"] == "1" and r["remembered2"] is None
    again = _run(tmp_path, "/remediations", remembered=True)
    assert again["panelAtLoad"] and again["frameAtLoad"] == "/console?embed=1"


def test_following_to_the_next_page_does_not_take_the_keyboard(tmp_path):
    """Bugbot on #375. Restoring the open panel called the same open as a
    click, which focused the frame: a click on the rail then landed keyboard
    focus in the chat instead of on the page that was just opened. Restored,
    the panel shows and takes nothing, at load and once its frame has loaded."""
    again = _run(tmp_path, "/remediations", remembered=True)
    assert again["panelAtLoad"]
    assert again["restoredFrameFocus"] == 0, "restoring the panel moved focus into the frame"
    assert again["restoredComposerFocus"] == 0, "the restored frame focused its composer once loaded"


def test_opening_it_puts_the_keyboard_in_the_composer(tmp_path):
    """Asked for, it is where the next keystroke should go: the composer, as
    soon as the frame's document is there, and again on a reopen. A later load
    inside the frame is not the operator asking."""
    r = _run(tmp_path, "/events")
    assert r["frameFocused"]
    assert r["composerOnLoad"] == 1, "the composer was not focused when the frame finished loading"
    assert r["composerOnLaterLoad"] == 0, "a reload inside the frame took focus again"
    assert r["composerOnReopen"] == 1


def test_the_embedded_chat_takes_no_focus_of_its_own():
    """A same-origin frame's autofocus takes focus from the page around it, so
    the attribute is gone and the script focuses the composer on the full page
    only. In the panel the host decides (the two tests above)."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    textarea = re.search(r'<textarea id="message-input"[^>]*>', html).group(0)
    assert "autofocus" not in textarea
    boot = html[html.rindex("<script>"):]
    assert re.search(r"if \(!embedded\(\)\) \{\s*const composer = document\.getElementById\('message-input'\);", boot), (
        "the full page no longer focuses its composer on arrival")


_HANDOFF_STUB = r"""
const fs=require('fs');
const html=fs.readFileSync(process.argv[2],'utf8');
const grab=name=>{ const m=html.match(new RegExp('function '+name+'\\(\\) \\{[\\s\\S]*?\\n        \\}')); if(!m) throw new Error(name+' not found'); return m[0]; };
const run=(embed,hash)=>{ const calls=[];
  const document={documentElement:embed===null?{}:{classList:{contains:c=>embed&&c==='embed'}}};
  const location={pathname:'/console', hash:hash||''};
  const history={replaceState:(s,t,u)=>calls.push(u)};
  new Function('document','location','history', grab('embedded')+grab('consumeHandoff')+'consumeHandoff();')(document,location,history);
  return calls; };
console.log(JSON.stringify({full:run(false), embed:run(true), embedHash:run(true,'#x'), bare:run(null)}));
"""


def test_consuming_a_hand_off_keeps_the_frame_embedded(tmp_path):
    """Bugbot on #375. The row comes off the address so a reload does not ask
    twice; replaceState to the bare path dropped embed=1 with it, and a reload
    of the frame then painted the full console (rail, timers, its own Ask
    button) inside the panel."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path / "stub.js"
    stub.write_text(_HANDOFF_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "index.html")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["full"] == ["/console"], "the full page keeps a query it should have consumed"
    assert r["embed"] == ["/console?embed=1"]
    assert r["embedHash"] == ["/console?embed=1#x"]
    assert r["bare"] == ["/console"], "a document with no classList must not throw"
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert html.count("consumeHandoff();") == 2, "both hand-offs (remediation, investigation) consume through the one helper"
    assert "history.replaceState(null, '', location.pathname + location.hash)" not in html


def test_the_chat_page_has_no_panel_and_ends_the_following(tmp_path):
    r = _run(tmp_path, "/console", remembered=True)
    assert not r["askDrawn"] and not r["panelAtLoad"]
    assert r["rememberedAfterLoad"] is None, "coming to the full page did not end the panel following along"
    assert r["rail"] == "cfop-rail", "the full-page chat still has its rail"


def test_inside_the_frame_nothing_is_drawn_or_polled(tmp_path):
    """The host page has the rail, the status card and their timers. The frame
    must not draw a second rail, add an Ask button to itself or run four more
    status reads a minute -- and the chat still gets CFOP.me()."""
    r = _run(tmp_path, "/console", "?embed=1")
    assert r["rail"] == "" and not r["askDrawn"] and r["fetched"] == 0
    assert r["hasCFOP"]
    handed = _run(tmp_path, "/console", "?embed=1&investigation=7")
    assert handed["rail"] == "" and handed["fetched"] == 0


def test_a_page_that_merely_mentions_embed_is_not_embedded(tmp_path):
    r = _run(tmp_path, "/events", "?q=embed=1x")
    assert r["rail"] == "cfop-rail" and r["askDrawn"]


def test_the_chat_page_has_an_embed_mode():
    """What the frame loads has to drop the chrome the host already shows, and
    send its ordinary links to the host page, not into a 480px frame."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "classList.add('embed')" in html
    assert re.search(r"base\.target\s*=\s*'_top'", html), "links in the frame would open inside the frame"
    head = html[:html.index("<style>")]
    assert "classList.add('embed')" in head, "embed is marked after the first paint: the frame would flash the rail"
    assert re.search(r"html\.embed #cfop-nav[^{]*\{\s*display:\s*none", html), "the frame would show a second rail"
    assert "html.embed body { padding-left: 0; }" in html


def test_the_panel_is_styled_in_the_stylesheet_under_the_toasts():
    css = (UI / "console.css").read_text(encoding="utf-8")
    panel = int(re.search(r"\.cfop-chat \{[^}]*z-index:(\d+)", css).group(1))
    drawer = int(re.search(r"#detail \{[^}]*z-index:(\d+)", css).group(1))
    toasts = int(re.search(r"#toasts \{[^}]*z-index:(\d+)", css).group(1))
    assert drawer < panel < toasts, (
        "the panel sits over the drawer it was opened from and under the toasts that share its corner")
