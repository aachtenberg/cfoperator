"""Tooltips: one mechanism, and a sentence for every word the console shows (CFOP-707).

The pipeline switches on Remediations said ``feed``, ``drain``, ``reap``,
``verify`` and nothing else; what each stage does was written only in docs/.
The same was true of the statuses, classes, outcomes and decisions every page
is built from.

Two things are held here.

The mechanism: common.js presents any element's ``title`` in one themed
tooltip, and installs itself. It is driven under node with a small recording
DOM: shown on hover and on keyboard focus, the title taken off while shown and
put back after, nothing parsed as markup.

The vocabulary: the guard is for the class, not today's wording. A page's own
list of flags / statuses / classes / outcomes / decisions, and the backend's
list of flags, must each have a sentence in the glossary. Adding a stage to
the pipeline without saying what it does fails here.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
COMMON_JS = UI / "common.js"
PAGES = sorted(p.name for p in UI.glob("*.html") if p.name != "login.html")


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def _node(tmp_path_factory, name, stub, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = tmp_path_factory.mktemp(name) / "stub.js"
    path.write_text(stub, encoding="utf-8")
    out = subprocess.run([node, str(path), str(COMMON_JS), *args], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------

_GLOSSARY_STUB = r"""
const fs=require('fs'), vm=require('vm');
const box={console,JSON,Array,Object,String,Number,Date,Math,document:{documentElement:{}},
  getComputedStyle:()=>({getPropertyValue:()=>''})};
box.globalThis=box; vm.createContext(box); vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),box);
console.log(JSON.stringify({
  glossary: vm.runInContext('GLOSSARY', box),
  known: box.gloss('flag','queue_reap'), unknown: box.gloss('flag','queue_nope'),
  noKind: box.gloss('nope','x'), proto: box.gloss('flag','constructor'),
  attr: box.glossAttr('flag','queue_feed'), attrNone: box.glossAttr('flag','queue_nope'),
  badgeTip: box.badge('filed','--x','a "quoted" <tip>'), badgePlain: box.badge('filed','--x'),
}));
"""


@pytest.fixture(scope="module")
def vocab(tmp_path_factory):
    return _node(tmp_path_factory, "glossary", _GLOSSARY_STUB)


def _js_list(src, pattern):
    """The quoted strings inside the first match's group 1."""
    m = re.search(pattern, src, re.S)
    assert m, f"pattern not found: {pattern}"
    return re.findall(r"'([^']+)'", m.group(1))


def _flag_keys(page):
    m = re.search(r"const FLAG_LABEL\s*=\s*\{(.*?)\};", read(page), re.S)
    assert m, f"{page} has no FLAG_LABEL"
    return re.findall(r"(\w+):'", m.group(1))


def test_the_pages_were_found():
    """A wrong root makes every per-page check below pass over nothing."""
    assert {"remediations.html", "inbox.html", "investigations.html", "events.html"} <= set(PAGES)


@pytest.mark.parametrize("page", ["remediations.html", "inbox.html"])
def test_every_pipeline_stage_a_page_shows_says_what_it_does(vocab, page):
    keys = _flag_keys(page)
    assert len(keys) >= 9, f"{page}: FLAG_LABEL parsed to {keys}"
    missing = [k for k in keys if not vocab["glossary"]["flag"].get(k)]
    assert not missing, (
        f"{page} shows pipeline stage(s) {missing} with no sentence in GLOSSARY.flag (ui/common.js): "
        "the switch would be a bare word again")
    assert "glossAttr('flag',k)" in read(page), f"{page} draws its pipeline stages without their tip"


def test_every_flag_the_backend_takes_has_a_sentence(vocab):
    """The page's list is the console's; this is the agent's. A stage added to
    the pipeline has to say what it does before the console can toggle it."""
    src = (REPO_ROOT / "agent" / "agent.py").read_text(encoding="utf-8")
    backend = _js_list(src, r"_REMEDIATION_FLAGS\s*=\s*\((.*?)\)")
    assert {"queue_feed", "queue_drain", "queue_reap", "queue_verify"} <= set(backend), backend
    missing = [k for k in backend if not vocab["glossary"]["flag"].get(k)]
    assert not missing, f"agent.py accepts flag(s) {missing} that GLOSSARY.flag does not describe"


