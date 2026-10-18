"""The console side of a host-write confirm (CFOP-442).

The class of regression guarded here: something other than a console admin
session answering a host-write question (a token, a member, a chat that never
asked it), a Stop that leaves a turn parked on a question, a transcript that
forgets what the admin was shown, and the page rendering model-written text
as markup.
"""

import os
import re
import threading
import time
from unittest.mock import MagicMock

from flask import Flask
from sqlalchemy import create_engine

from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore
from repo_paths import REPO_ROOT

PASSWORD = "correct horse battery staple"


def _client(*, store=None, auth_disabled=True, operator=None):
    from web_auth import install_auth
    from web_server import WebServer

    operator = operator if operator is not None else MagicMock()
    operator.resolve_host_write_confirm.return_value = "ok"
    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = store
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "true" if auth_disabled else ""
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN"):
        os.environ[name] = ""
    try:
        install_auth(server.app, ui_dir="ui", store=store)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return server.app.test_client(), server, operator


def _store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    admin = s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    s.create_user("bob", PASSWORD, role=ROLE_MEMBER)
    return s, admin


def _login(client, username):
    assert client.post("/login", json={"username": username, "password": PASSWORD}).status_code == 200


def _asked(server, chat_id="c1", confirms=("q1",), done=False):
    server._chat_sessions[chat_id] = {"events": [], "cursor": 0, "done": done, "cancelled": False,
                                      "created": time.time(), "confirms": set(confirms)}


