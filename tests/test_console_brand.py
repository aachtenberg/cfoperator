"""Guard the shared brand mark (rail logo, Ask button, favicon, login).

The console pages each used to inline the logo SVG. Investigations once drifted
to a search glyph while every other page kept the mark; the tab favicon was the
browser default (blue) instead of the green one. Since CFOP-696 the rail in
nav.js draws the logo for every console page, so the drift these tests guard is
narrower and the guard is different: one copy in nav.js, the same two paths in
the two files that cannot load nav.js (favicon.svg, login.html), and no page
growing a logo of its own again.
"""

from repo_paths import REPO_ROOT
import re

import pytest

UI = REPO_ROOT / "ui"

# The loop circling a spark. Geometry only: stroke and fill differ by where
# the mark is drawn (currentColor in the pages, the accent in the favicon).
BRAND_LOOP = 'd="M20 12a8 8 0 1 1-2.35-5.65M20 4v4h-4"'
BRAND_SPARK = 'd="M12 7.6l1.1 3.3 3.3 1.1-3.3 1.1-1.1 3.3-1.1-3.3-3.3-1.1 3.3-1.1z"'
# The mark this one replaced. Nothing should still draw it.
OLD_CLOCK = '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>'

CONSOLE_PAGES = [
    "index.html",
    "inbox.html",
    "remediations.html",
    "investigations.html",
    "events.html",
    "account.html",
    "admin.html",
]
ALL_PAGES = CONSOLE_PAGES + ["login.html"]

FAVICON_LINK = 'rel="icon" href="/favicon.svg" type="image/svg+xml"'


def read(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["nav.js", "favicon.svg", "login.html"])
def test_the_mark_is_the_same_two_paths_everywhere_it_is_drawn(name):
    text = read(name)
    assert BRAND_LOOP in text and BRAND_SPARK in text, (
        f"{name} does not draw the brand mark — the rail, the favicon and the "
        "login page must stay the same glyph")


def test_favicon_is_the_green_mark():
    svg = read("favicon.svg")
    assert 'stroke="#22c55e"' in svg


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_links_the_shared_favicon(page):
    assert FAVICON_LINK in read(page), f"{page} is missing the shared favicon link"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_console_pages_do_not_draw_their_own_logo(page):
    """The rail owns the logo. A page inlining the mark is the copy that drifts."""
    html = read(page)
    header = re.search(r"<header.*?</header>", html, re.S)
    assert header, f"{page} has no <header>"
    assert "<svg" not in header.group(0) or page == "index.html", (
        f"{page} draws an icon in its header; the logo is the rail's (ui/nav.js)")
    assert BRAND_LOOP not in header.group(0), f"{page} inlines the brand mark in its header"
    assert 'class="logo"' not in html, f"{page} still carries a .logo of its own"


@pytest.mark.parametrize("name", ALL_PAGES + ["nav.js", "favicon.svg"])
def test_nothing_still_draws_the_old_clock(name):
    assert OLD_CLOCK not in read(name), f"{name} still draws the mark CFOP-696 replaced"


def test_favicon_is_auth_exempt():
    """Login renders before a session; the tab icon must still load."""
    from web_auth import EXEMPT_PATHS

    assert "/favicon.svg" in EXEMPT_PATHS


def test_investigations_does_not_use_search_glyph():
    """Regression: Investigations once substituted a magnifying-glass mark."""
    html = read("investigations.html")
    header = re.search(r"<header.*?</header>", html, re.S)
    assert header, "investigations.html has no <header>"
    assert 'd="M21 21l-4.3-4.3"' not in header.group(0)
