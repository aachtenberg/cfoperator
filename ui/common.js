/**
 * Shared console helpers (CFOP-95).
 *
 * Every console page used to carry its own copy of these — five copies of
 * esc(), four of badge()/color(), five toast()s with three different
 * durations. This is the one copy. Served from /common.js (see web_server.py),
 * same posture as nav.js: no imports, no bundler, no outbound network.
 *
 * Unlike nav.js this is NOT deferred. It touches no DOM at load, and the
 * pages' own inline scripts are not deferred either — they call these at top
 * level (trapFocus on the drawer, the first paint() whenever the initial
 * fetch resolves), so it has to be there before the page script runs:
 *
 *     <script src="/common.js"></script>
 *     <script> ...page... </script>
 *
 * test_console_common.py pins that ordering, and that no page grows a local
 * copy of anything defined here again.
 */

// Quotes are escaped too: these values also land inside HTML attributes
// (href, value), where a bare " or ' breaks out of the attribute.
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// A theme colour by CSS variable name ('--accent'), with a fallback so a
// badge for an unknown state is grey rather than invisible.
function color(v){ return (getComputedStyle(document.documentElement).getPropertyValue(v)||'#888').trim(); }

function badge(text, varname){ const c=color(varname);
  return `<span class="badge" style="background:${c}22;color:${c};border:1px solid ${c}55">${esc(text)||'—'}</span>`; }

// ---- timestamps (CFOP-412) -------------------------------------------------
// Server stamps come in three shapes: the knowledge base's bare
// "2026-10-15T11:04:51.105391" (UTC, no zone — read as local time if taken
// as it is), the event runtime's zoned "+00:00" (CFOP-215 — appending Z to
// it made an invalid date and a "NaNd" age), and the auth tables' date with
// a space before the time. One parser for all three; null for anything
// else, so a caller can show the raw text rather than "Invalid Date".
const ZONED=/(Z|[+-]\d\d:?\d\d)$/;
function parseTs(iso){
  if(!iso) return null;
  const raw=String(iso).trim();
  if(!/^\d{4}-\d\d-\d\d/.test(raw)) return null;
  const s=raw.replace(/^(\d{4}-\d\d-\d\d) /,'$1T');
  const bare=/\d\d:\d\d/.test(s) && !ZONED.test(s);
  const d=new Date(bare?s+'Z':s);
  return isNaN(d.getTime())?null:d;
}

// Relative age of a stamp: "3h", "2d".
function age(iso){ const d=parseTs(iso); if(!d) return '—'; const s=(Date.now()-d.getTime())/1000;
  if(s<90) return Math.round(s)+'s'; if(s<5400) return Math.round(s/60)+'m';
  if(s<129600) return Math.round(s/3600)+'h'; return Math.round(s/86400)+'d'; }

// The one way a timestamp is shown. The text is the absolute local time —
// the lists used to show "3h" alone, which the operator read as no
// timestamp at all — and the hover title carries the relative age and the
// server's own value, so neither is lost and the microseconds are out of
// the way. Lists show minutes; drawers pass {seconds:true}. {sameDayAs}
// drops the date when it matches that other stamp's, for "started ·
// completed" on one line. A value that does not parse is shown as it came.
function fmtTs(iso, opts){
  const o=opts||{};
  if(!iso) return '';
  const d=parseTs(iso); if(!d) return esc(iso);
  const p=n=>String(n).padStart(2,'0');
  const dayOf=x=>`${x.getFullYear()}-${p(x.getMonth()+1)}-${p(x.getDate())}`;
  const day=dayOf(d);
  let time=`${p(d.getHours())}:${p(d.getMinutes())}`; if(o.seconds) time+=':'+p(d.getSeconds());
  const ref=o.sameDayAs?parseTs(o.sameDayAs):null;
  return `<span class="ts" data-age="${esc(iso)}" title="${esc(ageTitle(iso))}">${ref&&dayOf(ref)===day?time:day+' '+time}</span>`;
}
function ageTitle(iso){ return age(iso)+' ago · '+String(iso); }

