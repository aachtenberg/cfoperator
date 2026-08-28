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

// Relative age of an ISO timestamp. Server timestamps are UTC but not always
// suffixed; a bare one would otherwise be read as local time.
function age(iso){ if(!iso) return '—'; const s=(Date.now()-new Date(iso+(iso.endsWith('Z')?'':'Z')).getTime())/1000;
  if(s<90) return Math.round(s)+'s'; if(s<5400) return Math.round(s/60)+'m';
  if(s<129600) return Math.round(s/3600)+'h'; return Math.round(s/86400)+'d'; }

// Refresh every [data-age] cell in place. The list pages no longer rebuild
// their rows on a poll that brought nothing new (CFOP-95), so the age column
// is updated by text, not by replacing the row under the pointer.
function refreshAges(){
  if(!document.querySelectorAll) return;
  document.querySelectorAll('[data-age]').forEach(el=>{ el.textContent=age(el.getAttribute('data-age')); });
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
