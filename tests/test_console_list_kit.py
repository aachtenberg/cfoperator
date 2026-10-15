"""The console list kit (CFOP-412): sortable headers, readable timestamps and
a search box, shared by every list page.

The three list pages showed relative age only ("3h"), no column could be
sorted, and only Events had a search box — in the operator's words, the lists
show no timestamps and cannot be sorted. The fix is one kit in ``ui/common.js``
(``listKit``, ``sortRows``, ``fmtTs``, ``textMatch``) that each page wires its
headers and its paint() into.

These guards are about the class of regression, in the style of
``test_console_nav.py``. A list page is found by its *shape* — a ``#rows``
tbody plus a ``#detail`` dialog — not by name, so a fourth page built the same
way is held to the same contract rather than quietly shipping without sorting
or timestamps. The behavioural half runs the kit on its own and then each
page's inline script under node, the way ``test_console_drawer.py`` does,
because "a header click reorders the rows and the next poll keeps that order"
is a round-trip a grep cannot see. Fails closed without node, like
``test_console_js``: a skip would be a green run that tested nothing.
"""

from repo_paths import REPO_ROOT
import json
import os
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"
COMMON_JS = UI / "common.js"

#: A list page: a polled row list with a slide-in drawer. Derived from the
#: markup rather than listed, so a new page of that shape is covered the day
#: it exists.
LIST_PAGES = sorted(
    p.name for p in UI.glob("*.html")
    if '<tbody id="rows">' in p.read_text(encoding="utf-8")
    and 'id="detail"' in p.read_text(encoding="utf-8"))

#: The rows' timestamp column on each page, by sort key, and which API the
#: page reads. A new list page needs an entry here for the behavioural half.
PAGES = {
    "investigations.html": {"api": "/api/investigations", "key": "investigations", "time": "started", "search": True},
    "remediations.html": {"api": "/api/remediations", "key": "remediations", "time": "created", "search": True},
    # Events searches on the server (q in the query string), not in the kit.
    "events.html": {"api": "/api/events", "key": "alerts", "time": "when", "search": False},
}


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def inline_script(name):
    blocks = re.findall(r"<script>(.*?)</script>", read(name), re.S)
    assert len(blocks) == 1, f"{name} has {len(blocks)} inline scripts, expected 1"
    return blocks[0]


def node_bin():
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the list kit; skipping would pass while not guarding")
    return node


