"""Tests for console auth on :8083.

The console is reachable from every device on the LAN once the host firewall
guard opens up, and /api/remediations/<id>/approve queues work for the executor,
so these tests care most about the ways auth could silently fall open:
misconfiguration, a wrong-but-close bearer token, and the difference between a
browser and a service client.
"""

from repo_paths import REPO_ROOT
import importlib

import pytest
from flask import Flask, jsonify
from werkzeug.security import generate_password_hash

import web_auth


USERNAME = "operator"
PASSWORD = "correct horse battery staple"
TOKEN = "service-token-abc123"


def build_app(monkeypatch, **overrides):
    """A minimal app with the same shape as the real one: one exempt health
    route, one exempt metrics route, and one protected mutating route."""
    env = {
        "CFOP_UI_USERNAME": USERNAME,
        "CFOP_UI_PASSWORD_HASH": generate_password_hash(PASSWORD),
        "CFOP_API_TOKEN": TOKEN,
        "CFOP_SESSION_SECRET": "test-session-secret",
        "CFOP_AUTH_DISABLED": "",
    }
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # Reset the module-level lockout table between tests.
    importlib.reload(web_auth)

    app = Flask(__name__, static_folder="ui", static_url_path="",
                  root_path=str(REPO_ROOT))
    app.config["TESTING"] = True

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/metrics")
    def metrics():
        return "cfoperator_up 1"

    @app.route("/api/remediations/<int:rid>/approve", methods=["POST"])
    def approve(rid):
        return jsonify({"approved": rid})

    # A machine-callback route: authenticates with the X-CFOP-Token completion
    # secret at the route level, so the console gate must let that secret reach
    # it (but nowhere else).
    @app.route("/v1/remediations/<int:rid>/complete", methods=["POST"])
    def complete(rid):
        return jsonify({"completed": rid})

    @app.route("/")
    def index():
        return "<html>console</html>"

    web_auth.install_auth(app, ui_dir="ui")
    return app


# ---- exempt routes ----------------------------------------------------


def test_health_and_metrics_stay_open(monkeypatch):
    """kubelet probes and the Prometheus scrape must not need credentials."""
    c = build_app(monkeypatch).test_client()
    assert c.get("/api/health").status_code == 200
    assert c.get("/metrics").status_code == 200


# ---- unauthenticated access ------------------------------------------


def test_mutating_route_blocked_without_credentials(monkeypatch):
    """The route that queues work for the executor is the one that matters."""
    c = build_app(monkeypatch).test_client()
    assert c.post("/api/remediations/1/approve").status_code == 401


def test_browser_is_redirected_but_api_client_gets_401(monkeypatch):
    """A person navigating should land on the login form; a script should get a
    machine-readable 401 rather than an HTML page."""
    c = build_app(monkeypatch).test_client()

    browser = c.get("/", headers={"Accept": "text/html"})
    assert browser.status_code == 302
    assert "/login" in browser.headers["Location"]

    api = c.get("/", headers={"Accept": "application/json"})
    assert api.status_code == 401


# ---- service token ----------------------------------------------------