// Refresh every stamp's hover age in place. The list pages do not rebuild
// their rows on a poll that brought nothing new (CFOP-95), so the age is
// updated by attribute, not by replacing the row under the pointer.
function refreshAges(){
  if(!document.querySelectorAll) return;
  document.querySelectorAll('[data-age]').forEach(el=>{ el.title=ageTitle(el.getAttribute('data-age')); });
}

// One duration, deliberately not a parameter — a per-call override is how the
// pages drifted to 3200/3800/6000 in the first place. 5 s reads the longest
// message the console emits (error toasts carry the server's reason, e.g.
// "the cockpit bridge is not enabled on this agent (cockpit.bridge_enabled)")
// without the pipeline flag toggles, each of which toasts, stacking up.
const TOAST_MS = 5000;
function toast(msg, type){ const c=document.getElementById('toasts'); if(!c) return;
  const t=document.createElement('div'); t.className='toast '+(type||''); t.textContent=msg; c.appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; setTimeout(()=>t.remove(),220); }, TOAST_MS); }

// A Save button lit only while there is something to save (CFOP-561). Every
// Admin Save was a primary chip that never changed, so it looked as urgent
// straight after a save as before one. armSave() snapshots the inputs as they
// are when called — pages call it right after rendering from the server, so
// that is what is stored — and keeps the button disabled while every input
// still matches. Values compare trimmed, the way the saves send them, so a
// stray space cannot light a Save that would change nothing. A successful save
// re-renders and re-arms, which greys it; a failed one leaves it lit, because
// the change is still unsaved.
function armSave(btn, inputs){
  if(!btn) return;
  const els=(inputs||[]).filter(Boolean);
  const val=el=>el.type==='checkbox'?el.checked:String(el.value==null?'':el.value).trim();
  const base=els.map(val);
  btn._armed=()=>{ btn.disabled=!!btn._saving||els.every((el,i)=>val(el)===base[i]); };
  els.forEach(el=>{ el.addEventListener('input',btn._armed); el.addEventListener('change',btn._armed); });
  btn._armed();
}

// Run a save with its button held disabled until fn settles, re-render
// included. Without it, an edit made mid-request re-lit the Save through
// armSave, ready for a second submit of the same change.
async function runSave(btn, fn){
  if(!btn) return fn();
  btn._saving=true; btn.disabled=true;
  try { return await fn(); }
  finally { btn._saving=false; if(btn._armed) btn._armed(); else btn.disabled=false; }
}

// Keep Tab inside an open dialog (CFOP-94 follow-up, CFOP-95). The drawer is
// aria-modal, but aria-modal is a promise to assistive tech, not a mechanism:
// without this, Tab from the last control walks out into the page behind the
// backdrop. Wraps at both ends. A Tab something inside already consumed —
// the xterm terminal in the investigations drawer hands Tab to the shell and
// calls preventDefault — is left alone.
const FOCUSABLE='a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
function trapFocus(dialog){
  if(!dialog||!dialog.addEventListener) return;
  dialog.addEventListener('keydown', e=>{
    if(e.key!=='Tab'||e.defaultPrevented) return;
    const items=Array.from(dialog.querySelectorAll(FOCUSABLE)).filter(el=>!el.hidden);
    if(!items.length){ e.preventDefault(); return; }
    const first=items[0], last=items[items.length-1], a=document.activeElement;
    const inside=items.indexOf(a)>=0;
    if(e.shiftKey){ if(!inside||a===first){ e.preventDefault(); last.focus(); } }
    else { if(!inside||a===last){ e.preventDefault(); first.focus(); } }
  });
}

// Copy an element's text and say what happened (CFOP-73, CFOP-109).
// navigator.clipboard is a secure-context API, and this console is normally
// reached over plain HTTP on the LAN — so the execCommand path is the one that
// actually runs for most operators here, not a legacy branch. The element the
// operator can see is selected in place rather than an off-screen textarea: a
// detached node is the flakier thing to ask execCommand to copy, and a failed
// copy then leaves the text visibly selected for a manual one.
function copyElementText(el, what){
  const text=el?(el.textContent||''):'';
  if(!text) return;
  const label=what||'text';
  const fallback=()=>{
    let ok=false;
    try{
      const sel=window.getSelection(), range=document.createRange();
      range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
      ok=document.execCommand('copy');
    }catch(e){ ok=false; }
    toast(ok?label+' copied':'copy failed — select the '+label+' and copy manually', ok?'ok':'err');
  };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(()=>toast(label+' copied','ok')).catch(fallback);
  } else { fallback(); }
}

