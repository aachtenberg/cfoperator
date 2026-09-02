"""Keyboard operability of the console list pages (CFOP-94).

investigations.html and remediations.html were mouse-only: rows opened on
click but could not be reached by Tab, the filter chips were ``<span
onclick>``, and the detail drawer was a div with no dialog role that neither
took focus when it opened nor gave it back when it closed. Esc worked and
nav.js carried aria-current, so the baseline was not zero — this is the gap
between that baseline and being able to work the page without a mouse.

The guards are about a *class* of regression rather than today's markup: a
third list page, or a refactor that re-renders a chip as a span because it is
shorter, should fail here rather than be noticed by the next keyboard user.
The static half is a grep in the style of ``test_console_nav.py``; the
behavioural half runs each page's inline script under node, the way
``test_console_drawer.py`` does, because focus moving in and back out is a
round-trip a grep cannot see.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = REPO_ROOT
UI = REPO_ROOT / "ui"

#: Pages whose main content is a row list plus a slide-in detail drawer. A new
#: one belongs here; the guards below are the price of a drawer.
LIST_PAGES = ["investigations.html", "remediations.html"]


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def inline_script(name):
    blocks = re.findall(r"<script>(.*?)</script>", read(name), re.S)
    assert len(blocks) == 1, f"{name} has {len(blocks)} inline scripts, expected 1"
    return blocks[0]


# --------------------------------------------------------------------------
# controls are real controls
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", LIST_PAGES)
def test_chips_are_buttons_not_spans(page):
    """A span with a click handler is invisible to Tab. account.html and
    admin.html already render chips as buttons; the list pages must too."""
    assert not re.search(r'<span class="chip[ "]', read(page)), (
        f"{page} renders a chip as <span> — use <button type=\"button\" "
        "class=\"chip\"> so it is reachable by keyboard")


@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_close_control_is_a_button(page):
    html = read(page)
    assert '<span class="x"' not in html, f"{page} closes the drawer with a <span>"
    assert re.search(r'<button[^>]*class="x"[^>]*aria-label=', html), (
        f"{page} has no <button class=\"x\" aria-label=…> close control — the "
        "× glyph alone is not a name")


@pytest.mark.parametrize("page", LIST_PAGES)
def test_no_click_handler_on_a_non_control(page):
    """The general form of the two guards above. The only exception is the
    backdrop: a click-to-dismiss surface with no keyboard equivalent needed,
    because Esc and the close button are that equivalent."""
    html = read(page)
    for tag in re.finditer(r"<(span|div)\b[^>]*\bonclick=", html):
        assert 'id="backdrop"' in tag.group(0), (
            f"{page}: click handler on a <{tag.group(1)}> — {tag.group(0)[:80]}")


# --------------------------------------------------------------------------
# rows open from the keyboard
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", LIST_PAGES)
def test_clickable_rows_are_focusable_and_keyed(page):
    """Every ``<tr onclick>`` must also be reachable (tabindex) and operable
    (a key handler, or be a link). A row that only opens on click is the
    original defect."""
    rows = re.findall(r"<tr\b[^>]*\bonclick=[^>]*>", inline_script(page))
    assert rows, f"{page} renders no clickable rows — did the list move?"
    for tr in rows:
        assert "tabindex=" in tr, f"{page}: row is not focusable — {tr[:80]}"
        assert "onkeydown=" in tr, f"{page}: row has no key handler — {tr[:80]}"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_row_key_handler_takes_enter_and_space(page):
    """Enter and Space are what a button answers to; a row standing in for one
    should answer to both, and Space must not scroll the page."""
    js = inline_script(page)
    m = re.search(r"function rowKey\((.*?)\n}", js, re.S)
    assert m, f"{page} has no rowKey handler"
    body = m.group(0)
    assert "'Enter'" in body and "' '" in body, f"{page}: rowKey ignores Enter or Space"
    assert "preventDefault" in body, f"{page}: Space on a row would scroll the page"


# --------------------------------------------------------------------------
# the drawer is a dialog
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_drawer_is_a_dialog(page):
    m = re.search(r'<div id="detail"[^>]*>', read(page))
    assert m, f"{page} has no #detail drawer"
    tag = m.group(0)
    assert 'role="dialog"' in tag, f"{page}: #detail has no dialog role"
    assert 'aria-modal="true"' in tag, f"{page}: #detail is not aria-modal"
    assert "aria-labelledby=" in tag or "aria-label=" in tag, (
        f"{page}: the dialog has no accessible name")


@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_closed_drawer_is_out_of_the_tab_order(page):
    """The drawer is translated off-screen when closed, not removed: without
    ``inert`` its buttons are still Tab stops you cannot see."""
    assert re.search(r'<div id="detail"[^>]*\binert\b', read(page)), (
        f"{page}: #detail does not start inert")
    js = inline_script(page)
    assert "inert=false" in js.replace(" ", ""), f"{page}: opening never lifts inert"
    assert "inert=true" in js.replace(" ", ""), f"{page}: closing never restores inert"


# --------------------------------------------------------------------------
# focus moves in on open and back out on close — for real, under node
# --------------------------------------------------------------------------

# The stub tracks focus: every element records focus() calls, and
# document.activeElement follows the last one focused, so the script's own
# activeElement read sees the row the harness "tabbed" to.
_STUB = r"""
const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const api=process.argv[3];   // '/api/investigations' or '/api/remediations'