def test_an_admin_session_answers_a_question_its_chat_asked():
    s, _ = _store()
    client, server, op = _client(store=s, auth_disabled=False)
    _login(client, "root")
    _asked(server)
    resp = client.post("/api/chat/c1/confirm", json={"confirm_id": "q1", "approve": True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {"status": "approved", "by": "root"}
    # The one who clicks is the approver on the change record.
    op.resolve_host_write_confirm.assert_called_once_with("q1", True, "root")


def test_a_remediate_token_cannot_answer():
    """require_role maps remediate to admin; a token was never shown the
    command and cannot approve it."""
    s, admin = _store()
    _row, secret = s.create_token("slack-bridge", ["remediate"], created_by=admin["id"])
    client, server, op = _client(store=s, auth_disabled=False)
    _asked(server)
    resp = client.post("/api/chat/c1/confirm", json={"confirm_id": "q1", "approve": True},
                       headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 403
    op.resolve_host_write_confirm.assert_not_called()


def test_a_member_cannot_answer():
    s, _ = _store()
    client, server, op = _client(store=s, auth_disabled=False)
    _login(client, "bob")
    _asked(server)
    resp = client.post("/api/chat/c1/confirm", json={"confirm_id": "q1", "approve": True})
    assert resp.status_code == 403
    op.resolve_host_write_confirm.assert_not_called()


def test_a_question_from_another_chat_cannot_be_answered_here():
    client, server, op = _client()
    _asked(server, "c1", ("q1",))
    _asked(server, "c2", ("q2",))
    resp = client.post("/api/chat/c1/confirm", json={"confirm_id": "q2", "approve": True})
    assert resp.status_code == 404
    op.resolve_host_write_confirm.assert_not_called()


def test_the_answer_must_be_a_boolean():
    client, server, op = _client()
    _asked(server)
    for body in ({"confirm_id": "q1"}, {"confirm_id": "q1", "approve": "yes"}, {"approve": True}):
        assert client.post("/api/chat/c1/confirm", json=body).status_code == 400
    op.resolve_host_write_confirm.assert_not_called()


def test_an_answered_or_expired_question_says_so():
    client, server, op = _client()
    _asked(server)
    op.resolve_host_write_confirm.return_value = "answered"
    assert client.post("/api/chat/c1/confirm", json={"confirm_id": "q1", "approve": True}).status_code == 409
    op.resolve_host_write_confirm.return_value = "unknown"
    assert client.post("/api/chat/c1/confirm", json={"confirm_id": "q1", "approve": True}).status_code == 410


def test_stop_declines_every_question_the_turn_is_parked_on():
    """Otherwise the worker sits out the whole confirm window before it
    notices the Stop."""
    client, server, op = _client()
    _asked(server, confirms=("q1", "q2"))
    assert client.post("/api/chat/c1/stop").status_code == 200
    answered = {(cid, approved, why) for cid, approved, _who, why in
                (c.args for c in op.resolve_host_write_confirm.call_args_list)}
    assert answered == {("q1", False, "stopped"), ("q2", False, "stopped")}


def _run_turn(client, op, events, headers=None):
    def stream(*args, **kwargs):
        yield from events
    op.handle_chat_message_stream.side_effect = stream
    resp = client.post("/api/chat", json={"message": "stop square-redis"}, headers=headers or {})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    chat_id = resp.get_json()["chat_id"]
    for _ in range(200):
        if client.get(f"/api/chat/events/{chat_id}", headers=headers or {}).get_json().get("done"):
            break
        time.sleep(0.01)
    return chat_id, op.handle_chat_message_stream.call_args.kwargs


def test_the_route_remembers_which_questions_a_turn_asked():
    client, server, op = _client()
    chat_id, _ = _run_turn(client, op, [
        {"event": "confirm", "data": {"id": "qq", "host": "box1", "command": "x", "reason": "r"}},
        {"event": "done", "data": {"response": "ok"}},
    ])
    assert server._chat_sessions[chat_id]["confirms"] == {"qq"}


def test_a_browser_session_turn_may_confirm_and_a_token_turn_may_not():
    s, admin = _store()
    _row, secret = s.create_token("mcp", ["remediate"], created_by=admin["id"])
    client, server, op = _client(store=s, auth_disabled=False)
    done = [{"event": "done", "data": {"response": "ok"}}]
    _, kw = _run_turn(client, op, done, headers={"Authorization": f"Bearer {secret}"})
    assert kw["actor_role"] == "admin" and kw["can_confirm_host_writes"] is False
    _login(client, "root")
    _, kw = _run_turn(client, op, done)
    assert kw["can_confirm_host_writes"] is True


def test_the_transcript_keeps_the_question_and_its_answer():
    from web_server import persist_chat_event
    kb = MagicMock()
    persist_chat_event(kb, 5, {"event": "confirm", "data": {
        "id": "q1", "host": "box1", "command": "sudo -n docker stop x", "reason": "stops a container"}})
    persist_chat_event(kb, 5, {"event": "confirm_result", "data": {
        "id": "q1", "decision": "approved", "by": "root"}})
    (call_q, call_a) = kb.append_chat_message.call_args_list
    assert call_q.args[1] == "tool_call"
    assert call_q.kwargs["extra"]["args"]["command"] == "sudo -n docker stop x"
    assert call_a.args[1:] == ("tool_result", "approved by root")


# ---- the page --------------------------------------------------------------

INDEX = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def _function(name):
    m = re.search(r"function " + name + r"\(.*?\n        \}\n", INDEX, re.S)
    assert m, f"{name} not found in ui/index.html"
    return m.group(0)


def test_the_chat_renders_and_settles_a_question():
    handler = _function("handleChatEvent")
    assert "type === 'confirm'" in handler and "addHostConfirm(" in handler
    assert "type === 'confirm_result'" in handler and "settleHostConfirm(" in handler
    assert "/api/chat/${chatId}/confirm" in _function("answerHostConfirm")


def test_the_command_is_text_never_markup():
    """The command is model output, and the model reads alert and log text.
    It is put on the page with textContent; every other field the card
    interpolates goes through escapeHtml."""
    card = _function("addHostConfirm")
    assert ".hc-command').textContent = q.command" in card
    assert "${q.command" not in card and "${escapeHtml(q.command" not in card
    for interpolated in re.findall(r"\$\{([^}]*)\}", card):
        assert "q." not in interpolated or "escapeHtml(" in interpolated, interpolated
