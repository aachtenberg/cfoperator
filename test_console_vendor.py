"""Vendored console assets: pinned, hashed, and the only third-party code (CFOP-59).

The console rules are no build step and no outbound network — a CDN reference
hangs rather than fails on a LAN with no route out, which is the LAN an
incident happens on. xterm.js is the first third-party script the console
ships, so it is committed under ``ui/vendor/`` with a manifest of sha256s.

Two classes of regression, guarded separately:

* a vendored file edited in place, or swapped, without the manifest changing —
  the ``DEFAULT_CFASSIST_VERSION`` shape: silent drift is the failure mode, so
  the hash is what is checked, not the version string;
* a page growing a CDN ``<script src="http…">``. The one that exists is
  ``marked`` in ``index.html``, pinned and SRI-checked, and it is named here
  as the sole exception rather than tolerated by pattern.
"""

import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
UI = REPO_ROOT / "ui"
VENDOR = UI / "vendor"
MANIFEST = VENDOR / "manifest.json"

#: The single permitted remote script, by page. Anything else is a hang
#: waiting to happen on a disconnected LAN.
CDN_EXCEPTIONS = {"index.html": "marked"}


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


def test_no_page_reaches_a_cdn_for_a_script():
    for page in sorted(UI.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        remote = re.findall(r'<script[^>]*\ssrc="(https?://[^"]+)"[^>]*>', html)
        allowed = CDN_EXCEPTIONS.get(page.name)
        for url in remote:
            assert allowed and allowed in url, (
                f"{page.name} loads {url} from a CDN. The console ships no build step and "
                "reaches no network at runtime: vendor it under ui/vendor/ (see "
                "scripts/vendor_ui.py) instead.")
        if allowed:
            tags = re.findall(r'<script[^>]*src="https?://[^"]*' + re.escape(allowed) + r'[^"]*"[^>]*>', html)
            for tag in tags:
                assert 'integrity="' in tag, f"{page.name}: the {allowed} CDN script has lost its SRI hash"


def test_vendored_scripts_are_served_from_vendor_paths():
    """Pages that use a vendored asset reference it by its served path, not
    by a copy somewhere else in ui/."""
    names = set(manifest()["files"])
    for page in sorted(UI.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        for name in names:
            for ref in re.findall(r'(?:src|href)="([^"]*' + re.escape(name) + r')"', html):
                assert ref == f"/vendor/{name}", f"{page.name} references {ref}, want /vendor/{name}"
