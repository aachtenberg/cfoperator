"""Vendored console assets: pinned, hashed, and the only third-party code (CFOP-59, CFOP-90).

The console rules are no build step and no outbound network — a CDN reference
hangs rather than fails on a LAN with no route out, which is the LAN an
incident happens on. Third-party code the console ships (xterm.js, marked) is
committed under ``ui/vendor/`` with a manifest of sha256s.

Two classes of regression, guarded separately:

* a vendored file edited in place, or swapped, without the manifest changing —
  the ``DEFAULT_CFASSIST_VERSION`` shape: silent drift is the failure mode, so
  the hash is what is checked, not the version string;
* a page growing a reference to another origin. Until CFOP-90 this checked
  ``<script src>`` only and named ``marked`` as the sole exception — which is
  how three pages carried a render-blocking ``fonts.googleapis.com``
  stylesheet for a year without a test noticing. It now scans the whole page
  for ``http(s)://`` and allowlists nothing, the same rule
  ``test_console_nav.py`` already applies to ``nav.js``.
"""

from repo_paths import REPO_ROOT
import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = REPO_ROOT
UI = REPO_ROOT / "ui"
VENDOR = UI / "vendor"
MANIFEST = VENDOR / "manifest.json"


def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_every_vendored_file_matches_its_manifest_hash():
    files = manifest()["files"]
    assert files, "an empty manifest vendors nothing"
    for name, meta in files.items():
        path = VENDOR / name
        assert path.exists(), f"{name} is in the manifest but not in ui/vendor/"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == meta["sha256"], (
            f"{name} differs from the manifest — a vendored file was edited or swapped. "
            "Bump the version in the manifest and run scripts/vendor_ui.py to change it "
            "deliberately.")


def test_every_vendored_file_is_in_the_manifest():
    """A file dropped into ui/vendor/ that nothing hashes is a file nothing
    guards. Licences and the manifest itself are the only exceptions."""
    listed = set(manifest()["files"])
    for path in VENDOR.iterdir():
        if path.name == "manifest.json" or path.name.startswith("LICENSE"):
            continue
        assert path.name in listed, f"ui/vendor/{path.name} is not in the manifest"


def test_every_vendored_package_ships_its_licence():
    packages = {meta["package"].split("/")[-1] for meta in manifest()["files"].values()}
    for package in packages:
        assert (VENDOR / f"LICENSE.{package}").exists(), (
            f"no LICENSE.{package} beside the vendored file")


def test_the_manifest_pins_a_version_and_a_source():
    for name, meta in manifest()["files"].items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", meta["version"]), f"{name}: unpinned version"
        assert meta["source"].startswith("https://") and meta["version"] in meta["source"], (
            f"{name}: the source URL does not name the pinned version")


#: Anything that would make the browser open a connection to another origin:
#: an absolute URL anywhere in the page (script, stylesheet, preconnect,
#: preload, image, @import, fetch — the tag does not matter, the origin does),
#: or a protocol-relative one. No allowlist: the fix is scripts/vendor_ui.py.
EXTERNAL_REFERENCE = re.compile(r'https?://|(?:src|href)=["\']//', re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def test_no_page_references_an_external_origin():
    """Every page in ui/, login included: it is the operator's browser that
    fetches these, and a WAN outage is exactly when the console gets opened.
    A render-blocking stylesheet stalls first paint on a TCP timeout; a
    missing script can kill a page outright."""
    for page in sorted(UI.glob("*.html")):
        # HTML comments never open a connection, so a documentation URL in
        # one is not a network reference; strip them before scanning.
        html = HTML_COMMENT.sub("", page.read_text(encoding="utf-8"))
        hits = [html[m.start():m.start() + 80].splitlines()[0] for m in EXTERNAL_REFERENCE.finditer(html)]
        assert not hits, (
            f"{page.name} reaches off-box: {hits}. The console ships no build step and "
            "reaches no network at runtime: vendor it under ui/vendor/ (see "
            "scripts/vendor_ui.py) or drop it.")


def test_the_chat_page_loads_marked_from_the_vendor_path():
    """marked used to come from jsdelivr, pinned and SRI-checked, and a failed
    load killed the whole chat script (CFOP-90)."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="/vendor/marked.min.js"' in html, "index.html does not load /vendor/marked.min.js"
    assert "marked.min.js" in manifest()["files"], "marked.min.js is not in the vendor manifest"


def test_vendored_scripts_are_served_from_vendor_paths():
    """Pages that use a vendored asset reference it by its served path, not
    by a copy somewhere else in ui/."""
    names = set(manifest()["files"])
    for page in sorted(UI.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        for name in names:
            for ref in re.findall(r'(?:src|href)="([^"]*' + re.escape(name) + r')"', html):
                assert ref == f"/vendor/{name}", f"{page.name} references {ref}, want /vendor/{name}"
