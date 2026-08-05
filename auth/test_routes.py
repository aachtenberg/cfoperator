"""Tests for the /api/auth management routes.

Driven through a real Flask app with the real console gate installed, against a
real (SQLite) database — the properties that matter here are all about the seam
between the two. Whether a member can revoke someone else's token depends on the
decorator, the session lookup, and the ``created_by`` filter agreeing, and a
mock of any one of them would let the others through.
"""

import pytest
from flask import Flask
from sqlalchemy import create_engine

import web_auth
from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.routes import build_auth_blueprint
from auth.store import AuthStore

PASSWORD = "correct horse battery staple"


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


@pytest.fixture(autouse=True)
def console_env(monkeypatch):
    monkeypatch.setenv("CFOP_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("CFOP_AUTH_DISABLED", "")
    monkeypatch.setenv("CFOP_UI_USERNAME", "")
    monkeypatch.setenv("CFOP_UI_PASSWORD_HASH", "")
    monkeypatch.setenv("CFOP_API_TOKEN", "")


# Lets a test give the blueprint one store and the gate another, so a failing
# management query can be told apart from a failing credential lookup.
_SAME_STORE = object()


def build_app(store, gate_store=_SAME_STORE):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(build_auth_blueprint(store))
    # After the routes, as the console does: the gate is a before_request hook
    # and must see every route it is protecting.
    web_auth.install_auth(app, ui_dir="ui", store=store if gate_store is _SAME_STORE else gate_store)
    return app


@pytest.fixture
def admin(store):
    return store.create_user("root", PASSWORD, role=ROLE_ADMIN)


@pytest.fixture
def member(store):
    return store.create_user("alice", PASSWORD, role=ROLE_MEMBER)


def login(app, username):
    client = app.test_client()
    r = client.post("/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200
    return client


# ---- user management is admin-only ------------------------------------


def test_admin_can_list_and_create_users(store, admin):
    c = login(build_app(store), "root")

    r = c.post("/api/auth/users", json={"username": "bob", "password": "pw", "role": ROLE_MEMBER})
    assert r.status_code == 201
    created = r.get_json()
    assert created["username"] == "bob" and created["role"] == ROLE_MEMBER
    assert "password_hash" not in created

    listed = c.get("/api/auth/users").get_json()["users"]
    assert {u["username"] for u in listed} == {"root", "bob"}
    assert all("password_hash" not in u for u in listed)


def test_admin_can_patch_role_and_status(store, admin, member):
    c = login(build_app(store), "root")

    r = c.patch(f"/api/auth/users/{member['id']}", json={"role": ROLE_ADMIN})
    assert r.status_code == 200 and r.get_json()["role"] == ROLE_ADMIN

    r = c.patch(f"/api/auth/users/{member['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.get_json()["is_active"] is False
    assert store.get_user(member["id"])["is_active"] is False


def test_store_guards_surface_as_400_not_500(store, admin):
    """The last-admin guard is a caller mistake, not a server fault."""
    c = login(build_app(store), "root")
    r = c.patch(f"/api/auth/users/{admin['id']}", json={"is_active": False})
    assert r.status_code == 400
    assert "last active admin" in r.get_json()["error"]


def test_duplicate_username_is_a_400(store, admin):
    c = login(build_app(store), "root")
    c.post("/api/auth/users", json={"username": "bob", "password": "pw"})
    r = c.post("/api/auth/users", json={"username": "BOB", "password": "pw"})
    assert r.status_code == 400 and "already exists" in r.get_json()["error"]


def test_member_cannot_manage_users(store, admin, member):
    """User management is the console's escalation surface; members stay out."""
    c = login(build_app(store), "alice")

    assert c.get("/api/auth/users").status_code == 403
    assert c.post("/api/auth/users", json={"username": "x", "password": "pw"}).status_code == 403
    assert c.patch(f"/api/auth/users/{admin['id']}", json={"role": ROLE_MEMBER}).status_code == 403
    assert c.post(f"/api/auth/users/{admin['id']}/password",
                  json={"password": "owned"}).status_code == 403
    # ...and the attempt changed nothing.
    assert store.verify_login("root", PASSWORD) is not None
    assert store.count_users() == 2


def test_admin_can_reset_another_users_password(store, admin, member):
    c = login(build_app(store), "root")
    r = c.post(f"/api/auth/users/{member['id']}/password", json={"password": "reset-pw"})
    assert r.status_code == 200
    assert store.verify_login("alice", "reset-pw") is not None
    assert store.verify_login("alice", PASSWORD) is None


# ---- tokens -----------------------------------------------------------


def test_token_secret_is_returned_once_and_never_listed(store, admin):
    c = login(build_app(store), "root")

    r = c.post("/api/auth/tokens", json={"label": "event_runtime", "scopes": ["investigate"]})
    assert r.status_code == 201
    body = r.get_json()
    secret = body["secret"]
    assert secret.startswith("cfop_")
    assert "token_hash" not in body

    # The one place it existed was that response.
    listed = c.get("/api/auth/tokens").get_json()["tokens"]
    assert len(listed) == 1
    assert secret not in str(listed)
    assert "secret" not in listed[0] and "token_hash" not in listed[0]
    assert listed[0]["token_prefix"] == secret[:13]


def test_member_sees_only_their_own_tokens(store, admin, member):
    admin_client = login(build_app(store), "root")
    admin_client.post("/api/auth/tokens", json={"label": "roots", "scopes": ["remediate"]})

    member_client = login(build_app(store), "alice")
    member_client.post("/api/auth/tokens", json={"label": "alices", "scopes": ["read"]})

    mine = member_client.get("/api/auth/tokens").get_json()["tokens"]
    assert [t["label"] for t in mine] == ["alices"]

    # The admin still sees everything, which is how tokens stay auditable.
    everything = admin_client.get("/api/auth/tokens").get_json()["tokens"]
    assert {t["label"] for t in everything} == {"roots", "alices"}


def test_member_cannot_mint_a_remediate_token(store, admin, member):
    """Otherwise token creation is a way around the role check entirely."""
    c = login(build_app(store), "alice")
    r = c.post("/api/auth/tokens", json={"label": "escalate", "scopes": ["remediate"]})
    assert r.status_code in (400, 403)
    assert store.list_tokens() == []


def test_member_can_mint_within_their_ceiling(store, admin, member):
    c = login(build_app(store), "alice")
    r = c.post("/api/auth/tokens", json={"label": "laptop", "scopes": ["investigate"]})
    assert r.status_code == 201
    assert store.list_tokens()[0]["created_by"] == member["id"]


def test_member_cannot_revoke_someone_elses_token(store, admin, member):
    admin_client = login(build_app(store), "root")
    victim = admin_client.post(
        "/api/auth/tokens", json={"label": "roots", "scopes": ["remediate"]},
    ).get_json()

    member_client = login(build_app(store), "alice")
    r = member_client.delete(f"/api/auth/tokens/{victim['id']}")
    assert r.status_code == 403
    assert store.list_tokens()[0]["status"] == "active"


def test_member_revoking_an_unknown_token_is_also_403(store, admin, member):
    """A 404 here would say which token ids exist."""
    c = login(build_app(store), "alice")
    assert c.delete("/api/auth/tokens/9999").status_code == 403


def test_member_can_revoke_their_own_token(store, admin, member):
    c = login(build_app(store), "alice")
    mine = c.post("/api/auth/tokens", json={"label": "laptop", "scopes": ["read"]}).get_json()

    assert c.delete(f"/api/auth/tokens/{mine['id']}").status_code == 200
    assert store.list_tokens()[0]["status"] == "revoked"


def test_admin_can_revoke_any_token(store, admin, member):
    member_client = login(build_app(store), "alice")
    theirs = member_client.post(
        "/api/auth/tokens", json={"label": "alices", "scopes": ["read"]},
    ).get_json()

    admin_client = login(build_app(store), "root")
    assert admin_client.delete(f"/api/auth/tokens/{theirs['id']}").status_code == 200
    assert store.list_tokens()[0]["status"] == "revoked"


def test_bad_token_requests_are_400(store, admin):
    c = login(build_app(store), "root")
    assert c.post("/api/auth/tokens", json={"label": "", "scopes": ["read"]}).status_code == 400
    assert c.post("/api/auth/tokens", json={"label": "x", "scopes": []}).status_code == 400
    assert c.post("/api/auth/tokens",
                  json={"label": "x", "scopes": ["root"]}).status_code == 400
    assert c.post("/api/auth/tokens",
                  json={"label": "x", "scopes": "read"}).status_code == 400


# ---- self-service password change -------------------------------------


def test_password_change_requires_the_correct_current_password(store, admin, member):
    c = login(build_app(store), "alice")

    r = c.post("/api/auth/password",
               json={"current_password": "not it", "new_password": "brand new"})
    assert r.status_code == 403
    # The old password must still be the one that works.
    assert store.verify_login("alice", PASSWORD) is not None
    assert store.verify_login("alice", "brand new") is None

    r = c.post("/api/auth/password",
               json={"current_password": PASSWORD, "new_password": "brand new"})
    assert r.status_code == 200
    assert store.verify_login("alice", "brand new") is not None
    assert store.verify_login("alice", PASSWORD) is None


def test_password_change_needs_a_new_password(store, admin, member):
    c = login(build_app(store), "alice")
    r = c.post("/api/auth/password", json={"current_password": PASSWORD})
    assert r.status_code == 400


def test_password_change_without_a_user_account_is_a_clear_error(store, admin, monkeypatch):
    """Legacy environment-credential mode has a session but no user row. It must
    say so, not crash on a None id."""
    from werkzeug.security import generate_password_hash

    monkeypatch.setenv("CFOP_UI_USERNAME", "operator")
    monkeypatch.setenv("CFOP_UI_PASSWORD_HASH", generate_password_hash("legacy-pw"))
    monkeypatch.setenv("CFOP_API_TOKEN", "legacy-token")

    app = build_app(store, gate_store=None)
    c = app.test_client()
    assert c.post("/login", json={"username": "operator", "password": "legacy-pw"}).status_code == 200

    r = c.post("/api/auth/password",
               json={"current_password": "legacy-pw", "new_password": "new-pw"})
    assert r.status_code == 409
    assert "user account" in r.get_json()["error"]


def test_token_caller_changing_a_password_gets_409(store, admin):
    """A bearer token belongs to no human, so there is nothing to re-key."""
    _row, secret = store.create_token("mcp", ["remediate"], created_by=admin["id"])
    c = build_app(store).test_client()

    r = c.post("/api/auth/password",
               headers={"Authorization": f"Bearer {secret}"},
               json={"current_password": PASSWORD, "new_password": "x"})
    assert r.status_code == 409


# ---- audit ------------------------------------------------------------


def test_user_creation_is_audited(store, admin):
    c = login(build_app(store), "root")
    c.post("/api/auth/users", json={"username": "bob", "password": "pw"})

    rows = store.recent_audit(event="user.created")
    assert len(rows) == 1
    assert rows[0]["actor"] == "root"
    assert rows[0]["target"] == "bob"
    assert rows[0]["source_ip"]


def test_deactivation_is_audited_under_its_own_event(store, admin, member):
    c = login(build_app(store), "root")
    c.patch(f"/api/auth/users/{member['id']}", json={"is_active": False})

    assert [r["target"] for r in store.recent_audit(event="user.deactivated")] == ["alice"]
    assert store.recent_audit(event="user.updated") == []


def test_token_create_and_revoke_are_audited(store, admin):
    c = login(build_app(store), "root")
    row = c.post("/api/auth/tokens", json={"label": "mcp", "scopes": ["read"]}).get_json()
    c.delete(f"/api/auth/tokens/{row['id']}")

    created = store.recent_audit(event="token.created")
    revoked = store.recent_audit(event="token.revoked")
    assert len(created) == 1 and len(revoked) == 1
    assert created[0]["actor"] == revoked[0]["actor"] == "root"
    # The prefix identifies the row; the secret must not be anywhere near it.
    assert created[0]["target"] == row["token_prefix"]
    assert row["secret"] not in str(created)


def test_password_changes_are_audited(store, admin, member):
    c = login(build_app(store), "alice")
    c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "new one"})

    rows = store.recent_audit(event="user.password_changed")
    assert len(rows) == 1 and rows[0]["detail"]["self_service"] is True


def test_audit_is_admin_only_and_bounded(store, admin, member):
    c = login(build_app(store), "alice")
    assert c.get("/api/auth/audit").status_code == 403

    admin_client = login(build_app(store), "root")
    body = admin_client.get("/api/auth/audit?limit=999999&event=login.success").get_json()
    assert body["limit"] == 1000
    assert {r["event"] for r in body["audit"]} == {"login.success"}


# ---- a broken database must not read as a denial ----------------------


class _BrokenStore:
    """Every management query raises, as it would with PostgreSQL down."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise RuntimeError("connection refused")
        return boom


def test_database_failure_returns_503_not_403(store, admin):
    """503 says "try again"; 403 would send an admin looking for a role problem
    that does not exist."""
    app = build_app(_BrokenStore(), gate_store=store)
    c = login(app, "root")

    assert c.get("/api/auth/users").status_code == 503
    assert c.get("/api/auth/audit").status_code == 503
    r = c.post("/api/auth/tokens", json={"label": "x", "scopes": ["read"]})
    assert r.status_code == 503
    assert r.get_json()["error"] == "authentication backend unavailable"
