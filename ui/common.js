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
