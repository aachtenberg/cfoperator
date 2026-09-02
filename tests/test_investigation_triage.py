"""Operator triage of an investigation (CFOP-65).

Until this landed the investigations table was append-only from the console's
side: the read API returned `triage_action`/`operator_notes` but nothing could
set them. An attached cfassist session read those fields and told a human to
"go to the console and change the status to Resolved" — a control that did not
exist. These guard the endpoint's contract, not its wording.
"""

from repo_paths import REPO_ROOT
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT))

import web_server  # noqa: E402


def _server():
    op = MagicMock()
    op.kb = MagicMock()
    op.kb.update_investigation_triage.return_value = True
    op.kb.get_investigation.return_value = {
        "id": 7, "triage_action": "resolved", "operator_notes": "verified by hand",
        "outcome": "needs_action",
    }
    ws = web_server.WebServer.__new__(web_server.WebServer)
    ws.operator = op
    from flask import Flask
    ws.app = Flask(__name__, static_folder="ui", static_url_path="",
                  root_path=str(REPO_ROOT))
    ws.app.config["TESTING"] = True
    ws._chat_sessions = {}
    import threading
    ws._sessions_lock = threading.Lock()
    # Mirrors __init__'s WEBSOCKET_AVAILABLE=False branch; _setup_routes reads
    # both. Constructed field-by-field rather than through __init__ so the test
    # needs no database, no auth store and no live operator.
    ws._setup_routes()

    # Run the REAL require_role decorator, in its auth-disabled mode (a
    # supported deployment state, see web_auth). Stubbing the decorator out
    # instead would leave these tests passing even if the gate were removed —
    # test_the_endpoint_is_admin_gated covers that it is present, and this
    # covers the behaviour behind it.
    class _StubAuth:
        disabled = True

    @ws.app.before_request
    def _grant():
        from flask import g
        g.cfop_auth = _StubAuth()

    return ws, op


def test_resolve_records_the_action_and_note():
    ws, op = _server()
    res = ws.app.test_client().post("/api/investigations/7/triage",
                                    json={"action": "resolved", "note": "verified by hand"})
    assert res.status_code == 200
    op.kb.update_investigation_triage.assert_called_once()
    args, kwargs = op.kb.update_investigation_triage.call_args
    assert args[0] == 7 and args[1] == "resolved"
    assert kwargs["operator_notes"] == "verified by hand"


def test_the_agents_outcome_is_never_overwritten():
    """`outcome` is the agent's own conclusion and is what
    find_similar_investigations_hybrid cites as precedent to FUTURE triage
    decisions. Letting an operator verdict rewrite it would edit the corpus
    later classifications reason from — the human's verdict belongs in
    triage_action."""
    ws, op = _server()
    ws.app.test_client().post("/api/investigations/7/triage",
                              json={"action": "resolved", "note": "n"})
    _args, kwargs = op.kb.update_investigation_triage.call_args
    assert "outcome" not in kwargs or kwargs["outcome"] is None


@pytest.mark.parametrize("action", ["retry", "context", "suppress", "", "bogus"])
def test_unimplemented_actions_are_refused(action):
    """retry/context need the re-investigation path wired; suppress needs a
    reader in the alert path. Accepting them would write a value that changes
    nothing while looking like it worked."""
    ws, op = _server()
    res = ws.app.test_client().post("/api/investigations/7/triage",
                                    json={"action": action})
    assert res.status_code == 400
    op.kb.update_investigation_triage.assert_not_called()


def test_missing_investigation_is_404():
    ws, op = _server()
    op.kb.update_investigation_triage.return_value = False
    res = ws.app.test_client().post("/api/investigations/999/triage",
                                    json={"action": "ack"})
    assert res.status_code == 404


def test_the_endpoint_is_admin_gated():
    """Triage changes what the console asserts about production history, so it
    is admin per the role policy. A dropped decorator must fail loudly."""
    src = (ROOT / "web_server.py").read_text(encoding="utf-8")
    idx = src.index("def triage_investigation_api")
    preceding = src[:idx].rsplit("@self.app.route", 1)[1]
    assert "require_role(ROLE_ADMIN)" in preceding


def test_console_exposes_the_control():
    """The gap was a missing control, not a missing backend — guard the UI
    half too, or the endpoint exists and nobody can reach it."""
    html = (ROOT / "ui" / "investigations.html").read_text(encoding="utf-8")
    assert "/triage" in html
    assert "triageBlock" in html
    for action in ("resolved", "ack"):
        assert f"'{action}'" in html


def test_the_list_payload_carries_the_operator_verdict():
    """PR #151 review: without this the badge and filter never fire, because
    the LIST endpoint omitted triage_action even though the drill-in returned
    it — a resolved row would keep sitting in the needs_action pile looking
    unhandled, which is the visible half of the gap this issue closes."""
    src = (ROOT / "agent" / "knowledge_base.py").read_text(encoding="utf-8")
    start = src.index("def get_recent_investigations")
    body = src[start:start + 2000]
    assert '"triage_action": inv.triage_action' in body


def test_the_list_shows_and_filters_on_the_verdict():
    html = (ROOT / "ui" / "investigations.html").read_text(encoding="utf-8")
    assert "r.triage_action" in html, "the row never renders the verdict"
    assert "__triaged" in html, "no way to filter by triaged state"