// ---- click-to-copy (CFOP-113) ---------------------------------------------
// copyText: the same two paths as copyElementText, for text that is not one
// visible element (a title plus its trigger, a numbered list of steps). The
// fallback puts the text in a textarea appended to the body for the duration
// of the execCommand — attached, so it is the reliable shape, and gone before
// anyone sees it. Resolves true/false and says nothing itself; the caller
// decides what feedback fits.
function copyText(text){
  const s=text==null?'':String(text);
  if(!s) return Promise.resolve(false);
  const fallback=()=>{
    let ok=false, ta=null;
    try{
      ta=document.createElement('textarea'); ta.value=s; ta.setAttribute('readonly','');
      ta.style.position='fixed'; ta.style.top='0'; ta.style.left='-9999px';
      document.body.appendChild(ta); ta.select();
      ok=document.execCommand('copy');
    }catch(e){ ok=false; }
    if(ta&&ta.remove) ta.remove();
    return ok;
  };
  if(navigator.clipboard && navigator.clipboard.writeText){
    return navigator.clipboard.writeText(s).then(()=>true).catch(()=>fallback());
  }
  return Promise.resolve(fallback());
}

// The copy icon: a clipboard that becomes a check for a moment. No toast on
// success — the check is the feedback, in place, where the eye already is. A
// failure still toasts, because then the operator has to do something. Text
// comes from data-copy, or from the textContent of the element data-copy-from
// names, so long values (raw JSON) are not doubled into an attribute.
const COPY_ICON='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="1.5"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const CHECK_ICON='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
function copyIcon(what, opts){
  const o=opts||{};
  const src=o.from?` data-copy-from="${esc(o.from)}"`:'';
  const txt=o.text!=null?` data-copy="${esc(o.text)}"`:'';
  return `<button type="button" class="cp" aria-label="Copy ${esc(what)}" title="Copy ${esc(what)}"${src}${txt}>${COPY_ICON}</button>`;
}
// One delegated listener per page. The drawers repaint by innerHTML, so a
// listener per button would be gone on the next paint; this one survives. It
// stops the click too: an icon inside a <summary> must not toggle it, and one
// inside a table row must not open the row.
function bindCopyIcons(root){
  const r=root||document;
  if(!r.addEventListener) return;
  r.addEventListener('click', e=>{
    const t=e.target;
    const b=(t&&t.closest)?t.closest('button.cp'):null;
    if(!b) return;
    e.preventDefault(); e.stopPropagation();
    const from=b.getAttribute('data-copy-from');
    const src=from?document.getElementById(from):null;
    const text=b.hasAttribute('data-copy')?b.getAttribute('data-copy'):(src?(src.textContent||''):'');
    copyText(text).then(ok=>{
      if(!ok){ toast('copy failed — select the text and copy manually','err'); return; }
      b.classList.add('done'); b.innerHTML=CHECK_ICON;
      setTimeout(()=>{ b.classList.remove('done'); b.innerHTML=COPY_ICON; }, 1500);
    });
  });
}

// ---- markdown (from index.html, CFOP-113) ---------------------------------
// Agent output is LLM text that quotes log and alert lines verbatim, so the
// markdown it contains is untrusted, and marked emits raw HTML by design.
// Parse, then walk the result and drop anything scriptable before it reaches
// innerHTML. marked itself is vendored (ui/vendor); when it is absent — a 404
// on the script, or a page that never loaded it — the text is escaped, never
// dropped. The chat page and the investigations drawer render the same agent
// prose, which is why this is here and not in either of them.
const MD_ALLOWED_TAGS = new Set(['A','B','BLOCKQUOTE','BR','CODE','DEL','DIV','EM','H1','H2',
  'H3','H4','H5','H6','HR','I','LI','OL','P','PRE','SPAN','STRONG','TABLE','TBODY','TD',
  'TH','THEAD','TR','UL']);