def run_node(stub_text, tmp_path, *args):
    stub = tmp_path / "stub.js"
    stub.write_text(stub_text, encoding="utf-8")
    # A fixed zone so fmtTs() renders the same wall-clock time on every box.
    env = dict(os.environ, TZ="UTC")
    out = subprocess.run([node_bin(), str(stub), *map(str, args)],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# the set itself
# --------------------------------------------------------------------------

def test_the_list_pages_are_found_by_shape():
    """The derivation must find the three that exist, or every guard below
    is vacuous; and the behavioural table must know each of them."""
    assert {"investigations.html", "remediations.html", "events.html"} <= set(LIST_PAGES)
    assert set(LIST_PAGES) <= set(PAGES), f"add {set(LIST_PAGES) - set(PAGES)} to PAGES"


# --------------------------------------------------------------------------
# every list page uses the kit — wiring, not rendering
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_page_wires_its_headers_into_the_kit(page):
    js = inline_script(page)
    assert re.search(r"\blistKit\(\{", js), f"{page} never calls listKit()"
    assert "KIT.sort(" in js, f"{page} paints rows without KIT.sort() — a sort would not survive a repaint"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_every_header_row_has_sortable_columns(page):
    """Each thead — remediations has three — carries data-sort headers. Which
    key each names is checked under node, where listKit() refuses one the
    page did not define."""
    heads = re.findall(r"<thead>(.*?)</thead>", read(page), re.S)
    assert heads, f"{page} has no <thead>"
    for h in heads:
        assert re.search(r'<th data-sort="\w+">', h), f"{page}: a header row has no sortable column"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_the_page_has_a_search_box(page):
    """Same id, type and labelling on every page: Events had one, the others
    did not. A search box is reachable by label, not placeholder."""
    m = re.search(r'<input[^>]*\bid="f-q"[^>]*>', read(page))
    assert m, f"{page} has no #f-q search box"
    assert 'type="search"' in m.group(0), f"{page}: #f-q is not type=search"
    assert "aria-label=" in m.group(0), f"{page}: #f-q has no accessible name"


@pytest.mark.parametrize("page", LIST_PAGES)
def test_timestamps_go_through_fmtts(page):
    """The defect: lists showed age only, drawers printed the server's raw
    string with microseconds. Every stamp a page shows comes from fmtTs(),
    and no *_at field reaches the page as text."""
    js = inline_script(page)
    assert "fmtTs(" in js, f"{page} shows no fmtTs() stamp"
    raw = re.search(r"esc\(\s*[\w.]+_at\b[^)]*\)", js)
    assert not raw, f"{page} prints a raw server stamp: {raw.group(0)}"
    bare = re.search(r"\$\{\s*[\w.]+_at\s*\}", js)
    assert not bare, f"{page} prints a raw server stamp: {bare.group(0)}"
    assert not re.search(r"(?<![\w.])age\(", js), f"{page} calls age() itself — the age is fmtTs()'s hover title"


# --------------------------------------------------------------------------
# the kit on its own, under node
# --------------------------------------------------------------------------

_KIT_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
// A header cell: records attributes, its handlers, and the arrow span the kit paints.
function th(key,label){ return {attrs:{'data-sort':key},textContent:label,innerHTML:'',handlers:{},arrow:{textContent:''},
  getAttribute(n){ return n in this.attrs?this.attrs[n]:null; }, setAttribute(n,v){ this.attrs[n]=String(v); },
  removeAttribute(n){ delete this.attrs[n]; }, addEventListener(t,f){ this.handlers[t]=f; },
  querySelector(sel){ return sel==='.arrow'?this.arrow:null; }, click(){ this.handlers.click(); }}; }
// Two tables sharing a header set (the remediations shape): 'id' appears twice.
let heads=[th('id','ID'),th('when','<When>'),th('id','ID')];
let stored={}, styles=0;
const aged={attrs:{'data-age':'2026-10-15T11:04:51.105391'},title:'',getAttribute(n){ return this.attrs[n]; }};
const box={console,JSON,Math,Date,Number,String,Array,Object,Intl,
  localStorage:{getItem:k=>(k in stored?stored[k]:null), setItem(k,v){ stored[k]=String(v); }},
  document:{documentElement:{}, head:{appendChild(){ styles++; }}, createElement:()=>({}),
    querySelectorAll:sel=>sel==='th[data-sort]'?heads:(sel==='[data-age]'?[aged]:[])},
  getComputedStyle:()=>({getPropertyValue:()=>''})};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
const out={};

// sortRows: pure.
const rows=[{id:3,n:'b10',t:null},{id:1,n:'B9',t:'2026-10-15T10:00:00Z'},{id:2,n:'a1',t:'2026-10-15T12:00:00+00:00'},{id:4,n:'',t:'2026-10-15T11:00:00'}];
const keys={id:r=>r.id, n:r=>r.n, t:r=>box.tsKey(r.t)};
const ids=rs=>rs.map(r=>r.id);
out.numAsc=ids(box.sortRows(rows,keys,{key:'id',dir:'asc'}));
out.numDesc=ids(box.sortRows(rows,keys,{key:'id',dir:'desc'}));
out.strAsc=ids(box.sortRows(rows,keys,{key:'n',dir:'asc'}));     // a1, B9, b10, then the blank
out.strDesc=ids(box.sortRows(rows,keys,{key:'n',dir:'desc'}));   // b10, B9, a1, then the blank
out.tsAsc=ids(box.sortRows(rows,keys,{key:'t',dir:'asc'}));      // zoned and bare stamps by instant; null last
out.tsDesc=ids(box.sortRows(rows,keys,{key:'t',dir:'desc'}));
out.noKey=ids(box.sortRows(rows,keys,{key:null,dir:'desc'}));
out.tiesKeepOrder=ids(box.sortRows([{id:9,s:'x'},{id:7,s:'x'},{id:8,s:'x'}],{s:r=>r.s},{key:'s',dir:'asc'}));
out.inputUntouched=ids(rows);

// fmtTs: one format, three stamp shapes, TZ=UTC.
out.bare=box.fmtTs('2026-10-15T11:04:51.105391');
out.bareSeconds=box.fmtTs('2026-10-15T11:04:51.105391',{seconds:true});
out.zoned=box.fmtTs('2026-10-15T13:04:51+02:00');
out.spaced=box.fmtTs('2026-10-15 11:04:51');
out.sameDay=box.fmtTs('2026-10-15T11:09:00Z',{seconds:true,sameDayAs:'2026-10-15T11:04:51.105391'});
out.otherDay=box.fmtTs('2026-10-16T11:09:00Z',{seconds:true,sameDayAs:'2026-10-15T11:04:51.105391'});
out.garbage=box.fmtTs('<soon>');
out.empty=box.fmtTs('')+box.fmtTs(null);
box.refreshAges(); out.refreshedTitle=aged.title;

// textMatch: words, any field, case-insensitive.
out.matchWords=box.textMatch('pi4 DISK',['raspberrypi4','disk full on /']);
out.matchMissingWord=box.textMatch('pi4 disk',['raspberrypi4']);
out.matchEmpty=box.textMatch('',['x']) && box.textMatch('  ',[]);
out.matchNulls=box.textMatch('y',[null,undefined,'y']);

// listKit: state, headers, memory.
let changes=0;
const kit=box.listKit({page:'t', initial:{key:'when',dir:'desc'}, dir:{when:'desc'},
  keys:{id:r=>r.id, when:r=>r.w}, onChange:()=>changes++});
out.initial=kit.state;
out.headerMarkup=heads[1].innerHTML;
out.ariaInitial=heads.map(h=>h.attrs['aria-sort']||null);
out.arrowInitial=heads.map(h=>h.arrow.textContent);
out.storedBeforeClick=Object.keys(stored);
heads[0].click();
out.afterClick=kit.state;
out.ariaAfterClick=heads.map(h=>h.attrs['aria-sort']||null);   // both 'id' headers, not just the one clicked
out.arrowAfterClick=heads.map(h=>h.arrow.textContent);
out.stored=JSON.parse(stored['cfop.sort.t']);
out.changes=changes;
heads[2].click();
out.afterSecondClick=kit.state;                                   // same column: reversed
heads[1].click();
out.afterWhen=kit.state;                                          // a time column opens newest first
out.sorted=kit.sort([{id:1,w:'2026-10-15T10:00:00Z'},{id:2,w:'2026-10-15T12:00:00Z'}]).map(r=>r.id);
out.stylesInjectedOnce=styles;

// A remembered choice is honoured; a remembered key the page no longer has is not.
stored['cfop.sort.t']=JSON.stringify({key:'id',dir:'asc'});
heads=[th('id','ID'),th('when','When')];
out.remembered=box.listKit({page:'t', initial:{key:'when',dir:'desc'}, keys:{id:r=>r.id, when:r=>r.w}}).state;
stored['cfop.sort.t']=JSON.stringify({key:'gone',dir:'asc'});
out.forgotten=box.listKit({page:'t', initial:{key:'when',dir:'desc'}, keys:{id:r=>r.id, when:r=>r.w}}).state;
stored['cfop.sort.t']='not json';
out.unreadable=box.listKit({page:'t', initial:{key:'when',dir:'desc'}, keys:{id:r=>r.id, when:r=>r.w}}).state;

// A header naming a key the page did not define fails at load, loudly.
heads=[th('typo','Typo')];
try{ box.listKit({page:'t', keys:{id:r=>r.id}}); out.unknownKeyThrows=false; }
catch(e){ out.unknownKeyThrows=String(e.message); }
// No DOM at all (the other harnesses): the kit still sorts.
delete box.document.querySelectorAll; delete box.localStorage;
out.headless=box.listKit({page:'h', initial:{key:'id',dir:'asc'}, keys:{id:r=>r.id}}).sort([{id:2},{id:1}]).map(r=>r.id);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    return run_node(_KIT_STUB, tmp_path_factory.mktemp("kit"), COMMON_JS)


def test_sortrows_orders_numbers_strings_and_instants(kit):
    assert kit["numAsc"] == [1, 2, 3, 4] and kit["numDesc"] == [4, 3, 2, 1]
    # Numeric-aware and case-insensitive: a1 < B9 < b10.
    assert kit["strAsc"] == [2, 1, 3, 4] and kit["strDesc"] == [3, 1, 2, 4]
    # Bare and zoned stamps compare by instant.
    assert kit["tsAsc"] == [1, 4, 2, 3] and kit["tsDesc"] == [2, 4, 1, 3]


def test_sortrows_puts_blanks_last_either_way(kit):
    """A column sorted to find the largest value must not open on blanks."""
    assert kit["strAsc"][-1] == 4 and kit["strDesc"][-1] == 4
    assert kit["tsAsc"][-1] == 3 and kit["tsDesc"][-1] == 3


def test_sortrows_is_stable_and_pure(kit):
    assert kit["tiesKeepOrder"] == [9, 7, 8]
    assert kit["noKey"] == [3, 1, 2, 4]
    assert kit["inputUntouched"] == [3, 1, 2, 4]


def test_fmtts_shows_local_time_with_age_and_raw_value_on_hover(kit):
    assert kit["bare"].endswith(">2026-10-15 11:04</span>")
    assert 'data-age="2026-10-15T11:04:51.105391"' in kit["bare"]
    assert re.search(r'title="\S+ ago · 2026-10-15T11:04:51\.105391"', kit["bare"]), kit["bare"]
    assert kit["bareSeconds"].endswith(">2026-10-15 11:04:51</span>")


def test_fmtts_reads_every_stamp_shape_the_servers_send(kit):
    """Bare (UTC, no zone), zoned, and date-space-time all land on the same
    wall-clock minute; a bare stamp read as local time would be hours off."""
    assert kit["zoned"].endswith(">2026-10-15 11:04</span>")
    assert kit["spaced"].endswith(">2026-10-15 11:04</span>")


def test_fmtts_folds_the_date_on_the_same_day_only(kit):
    assert kit["sameDay"].endswith(">11:09:00</span>")
    assert kit["otherDay"].endswith(">2026-10-16 11:09:00</span>")


def test_fmtts_never_prints_invalid_date(kit):
    assert kit["garbage"] == "&lt;soon&gt;"
    assert kit["empty"] == ""


def test_refreshages_updates_the_hover_title(kit):
    assert re.fullmatch(r"\S+ ago · 2026-10-15T11:04:51\.105391", kit["refreshedTitle"]), kit["refreshedTitle"]


def test_textmatch_is_word_wise_and_case_insensitive(kit):
    assert kit["matchWords"] is True
    assert kit["matchMissingWord"] is False
    assert kit["matchEmpty"] is True
    assert kit["matchNulls"] is True


def test_listkit_marks_the_sorted_header_and_shares_it_across_tables(kit):
    assert kit["initial"] == {"key": "when", "dir": "desc"}
    assert kit["ariaInitial"] == [None, "descending", None]
    assert kit["arrowInitial"] == ["", " ▼", ""]
    # The header text is escaped into a real button.
    assert kit["headerMarkup"].startswith('<button type="button" class="sort">&lt;When&gt;')
    assert kit["afterClick"] == {"key": "id", "dir": "asc"}
    assert kit["ariaAfterClick"] == ["ascending", None, "ascending"]
    assert kit["arrowAfterClick"] == [" ▲", "", " ▲"]
    assert kit["changes"] == 1


def test_listkit_click_again_reverses_and_time_columns_open_newest_first(kit):
    assert kit["afterSecondClick"] == {"key": "id", "dir": "desc"}
    assert kit["afterWhen"] == {"key": "when", "dir": "desc"}
    assert kit["sorted"] == [2, 1]


def test_listkit_remembers_a_choice_but_not_a_default(kit):
    assert kit["storedBeforeClick"] == [], "a page sorts the way it always did until a header is clicked"
    assert kit["stored"] == {"key": "id", "dir": "asc"}
    assert kit["remembered"] == {"key": "id", "dir": "asc"}
    assert kit["forgotten"] == {"key": "when", "dir": "desc"}
    assert kit["unreadable"] == {"key": "when", "dir": "desc"}


def test_listkit_refuses_a_header_naming_no_key(kit):
    assert kit["unknownKeyThrows"] and "typo" in kit["unknownKeyThrows"]


def test_listkit_styles_once_and_works_without_a_dom(kit):
    assert kit["stylesInjectedOnce"] == 1
    assert kit["headless"] == [1, 2]


# --------------------------------------------------------------------------
# each page end to end, under node
# --------------------------------------------------------------------------

# The page's inline script with common.js loaded first, as the browser does.
# Headers are stubbed from the page's own markup, so a key a page names but
# does not define fails here. Three rows arrive out of order; the harness
# reads the painted order, clicks the time column, polls with a change, and
# (where the search is the kit's) types into the box.
_PAGE_STUB = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const common=fs.readFileSync(path.join(path.dirname(process.argv[2]),'common.js'),'utf8');
const api=process.argv[3], key=process.argv[4], timeKey=process.argv[5], search=process.argv[6]==='1';

const stamp={5:'2026-10-15T10:00:00',9:'2026-10-15T12:00:00',7:'2026-10-15T11:00:00'};
const mk=(id,extra)=>Object.assign({id:id,alert_id:String(id),status:'queued',outcome:'monitoring',risk:'low',
  remediation_class:'gitops-patch',host_id:'h',trigger:'alert '+id,summary:'alert '+id,
  payload:{recommendation:'fix '+id},created_at:stamp[id],started_at:stamp[id],latest_event_at:stamp[id]+'+00:00'},extra||{});
let data=[mk(5),mk(9),mk(7,{trigger:'needle in 7',summary:'needle',payload:{recommendation:'needle fix'}})];

const heads=[]; html.replace(/<th data-sort="(\w+)">([^<]*)<\/th>/g,(m,k,label)=>{ heads.push({attrs:{'data-sort':k},textContent:label,innerHTML:'',handlers:{},arrow:{textContent:''},
  getAttribute(n){ return n in this.attrs?this.attrs[n]:null; }, setAttribute(n,v){ this.attrs[n]=String(v); },
  removeAttribute(n){ delete this.attrs[n]; }, addEventListener(t,f){ this.handlers[t]=f; },
  querySelector(sel){ return sel==='.arrow'?this.arrow:null; }, click(){ this.handlers.click(); }}); return m; });
const stored={};
const doc={documentElement:{},body:{appendChild(){}},head:{appendChild(){}},addEventListener(){},activeElement:null,hidden:false,
  querySelectorAll:sel=>sel==='th[data-sort]'?heads:[]};
function el(id){const e={id:id||'',className:'',textContent:'',value:'',hidden:false,
  style:{},classList:{add(){},remove(){}},setAttribute(){},getAttribute(){return null;},remove(){},
  appendChild(){},addEventListener(){},focus(){},isConnected:true,_html:''};
  Object.defineProperty(e,'innerHTML',{get(){return e._html;},set(v){ e._html=v; }});
  return e;}
const els={};
doc.createElement=()=>el('');
doc.getElementById=id=>(els[id]=els[id]||el(id));
const loc={pathname:'/x',search:'',hash:'',href:'http://cfop/x'};
const box={console,JSON,Math,Date,Number,String,Array,Object,URL,Promise,Set,Intl,
  setTimeout,clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  localStorage:{getItem:k=>(k in stored?stored[k]:null), setItem(k,v){ stored[k]=String(v); }},
  location:loc, history:{replaceState(){}}, document:doc, navigator:{},
  getComputedStyle:()=>({getPropertyValue:()=>'#888888'}),
  fetch:(url)=>{
    if(url.indexOf(api)===0 && url.indexOf(api+'/')!==0)
      return Promise.resolve({ok:true,json:()=>Promise.resolve({[key]:data})});
    return Promise.resolve({ok:true,json:()=>Promise.resolve({})});
  }};
box.window={location:loc,history:box.history,addEventListener(){},removeEventListener(){},
  CFOP:{me:()=>Promise.resolve({role:'admin'})}};
box.globalThis=box; vm.createContext(box); vm.runInContext(common,box); vm.runInContext(src,box);

const tick=()=>new Promise(r=>setImmediate(r));
const order=()=>(els['rows']._html.match(/id="row-(\d+)"/g)||[]).map(s=>Number(s.match(/\d+/)[0]));
const timeHead=heads.find(h=>h.attrs['data-sort']===timeKey);
(async () => {
  await tick(); await tick();
  const out={};
  out.defaultOrder=order();
  out.stampsShown=(els['rows']._html.match(/class="ts"/g)||[]).length;
  out.cellText=(els['rows']._html.match(/class="ts"[^>]*>([^<]*)</)||[])[1];
  out.ariaDefault=heads.filter(h=>h.attrs['aria-sort']).map(h=>h.attrs['data-sort']+':'+h.attrs['aria-sort']);
  timeHead.click(); await tick();
  out.afterClick=order();
  out.stored=stored['cfop.sort.'+path.basename(process.argv[2],'.html')];
  // The poll brings a change: the repaint keeps the operator's order.
  data=data.map(r=>r.id===7?Object.assign({},r,{status:'claimed',outcome:'needs_action',event_count:9}):r);
  await box.load(); await tick();
  out.afterPoll=order();
  timeHead.click(); await tick();
  out.afterSecondClick=order();
  if(search){
    box.searchInput('NEEDLE'); await tick();
    out.searched=order();
    box.searchInput(''); await tick();
    out.cleared=order();
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module", params=sorted(PAGES))
def page_behaviour(request, tmp_path_factory):
    page = request.param
    spec = PAGES[page]
    out = run_node(_PAGE_STUB, tmp_path_factory.mktemp("page"), UI / page,
                   spec["api"], spec["key"], spec["time"], "1" if spec["search"] else "0")
    return page, spec, out


def test_the_default_order_is_newest_first_as_before(page_behaviour):
    """Nothing changes until a header is clicked: the list reads as it did."""
    page, spec, b = page_behaviour
    assert b["defaultOrder"] == [9, 7, 5], f"{page}: default order {b['defaultOrder']}"
    # Every header row (remediations has three) marks the same column.
    assert b["ariaDefault"] and set(b["ariaDefault"]) == {f"{spec['time']}:descending"}, f"{page}: {b['ariaDefault']}"


def test_rows_show_an_absolute_local_time(page_behaviour):
    page, spec, b = page_behaviour
    assert b["stampsShown"] == 3, f"{page}: {b['stampsShown']} rows carry a stamp"
    assert b["cellText"] == "2026-10-15 12:00", f"{page}: the cell reads {b['cellText']!r}"


def test_a_header_click_reorders_and_the_poll_keeps_it(page_behaviour):
    """The round-trip: click the time column (reversing newest-first), then a
    poll that repaints — the repaint must not fall back to the server's order."""
    page, spec, b = page_behaviour
    assert b["afterClick"] == [5, 7, 9], f"{page}: after click {b['afterClick']}"
    assert b["afterPoll"] == [5, 7, 9], f"{page}: the poll's repaint lost the sort: {b['afterPoll']}"
    assert b["afterSecondClick"] == [9, 7, 5], f"{page}: second click {b['afterSecondClick']}"


def test_the_choice_is_remembered_per_page(page_behaviour):
    page, spec, b = page_behaviour
    assert json.loads(b["stored"]) == {"key": spec["time"], "dir": "asc"}, f"{page}: {b['stored']}"


def test_the_search_box_narrows_the_rows(page_behaviour):
    page, spec, b = page_behaviour
    if not spec["search"]:
        pytest.skip(f"{page} searches on the server")
    assert b["searched"] == [7], f"{page}: search found {b['searched']}"
    # Cleared after the second click, so the rows come back in that order.
    assert b["cleared"] == [9, 7, 5], f"{page}: clearing the search did not restore the rows"