const focusLog=[];
const doc={documentElement:{},body:{appendChild(){}},addEventListener(){},activeElement:null};
function el(id){const e={id:id||'',className:'',innerHTML:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},select(){},remove(){},
  scrollIntoView(){},appendChild(){},addEventListener(){},isConnected:true,
  focus(){ focusLog.push(this.id); doc.activeElement=this; }};
  return e;}
const els={};
doc.createElement=()=>el('');
doc.getElementById=id=>(els[id]=els[id]||el(id));
const loc={pathname:'/x',search:'',hash:'',href:'http://cfop/x'};
const hist={replaceState(state,title,url){ loc.hash=(typeof url==='string'&&url.startsWith('#'))?url:''; }};

const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  location:loc, history:hist, document:doc, navigator:{},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  fetch:(url)=>{
    if(url.indexOf(api+'/')===0){
      const id=Number(url.split('/').pop());
      return Promise.resolve({ok:true,json:()=>Promise.resolve({id:id,trigger:'t',outcome:'monitoring',
        status:'queued',risk:'low',remediation_class:'gitops-patch',host_id:'h',payload:{},
        attach_command:'cfassist attach '+id,findings:{}})});
    }
    if(url.indexOf(api)===0) return Promise.resolve({json:()=>Promise.resolve({investigations:[],remediations:[]})});
    return Promise.resolve({ok:true,json:()=>Promise.resolve({})});
  }};
box.window={location:loc,history:hist,addEventListener(){},removeEventListener(){},
  CFOP:{me:()=>Promise.resolve({role:'admin'})}};