const MD_ALLOWED_ATTRS = new Set(['href', 'title', 'class', 'colspan', 'rowspan']);
function sanitizeHtml(html) {
  const doc = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html');
  const root = doc.body.firstChild;
  for (const el of Array.from(root.querySelectorAll('*'))) {
    if (!MD_ALLOWED_TAGS.has(el.tagName)) {
      el.replaceWith(...el.childNodes);  // keep the text, drop the element
      continue;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (!MD_ALLOWED_ATTRS.has(name)) { el.removeAttribute(attr.name); continue; }
      // No fragment hrefs: the investigations drawer is hash-routed, so
      // agent markdown like [see](#2272) would swap the open row (or, with the
      // target=_blank below, open another investigation in a new tab).
      if (name === 'href' && !/^(https?:|mailto:|\/)/i.test(attr.value.trim())) {
        el.removeAttribute(attr.name);
      }
    }
    if (el.tagName === 'A') { el.setAttribute('rel', 'noopener noreferrer'); el.setAttribute('target', '_blank'); }
  }
  return root.innerHTML;
}
function renderMarkdown(text) {
  const s = text == null ? '' : String(text);
  if (typeof marked === 'undefined' || !marked || !marked.parse) return esc(s);
  try {
    marked.setOptions({ breaks: true, gfm: true });
    return sanitizeHtml(marked.parse(s));
  } catch (e) {
    return esc(s);
  }
}

// ---- list kit: sortable headers and search (CFOP-412) ---------------------
// Three list pages (investigations, remediations, events) share one way to
// sort a column and one way to search. The page supplies what each column
// sorts by; the kit owns the header buttons, the aria-sort state, the arrow,
// and remembering the choice. Nothing here runs at load: the page calls
// listKit() once its table exists.

// Pure: a sorted copy of rows. `keys` maps a column name to an accessor
// (a function, or {get}) returning a string, a number or nothing; `state`
// is {key, dir}. Empty values go last whichever way the sort runs — a column
// sorted to find the largest value must not open on a page of blanks — and
// ties keep the incoming order, which is the server's newest-first, so a sort
// by status still reads newest-first within each status. Strings compare
// numerically aware and case-insensitively ("#9" before "#10").
function sortRows(rows, keys, state){
  const get=keyAccessor(keys, state&&state.key);
  if(!get) return rows.slice();
  const dir=(state.dir==='asc')?1:-1;
  const coll=(typeof Intl!=='undefined'&&Intl.Collator)?new Intl.Collator(undefined,{numeric:true,sensitivity:'base'}):null;
  const blank=v=>v==null||v===''||(typeof v==='number'&&isNaN(v));
  const cmp=(a,b)=>{ if(typeof a==='number'&&typeof b==='number') return a-b;
    a=String(a); b=String(b); return coll?coll.compare(a,b):(a<b?-1:a>b?1:0); };
  return rows.map((r,i)=>({r,i,v:get(r)})).sort((x,y)=>{
    const bx=blank(x.v), by=blank(y.v);
    if(bx||by) return bx===by ? x.i-y.i : (bx?1:-1);
    const c=cmp(x.v,y.v);
    return c ? c*dir : x.i-y.i;
  }).map(x=>x.r);
}
function keyAccessor(keys, name){ const k=(keys&&name)?keys[name]:null; if(!k) return null; return typeof k==='function'?k:k.get; }
// A timestamp column sorts by instant, not by text: a bare stamp and a zoned
// one for the same moment are different strings.
function tsKey(iso){ const d=parseTs(iso); return d?d.getTime():null; }

// Client-side search: every word of the query must appear somewhere in the
// row's searchable values, case-insensitively. Words rather than the phrase,
// so "pi4 disk" finds a row whose host is raspberrypi4 and whose trigger
// mentions disk. An empty query matches everything.
function textMatch(q, values){
  const words=String(q||'').toLowerCase().split(/\s+/).filter(Boolean);
  if(!words.length) return true;
  const hay=(Array.isArray(values)?values:[values]).map(v=>v==null?'':String(v)).join('\n').toLowerCase();
  return words.every(w=>hay.indexOf(w)>=0);
}

