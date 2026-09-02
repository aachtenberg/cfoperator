"""Tests for the shared console header (ui/nav.js).

The console is static pages with no shared layout. Before nav.js each one
hand-rolled its own header, and they drifted into different markup shapes and
link sets, with the logged-in user shown on only some pages and no way to log
out from any of them.

These tests exist to stop that happening again. They are deliberately about
wiring rather than rendering: that every console page mounts the shared header
and none has started growing its own again. A new page added without the header
should fail here rather than being noticed months later.
"""

from repo_paths import REPO_ROOT
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = REPO_ROOT / "ui"
NAV_JS = UI / "nav.js"

# login.html is deliberately excluded: it is what an *unauthenticated* browser
# sees, so it has no identity to show, nothing to log out of, and no sections
# to navigate to.
CONSOLE_PAGES = ["index.html", "remediations.html", "investigations.html",
                 "account.html", "admin.html"]

# Admin is adminOnly and appears after /me; the rest always render.
SECTIONS = ["/", "/remediations", "/investigations", "/account", "/admin"]


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def test_every_console_page_exists():
    for page in CONSOLE_PAGES:
        assert (UI / page).is_file(), f"{page} is missing"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_mounts_the_shared_header(page):
    """The mount point plus the script — one without the other renders nothing."""
    html = read(page)
    assert 'id="cfop-nav"' in html, f"{page} has no header mount"
    assert re.search(r'<script src="/nav\.js"[^>]*\bdefer\b', html), \
        f"{page} does not load /nav.js with defer"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_page_does_not_hand_roll_its_own_nav(page):
    """Guards against the drift nav.js was introduced to end.

    A page listing sibling sections in its own markup is the exact pattern that
    produced different link sets. The header owns those links now.
    """
    html = read(page)
    header = re.search(r"<header.*?</header>", html, re.S)
    assert header, f"{page} has no <header>"
    body = header.group(0)

    # Its own link to another section, outside the mount.
    for target in ["/remediations", "/investigations", "/account", "/admin"]:
        assert f'href="{target}"' not in body, \
            f'{page} still hard-codes a nav link to {target} in its header'

    # Its own identity readout.
    assert 'id="whoami"' not in html, \
        f"{page} still renders its own identity — nav.js owns that now"


def test_nav_js_covers_every_section():
    js = NAV_JS.read_text(encoding="utf-8")
    for href in SECTIONS:
        assert f"href: '{href}'" in js, f"nav.js is missing the {href} section"
    assert "adminOnly: true" in js, "Admin must be marked adminOnly"


def test_logout_is_a_post():
    """A GET logout can be triggered by a prefetch or a crawler."""
    js = NAV_JS.read_text(encoding="utf-8")
    assert "fetch('/logout', { method: 'POST'" in js, \
        "logout must POST — a link or GET can be triggered without user intent"


def test_logout_lands_on_login():
    js = NAV_JS.read_text(encoding="utf-8")
    assert "window.location.href = '/login'" in js


def test_active_section_is_not_signalled_by_colour_alone():
    js = NAV_JS.read_text(encoding="utf-8")
    assert 'aria-current="page"' in js, \
        "the active section needs aria-current, not just an accent colour"


# --- active-section behaviour -------------------------------------------
#
# Run against node when it is available (it is on the CI runner) rather than
# grepping the source, because the interesting part is a matching rule with
# edge cases: a bare prefix match would let /account claim /accountsomething,
# which is exactly the bug this replaced. Admin is adminOnly, so the stub waits
# for /api/auth/me before asserting (admin role → Admin link present).

_STUB = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
function el(){return{className:'',innerHTML:'',textContent:'',hidden:true,_q:{},
  addEventListener(){},querySelector(s){return this._q[s.replace('#','')];}};}
