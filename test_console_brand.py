"""Guard the shared brand mark (header logo + favicon).

The console pages each inline the logo SVG. Investigations once drifted to a
search glyph while every other page kept the clock; the tab favicon was the
browser default (blue) instead of the green mark. These tests catch that class
of drift rather than pinning full markup.
"""

import re
from pathlib import Path

import pytest

UI = Path(__file__).parent / "ui"

# Brand clock glyph shared by header logos and favicon.svg.
BRAND_PATHS = '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>'

CONSOLE_PAGES = [
    "index.html",
    "remediations.html",
    "investigations.html",
    "account.html",
    "admin.html",
]
ALL_PAGES = CONSOLE_PAGES + ["login.html"]

FAVICON_LINK = 'rel="icon" href="/favicon.svg" type="image/svg+xml"'


def read(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def test_favicon_asset_exists_and_matches_brand_mark():
    svg = read("favicon.svg")
    assert 'stroke="#22c55e"' in svg
    # Same clock geometry as the header, scaled into the 32×32 favicon viewBox.
    assert "<circle cx=\"16\" cy=\"16\" r=\"10\"/>" in svg
    assert 'd="M16 10v6l4 2"' in svg


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_links_the_shared_favicon(page):
    assert FAVICON_LINK in read(page), f"{page} is missing the shared favicon link"


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_uses_the_brand_clock_glyph(page):
    html = read(page)
    assert BRAND_PATHS in html, (
        f"{page} does not use the brand clock glyph — "
        "header/login logos must stay the same mark as favicon.svg"
    )


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