def test_every_remediation_status_and_class_has_a_sentence(vocab):
    page = read("remediations.html")
    statuses = _js_list(page, r"const STATUS\s*=\s*\[(.*?)\];")
    assert len(statuses) >= 10, statuses
    missing = [s for s in statuses if not vocab["glossary"]["remediation"].get(s)]
    assert not missing, f"remediation status(es) {missing} have no sentence in GLOSSARY.remediation"
    classes = _js_list(page, r"pickRow\('class',\[(.*?)\]")
    assert len(classes) >= 7, classes
    missing = [c for c in classes if not vocab["glossary"]["class"].get(c)]
    assert not missing, f"remediation class(es) {missing} have no sentence in GLOSSARY.class"


def test_every_investigation_outcome_and_verdict_has_a_sentence(vocab):
    outcomes = _js_list(read("investigations.html"), r"const OUTCOMES\s*=\s*\[(.*?)\];")
    assert len(outcomes) >= 8, outcomes
    missing = [o for o in outcomes if not vocab["glossary"]["outcome"].get(o)]
    assert not missing, f"investigation outcome(s) {missing} have no sentence in GLOSSARY.outcome"
    server = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    verdicts = _js_list(server, r"_INVESTIGATION_TRIAGE_ACTIONS\s*=\s*\((.*?)\)")
    assert verdicts, "no triage actions found in web_server.py"
    missing = [v for v in verdicts if not vocab["glossary"]["triage"].get(v)]
    assert not missing, f"operator verdict(s) {missing} have no sentence in GLOSSARY.triage"


def test_every_event_status_and_decision_has_a_sentence(vocab):
    page = read("events.html")
    statuses = _js_list(page, r"const STATUSES\s*=\s*\[(.*?)\];")
    decisions = _js_list(page, r"const ACTIONS\s*=\s*\[(.*?)\];")
    assert len(statuses) >= 7 and len(decisions) >= 6, (statuses, decisions)
    missing = [s for s in statuses if not vocab["glossary"]["event"].get(s)]
    assert not missing, f"event status(es) {missing} have no sentence in GLOSSARY.event"
    missing = [d for d in decisions if not vocab["glossary"]["decision"].get(d)]
    assert not missing, f"triage decision(s) {missing} have no sentence in GLOSSARY.decision"


def test_a_sentence_is_a_sentence(vocab):
    """Something to read in a tooltip: one or two sentences, ended, short
    enough for the 340px box."""
    seen = 0
    for kind, words in vocab["glossary"].items():
        for word, text in words.items():
            seen += 1
            assert text.strip() == text and text.endswith("."), f"{kind}.{word}: {text!r}"
            assert 12 <= len(text) <= 260, f"{kind}.{word} is {len(text)} characters"
    assert seen >= 45, f"the glossary shrank to {seen} entries"


def test_an_unknown_word_gets_no_tip_rather_than_a_wrong_one(vocab):
    assert vocab["known"].startswith("Recovers rows")
    assert vocab["unknown"] == "" and vocab["noKind"] == "" and vocab["attrNone"] == ""
    assert vocab["proto"] == "", "an inherited property name was read as a glossary entry"
    assert vocab["attr"].startswith(' title="Turns findings into queue rows')


def test_a_badge_carries_its_tip_escaped(vocab):
    assert 'title="a &quot;quoted&quot; &lt;tip&gt;"' in vocab["badgeTip"]
    assert "title=" not in vocab["badgePlain"]


@pytest.mark.parametrize("page,needles", [
    ("remediations.html", ["glossAttr('remediation',s)", "glossAttr('remediation',value)",
                           "gloss('remediation',r.status)", "glossAttr('class',r.remediation_class)"]),
    ("investigations.html", ["gloss('outcome',r.outcome)", "glossAttr('outcome',value)",
                             "gloss('triage',r.triage_action)", "glossAttr('remediation',m.status)"]),
    ("events.html", ["gloss('decision',r.action)", "gloss('event',r.status)", "glossAttr('event',value)"]),
    ("inbox.html", ["glossAttr('flag',k)", "gloss('remediation',m.status)", "gloss('class',r.remediation_class)"]),
])
def test_the_pages_draw_their_words_with_the_tip(page, needles):
    html = read(page)
    for needle in needles:
        assert needle in html, f"{page} no longer passes {needle} to what it draws"


