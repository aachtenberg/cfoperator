"""Console auth with a database behind it.

test_web_auth.py covers the legacy environment-credential mode and still passes
unchanged — that is the point: an existing deploy that has no auth database
behaves exactly as before. These tests cover what the database adds, and the
ways it could go wrong: a user deactivated mid-session, a role revoked while a
cookie is still valid, and the database itself being unreachable.
"""

from repo_paths import REPO_ROOT
import importlib

import pytest
from flask import Flask, jsonify
from sqlalchemy import create_engine

import web_auth
from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore

PASSWORD = "correct horse battery staple"


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


def build_app(monkeypatch, store, **overrides):
    """Same shape as the real console: exempt probes, a protected mutating
    route, and an admin-only route."""
    env = {
        "CFOP_SESSION_SECRET": "test-session-secret",
        "CFOP_AUTH_DISABLED": "",
        "CFOP_UI_USERNAME": "",
        "CFOP_UI_PASSWORD_HASH": "",
        "CFOP_API_TOKEN": "",
    }
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    importlib.reload(web_auth)

    app = Flask(__name__, static_folder="ui", static_url_path="",
                  root_path=str(REPO_ROOT))
    app.config["TESTING"] = True

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/remediations/<int:rid>/approve", methods=["POST"])
    @web_auth.require_role(web_auth.ROLE_ADMIN)
    def approve(rid):
        return jsonify({"approved": rid})

    @app.route("/api/investigations")
    def investigations():
        return jsonify({"investigations": []})

    @app.route("/api/learnings", methods=["POST"])
    @web_auth.require_token_scope("investigate")
    def create_learning():
        return jsonify({"id": 1})

    @app.route("/")
    def index():
        return "<html>console</html>"

    web_auth.install_auth(app, ui_dir="ui", store=store)
    return app


# ---- database-backed login -------------------------------------------


