"""The console has no WebSocket, and must not grow one back (CFOP-91).

The agent serves under Waitress, which is WSGI and cannot upgrade a
connection. For a long time index.html carried a WebSocket client anyway:
the connection dot answered to a socket that never opened, and
``submitAnswer`` pushed the operator's answer into it — so answering an
investigation's question from the console silently did nothing while a
working ``POST /api/qa`` sat unused.

These tests are source-level, in the style of test_console_nav.py: they
pin the wiring, not the rendering. Each guards a way the bug could come
back — a socket client reappearing, an answer that stops going over HTTP,
a card that vanishes before the server has taken the answer, a status dot
nothing drives.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent
INDEX = ROOT / "ui" / "index.html"
WEB_SERVER = ROOT / "web_server.py"


def _index():
    return INDEX.read_text(encoding="utf-8")


def _function(html, name):
    """The body of a top-level ``function name(...) {`` in the page script.

    Ends at the first line that is exactly the closing brace at the
    function's own indentation, which is how the page is written.
    """
    m = re.search(r"^( *)function %s\(.*?\n\1\}" % re.escape(name), html, re.S | re.M)
    assert m, f"index.html has no function {name}"
    return m.group(0)


def test_no_websocket_client_in_the_console():
    html = _index()
    assert "new WebSocket" not in html, \
        "index.html opens a WebSocket — Waitress cannot serve one; poll instead"
    assert not re.search(r"\bws\.(send|onmessage|onopen|onclose)\b", html), \
        "index.html still talks to a socket handle"
    assert not re.search(r"""/ws['"`]""", html), \
        "index.html still references the /ws endpoint"


def test_answers_go_over_http():
    """submitAnswer POSTs to /api/qa with the contract web_server.py accepts."""
    body = _function(_index(), "submitAnswer")
    assert "fetch('/api/qa'" in body, "submitAnswer does not call /api/qa"
    assert "method: 'POST'" in body
    assert "question_id" in body and "answer" in body, \
        "POST /api/qa requires question_id and answer"


def test_question_card_survives_a_failed_answer():
    """The card leaves only after the server has said yes.

    An unconditional remove is exactly the old failure: the operator sees
    the question disappear and believes it was answered.
    """
    body = _function(_index(), "submitAnswer")
    # Removal sits inside the success path, after the response was checked.
    ok_check = body.find("if (!r.ok) throw")
    assert ok_check != -1, "submitAnswer never checks the response status"
    removals = [m.start() for m in re.finditer(r"card\.remove\(\)", body)]
    assert removals, "submitAnswer never removes the answered card"
    assert all(pos > ok_check for pos in removals), \
        "the question card is removed before the answer was accepted"
    # And the failure path says so, rather than swallowing it.
    assert ".catch(" in body, "a failed answer must be reported, not eaten"
    assert "answer-error" in body and "toast(" in body


def test_connection_dot_is_driven_by_the_health_poll():
    """Green + Connected on a healthy /api/health, red + Disconnected otherwise.

    The dot and the label must move together — a red dot next to the word
    Connected was the original contradiction.
    """
    html = _index()
    assert 'id="connection-status"' in html
    assert 'id="connection-label"' in html, "the label needs an id so it can follow the dot"

    status = _function(html, "updateStatus")
    assert "fetch('/api/health'" in status
    assert "setConnected(true)" in status and "setConnected(false)" in status, \
        "updateStatus must report both outcomes of the health poll"

    setter = _function(html, "setConnected")
    assert "classList.toggle('disconnected'" in setter
    assert "'Connected'" in setter and "'Disconnected'" in setter


def test_server_registers_no_socket_route():
    source = WEB_SERVER.read_text(encoding="utf-8")
    assert "@self.sock.route" not in source
    assert "Sock(" not in source, "flask-sock cannot serve under Waitress"
    assert "WEBSOCKET_AVAILABLE" not in source
    assert "@self.app.route('/api/qa'" in source, \
        "the page POSTs answers to /api/qa — web_server.py must serve it"
