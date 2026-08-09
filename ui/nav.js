/*
 * Shared console header: section nav, active-page indicator, identity, logout.
 *
 * The console is static pages with no shared layout and no build step.
 * Before this file each page hand-rolled its own header, which is how they
 * drifted into three markup shapes and different link sets, with the
 * logged-in user shown on only some pages. Everything header-shaped
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
  // whose href owns location.pathname. Array order is render order — left to
  // right in the header — and has no bearing on which entry is marked active;
  // activeHref() below is order-independent.
  //
  // adminOnly entries are omitted from the bar until /api/auth/me says the
  // caller is an admin (or auth is disabled). Account stays for every role —
  // that is where members change their password and manage their own tokens.
  var SECTIONS = [
    { href: '/', label: 'Console',
      icon: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>' },
    { href: '/remediations', label: 'Remediations',
      icon: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>' },
    { href: '/investigations', label: 'Investigations',
      icon: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>' },
    { href: '/account', label: 'Account',
      icon: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>' },
    { href: '/admin', label: 'Admin', adminOnly: true,
      icon: '<circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>' }
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

  function isAdmin(who) {
    return !!(who && (who.role === 'admin' || who.auth_disabled));
  }

  function visibleSections(who) {
    return SECTIONS.filter(function (s) {
      return !s.adminOnly || isAdmin(who);
    });
  }

  /* The href of the section owning this pathname, or null if none does.
   *
   * pathname keeps a trailing slash — it drops only the query string and the
   * hash — so it is normalised before matching and /account/ lands on /account.
   *
   * Matching is on a path-segment boundary rather than a bare prefix, so
   * /account cannot claim a sibling like /accountsomething. "/" is exact-only
   * for the same reason: it is a prefix of every other section.
   *
   * Longest match wins. Matching walks every section (including adminOnly) so
   * /admin is recognised even before identity arrives. */
  function activeHref(pathname) {
    var path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
    var best = null;
    for (var i = 0; i < SECTIONS.length; i++) {
      var href = SECTIONS[i].href;
      var hit = href === '/'
        ? path === '/'
        : (path === href || path.indexOf(href + '/') === 0);
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

  /* A token caller shows its label, a session shows the username, and the role
   * rides along because "member" is the answer to "why is that button missing". */
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

  function render(mount, who) {
    var current = activeHref(window.location.pathname);
    var sections = visibleSections(who);

    var links = sections.map(function (s) {
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

    if (who) {
      mount.querySelector('#cfop-who').textContent = describe(who);
      // Nothing to log out of when the server is not enforcing auth —
      // offering the control there would imply otherwise.
      btn.hidden = !!who.auth_disabled;
    }
  }

  function init() {
    var mount = document.getElementById('cfop-nav');
    if (!mount) return;
    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    // Paint the public sections immediately; Admin appears once identity lands.
    render(mount, null);
    me().then(function (who) {
      render(mount, who);
    }).catch(function () {
      // Identity unavailable (auth backend down, or /api/auth/me itself
      // refused). The public nav is still useful; Admin stays hidden and
      // logout stays hidden rather than offering an untrusted action.
    });
  }

  window.CFOP = { me: me, describe: describe };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