def test_db_user_can_log_in_and_reach_protected_routes(monkeypatch, store):
    store.create_user("alice", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()

    r = c.post("/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200
    assert r.get_json()["user"] == {"username": "alice", "role": ROLE_ADMIN}

    assert c.post("/api/remediations/3/approve").status_code == 200


def test_wrong_db_password_gives_the_generic_error(monkeypatch, store):
    store.create_user("alice", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()
    r = c.post("/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "invalid credentials"


def test_unknown_user_is_indistinguishable_from_a_wrong_password(monkeypatch, store):
    store.create_user("alice", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()
    unknown = c.post("/login", json={"username": "nobody", "password": PASSWORD})
    wrong = c.post("/login", json={"username": "alice", "password": "nope"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.get_json() == wrong.get_json()


# ---- revocation takes effect immediately -----------------------------


def test_deactivating_a_user_ends_their_session_at_once(monkeypatch, store):
    """Not when the cookie expires — the account is re-checked per request."""
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    alice = store.create_user("alice", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()

    c.post("/login", json={"username": "alice", "password": PASSWORD})
    assert c.post("/api/remediations/1/approve").status_code == 200

    store.update_user(alice["id"], is_active=False)
    assert c.post("/api/remediations/1/approve").status_code == 401


def test_demoting_an_admin_revokes_admin_routes_mid_session(monkeypatch, store):
    """The session cookie still says admin; the database is what counts."""
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    alice = store.create_user("alice", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()

    c.post("/login", json={"username": "alice", "password": PASSWORD})
    assert c.post("/api/remediations/1/approve").status_code == 200

    store.update_user(alice["id"], role=ROLE_MEMBER)
    assert c.post("/api/remediations/1/approve").status_code == 403
    # ...but ordinary routes still work: demotion is not a lockout.
    assert c.get("/api/investigations").status_code == 200


def test_member_cannot_reach_an_admin_route(monkeypatch, store):
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    store.create_user("alice", PASSWORD, role=ROLE_MEMBER)
    c = build_app(monkeypatch, store).test_client()

    c.post("/login", json={"username": "alice", "password": PASSWORD})
    r = c.post("/api/remediations/1/approve")
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"


# ---- database-backed tokens ------------------------------------------


def test_db_token_authenticates(monkeypatch, store):
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("event_runtime", ["remediate"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    r = c.post("/api/remediations/7/approve", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200
    assert r.get_json()["approved"] == 7


def test_read_only_token_cannot_approve(monkeypatch, store):
    """Scope is what stops a dashboard credential queueing work for the executor."""
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("dashboard", ["read"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    assert c.get("/api/investigations", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    r = c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 403


def test_read_only_token_cannot_write_learnings(monkeypatch, store):
    """require_role can't express this gate: token scopes collapse to a role
    there (read -> member), and members may write learnings — so without a
    scope check a dashboard credential could seed the KB."""
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("dashboard", ["read"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    r = c.post("/api/learnings", json={}, headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 403
    assert "investigate" in r.get_json()["detail"]


def test_investigate_token_can_write_learnings(monkeypatch, store):
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("discovery", ["investigate"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    r = c.post("/api/learnings", json={}, headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200
    assert r.get_json()["id"] == 1


def test_member_session_can_write_learnings(monkeypatch, store):
    store.create_user("bob", PASSWORD, role=ROLE_MEMBER)
    c = build_app(monkeypatch, store).test_client()
    assert c.post("/login", json={"username": "bob", "password": PASSWORD}).status_code == 200

    assert c.post("/api/learnings", json={}).status_code == 200


def test_anonymous_cannot_write_learnings(monkeypatch, store):
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()
    assert c.post("/api/learnings", json={}).status_code == 401


def test_revoked_token_is_refused(monkeypatch, store):
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    row, secret = store.create_token("doomed", ["remediate"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    assert c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    store.revoke_token(row["id"])
    assert c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {secret}"}).status_code == 401


@pytest.mark.parametrize("bad", ["", "wrong-token", "cfop_nonsense"])
def test_bad_tokens_refused(monkeypatch, store, bad):
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()
    r = c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


# ---- legacy shared token during the migration window -----------------


LEGACY = "legacy-shared-token-xyz"


def test_legacy_token_still_works_alongside_the_database(monkeypatch, store):
    """Services keep working across the deploy that introduces the database."""
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store, CFOP_API_TOKEN=LEGACY).test_client()

    r = c.post("/api/remediations/9/approve", headers={"Authorization": f"Bearer {LEGACY}"})
    assert r.status_code == 200


def test_legacy_token_use_is_audited(monkeypatch, store):
    """So the variable can be removed on evidence rather than hope."""
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store, CFOP_API_TOKEN=LEGACY).test_client()
    c.post("/api/remediations/9/approve", headers={"Authorization": f"Bearer {LEGACY}"})

    rows = store.recent_audit(event="token.legacy_used")
    assert len(rows) == 1
    assert rows[0]["detail"]["path"] == "/api/remediations/9/approve"


def test_a_real_token_is_not_shadowed_by_the_legacy_one(monkeypatch, store):
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("real", ["remediate"], created_by=admin["id"])
    c = build_app(monkeypatch, store, CFOP_API_TOKEN=LEGACY).test_client()

    c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {secret}"})
    # The database token resolved, so nothing was attributed to the legacy path.
    assert store.recent_audit(event="token.legacy_used") == []


# ---- the database being unreachable must fail CLOSED -----------------


class _BrokenStore:
    """Every lookup raises, as it would with PostgreSQL down."""

    def verify_token(self, presented):
        raise RuntimeError("connection refused")

    def verify_login(self, username, password):
        raise RuntimeError("connection refused")

    def get_user(self, user_id):
        raise RuntimeError("connection refused")

    def record(self, *args, **kwargs):
        raise RuntimeError("connection refused")

    def record_legacy_token_use(self, *args, **kwargs):
        raise RuntimeError("connection refused")


def test_database_down_returns_503_not_401(monkeypatch):
    """503 says "ask again later"; 401 would tell a real user their password is
    wrong, and any 2xx would be falling open."""
    c = build_app(monkeypatch, _BrokenStore()).test_client()
    r = c.post("/api/remediations/1/approve", headers={"Authorization": "Bearer cfop_something"})
    assert r.status_code == 503


def test_database_down_does_not_break_the_liveness_probe(monkeypatch):
    """The pod must stay diagnosable rather than being restart-looped."""
    c = build_app(monkeypatch, _BrokenStore()).test_client()
    assert c.get("/api/health").status_code == 200


def test_database_down_during_login_returns_503(monkeypatch):
    c = build_app(monkeypatch, _BrokenStore()).test_client()
    r = c.post("/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 503


def test_role_check_reuses_the_gates_lookup(monkeypatch, store):
    """One user lookup per request, not two.

    The gate already fetches and validates the session user, so a role check
    that queried again would double the per-request database load — and open a
    window where a request the gate admitted is then refused because the
    database went away in between."""
    store.create_user("alice", PASSWORD, role=ROLE_ADMIN)

    lookups = []
    real_get_user = store.get_user
    store.get_user = lambda uid: (lookups.append(uid), real_get_user(uid))[1]

    c = build_app(monkeypatch, store).test_client()
    c.post("/login", json={"username": "alice", "password": PASSWORD})

    lookups.clear()
    assert c.post("/api/remediations/1/approve").status_code == 200
    assert len(lookups) == 1, f"expected one lookup for the request, got {len(lookups)}"


def test_role_check_reports_an_outage_as_503_not_401(monkeypatch, store):
    """A role check that cannot reach the store must not read as "not logged in".

    Exercised through effective_role directly: the gate normally fails first,
    so this covers the narrow case where the store dies between the gate and
    the handler."""
    app = build_app(monkeypatch, store)

    with app.test_request_context("/api/remediations/1/approve", method="POST"):
        from flask import g, session as flask_session

        console = web_auth.ConsoleAuth(app, ui_dir="ui", store=_BrokenStore())
        g.cfop_auth = console
        flask_session["cfop_user"] = "alice"
        flask_session["cfop_user_id"] = 1

        with pytest.raises(web_auth.AuthBackendUnavailable):
            console.effective_role()

        guarded = web_auth.require_role(web_auth.ROLE_ADMIN)(lambda: "reached")
        response, status = guarded()
        assert status == 503
        assert response.get_json()["error"] == "authentication backend unavailable"


# ---- identity endpoint ------------------------------------------------


def test_whoami_reports_the_logged_in_user(monkeypatch, store):
    store.create_user("alice", PASSWORD, role=ROLE_MEMBER)
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    c = build_app(monkeypatch, store).test_client()

    c.post("/login", json={"username": "alice", "password": PASSWORD})
    body = c.get("/api/auth/me").get_json()
    assert body["username"] == "alice"
    assert body["role"] == ROLE_MEMBER
    # The UI uses this to hide controls; it must never carry credentials.
    assert "password_hash" not in body


def test_whoami_reports_token_scopes(monkeypatch, store):
    admin = store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = store.create_token("mcp", ["investigate"], created_by=admin["id"])
    c = build_app(monkeypatch, store).test_client()

    body = c.get("/api/auth/me", headers={"Authorization": f"Bearer {secret}"}).get_json()
    assert body["token_label"] == "mcp"
    assert set(body["scopes"]) == {"investigate", "read"}
