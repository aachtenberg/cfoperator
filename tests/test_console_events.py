"""The console's /api/events proxy to the event runtime (CFOP-215).

Driven against the runtime's real request handler with its bearer gate on, so
what is tested is the actual round trip: the agent's client sends the token,
the runtime validates the query, and each way it can fail reaches the page as
a distinct reason. A proxy that turned every failure into one opaque 500
would leave the operator guessing whether to log in again, fix a deploy, or
wait for Postgres.
"""
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

from alert_events import fleet
from event_runtime.defaults import OpenReasoningDecisionEngine
from event_runtime.engine import EventRuntime
from event_runtime.plugin_manager import PluginManager
from event_runtime.server import make_handler
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink

RUNTIME_TOKEN = "runtime-bearer-for-tests"
CONSOLE_TOKEN = "console-token-for-tests"


@pytest.fixture
def runtime_url(tmp_path, monkeypatch):
    outbox = LocalOutboxStateSink(directory=str(tmp_path / "outbox"))
    outbox.append(fleet(12))
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([outbox]))
    plugins.register_decision_engine(OpenReasoningDecisionEngine())
    monkeypatch.setenv("CFOP_RUNTIME_TOKEN", RUNTIME_TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(EventRuntime(plugins)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_URL", url)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def console(monkeypatch):
    """The real WebServer routes behind the real console gate (legacy env
    token mode, so no database is needed)."""
    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.current_investigation = None
    operator.config = {}
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    server._cockpit = None
    server._ladder = None
    server._setup_routes()
    for name, value in {"CFOP_AUTH_DISABLED": "", "CFOP_SESSION_SECRET": "test-session-secret",
                        "CFOP_UI_USERNAME": "operator",
                        "CFOP_UI_PASSWORD_HASH": generate_password_hash("not-used-here"),
                        "CFOP_API_TOKEN": CONSOLE_TOKEN}.items():
        monkeypatch.setenv(name, value)
    install_auth(server.app, ui_dir="ui", store=None)
    client = server.app.test_client()

    def get(path, **kw):
        return client.get(path, headers={"Authorization": f"Bearer {CONSOLE_TOKEN}"}, **kw)
    get.anonymous = client.get
    return get


def test_the_console_lists_alerts_through_the_runtime(runtime_url, console):
    resp = console("/api/events?limit=5&source=cfoperator-sweep")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["store"] == "outbox" and body["alerts"]
    assert all(row["source"] == "cfoperator-sweep" for row in body["alerts"])


def test_paging_passes_the_cursor_through(runtime_url, console):
    first = console("/api/events?limit=4").get_json()
    second = console(f"/api/events?limit=4&cursor={first['next_cursor']}").get_json()
    ids = [r["alert_id"] for r in first["alerts"]] + [r["alert_id"] for r in second["alerts"]]
    assert len(ids) == len(set(ids)) == 8


def test_detail_and_unknown_alert(runtime_url, console):
    alert_id = console("/api/events?limit=1").get_json()["alerts"][0]["alert_id"]
    detail = console(f"/api/events/{alert_id}")
    assert detail.status_code == 200 and detail.get_json()["events"]
    missing = console("/api/events/no-such-alert")
    assert missing.status_code == 404 and missing.get_json()["reason"] == "not_found"


def test_the_runtimes_own_validation_reaches_the_page(runtime_url, console):
    resp = console("/api/events?statu=failed")
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "bad_request" and "statu" in resp.get_json()["error"]


def test_a_refused_token_is_a_deploy_problem_not_a_login_one(runtime_url, console, monkeypatch):
    monkeypatch.setattr("event_runtime.http_actions._expected_runtime_token", lambda: "runtime-only")
    resp = console("/api/events")
    assert resp.status_code == 502
    assert resp.get_json()["reason"] == "unauthorized"
    assert "CFOP_RUNTIME_TOKEN" in resp.get_json()["error"]


def test_a_runtime_that_is_down(console, monkeypatch):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_URL", "http://127.0.0.1:9")
    resp = console("/api/events")
    assert resp.status_code == 502 and resp.get_json()["reason"] == "unreachable"


def test_no_runtime_configured(console, monkeypatch):
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_URL", raising=False)
    resp = console("/api/events")
    assert resp.status_code == 503 and resp.get_json()["reason"] == "not_configured"


def test_the_route_is_behind_the_console_gate(runtime_url, console):
    assert console.anonymous("/api/events").status_code == 401
    assert console.anonymous("/api/events/x").status_code == 401