def test_the_switch_itself_carries_the_tip():
    """The operator's example. The tip is on the control that is hovered and
    focused, not on a label beside it."""
    page = read("remediations.html")
    switch = re.search(r'<button type="button" role="switch"[^>]*>', page).group(0)
    assert "${glossAttr('flag',k)}" in switch


# --------------------------------------------------------------------------
# the mechanism, under node
# --------------------------------------------------------------------------

_TIP_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
const on={}, timers=[], intervals=[], appended=[]; let under=null, now=1e6;
function node(tag, title, text, o){ o=o||{};
  const e={tagName:tag, attrs:{}, style:{}, hidden:false, textContent:'', innerHTML:'', isConnected:true,
    innerText:text, scrollWidth:o.scrollWidth||100, clientWidth:o.clientWidth||100, offsetWidth:120, offsetHeight:30,
    setAttribute(k,v){ this.attrs[k]=String(v); }, getAttribute(k){ return k in this.attrs?this.attrs[k]:null; },
    hasAttribute(k){ return k in this.attrs; }, removeAttribute(k){ delete this.attrs[k]; },
    closest(sel){ return (sel==='[title]' && 'title' in this.attrs)?this:(o.parent?o.parent.closest(sel):null); },
    contains(n){ for(let p=n; p; p=p.parentOf) if(p===this) return true; return false; }, parentOf:o.parent||null,
    matches(sel){ return sel===':focus-visible' && !!o.keyboard; },
    getBoundingClientRect(){ return o.rect||{left:300,top:100,right:400,bottom:130,width:100,height:30}; }};
  if(title!=null) e.attrs.title=title;
  return e; }
const doc={documentElement:{}, body:{appendChild(n){ appended.push(n); }},
  addEventListener(t,f){ (on[t]=on[t]||[]).push(f); },
  createElement:t=>node(t.toUpperCase(), null, ''), elementFromPoint:()=>under};
const box={console,JSON,Array,Object,String,Number,Math,
  Date:{now:()=>now}, window:{innerWidth:1200, innerHeight:800}, document:doc,
  setTimeout:(f,ms)=>{ timers.push({f,ms}); return timers.length; }, clearTimeout:i=>{ if(timers[i-1]) timers[i-1].f=null; },
  setInterval:(f,ms)=>{ intervals.push({f,ms}); return intervals.length; }, clearInterval:i=>{ if(intervals[i-1]) intervals[i-1].f=null; }};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
const fire=(t,e)=>(on[t]||[]).forEach(f=>f(e||{}));
const runTimers=()=>{ const t=timers.splice(0); t.forEach(x=>x.f&&x.f()); };
const tick=()=>intervals.forEach(x=>x.f&&x.f());
const tip=()=>appended[appended.length-1];
const shown=()=>!!(tip() && tip().hidden===false);
const out={listeners:Object.keys(on).sort()};

// hover: waits, then shows; the title is off the anchor while it is up
const sw=node('BUTTON','Recovers rows whose executor died.','reap on');
fire('mouseover',{target:sw, clientX:350, clientY:115});
out.waited=timers.length===1 && timers[0].ms>0 && !shown();
runTimers();
out.shown=shown(); out.text=tip().textContent; out.role=tip().attrs.role; out.id=tip().id;
out.titleWhileShown='title' in sw.attrs; out.describedBy=sw.attrs['aria-describedby'];
out.left=tip().style.left; out.top=tip().style.top; out.markup=tip().innerHTML;
// still on it (a child): nothing changes
const child=node('SPAN',null,'reap',{parent:sw});
fire('mouseover',{target:child}); out.childKeeps=shown() && timers.length===0;
// something titled inside the anchor is nearer, and wins
const inner=node('SPAN','Its own tip.','filed',{parent:sw});
fire('mouseover',{target:inner}); runTimers();
out.nestedWins=shown() && tip().textContent==='Its own tip.' && sw.attrs.title==='Recovers rows whose executor died.';
fire('mouseover',{target:sw}); runTimers();
// leaving puts everything back
const blank=node('DIV',null,'');
fire('mouseover',{target:blank});
out.hiddenAfterLeave=!shown(); out.titleRestored=sw.attrs.title; out.describedByGone=!('aria-describedby' in sw.attrs);
// straight on to the next tip: no wait
const sw2=node('BUTTON','Follows open pull requests.','verify on');
fire('mouseover',{target:sw2});
out.warmWait=timers[0].ms; runTimers(); out.warmShown=shown() && tip().textContent==='Follows open pull requests.';
// a key press ends it
fire('keydown',{key:'a'}); out.keyHides=!shown() && sw2.attrs.title==='Follows open pull requests.';
// leaving before the wait is over shows nothing
now+=5000; fire('mouseover',{target:sw}); fire('mouseover',{target:blank}); runTimers();
out.cancelled=!shown();

