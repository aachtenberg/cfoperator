#!/usr/bin/env python3
"""Every doc points at something that exists (CFOP-547).

Three docs sweeps (CFOP-23, CFOP-99, CFOP-209) each found the same class of
rot: a page naming a file that had moved or been dropped, and an index that
no longer listed what ``docs/`` held. The fourth sweep found a link to
``agent.py`` from before the ``agent/`` package existed, three pages absent
from the README index, and a row still citing ``docs/ROADMAP.md`` two months
after it was removed. None of that needs a reader to catch it.

Three checks, each over the whole doc tree so a new page is covered without
registration:

- every relative Markdown link resolves to a file in this repository, and a
  ``#fragment`` on a link to a Markdown page (or on a bare ``#fragment``)
  names a heading that page has, by GitHub's slug rule;
- every ``docs/*.md`` is linked from the README, which is the only index;
- every back-ticked ``dir/file.ext`` whose first segment is a top-level
  directory of this repository resolves. Paths into other repositories
  (``k3s/...``, ``ansible/...`` in homelab-infra) do not start with one of
  our directories and are not checked; "create this file" examples are
  listed explicitly below, with the reason each one is hypothetical.

What is deliberately NOT checked: prose, and anything a page says *about* the
file it names. Those are what the sweeps are for.
"""

from __future__ import annotations

import re

import pytest

from repo_paths import REPO_ROOT

DOC_PAGES = sorted((REPO_ROOT / "docs").glob("*.md"))
ROOT_PAGES = [REPO_ROOT / n for n in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md")]
ALL_PAGES = DOC_PAGES + ROOT_PAGES

# Back-ticked paths a doc tells the reader to create, or that exist only on a
# training box. Each entry says why it is not expected in the tree.
HYPOTHETICAL_PATHS = {
    # infrastructure-config.md's "writing your own" walkthrough (kept on
    # purpose by CFOP-23): the file is the reader's to write.
    "observability/mymetrics.py",
}

_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
_ANCHORED = re.compile(r"\]\(([^)#\s]*)#([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_HTML_ID = re.compile(r"""<a\s+(?:name|id)=["']([^"']+)["']""")
_PATH = re.compile(
    r"`([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+"
    r"\.(?:py|md|yml|yaml|json|sh|html|js|go|txt|toml|cfg|ini|j2|example))`"
)


def _slugs(page) -> set[str]:
    """Anchors GitHub generates for a page: one per heading, -1/-2 on repeats.

    GitHub lowercases the heading's text, drops everything that is not a word
    character, a hyphen or a space, and turns spaces into hyphens. Inline code
    and emphasis markers go with the punctuation; link text survives its URL.
    """
    seen: dict[str, int] = {}
    out = set()
    in_fence = False
    for line in page.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        out.update(_HTML_ID.findall(line))
        m = None if in_fence else _HEADING.match(line)
        if not m:
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def _top_level_dirs() -> set[str]:
    return {p.name for p in REPO_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")} | {".github"}


def _lines(page):
    return enumerate(page.read_text(encoding="utf-8").splitlines(), 1)


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_relative_link_resolves(page):
    broken = []
    for n, line in _lines(page):
        for m in _LINK.finditer(line):
            target = m.group(1)
            if re.match(r"[a-z]+:", target):  # http(s):, mailto:
                continue
            if not (page.parent / target).resolve().exists():
                broken.append(f"{page.relative_to(REPO_ROOT)}:{n} -> {target}")
    assert not broken, "links to files that do not exist:\n" + "\n".join(broken)


def test_readme_indexes_every_doc_page():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in DOC_PAGES if f"docs/{p.name}" not in readme]
    assert not missing, (
        "docs/ pages not linked from README.md, which is the only index a reader "
        "has:\n  " + "\n  ".join(missing)
        + "\nAdd each to the Documentation section, or fold it into the page that owns the subject."
    )


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_in_repo_path_reference_resolves(page):
    tops = _top_level_dirs()
    dead = []
    for n, line in _lines(page):
        for m in _PATH.finditer(line):
            rel = m.group(1)
            if rel.split("/", 1)[0] not in tops or rel in HYPOTHETICAL_PATHS:
                continue
            if not (REPO_ROOT / rel).exists():
                dead.append(f"{page.relative_to(REPO_ROOT)}:{n}  {rel}")
    assert not dead, (
        "paths named in back-ticks that do not exist in this repository:\n"
        + "\n".join(dead)
        + "\nFix the reference, or add it to HYPOTHETICAL_PATHS with the reason."
    )


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_anchor_names_a_heading(page):
    """A link that lands on the top of a 1,000-line page instead of the section
    it names is as lost as a dead one; three such links turned up in one sweep."""
    broken = []
    for n, line in _lines(page):
        for m in _ANCHORED.finditer(line):
            target, fragment = m.group(1), m.group(2)
            if re.match(r"[a-z]+:", target):
                continue
            dest = (page.parent / target).resolve() if target else page
            if dest.suffix != ".md" or not dest.exists():
                continue  # a dead file is the other test's failure
            if fragment.lower() not in _slugs(dest):
                broken.append(f"{page.relative_to(REPO_ROOT)}:{n} -> {target}#{fragment}")
    assert not broken, "anchors that name no heading on their page:\n" + "\n".join(broken)
