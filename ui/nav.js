/*
 * The console's rail: brand, section nav, active-page indicator, a status
 * card, identity, logout — and the Ask CFOperator button.
 *
 * The console is static pages with no shared layout and no build step.
 * Before this file each page hand-rolled its own header, which is how they
 * drifted into three markup shapes and different link sets, with the
 * logged-in user shown on only some pages. Everything shell-shaped lives
 * here now; the pages contribute a mount point and nothing else. It was a
 * bar across the header until CFOP-696 made it a rail down the left: six
 * sections plus identity no longer fitted beside a page's own controls.
 *
 * Served from /nav.js (see web_server.py). No imports, no bundler — same
 * constraint as the pages themselves: this box has no outbound network, so
 * anything fetched from a CDN would hang rather than fail. Its rules are in
 * /console.css, which every page links; this file injects none.
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
  // whose href owns location.pathname. Array order is render order — top to
  // bottom within a group — and has no bearing on which entry is marked
  // active; activeHref() below is order-independent.
  //
  // group 'settings' entries render under the rule, apart from the places
  // work happens. adminOnly entries are omitted until /api/auth/me says the
  // caller is an admin (or auth is disabled). Account stays for every role —
  // that is where members change their password and manage their own tokens.
  //
  // "/" is the chat and stays the chat: /?investigation= and /?remediation=
  // are how a drawer hands its row over, so Inbox lives at /inbox.
  var SECTIONS = [
    { href: '/inbox', label: 'Inbox', count: true,
      icon: '<path d="M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6z"/>' },
    { href: '/', label: 'Console',
      icon: '<path d="M4 6l5 5-5 5M12 18h8"/>' },
    { href: '/investigations', label: 'Investigations',
      icon: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5"/>' },
    { href: '/remediations', label: 'Remediations',
      icon: '<path d="M9 11l3 3 8-8M20 12v7H4V5h11"/>' },
    { href: '/events', label: 'Events',
      icon: '<path d="M3 12h4l3-8 4 16 3-8h4"/>' },
    { href: '/admin', label: 'Admin', adminOnly: true, group: 'settings',
      icon: '<path d="M4 6h16M4 12h16M4 18h16M8 4v4M16 10v4M10 16v4"/>' },
    { href: '/account', label: 'Account', group: 'settings',
      icon: '<path d="M12 12a4 4 0 1 0 0-8a4 4 0 1 0 0 8zM4 21a8 8 0 0 1 16 0"/>' }
  ];

  var LOGOUT_ICON = '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>';

  // The brand mark: the loop circling a spark. The rail, the Ask button,
  // favicon.svg and login.html draw the same two paths — test_console_brand.py
  // holds them together.
  var BRAND_MARK = '<path d="M20 12a8 8 0 1 1-2.35-5.65M20 4v4h-4"/>' +
    '<path d="M12 7.6l1.1 3.3 3.3 1.1-3.3 1.1-1.1 3.3-1.1-3.3-3.3-1.1 3.3-1.1z" fill="currentColor" stroke-width="1"/>';

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function svg(paths) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
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

  /* ---- what "waiting on a human" means --------------------------------
   * An investigation the agent ended on needs_action that no operator has
   * triaged yet. One rule, here, because the rail's count and the Inbox
   * page's queue must be the same number: the page calls CFOP.needsAction. */
  function needsAction(rows) {
    return (rows || []).filter(function (r) {
      return r && r.outcome === 'needs_action' && !r.triage_action;
    });
  }

  /* ---- the status card -----------------------------------------------
   * Is the agent answering, on what build, through which provider, and when
   * did it last sweep. Four small reads; each fills its own line and a failed
   * one leaves its line out — only /api/health failing says "Disconnected".
   * STATUS is module state, not DOM state: render() runs again when identity
   * lands and must not lose what already arrived. */
  var STATUS = { connected: null, uptime: null, version: '', provider: '', sweep: '', inbox: null };

  function getJson(url) {
    return fetch(url, { headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error(url + ' returned ' + r.status);
      return r.json();
    });
  }

  function uptimeText(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s < 0) return '';
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return 'up ' + (h >= 48 ? Math.floor(h / 24) + 'd ' + (h % 24) + 'h' : h + 'h ' + m + 'm');
  }

  function clock(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return String(n).padStart(2, '0'); };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function statusHtml() {
    if (STATUS.connected === null) return '';
    var up = STATUS.connected;
    var rows = '<div class="conn' + (up ? '' : ' down') + '"><i aria-hidden="true"></i>' +
      (up ? 'Connected' : 'Disconnected') +
      (up && STATUS.uptime ? '<span class="up">' + esc(STATUS.uptime) + '</span>' : '') + '</div>';
    function row(label, value, quiet) {
      return value ? '<div class="row"><span>' + label + '</span><span class="mono' +
        (quiet ? ' quiet' : '') + '" title="' + esc(value) + '">' + esc(value) + '</span></div>' : '';
    }
    return rows + row('Provider', STATUS.provider) + row('Last sweep', STATUS.sweep) +
      row('Build', STATUS.version, true);
  }

  function paintStatus() {
    var card = document.getElementById('cfop-status');
    if (card) card.innerHTML = statusHtml();
    var n = document.getElementById('cfop-inbox-count');
    if (n) { n.textContent = STATUS.inbox ? String(STATUS.inbox) : ''; n.hidden = !STATUS.inbox; }
  }

  function loadStatus() {
    if (document.hidden) return;
    getJson('/api/health').then(function (h) {
      STATUS.connected = true;
      STATUS.uptime = uptimeText(h.uptime_seconds);
      // "1.1.0" for a release, "main-<sha>" or "dev" otherwise (CFOP-92).
      STATUS.version = h.version ? (/^\d/.test(h.version) ? 'v' + h.version : String(h.version)) : '';
    }).catch(function () { STATUS.connected = false; }).then(paintStatus);
    getJson('/api/settings/provider').then(function (p) {
      STATUS.provider = (p && p.backend) ? String(p.backend) : '';
    }).catch(function () {}).then(paintStatus);
    getJson('/api/sweep-reports?limit=1').then(function (d) {
      var r = ((d && d.reports) || [])[0];
      STATUS.sweep = r ? ['#' + r.id, clock(r.swept_at)].filter(Boolean).join(' · ') : '';
    }).catch(function () {}).then(paintStatus);
    getJson('/api/investigations?limit=80').then(function (d) {
      STATUS.inbox = needsAction(d && d.investigations).length;
    }).catch(function () {}).then(paintStatus);
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

  function whoHtml(who) {
    if (!who) return '';
    var name = who.token_label ? 'token ' + who.token_label : (who.username || 'anonymous');
    var sub = [who.token_label ? who.username : '', who.role].filter(Boolean).join(' · ');
    var initials = String(who.username || who.token_label || '?').slice(0, 2).toUpperCase();
    return '<span class="cfop-avatar" aria-hidden="true">' + esc(initials) + '</span>' +
      '<span class="cfop-who" id="cfop-who" title="' + esc(describe(who)) + '">' +
      '<span class="name">' + esc(name) + '</span>' +
      (sub ? '<span class="role">' + esc(sub) + '</span>' : '') + '</span>';
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

  function linkHtml(s, current) {
    var active = s.href === current;
    // The count is the Inbox entry's: hidden until there is one to show, and
    // named for a screen reader, which would otherwise hear "Inbox 42".
    var count = s.count
      ? '<span class="badge" id="cfop-inbox-count" style="--tone:var(--orange)" ' +
        'title="investigations waiting on a human"' + (STATUS.inbox ? '' : ' hidden') + '>' +
        (STATUS.inbox ? esc(STATUS.inbox) : '') + '</span>'
      : '';
    return '<a href="' + s.href + '" title="' + s.label + '"' +
           (active ? ' aria-current="page"' : '') + '>' +
           svg(s.icon) + '<span class="lbl">' + s.label + '</span>' + count + '</a>';
  }

  function render(mount, who) {
    var current = activeHref(window.location.pathname);
    var sections = visibleSections(who);
    var main = sections.filter(function (s) { return s.group !== 'settings'; });
    var settings = sections.filter(function (s) { return s.group === 'settings'; });
    function links(list) { return list.map(function (s) { return linkHtml(s, current); }).join(''); }

    mount.className = 'cfop-rail';
    mount.innerHTML =
      '<a class="cfop-brand" href="/inbox" title="CFOperator">' + svg(BRAND_MARK) + '<span>CFOperator</span></a>' +
      '<nav class="cfop-nav" aria-label="Console sections">' + links(main) + '</nav>' +
      '<div class="cfop-sep"></div>' +
      '<nav class="cfop-nav" aria-label="Settings">' + links(settings) + '</nav>' +
      '<div class="cfop-status" id="cfop-status" role="status">' + statusHtml() + '</div>' +
      // Nothing to log out of when the server is not enforcing auth —
      // offering the control there would imply otherwise. Hidden too until
      // identity lands: an untrusted action is not offered on a guess.
      '<div class="cfop-id">' + whoHtml(who) +
      '<button type="button" class="cfop-logout" id="cfop-logout" aria-label="Log out" title="Log out"' +
      ((who && !who.auth_disabled) ? '' : ' hidden') + '>' + svg(LOGOUT_ICON) + '</button></div>';

    var btn = mount.querySelector('#cfop-logout');
    if (btn) btn.addEventListener('click', function () { logout(btn); });
  }

  /* Ask CFOperator: the chat, one click from any page. Not drawn on the chat
   * itself. A link, not a button — it is navigation. */
  function mountAsk() {
    if (!document.body || !document.body.appendChild) return;
    if (activeHref(window.location.pathname) === '/') return;
    var a = document.createElement('a');
    a.className = 'cfop-ask';
    a.href = '/';
    a.setAttribute('aria-label', 'Ask CFOperator');
    a.innerHTML = svg(BRAND_MARK) + '<span class="tip" aria-hidden="true">Ask CFOperator</span>';
    document.body.appendChild(a);
  }

  function init() {
    var mount = document.getElementById('cfop-nav');
    if (!mount) return;
    // Paint the public sections immediately; Admin appears once identity lands.
    render(mount, null);
    mountAsk();
    me().then(function (who) {
      render(mount, who);
    }).catch(function () {
      // Identity unavailable (auth backend down, or /api/auth/me itself
      // refused). The public nav is still useful; Admin stays hidden and
      // logout stays hidden rather than offering an untrusted action.
    });
    loadStatus();
    // A hidden tab has nobody looking; coming back catches up at once.
    if (typeof setInterval === 'function') setInterval(loadStatus, 60000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) loadStatus(); });
  }

  window.CFOP = { me: me, describe: describe, needsAction: needsAction };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