// a title that only repeats the visible text is not a tip...
now+=5000; const echo=node('A','Remediations','  remediations ');
fire('mouseover',{target:echo}); out.echoQueued=timers.length; runTimers(); out.echoShown=shown();
// ...unless the text is cut short, where it is the rest of the value
now+=5000; const cut=node('TD','a-long-host-name.example','a-long-host-name.example',{scrollWidth:300,clientWidth:120});
fire('mouseover',{target:cut}); runTimers(); out.cutShown=shown(); fire('mouseover',{target:blank});
// a frame's title names it; it is not a tip
now+=5000; const frame=node('IFRAME','CFOperator chat','');
fire('mouseover',{target:frame}); runTimers(); out.frameShown=shown();
// an element without a title's own, inside one that has it
now+=5000; const th=node('TH','What kind of change the fix is.','Class · risk');
const sortBtn=node('BUTTON',null,'Class · risk',{parent:th, keyboard:true});
fire('focusin',{target:sortBtn}); out.focusShown=shown() && tip().textContent==='What kind of change the fix is.';
out.focusImmediate=timers.length===0;
fire('focusout',{}); out.blurHides=!shown() && th.attrs.title==='What kind of change the fix is.';
// focus a mouse click left behind is not a reason to show one
const clicked=node('BUTTON','Close this row as done.','Resolve',{keyboard:false});
fire('focusin',{target:clicked}); out.mouseFocusShown=shown();
// model-written text is text
now+=5000; const host=node('TD','<img src=x onerror=alert(1)>','x');
fire('mouseover',{target:host}); runTimers();
out.unsafeText=tip().textContent; out.unsafeMarkup=tip().innerHTML; fire('mouseover',{target:blank});

// the poll replaced the anchor under the pointer: same words, the tip stays
now+=5000; const a1=node('BUTTON','Claims queued rows.','drain');
fire('mouseover',{target:a1, clientX:10, clientY:10}); runTimers();
a1.isConnected=false; const a2=node('BUTTON','Claims queued rows.','drain'); under=a2;
tick(); out.reanchored=shown() && !('title' in a2.attrs) && a2.attrs['aria-describedby']==='cfop-tip';
// ...different words, it goes
a2.isConnected=false; under=node('BUTTON','Something else.','other');
tick(); out.goneWhenDifferent=!shown();
// the page rewrote the title of what is being hovered (a timestamp's age)
now+=5000; const ts=node('SPAN','3m ago · 2026-11-15','11:08'); under=null;
fire('mouseover',{target:ts}); runTimers(); ts.attrs.title='4m ago · 2026-11-15'; tick();
out.rewritten=tip().textContent; out.rewrittenTitleOff=!('title' in ts.attrs);
// scrolling the page ends it
fire('scroll',{}); out.scrollHides=!shown() && ts.attrs.title==='4m ago · 2026-11-15';
out.tipElements=appended.length;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def tips(tmp_path_factory):
    return _node(tmp_path_factory, "tips", _TIP_STUB)


def test_the_tooltip_installs_itself(tips):
    """No page calls anything: a page that forgot a bindTips() call would
    keep the slow native tooltip and look fine."""
    for event in ("mouseover", "mouseout", "focusin", "focusout", "keydown", "mousedown", "scroll"):
        assert event in tips["listeners"], f"common.js registered no {event} listener at load"
    for page in PAGES:
        assert "bindTips(" not in read(page), f"{page} binds the tooltip itself; common.js already did"


