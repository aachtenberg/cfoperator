#!/usr/bin/env python3
"""Refresh the console's vendored assets from ``ui/vendor/manifest.json``.

The console ships no build step and reaches no CDN at runtime (a CDN reference
hangs rather than fails on a LAN with no route out), so third-party JavaScript
is committed under ``ui/vendor/``. This is the one place it comes from:

    scripts/vendor_ui.py            # re-download every entry, rewrite the hashes
    scripts/vendor_ui.py --check    # what test_console_vendor.py does, by hand

To bump a version, edit ``version`` in the manifest and run this. The hashes
are the point: ``test_console_vendor.py`` fails on any byte of drift between
the manifest and the files, so a vendored file cannot be edited in place — or
swapped for something else — without the change being deliberate.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "ui" / "vendor"
MANIFEST = VENDOR / "manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_url(meta: dict) -> str:
    return f"https://cdn.jsdelivr.net/npm/{meta['package']}@{meta['version']}/{meta['path']}"


def check() -> int:
    manifest = json.loads(MANIFEST.read_text())
    bad = 0
    for name, meta in manifest["files"].items():
        path = VENDOR / name
        if not path.exists():
            print(f"MISSING  {name}")
            bad += 1
            continue
        actual = sha256(path)
        if actual != meta["sha256"]:
            print(f"DRIFT    {name}: manifest {meta['sha256'][:12]}… file {actual[:12]}…")
            bad += 1
        else:
            print(f"ok       {name}  {meta['package']}@{meta['version']}")
    return 1 if bad else 0


def refresh() -> int:
    manifest = json.loads(MANIFEST.read_text())
    for name, meta in manifest["files"].items():
        url = source_url(meta)
        print(f"fetch    {url}")
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - pinned package URL
            data = resp.read()
        (VENDOR / name).write_bytes(data)
        meta["source"] = url
        meta["sha256"] = hashlib.sha256(data).hexdigest()
        print(f"wrote    {name}  {len(data)} bytes  {meta['sha256'][:12]}…")
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv[1:] else refresh())
