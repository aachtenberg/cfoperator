#!/usr/bin/env python3
"""Build the compose bundle a ``v*`` release publishes (CFOP-273).

``scripts/install.sh`` installs the docker-compose trial without a clone: it
downloads this bundle, verifies it, runs ``cfoperator init`` inside the image
and starts the stack. The repo's ``docker-compose.yml`` cannot be shipped as it
is, because its cfoperator services are ``build: .`` — they need the source
tree. The bundle carries a rendered copy instead, identical except that every
built service pulls the release's image.

Rendering is a text transform, not a YAML round trip, so the compose file's
comments (which are most of its documentation) survive into the bundle. The
result is then parsed and checked: no ``build:`` left anywhere, every service
that had one now names the release image, and nothing else changed. A compose
edit the transform does not understand fails the release rather than shipping
a half-rendered file.

The bundle's file list is derived, not listed: the compose files, every
relative bind-mount source they name, and ``.env.example``. A mount added to
the compose file later travels with the bundle without this script changing.

    python scripts/release_bundle.py --image ghcr.io/OWNER/cfoperator:v1.2.3 --out dist
"""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List

import yaml

ROOT = Path(__file__).resolve().parent.parent

#: The asset name scripts/install.sh downloads. tests/test_install_stack.py
#: checks the two agree.
ASSET = "cfoperator-compose.tar.gz"

COMPOSE = "docker-compose.yml"
#: Optional overlays shipped beside the compose file, rendered the same way.
OVERLAYS = ("docker-compose.extras.yml",)
EXTRA_FILES = (".env.example",)

_BUILD_LINE = re.compile(r"^[ \t]+build:[ \t]*\S.*\n", re.M)
_LOCAL_IMAGE_LINE = re.compile(r"^([ \t]+image:[ \t]*)cfoperator:local[ \t]*$", re.M)


class BundleError(Exception):
    """The repo's compose files are not in a shape this script can render."""


def _header(image: str) -> str:
    return (
        f"# Rendered for the release from the repo's docker-compose.yml by\n"
        f"# scripts/release_bundle.py: every cfoperator service pulls\n"
        f"# {image}\n"
        f"# instead of building from a clone. Installed by scripts/install.sh;\n"
        f"# re-run it with CFOP_VERSION=x.y.z to upgrade (your .env is kept).\n"
        f"#\n"
    )


def render(text: str, image: str) -> str:
    """The compose file ``text`` with every ``build:`` replaced by ``image``.

    Raises BundleError when the parsed result is anything other than the
    source with exactly that change.
    """
    rendered = _LOCAL_IMAGE_LINE.sub(lambda m: m.group(1) + image, _BUILD_LINE.sub("", text))
    source = yaml.safe_load(text) or {}
    result = yaml.safe_load(rendered) or {}

    expected = copy.deepcopy(source)
    built = []
    for name, service in (expected.get("services") or {}).items():
        if isinstance(service, dict) and "build" in service:
            del service["build"]
            service["image"] = image
            built.append(name)
    if result != expected:
        raise BundleError(
            "rendering changed more than build/image, or missed a build: — "
            "every built service must be `build: .` followed by `image: cfoperator:local`")
    leftover = [n for n, s in (result.get("services") or {}).items()
                if isinstance(s, dict) and "build" in s]
    if leftover:
        raise BundleError(f"services still build from source: {leftover}")
    return (_header(image) if built else "") + rendered


def relative_mounts(text: str) -> List[str]:
    """Every ``./``-relative bind-mount source in a compose file."""
    doc = yaml.safe_load(text) or {}
    found = []
    for service in (doc.get("services") or {}).values():
        for vol in (service or {}).get("volumes") or []:
            if isinstance(vol, dict):
                source = vol.get("source", "") if vol.get("type") == "bind" else ""
            else:
                source = str(vol).split(":", 1)[0]
            if source.startswith("./"):
                found.append(source[2:].rstrip("/"))
    return sorted(set(found))


def bundle_files(root: Path = ROOT) -> List[str]:
    """Repo-relative paths that go into the bundle, compose files first."""
    compose_files = [COMPOSE] + [o for o in OVERLAYS if (root / o).exists()]
    files = list(compose_files) + list(EXTRA_FILES)
    for name in compose_files:
        for source in relative_mounts((root / name).read_text(encoding="utf-8")):
            if not (root / source).exists():
                raise BundleError(f"{name} mounts ./{source}, which does not exist")
            files.append(source)
    seen, ordered = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def _walk(root: Path, rel: str) -> Iterable[str]:
    path = root / rel
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_file():
                yield child.relative_to(root).as_posix()
    else:
        yield rel


def build(image: str, out: Path, root: Path = ROOT) -> Path:
    """Write ``out/ASSET`` and return its path.

    Reproducible: fixed mtimes, owners and modes, sorted entries, and a gzip
    header without a timestamp, so the same commit and image give the same
    checksum.
    """
    out.mkdir(parents=True, exist_ok=True)
    target = out / ASSET
    compose_files = {COMPOSE, *OVERLAYS}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in bundle_files(root):
            for name in _walk(root, rel):
                data = (root / name).read_bytes()
                if name in compose_files:
                    data = render(data.decode("utf-8"), image).encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    with open(target, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as gz:
        gz.write(raw.getvalue())
    return target


_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def should_move_pointer(tag: str, tags: Iterable[str]) -> bool:
    """True when ``tag`` is the newest final ``vX.Y.Z`` among ``tags``.

    The release-stack job asks this before moving ``cfoperator-latest``. A
    pre-release (anything that is not plain vX.Y.Z, e.g. v1.3.0-rc1) never
    moves it, and neither does a re-run of an older tag.
    """
    mine = _RELEASE_TAG.match(tag)
    if not mine:
        return False
    version = tuple(map(int, mine.groups()))
    finals = [tuple(map(int, m.groups())) for m in map(_RELEASE_TAG.match, tags) if m]
    return all(version >= other for other in finals)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image",
                        help="the release image every built service pulls, e.g. ghcr.io/OWNER/cfoperator:v1.2.3")
    parser.add_argument("--out", default="dist", help="directory to write the bundle into")
    parser.add_argument("--should-move-pointer", metavar="TAG",
                        help="exit 0 if TAG is the newest final v* tag in this checkout, else 1")
    args = parser.parse_args(argv)
    if args.should_move_pointer:
        tags = subprocess.run(["git", "tag", "-l", "v*"], capture_output=True, text=True,
                              check=True, cwd=ROOT).stdout.split()
        return 0 if should_move_pointer(args.should_move_pointer, tags) else 1
    if not args.image:
        parser.error("--image is required")
    if "@" in args.image and not _DIGEST.search(args.image):
        # An empty build output would give "repo:tag@", and that must fail the
        # release rather than ship a bundle no install can pull.
        print(f"error: --image digest is not @sha256:<64 hex>: {args.image!r}", file=sys.stderr)
        return 2
    if ":" not in args.image.rsplit("/", 1)[-1]:
        print("error: --image needs an explicit tag; a bundle must not float", file=sys.stderr)
        return 2
    try:
        path = build(args.image, Path(args.out))
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