def test_loading_common_js_without_a_dom_does_not_throw(tmp_path_factory):
    """The node harnesses load it with a bare document; so does anything else
    that only wants esc()."""
    out = _node(tmp_path_factory, "bare", r"""
const fs=require('fs'), vm=require('vm');
const box={console,JSON,Array,Object,String,Number,Date,Math,document:{documentElement:{}}};
box.globalThis=box; vm.createContext(box); vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),box);
const none={console,JSON,Array,Object,String,Number,Date,Math};
none.globalThis=none; vm.createContext(none); vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),none);
console.log(JSON.stringify({ok: box.esc('<')==='&lt;' && none.esc('<')==='&lt;'}));
""")
    assert out["ok"]


def test_hover_waits_then_shows_the_title(tips):
    assert tips["waited"], "the tip appeared with no delay, or not at all"
    assert tips["shown"] and tips["text"] == "Recovers rows whose executor died."
    assert tips["role"] == "tooltip" and tips["id"] == "cfop-tip"
    assert tips["left"].endswith("px") and tips["top"].endswith("px")


def test_the_title_is_off_the_anchor_only_while_the_tip_is_up(tips):
    """Otherwise the browser shows its own tooltip as well. At rest the DOM
    is exactly what the page wrote."""
    assert tips["titleWhileShown"] is False
    assert tips["describedBy"] == "cfop-tip", "with the title off, nothing describes the control"
    assert tips["childKeeps"], (
        "moving onto a child of the anchor lost the tip: with its title off, the anchor "
        "has to be recognised by being the anchor")
    assert tips["nestedWins"], "something titled inside the anchor did not get its own tip"
    assert tips["hiddenAfterLeave"]
    assert tips["titleRestored"] == "Recovers rows whose executor died."
    assert tips["describedByGone"]


def test_moving_to_the_next_tip_does_not_wait_again(tips):
    assert tips["warmWait"] == 0 and tips["warmShown"]


def test_acting_on_the_control_ends_the_tip(tips):
    assert tips["keyHides"] and tips["scrollHides"]
    assert tips["cancelled"], "a tip appeared for a control the pointer had already left"


def test_a_title_that_repeats_the_visible_text_is_not_shown(tips):
    """The rail's links carry their label as a title for the icons-only rail.
    On the wide rail that would be an echo."""
    assert tips["echoQueued"] == 1 and tips["echoShown"] is False
    assert tips["cutShown"], "a truncated cell lost the tip that shows the rest of its value"


def test_a_frame_s_title_is_not_a_tip(tips):
    assert tips["frameShown"] is False


def test_keyboard_focus_shows_it_at_once_and_a_click_s_focus_does_not(tips):
    assert tips["focusShown"] and tips["focusImmediate"]
    assert tips["blurHides"]
    assert tips["mouseFocusShown"] is False


def test_a_title_is_never_parsed_as_markup(tips):
    """Some titles are what a model wrote: a host, a trigger."""
    assert tips["unsafeText"] == "<img src=x onerror=alert(1)>"
    assert tips["unsafeMarkup"] == "" and tips["markup"] == ""
    assert "innerHTML" not in re.search(r"function tipShow\(.*?\n\}", read("common.js"), re.S).group(0)


def test_a_repaint_under_the_pointer_keeps_the_tip_if_it_says_the_same(tips):
    """The lists and the switches repaint by innerHTML on a poll."""
    assert tips["reanchored"]
    assert tips["goneWhenDifferent"]


def test_a_title_rewritten_while_shown_is_followed(tips):
    assert tips["rewritten"] == "4m ago · 2026-11-15" and tips["rewrittenTitleOff"]


def test_there_is_one_tooltip_element(tips):
    assert tips["tipElements"] == 1


def test_no_page_grows_its_own_tooltip():
    for page in PAGES:
        html = read(page)
        assert 'role="tooltip"' not in html and "data-tip" not in html and "data-tooltip" not in html, (
            f"{page} has a tooltip of its own; write title=\"...\" and common.js presents it")


def test_the_tooltip_is_styled_once_above_everything_and_takes_no_clicks():
    css = read("console.css")
    rule = re.search(r"\.cfop-tip \{[^}]*\}", css).group(0)
    assert "position:fixed" in rule and "pointer-events:none" in rule
    z = int(re.search(r"z-index:(\d+)", rule).group(1))
    others = [int(n) for n in re.findall(r"z-index:(\d+)", css.replace(rule, ""))]
    assert z > max(others), "something in the stylesheet can cover the tooltip"