def test_valid_bearer_token_is_accepted(monkeypatch):
    c = build_app(monkeypatch).test_client()
    r = c.post("/api/remediations/7/approve", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.get_json()["approved"] == 7


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "wrong-token",            # unrelated
    TOKEN[:-1],               # truncated — catches prefix-only comparison
    TOKEN + "x",              # extended
    TOKEN.upper(),            # case variant
])
def test_wrong_bearer_tokens_rejected(monkeypatch, bad):
    c = build_app(monkeypatch).test_client()
    r = c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_non_bearer_authorization_header_rejected(monkeypatch):
    c = build_app(monkeypatch).test_client()
    r = c.post("/api/remediations/1/approve", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


# ---- completion shared secret ----------------------------------------


COMPLETION_SECRET = "completion-shared-secret-xyz"


def test_completion_token_passes_the_gate_on_callback_path(monkeypatch):
    """Executor Jobs post completions with X-CFOP-Token, not the console
    bearer token. The gate must let them reach the callback route's own auth
    check (regression: PR #93 401'd every executor completion)."""
    c = build_app(monkeypatch, CFOP_COMPLETION_SHARED_SECRET=COMPLETION_SECRET).test_client()
    r = c.post("/v1/remediations/5/complete", headers={"X-CFOP-Token": COMPLETION_SECRET})
    assert r.status_code == 200
    assert r.get_json()["completed"] == 5


def test_completion_token_rejected_on_console_route(monkeypatch):
    """The completion secret is broadly distributed (every executor Job pod),
    so it must be honored ONLY on the callback paths. On a privileged console
    route like /approve it must not authenticate — otherwise the lowest-
    privilege credential becomes a full console credential."""
    c = build_app(monkeypatch, CFOP_COMPLETION_SHARED_SECRET=COMPLETION_SECRET).test_client()
    r = c.post("/api/remediations/5/approve", headers={"X-CFOP-Token": COMPLETION_SECRET})
    assert r.status_code == 401


@pytest.mark.parametrize("bad", [
    "",
    "wrong-secret",
    COMPLETION_SECRET[:-1],
    COMPLETION_SECRET + "x",
])
def test_wrong_completion_tokens_rejected(monkeypatch, bad):
    c = build_app(monkeypatch, CFOP_COMPLETION_SHARED_SECRET=COMPLETION_SECRET).test_client()
    r = c.post("/v1/remediations/1/complete", headers={"X-CFOP-Token": bad})
    assert r.status_code == 401


def test_unset_completion_secret_is_not_a_bypass(monkeypatch):
    """When no shared secret is configured, X-CFOP-Token must be ignored even
    on a callback path — route-level verify_completion_auth falls open for
    portable deployments, but the console gate must not."""
    c = build_app(monkeypatch, CFOP_COMPLETION_SHARED_SECRET="").test_client()
    r = c.post("/v1/remediations/1/complete", headers={"X-CFOP-Token": "anything"})
    assert r.status_code == 401


# ---- session login ----------------------------------------------------


def test_login_then_access_and_logout(monkeypatch):
    c = build_app(monkeypatch).test_client()

    r = c.post("/login", json={"username": USERNAME, "password": PASSWORD})
    assert r.status_code == 200 and r.get_json()["ok"] is True

    assert c.post("/api/remediations/3/approve").status_code == 200

    assert c.post("/logout").status_code == 200
    assert c.post("/api/remediations/3/approve").status_code == 401


def test_login_preserves_next_destination(monkeypatch):
    c = build_app(monkeypatch).test_client()
    r = c.post("/login", json={"username": USERNAME, "password": PASSWORD, "next": "/remediations"})
    assert r.get_json()["next"] == "/remediations"


@pytest.mark.parametrize("user,password", [
    (USERNAME, "wrong"),
    ("wrong", PASSWORD),
    ("wrong", "wrong"),
    (USERNAME, ""),
    (USERNAME, PASSWORD + " "),   # trailing space must not pass
])
def test_bad_logins_rejected(monkeypatch, user, password):
    c = build_app(monkeypatch).test_client()
    r = c.post("/login", json={"username": user, "password": password})
    assert r.status_code == 401
    # One generic message: a wrong username must be indistinguishable from a
    # wrong password, or the form becomes a username oracle.
    assert r.get_json()["error"] == "invalid credentials"


# ---- brute force ------------------------------------------------------


def test_repeated_failures_lock_out(monkeypatch):
    c = build_app(monkeypatch).test_client()
    for _ in range(web_auth._MAX_FAILURES):
        assert c.post("/login", json={"username": USERNAME, "password": "no"}).status_code == 401
    # Even the CORRECT password is refused once locked out.
    r = c.post("/login", json={"username": USERNAME, "password": PASSWORD})
    assert r.status_code == 429


def test_successful_login_clears_failure_counter(monkeypatch):
    c = build_app(monkeypatch).test_client()
    for _ in range(web_auth._MAX_FAILURES - 1):
        c.post("/login", json={"username": USERNAME, "password": "no"})
    assert c.post("/login", json={"username": USERNAME, "password": PASSWORD}).status_code == 200
    c.post("/logout")
    # Counter reset, so a fresh run of failures is needed to lock out again.
    assert c.post("/login", json={"username": USERNAME, "password": "no"}).status_code == 401


# ---- misconfiguration must fail CLOSED -------------------------------


@pytest.mark.parametrize("missing", [
    "CFOP_UI_USERNAME",
    "CFOP_UI_PASSWORD_HASH",
    "CFOP_API_TOKEN",
])
def test_unconfigured_returns_503_not_open_access(monkeypatch, missing):
    """The critical property: partial config must never mean no protection."""
    c = build_app(monkeypatch, **{missing: ""}).test_client()
    assert c.post("/api/remediations/1/approve").status_code == 503
    # ...and the exempt probes still work, so the pod stays live and diagnosable.
    assert c.get("/api/health").status_code == 200


def test_unconfigured_rejects_the_real_token(monkeypatch):
    """With no configured token, a presented token must not be accepted."""
    c = build_app(monkeypatch, CFOP_API_TOKEN="").test_client()
    r = c.post("/api/remediations/1/approve", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503


# ---- explicit dev bypass ---------------------------------------------


def test_auth_disabled_flag_opens_everything(monkeypatch):
    """Escape hatch for local docker-compose. Only via the explicit env flag."""
    c = build_app(monkeypatch, CFOP_AUTH_DISABLED="true").test_client()
    assert c.post("/api/remediations/1/approve").status_code == 200
