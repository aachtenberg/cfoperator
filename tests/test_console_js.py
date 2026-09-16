"""Syntax-check the console's JavaScript (CFOP-79).

The console is static HTML with inline scripts plus two first-party files
(``nav.js``, ``common.js``). Every other console guard we have asserts on the
page's *text* — ids, endpoints, function names. All of those pass on a page
whose script never executes.

Specimen, caught during CFOP-77: a JS block comment contained the glob
``github_*/git_*``. The ``*/`` ended the comment early, the following prose
parsed as code, and Admin threw ``Uncaught SyntaxError``. Markup guards were
green. The failure is total — one syntax error kills the whole inline script —
and invisible to review, because the comment reads correctly to a human.

This suite extracts every ``<script>`` body that is not a ``src=`` tag and
runs ``node --check`` over those bodies plus our own ``ui/*.js``. Vendor is
third-party and already hashed by ``test_console_vendor.py``.

It fails closed if node is missing. A skip is the same class of vacuous green
this exists to close; ``ubuntu-latest`` ships node, so CI has it, and a local
run without node is a failed check rather than a silent pass.
"""

from repo_paths import REPO_ROOT
import re
import shutil
import subprocess

import pytest

UI = REPO_ROOT / "ui"

#: Every console page, login included. Globbed so a seventh page is checked
#: without a list update — the same reason ``test_console_vendor`` globs.
PAGES = sorted(p.name for p in UI.glob("*.html"))

#: First-party files we ship. ``ui/vendor/`` is a subdirectory, so it is not
#: in this glob; hashing that tree is ``test_console_vendor``'s job.
OUR_JS = sorted(p.name for p in UI.glob("*.js"))

_SCRIPT = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
# Not ``\bsrc=``: ``-`` is a non-word character, so that also matches
# ``data-src=`` and would skip a real inline body.
_HAS_SRC = re.compile(r"(?<![\w-])src\s*=", re.IGNORECASE)


def read(name):
    return (UI / name).read_text(encoding="utf-8")


def inline_script_bodies(html):
    """Bodies of ``<script>`` tags that are not ``src=`` loads.

    A tag with ``src`` has no body we authored (the SRI-pinned marked tag
    used to be the specimen; it is vendored now, but the shape is the same
    for ``/nav.js``, ``/common.js``, and ``/vendor/*``). Empty bodies are
    dropped so a ``<script src=...></script>`` cannot pass ``node --check``
    as if it were a script we wrote.
    """
    bodies = []
    for match in _SCRIPT.finditer(html):
        if _HAS_SRC.search(match.group("attrs") or ""):
            continue
        body = match.group("body").strip()
        if body:
            bodies.append(body)
    return bodies


def node_bin():
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node is required to syntax-check console JS (CFOP-79). "
            "Skipping would pass while not guarding. ubuntu-latest ships "
            "node; install it locally to run this suite."
        )
    return node


def node_check(source, path):
    """Write ``source`` to ``path`` and run ``node --check``. Fail the test
    if the check returns non-zero."""
    path.write_text(source, encoding="utf-8")
    out = subprocess.run(
        [node_bin(), "--check", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        pytest.fail(f"{path.name} failed node --check:\n{out.stderr or out.stdout}")
    return out


def test_console_html_pages_are_present():
    """A wrong REPO_ROOT makes the glob empty and every per-page test
    vacuously pass. login.html is the unauthenticated page the ticket named;
    account.html is the sixth page that landed after the ticket said five."""
    assert PAGES, "ui/*.html globbed nothing — REPO_ROOT is wrong"
    assert "login.html" in PAGES
    assert "account.html" in PAGES
    assert len(PAGES) >= 6, f"expected at least six console pages, got {PAGES}"


def test_first_party_js_is_present():
    assert "nav.js" in OUR_JS, "nav.js is the shared header; it must be checked"
    assert "common.js" in OUR_JS, "common.js is first-party, same class as nav.js"


@pytest.mark.parametrize("page", PAGES)
def test_inline_scripts_parse(page, tmp_path):
    bodies = inline_script_bodies(read(page))
    for i, body in enumerate(bodies):
        node_check(body, tmp_path / f"{page}.{i}.js")


@pytest.mark.parametrize("name", OUR_JS)
def test_first_party_js_parses(name):
    out = subprocess.run(
        [node_bin(), "--check", str(UI / name)],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"{name} failed node --check:\n{out.stderr or out.stdout}"


def test_extraction_skips_src_tags():
    """index.html loads marked, common.js, nav.js, then one inline block.
    A naive ``<script>.*</script>`` matcher is fine on today's pages, but a
    ``src`` tag with a body (or an empty one) must not be treated as ours."""
    html = (
        '<script src="/vendor/marked.min.js"></script>\n'
        '<script src="/common.js"></script>\n'
        '<script src="/nav.js" defer></script>\n'
        "<script>\nconst x = 1;\n</script>\n"
    )
    assert inline_script_bodies(html) == ["const x = 1;"]


def test_extraction_does_not_treat_data_src_as_src():
    """``\\bsrc=`` also matches ``data-src=`` because ``-`` is a non-word
    character. Skipping on that would drop a real inline body — the vacuous
    green this suite exists to close."""
    html = (
        '<script data-src="/nope.js">const a = 1;</script>\n'
        '<script src="/nav.js"></script>\n'
        "<script>const b = 2;</script>\n"
    )
    assert inline_script_bodies(html) == ["const a = 1;", "const b = 2;"]


def test_extraction_finds_the_pages_inline_scripts():
    """If the extractor matches nothing, the per-page parse tests pass
    without checking a single byte of JS."""
    found = [p for p in PAGES if inline_script_bodies(read(p))]
    assert found, "extracted zero inline scripts from ui/*.html"


def test_block_comment_glob_fails_node_check(tmp_path):
    """The CFOP-77 specimen: ``*/`` inside a glob, inside a block comment.

    A checker that never actually ran node, or that wrapped the source in a
    way that made it a string, would not fail this. Confirm a valid snippet
    still passes so a broken ``node --check`` invocation cannot hide behind
    a universal failure.
    """
    good = "const pattern = 'github_*/git_*';\n"
    node_check(good, tmp_path / "good.js")

    # The comment is closed by the glob's ``*/``, so ``git_* */`` parses as
    # code and node --check exits non-zero.
    bad = "/* github_*/git_* */\nconst x = 1;\n"
    path = tmp_path / "specimen.js"
    path.write_text(bad, encoding="utf-8")
    out = subprocess.run(
        [node_bin(), "--check", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode != 0, (
        "node --check accepted a block comment terminated by a glob's */; "
        "the CFOP-77 specimen would still ship"
    )
