"""The assembled console: gate + blueprint + pages together.

The unit suites each cover one layer well, which is exactly how the response
shape of ``GET /api/auth/users`` got to be ``{"users": [...]}`` on the server
while the page that consumes it expected a bare array. Nothing failed — the
table simply rendered empty. These tests pin the seams the layers meet at.
"""

from repo_paths import REPO_ROOT
import os

import pytest
from flask import Flask, redirect, send_from_directory
from sqlalchemy import create_engine

import web_auth
from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.routes import build_auth_blueprint
from auth.store import AuthStore

PASSWORD = "correct horse battery staple"


@pytest.fixture
def app(monkeypatch):
    for k in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN", "CFOP_AUTH_DISABLED"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("CFOP_SESSION_SECRET", "test-session-secret")

    store = AuthStore(engine=create_engine("sqlite://"))
    store.ensure_schema()
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    store.create_user("alice", PASSWORD, role=ROLE_MEMBER)

    flask_app = Flask(__name__, static_folder="ui", static_url_path="",
                  root_path=str(REPO_ROOT))
    flask_app.config["TESTING"] = True

    # Mirrors web_server.py: the console pages, then the blueprint, then the
    # gate over everything.
    flask_app.add_url_rule(
        "/account", "account", lambda: send_from_directory("ui", "account.html")
    )
    flask_app.add_url_rule(
        "/admin", "admin", lambda: send_from_directory("ui", "admin.html")
    )
    flask_app.add_url_rule(
        "/users", "users", lambda: redirect("/admin?tab=users", code=302)
    )
    flask_app.add_url_rule(
        "/tokens", "tokens", lambda: redirect("/admin?tab=tokens", code=302)
    )
    flask_app.register_blueprint(build_auth_blueprint(store))
    web_auth.install_auth(flask_app, ui_dir="ui", store=store)

    flask_app.auth_store = store
    return flask_app


def login(client, username="root", password=PASSWORD):
    return client.post("/login", json={"username": username, "password": password})


# ---- the shape the pages actually consume ----------------------------


def test_user_list_is_wrapped_the_way_the_console_wraps_lists(app):
    """`{"users": [...]}`, matching {'remediations': [...]} elsewhere. The page
    unwraps this key; a bare array would render an empty table in silence."""
    c = app.test_client()
    login(c)
    body = c.get("/api/auth/users").get_json()
    assert "users" in body
    assert {u["username"] for u in body["users"]} == {"root", "alice"}


def test_token_list_is_wrapped_the_same_way(app):
    c = app.test_client()
    login(c)
    c.post("/api/auth/tokens", json={"label": "event_runtime", "scopes": ["investigate"]})
    body = c.get("/api/auth/tokens").get_json()
    assert "tokens" in body
    assert [t["label"] for t in body["tokens"]] == ["event_runtime"]


def test_pages_are_served_and_reference_their_endpoints(app):
    """Catches a page being added without its route, and a route pointing at a
    file that no longer calls the API it was built against."""
    c = app.test_client()
    login(c)

    account = c.get("/account")
    assert account.status_code == 200
    assert b"/api/auth/password" in account.data
    assert b"/api/auth/tokens" in account.data

    admin = c.get("/admin")
    assert admin.status_code == 200
    assert b"/api/auth/users" in admin.data
    assert b"/api/auth/tokens" in admin.data
    assert b"/api/providers" in admin.data


def test_legacy_users_tokens_redirect_to_admin(app):
    c = app.test_client()
    login(c)

    users = c.get("/users", follow_redirects=False)
    assert users.status_code == 302
    assert users.headers["Location"].endswith("/admin?tab=users")

    tokens = c.get("/tokens", follow_redirects=False)
    assert tokens.status_code == 302
    assert tokens.headers["Location"].endswith("/admin?tab=tokens")


def test_console_pages_require_a_session(app):
    """The markup is not sensitive, but an anonymous visitor should land on the
    login form rather than a page whose every fetch 401s."""
    c = app.test_client()
    r = c.get("/account", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---- the secret is shown once and never again ------------------------


def test_minted_secret_is_returned_once_then_never_listed(app):
    c = app.test_client()
    login(c)

    created = c.post(
        "/api/auth/tokens", json={"label": "mcp", "scopes": ["remediate"]}
    ).get_json()
    secret = created["secret"]
    assert secret.startswith("cfop_")

    listed = c.get("/api/auth/tokens").get_json()
    assert secret not in str(listed)
    assert all("secret" not in t for t in listed["tokens"])


def test_minted_token_authenticates_with_exactly_its_own_scopes(app):
    """The whole point of per-token scopes: an `investigate` token must not
    inherit the console's ambient authority."""
    admin = app.test_client()
    login(admin)
    secret = admin.post(
        "/api/auth/tokens", json={"label": "event_runtime", "scopes": ["investigate"]}
    ).get_json()["secret"]

    caller = app.test_client()
    me = caller.get("/api/auth/me", headers={"Authorization": f"Bearer {secret}"}).get_json()
    assert me["token_label"] == "event_runtime"
    assert set(me["scopes"]) == {"investigate", "read"}
    assert "remediate" not in me["scopes"]


def test_revoking_a_token_stops_it_immediately(app):
    admin = app.test_client()
    login(admin)
    created = admin.post(
        "/api/auth/tokens", json={"label": "doomed", "scopes": ["read"]}
    ).get_json()

    caller = app.test_client()
    auth = {"Authorization": f"Bearer {created['secret']}"}
    assert caller.get("/api/auth/me", headers=auth).status_code == 200

    admin.delete(f"/api/auth/tokens/{created['id']}")
    assert caller.get("/api/auth/me", headers=auth).status_code == 401


# ---- role separation across the assembled stack ----------------------


def test_member_is_confined_to_their_own_view(app):
    c = app.test_client()
    login(c, "alice")

    assert c.get("/api/auth/users").status_code == 403
    assert c.get("/api/auth/audit").status_code == 403
    # ...but their own tokens are theirs to manage.
    assert c.get("/api/auth/tokens").get_json() == {"tokens": []}
    assert c.post(
        "/api/auth/tokens", json={"label": "mine", "scopes": ["read"]}
    ).status_code == 201


def test_member_cannot_see_or_revoke_another_users_token(app):
    admin = app.test_client()
    login(admin)
    other = admin.post(
        "/api/auth/tokens", json={"label": "admins", "scopes": ["read"]}
    ).get_json()

    member = app.test_client()
    login(member, "alice")
    assert member.get("/api/auth/tokens").get_json()["tokens"] == []
    assert member.delete(f"/api/auth/tokens/{other['id']}").status_code == 403


# ---- the audit trail the migration depends on ------------------------


def test_mutations_land_in_the_audit_trail(app):
    """Retiring CFOP_API_TOKEN is decided by querying this, so it has to be
    populated by the routes people actually use."""
    c = app.test_client()
    login(c)
    created = c.post(
        "/api/auth/tokens", json={"label": "temp", "scopes": ["read"]}
    ).get_json()
    c.delete(f"/api/auth/tokens/{created['id']}")
    c.post("/api/auth/users", json={"username": "bob", "password": PASSWORD, "role": ROLE_MEMBER})

    body = c.get("/api/auth/audit").get_json()
    # The key is pinned because docs/auth.md tells operators to decide whether
    # CFOP_API_TOKEN is still in use with `jq '.audit | length'`.
    assert "audit" in body
    events = {e["event"] for e in body["audit"]}
    assert {"login.success", "token.created", "token.revoked", "user.created"} <= events