// Where a page's sort choice is kept: localStorage, per page. It is an
// operator preference rather than a view to paste, and the Events URL
// already carries filters that mean "what the server returns" — a sort there
// would read as a server filter it is not. Nothing is written until a
// header is clicked, so a page sorts the way it always did until then.
const SORT_STORE='cfop.sort.';
// The header buttons inherit the th's type so a sortable header looks like
// the others; the sorted one reads in the primary colour, with an arrow.
const LIST_KIT_CSS='th button.sort{all:unset;cursor:pointer;font:inherit;color:inherit;text-transform:inherit;letter-spacing:inherit}'+
  'th button.sort:hover,th[aria-sort] button.sort{color:var(--text-primary)}'+
  'th button.sort:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}'+
  'th .arrow{font-size:9px}'+
  // A stamp is one token: a narrow column must not break it over two lines.
  '.ts{white-space:nowrap}';
let LIST_KIT_STYLED=false;
function injectListKitCss(){
  if(LIST_KIT_STYLED||!document.createElement||!document.head||!document.head.appendChild) return;
  const st=document.createElement('style'); st.textContent=LIST_KIT_CSS; document.head.appendChild(st); LIST_KIT_STYLED=true;
}

// Wire every <th data-sort="key"> on the page into one sort state.
//   opts.page      name for the remembered choice (cfop.sort.<page>)
//   opts.keys      column name -> accessor, as sortRows takes
//   opts.initial   {key, dir} until the operator chooses
//   opts.dir       first-click direction per key (default asc; a time or id
//                  column says desc so the first click gives newest first)
//   opts.onChange  called after a click; the page paints again
// The page's paint() runs its rows through kit.sort(), so the order holds
// across every repaint — the poll's included — by construction. A header
// naming a key the page did not define throws here, at load, so a typo
// fails the node harness rather than shipping a header that does nothing.
// A page with several header rows naming the same keys shares the one
// state: a click on any of them updates them all.
function listKit(opts){
  const o=opts||{}, keys=o.keys||{}, firstDir=o.dir||{};
  const valid=k=>!!keyAccessor(keys,k);
  const store=SORT_STORE+(o.page||'list');
  let state=null;
  try{ const saved=JSON.parse(localStorage.getItem(store)||'null');
    if(saved&&valid(saved.key)&&(saved.dir==='asc'||saved.dir==='desc')) state={key:saved.key,dir:saved.dir}; }catch(e){ state=null; }
  if(!state){ const i=o.initial||{}; state={key:i.key||null, dir:i.dir==='asc'?'asc':'desc'}; }
  if(state.key&&!valid(state.key)) throw new Error('listKit: unknown sort key '+state.key);
  const heads=document.querySelectorAll?Array.from(document.querySelectorAll('th[data-sort]')):[];
  heads.forEach(th=>{
    const k=th.getAttribute('data-sort');
    if(!valid(k)) throw new Error('listKit: th[data-sort="'+k+'"] names no key');
    th.innerHTML=`<button type="button" class="sort">${esc(th.textContent)}<span class="arrow" aria-hidden="true"></span></button>`;
    th.addEventListener('click', ()=>kit.set(k));
  });
  if(heads.length) injectListKitCss();
  function show(){
    heads.forEach(th=>{
      const on=th.getAttribute('data-sort')===state.key;
      if(on) th.setAttribute('aria-sort', state.dir==='asc'?'ascending':'descending');
      else if(th.removeAttribute) th.removeAttribute('aria-sort');
      const a=th.querySelector?th.querySelector('.arrow'):null;
      if(a) a.textContent=on?(state.dir==='asc'?' ▲':' ▼'):'';
    });
  }
  const kit={
    get state(){ return {key:state.key, dir:state.dir}; },
    sort(rows){ return sortRows(rows, keys, state); },
    // A click on the sorted column reverses it; on another column, that
    // column's first direction.
    set(key, dir){
      if(!valid(key)) throw new Error('listKit: unknown sort key '+key);
      const d=dir||(key===state.key?(state.dir==='asc'?'desc':'asc'):(firstDir[key]||'asc'));
      state={key:key, dir:d};
      try{ localStorage.setItem(store, JSON.stringify(state)); }catch(e){}
      show();
      if(o.onChange) o.onChange(kit.state);
    }
  };
  show();
  return kit;
}
