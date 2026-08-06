/*
 * Shared console header: section nav, active-page indicator, identity, logout.
 *
 * The console is five static pages with no shared layout and no build step.
 * Before this file each page hand-rolled its own header, which is how they
 * drifted into three markup shapes and five different link sets, with the
 * logged-in user shown on two pages out of five. Everything header-shaped
 * lives here now; the pages contribute a mount point and nothing else.
 *
 * Served from /nav.js (see web_server.py). No imports, no bundler — same
 * constraint as the pages themselves: this box has no outbound network, so
 * anything fetched from a CDN would hang rather than fail.
 *
 * Usage, in the page's <header>:
 *     <div id="cfop-nav"></div>
 *     <script src="/nav.js" defer></script>
 *
 * Pages needing the identity themselves await CFOP.me() — it resolves from
 * the same single /api/auth/me call this file already makes, rather than
 * each page issuing its own.
 */
(function () {
  'use strict';

  // href is also the identity of the section: the active entry is the one
  // whose href matches location.pathname. Keeping "/" last in the match
  // order matters — it is a prefix of everything else.
  var SECTIONS = [
    { href: '/', label: 'Console',
      icon: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>' },
    { href: '/remediations', label: 'Remediations',
      icon: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>' },
    { href: '/investigations', label: 'Investigations',
      icon: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>' },
    { href: '/tokens', label: 'Tokens',
      icon: '<circle cx="8" cy="15" r="4"/><path d="M10.85 12.15L19 4"/><path d="M18 5l2 2"/><path d="M15 8l2 2"/>' },
    // Shown to every role on purpose. The page is where a member changes
    // their own password; its admin-only controls are hidden client-side and
    // enforced server-side. See the note on the /users route in web_server.py.
    { href: '/users', label: 'Users',
      icon: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>' }
  ];

  var LOGOUT_ICON =
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>';

  // Every page defines the same :root tokens, so this inherits the theme
  // rather than introducing a second palette.
  var CSS = [
    '.cfop-navbar { margin-left:auto; display:flex; align-items:center; gap:10px; }',
    '.cfop-nav { display:flex; align-items:center; gap:8px; }',
    '.cfop-nav a { text-decoration:none; color:var(--text-secondary);',
    '  background:var(--bg-tertiary); display:inline-flex; align-items:center; gap:6px;',
    '  padding:6px 10px; border:1px solid var(--border); border-radius:var(--radius-sm);',
    '  font-family:\'JetBrains Mono\',monospace; font-size:13px; white-space:nowrap;',
    '  transition:border-color .15s, color .15s, background .15s; }',
    '.cfop-nav a:hover { color:var(--text-primary); border-color:var(--text-secondary);',
    '  text-decoration:none; }',
    // Active entry: accent border + tint, and never colour alone — the
    // [aria-current] attribute carries it for anyone not seeing the hue.
    '.cfop-nav a[aria-current="page"] { color:var(--accent); border-color:var(--accent);',
    '  background:color-mix(in srgb, var(--accent) 12%, var(--bg-tertiary)); font-weight:600; }',
    '.cfop-nav a svg { width:14px; height:14px; flex-shrink:0; }',
    '.cfop-who { color:var(--text-secondary); font-size:12px; white-space:nowrap;',
    '  font-family:\'JetBrains Mono\',monospace; }',
    '.cfop-logout { background:var(--bg-tertiary); color:var(--text-secondary);',
    '  border:1px solid var(--border); border-radius:var(--radius-sm); padding:6px 10px;',
    '  cursor:pointer; display:inline-flex; align-items:center; gap:6px;',
    '  font-family:\'JetBrains Mono\',monospace; font-size:13px;',
    '  transition:border-color .15s, color .15s; }',
    '.cfop-logout:hover { color:var(--red); border-color:var(--red); }',
    '.cfop-logout:disabled { opacity:.5; cursor:default; }',
    '.cfop-logout svg { width:14px; height:14px; }',
    // Below the width where the labels stop fitting alongside the page's own
    // header controls, keep the icons and drop the words.
    '@media (max-width: 900px) { .cfop-nav a span, .cfop-logout span { display:none; }',
    '  .cfop-who { display:none; } }'
  ].join('\n');

  function svg(paths) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + '</svg>';
  }

  /* Longest matching href wins, so /users beats the "/" entry. Trailing
   * slashes and query strings are already excluded by using pathname. */
  function activeHref(pathname) {
    var best = null;
    for (var i = 0; i < SECTIONS.length; i++) {
      var href = SECTIONS[i].href;
      var hit = href === '/' ? pathname === '/' : pathname.indexOf(href) === 0;
      if (hit && (best === null || href.length > best.length)) best = href;
    }
    return best;
  }

  /* One /api/auth/me for the whole page, shared with whoever asks. Rejects
   * are the caller's to handle; the header degrades on its own below. */
  var mePromise = null;
  function me() {
    if (mePromise === null) {
      mePromise = fetch('/api/auth/me', { headers: { Accept: 'application/json' } })
        .then(function (r) {
          if (!r.ok) throw new Error('auth/me returned ' + r.status);
          return r.json();
        });
    }
    return mePromise;
  }

  /* Matches the wording users.html used before this file existed: a token
   * caller shows its label, a session shows the username, and the role rides
   * along because "member" is the answer to "why is that button missing". */
  function describe(who) {
    if (!who) return '';
    var name = who.token_label
      ? 'token ' + who.token_label + (who.username ? ' (' + who.username + ')' : '')
      : (who.username || 'anonymous');
    return name + (who.role ? ' · ' + who.role : '');
  }

  function logout(btn) {
    btn.disabled = true;
    // POST, not a link: a prefetch or a crawler must not be able to end
    // someone's session. /logout is in EXEMPT_PATHS, so this still works
    // when the session is already gone — no 401 loop on the way out.
    fetch('/logout', { method: 'POST', headers: { Accept: 'application/json' } })
      .catch(function () { /* fall through — the redirect re-checks anyway */ })
      .then(function () { window.location.href = '/login'; });
  }

  function render(mount) {
    var current = activeHref(window.location.pathname);

    var links = SECTIONS.map(function (s) {
      var active = s.href === current;
      return '<a href="' + s.href + '" title="' + s.label + '"' +
             (active ? ' aria-current="page"' : '') + '>' +
             svg(s.icon) + '<span>' + s.label + '</span></a>';
    }).join('');

    mount.className = 'cfop-navbar';
    mount.innerHTML =
      '<nav class="cfop-nav" aria-label="Console sections">' + links + '</nav>' +
      '<span class="cfop-who" id="cfop-who"></span>' +
      '<button type="button" class="cfop-logout" id="cfop-logout" hidden>' +
      svg(LOGOUT_ICON) + '<span>Log out</span></button>';

    var btn = mount.querySelector('#cfop-logout');
    btn.addEventListener('click', function () { logout(btn); });

    me().then(function (who) {
      mount.querySelector('#cfop-who').textContent = describe(who);
      // Nothing to log out of when the server is not enforcing auth —
      // offering the control there would imply otherwise.
      btn.hidden = !!who.auth_disabled;
    }).catch(function () {
      // Identity unavailable (auth backend down, or /api/auth/me itself
      // refused). The nav is still useful, so leave it; the logout button
      // stays hidden rather than offering an action that cannot be trusted
      // to mean anything.
    });
  }

  function init() {
    var mount = document.getElementById('cfop-nav');
    if (!mount) return;
    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    render(mount);
  }

  window.CFOP = { me: me, describe: describe };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