function active(pathname){
  const mount=el(); mount._q={'cfop-who':el(),'cfop-logout':el()};
  const box={console,window:{location:{pathname,href:''}},
    document:{readyState:'complete',head:{appendChild(){}},
      getElementById:id=>id==='cfop-nav'?mount:null,
      createElement:()=>({textContent:''}),addEventListener:()=>{}},
    fetch:()=>Promise.resolve({ok:true,status:200,
      json:()=>Promise.resolve({username:'u',role:'admin'})})};
  box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
  return box.window.CFOP.me().then(() => new Promise(r => setImmediate(r))).then(() => {
    const m=mount.innerHTML.match(/<a href="([^"]+)"[^>]*aria-current="page"/);
    const n=(mount.innerHTML.match(/aria-current="page"/g)||[]).length;
    return [m?m[1]:null, n];
  });
}
active(process.argv[3]).then(r => console.log(JSON.stringify(r)));
"""


@pytest.mark.parametrize("path,expected", [
    ("/", "/"),
    ("/account", "/account"),
    ("/admin", "/admin"),
    ("/investigations", "/investigations"),
    # pathname keeps a trailing slash — it drops only the query and hash.
    ("/account/", "/account"),
    ("/admin//", "/admin"),
    # A path beneath a section still belongs to it.
    ("/account/settings", "/account"),
    # A sibling that merely starts with the same letters must not be claimed.
    ("/accountsomething", None),
    ("/remediationsX", None),
    # Nothing outside the nav lights anything up.
    ("/login", None),
])
def test_active_section_for_pathname(tmp_path, path, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    stub = tmp_path / "stub.js"
    stub.write_text(_STUB, encoding="utf-8")
    out = subprocess.run([node, str(stub), str(NAV_JS), path],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    href, count = json.loads(out.stdout)

    assert href == expected, f"{path} marked {href!r}, expected {expected!r}"
    assert count == (0 if expected is None else 1), \
        f"{path} marked {count} entries active"


def test_admin_link_hidden_for_members(tmp_path):
    """Members get Account; Admin stays out of the bar."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    runner = tmp_path / "runner.js"
    runner.write_text(r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[2],'utf8');
function el(){return{className:'',innerHTML:'',textContent:'',hidden:true,_q:{},
  addEventListener(){},querySelector(s){return this._q[s.replace('#','')];}};}
const mount=el(); mount._q={'cfop-who':el(),'cfop-logout':el()};
const box={console,window:{location:{pathname:'/',href:''}},
  document:{readyState:'complete',head:{appendChild(){}},
    getElementById:id=>id==='cfop-nav'?mount:null,
    createElement:()=>({textContent:''}),addEventListener:()=>{}},
  fetch:()=>Promise.resolve({ok:true,status:200,
    json:()=>Promise.resolve({username:'u',role:'member'})})};
box.globalThis=box; vm.createContext(box); vm.runInContext(src,box);
box.window.CFOP.me().then(() => new Promise(r => setImmediate(r))).then(() => {
  console.log(JSON.stringify({
    hasAdmin: mount.innerHTML.includes('href="/admin"'),
    hasAccount: mount.innerHTML.includes('href="/account"'),
  }));
});
""", encoding="utf-8")
    out = subprocess.run([node, str(runner), str(NAV_JS)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["hasAccount"] is True
    assert data["hasAdmin"] is False


def test_nav_js_has_no_external_dependencies():
    """This box has no outbound network: a CDN reference hangs rather than fails."""
    js = NAV_JS.read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js, \
        "nav.js must not reference anything off-box"


def test_nav_js_is_served():
    """The route web_server.py registers, exercised for real.

    Built standalone rather than through WebServer, which wants a live operator;
    what matters here is that send_from_directory can find the file and that it
    is served as JavaScript rather than as a download.
    """
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__, root_path=str(REPO_ROOT))

    @app.route("/nav.js")
    def nav_js():
        return flask.send_from_directory("ui", "nav.js", mimetype="application/javascript")

    res = app.test_client().get("/nav.js")
    assert res.status_code == 200
    assert res.headers["Content-Type"].startswith("application/javascript")
    assert b"cfop-navbar" in res.data


def test_web_server_registers_the_route():
    source = (REPO_ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "@self.app.route('/nav.js')" in source, \
        "the pages request /nav.js — web_server.py must serve it"
    assert "@self.app.route('/account')" in source
    assert "@self.app.route('/admin')" in source
    assert "redirect('/admin?tab=users'" in source
    assert "redirect('/admin?tab=tokens'" in source


def test_nav_js_ships_in_the_image():
    """`COPY ui/ ./ui/` carries it, but the pages 404 without it."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ui/ ./ui/" in dockerfile


def test_legacy_users_tokens_pages_are_gone():
    """Content moved into /admin and /account — avoid resurrecting the old pages."""
    assert not (UI / "users.html").exists()
    assert not (UI / "tokens.html").exists()
