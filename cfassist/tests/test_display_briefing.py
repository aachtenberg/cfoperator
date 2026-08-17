"""A briefing must reach the screen intact.

Rich treats square brackets as style markup by default, and a briefing is API
text full of them — log lines, `[tool]` traces, JSON arrays. Rendered with the
default settings, chunks of the incident disappear silently, which is the worst
kind of display bug: it looks like a working attach against a quiet incident.
"""

from cfassist.display import Display


def _captured():
    chunks = []
    return Display(output_callback=chunks.append), chunks


def test_bracketed_text_survives_rendering():
    """Mutation check: drop `markup=False` from show_briefing and the bracketed
    fragments vanish from the output, failing here."""
    display, chunks = _captured()
    display.show_briefing(
        "[2026-08-16 04:11:02] pod=immich-kiosk-0 [tool] kubectl\n"
        'labels: ["app", "kiosk"]'
    )
    rendered = "".join(chunks)
    assert "[tool]" in rendered
    assert "[2026-08-16 04:11:02]" in rendered
    assert '["app", "kiosk"]' in rendered


def test_markup_that_looks_like_a_style_tag_is_not_swallowed():
    display, chunks = _captured()
    display.show_briefing("threshold [red] exceeded [/red] on node")
    rendered = "".join(chunks)
    assert "[red]" in rendered and "[/red]" in rendered


def test_briefing_goes_to_the_callback_in_tui_mode():
    display, chunks = _captured()
    display.show_briefing("hello")
    assert chunks and "hello" in "".join(chunks)