// The helpers the page calls (esc, badge, toast, trapFocus) live in ui/common.js
// since CFOP-95; the browser loads it before the page script, so the harness does.
const common=fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'common.js'),'utf8');
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
(async () => {
  await tick(); await tick();
  const out={};

  // Esc with nothing open: no focus movement at all.
  box.closeDetail();
  out.focusedOnIdleClose=focusLog.slice();

  // Tab to a row, press Enter: the drawer takes focus at its close button.
  const row=doc.getElementById('row-7'); row.focus(); focusLog.length=0;
  await box.detail(7);
  out.drawerInertAfterOpen=doc.getElementById('detail').inert;
  out.focusedAfterOpen=focusLog.slice();

  // Close: focus goes back to the row that opened it.
  focusLog.length=0;
  box.closeDetail();
  out.drawerInertAfterClose=doc.getElementById('detail').inert;
  out.focusedAfterClose=focusLog.slice();

  // The poll replaced the row while the drawer was open: the same id is the
  // same row, and focus still lands on it.
  row.focus(); await box.detail(7);
  row.isConnected=false; els['row-7']=el('row-7');
  focusLog.length=0; box.closeDetail();
  out.focusedAfterCloseWhenRowReplaced=focusLog.slice();

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""

_API = {"investigations.html": "/api/investigations",
        "remediations.html": "/api/remediations"}


@pytest.fixture(scope="module", params=LIST_PAGES)
def focus_behaviour(request, tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    page = request.param
    stub = tmp_path_factory.mktemp("a11y") / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / page), _API[page]],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"{page}: {out.stderr}"
    return page, json.loads(out.stdout)


def test_opening_a_row_moves_focus_to_the_close_button(focus_behaviour):
    page, b = focus_behaviour
    assert b["focusedAfterOpen"] == ["detail-close"], (
        f"{page}: opening focused {b['focusedAfterOpen']}, expected the close button")
    assert b["drawerInertAfterOpen"] is False, f"{page}: the open drawer is still inert"


def test_closing_returns_focus_to_the_row_that_opened_it(focus_behaviour):
    page, b = focus_behaviour
    assert b["focusedAfterClose"] == ["row-7"], (
        f"{page}: closing focused {b['focusedAfterClose']}, expected the opener")
    assert b["drawerInertAfterClose"] is True, f"{page}: the closed drawer is not inert"


def test_closing_finds_the_row_again_after_a_repaint(focus_behaviour):
    """The list repaints on a timer by replacing innerHTML; the element that
    opened the drawer may be gone by the time it closes."""
    page, b = focus_behaviour
    assert b["focusedAfterCloseWhenRowReplaced"] == ["row-7"], (
        f"{page}: focus went to {b['focusedAfterCloseWhenRowReplaced']} after the row was replaced")


def test_escape_with_nothing_open_does_not_move_focus(focus_behaviour):
    """closeDetail runs on every Escape keypress, drawer or no drawer."""
    page, b = focus_behaviour
    assert b["focusedOnIdleClose"] == [], f"{page}: an idle close moved focus"


# --------------------------------------------------------------------------
# Tab stays inside the open dialog (CFOP-95, deferred from CFOP-94)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_drawer_installs_the_focus_trap(page):
    """aria-modal is a promise to assistive tech, not a mechanism: without the
    trap, Tab from the last control walks out into the page behind the
    backdrop. One helper in common.js, installed on the drawer by each page."""
    assert re.search(r"trapFocus\(document\.getElementById\('detail'\)\)", inline_script(page)), (
        f"{page} does not install trapFocus on #detail")


# A dialog with three controls and a document whose activeElement the harness
# sets by hand. Each case fires one keydown and reports where focus went and
# whether the default (the browser's own Tab) was cancelled.
_TRAP_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
const focusLog=[];
function el(id){ return {id, hidden:false, focus(){ focusLog.push(id); doc.activeElement=this; }}; }
const close=el('close'), input=el('input'), resolve=el('resolve'), outside=el('outside');
const doc={documentElement:{}, activeElement:null};
let handler=null;
const dialog={addEventListener(type,fn){ if(type==='keydown') handler=fn; },
  querySelectorAll(){ return [close,input,resolve]; }};
const box={console,JSON,Array,Object,document:doc,
  getComputedStyle:()=>({getPropertyValue:()=>''})};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
box.trapFocus(dialog);
function press(from, shift, consumed){
  doc.activeElement=from; focusLog.length=0;
  const e={key:'Tab', shiftKey:!!shift, defaultPrevented:!!consumed, preventDefault(){ this.defaultPrevented=true; }};
  handler(e);
  return {focused:focusLog.slice(), prevented:e.defaultPrevented};
}
const out={
  installed: typeof handler==='function',
  tabFromLast: press(resolve,false),
  tabFromMiddle: press(input,false),
  shiftTabFromFirst: press(close,true),
  shiftTabFromMiddle: press(input,true),
  tabFromOutside: press(outside,false),
  consumedTab: press(resolve,false,true),
};
doc.activeElement=resolve; focusLog.length=0;
const other={key:'Escape', shiftKey:false, defaultPrevented:false, preventDefault(){ this.defaultPrevented=true; }};
handler(other);
out.otherKey={focused:focusLog.slice(), prevented:other.defaultPrevented};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def trap(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    stub = tmp_path_factory.mktemp("trap") / "stub.js"
    stub.write_text(_TRAP_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(UI / "common.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_trap_wraps_at_both_ends(trap):
    assert trap["installed"]
    assert trap["tabFromLast"] == {"focused": ["close"], "prevented": True}
    assert trap["shiftTabFromFirst"] == {"focused": ["resolve"], "prevented": True}


def test_the_trap_leaves_the_middle_alone(trap):
    """Between the ends the browser's own Tab order is right; intercepting it
    would be a second, worse implementation of the same thing."""
    assert trap["tabFromMiddle"] == {"focused": [], "prevented": False}
    assert trap["shiftTabFromMiddle"] == {"focused": [], "prevented": False}


def test_the_trap_pulls_focus_in_from_outside(trap):
    """Focus that ended up behind the backdrop (a repaint, a script) is
    brought back to the dialog on the next Tab rather than wandering."""
    assert trap["tabFromOutside"] == {"focused": ["close"], "prevented": True}


def test_a_tab_something_inside_already_consumed_is_left_alone(trap):
    """The investigations drawer holds an xterm terminal; a Tab it handed to
    the shell has preventDefault called on it and must not be re-routed."""
    assert trap["consumedTab"] == {"focused": [], "prevented": True}


def test_the_trap_ignores_other_keys(trap):
    assert trap["otherKey"] == {"focused": [], "prevented": False}
