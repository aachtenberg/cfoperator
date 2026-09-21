"""Guards for the console node-action allowlist dial (CFOP-132).

The ceiling, Job env, and executor intersect already shipped in CFOP-133.
This is the control surface: GET/POST through the real Flask routes, a write
that cannot widen past the ceiling, and a gate Slack's bridge token cannot
pass. Policy helpers alone would leave the handler deletable (CFOP-49).
"""

from __future__ import annotations

from repo_paths import REPO_ROOT
import os
import sys
import threading
from unittest.mock import MagicMock

sys.path.insert(0, str(REPO_ROOT / "agent"))
from knowledge_base import ResilientKnowledgeBase

from flask import Flask
from sqlalchemy import create_engine

from auth.models import EVENT_REMEDIATION_ALLOWLIST, ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore

PASSWORD = "correct horse battery staple"

CEILING = {
    "allow_binaries": ["chmod", "chown", "systemctl"],
    "allow_systemctl_verbs": ["restart", "status", "is-active"],
    "max_commands": 4,
}

SETTING_B = "node_action_allow_binaries"
SETTING_V = "node_action_allow_systemctl_verbs"


def _client(*, stored=None, store=None, auth_disabled=True):
    """Real WebServer routes against a stub operator. Same harness as
    test_console_repos.py: dev-bypass for handler tests, a real store for
    role-gating."""
    from web_auth import install_auth
    from web_server import WebServer

    settings = {
        SETTING_B: (stored or {}).get(SETTING_B, ""),
        SETTING_V: (stored or {}).get(SETTING_V, ""),
    }

    operator = MagicMock()
    operator.config = {"remediation": {"executor": {"node_action": dict(CEILING)}}}
    operator.kb.get_setting.side_effect = lambda key, default=None, **_kw: settings.get(key, default)
    operator.kb.set_setting.side_effect = lambda key, value: settings.__setitem__(key, value)

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

    return server.app.test_client(), operator, settings


def _login(client, username):
    assert client.post("/login", json={"username": username, "password": PASSWORD}).status_code == 200


def test_get_reports_the_ceiling_as_effective_when_unset():
    client, _, _ = _client()
    body = client.get("/api/remediation/node-action-allowlist").get_json()
    assert body["source"] == "config"
    assert body["selected"] == {"binaries": None, "verbs": None}
    assert body["effective"]["binaries"] == ["chmod", "chown", "systemctl"]
    assert body["effective"]["verbs"] == ["is-active", "restart", "status"]
    assert body["ceiling"]["max_commands"] == 4
    assert "rm" in body["floor"]["deny_binaries"]
    assert body["floor"]["metacharacters"]


def test_a_stored_subset_is_what_the_next_job_would_see():
    """The GET effective set is ceiling ∩ selection — the same resolution
    _node_action_allowlist uses when it fills the Job env."""
    client, _, _ = _client(stored={SETTING_B: "systemctl", SETTING_V: "restart,status"})
    body = client.get("/api/remediation/node-action-allowlist").get_json()
    assert body["source"] == "db"
    assert body["effective"]["binaries"] == ["systemctl"]
    assert body["effective"]["verbs"] == ["restart", "status"]
    assert "chmod" not in body["effective"]["binaries"]


def test_narrowing_persists_the_subset():
    client, _, settings = _client()
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"],
        "verbs": ["restart", "is-active"],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "db"
    assert body["effective"]["binaries"] == ["systemctl"]
    assert settings[SETTING_B] == "systemctl"
    assert set(settings[SETTING_V].split(",")) == {"restart", "is-active"}


def test_posting_the_whole_ceiling_stores_unset():
    """A stale full-list row would freeze out a binary added in a later
    deploy. Equal-to-ceiling writes '' so source stays config."""
    client, _, settings = _client()
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": list(CEILING["allow_binaries"]),
        "verbs": list(CEILING["allow_systemctl_verbs"]),
    })
    assert resp.status_code == 200
    assert resp.get_json()["source"] == "config"
    assert settings[SETTING_B] == ""
    assert settings[SETTING_V] == ""


def test_widening_past_the_ceiling_is_refused_and_writes_nothing():
    """Mutation-check: journalctl is the diagnostic 131 deferred. A POST
    that added it and 200'd would make console-admin equivalent to a
    ceiling commit."""
    client, operator, settings = _client()
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["chmod", "journalctl"],
        "verbs": ["restart"],
    })
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert "journalctl" in err
    assert "ceiling" in err
    assert settings[SETTING_B] == ""
    operator.kb.set_setting.assert_not_called()


def test_an_empty_pick_is_refused():
    client, _, settings = _client()
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": [], "verbs": ["restart"],
    })
    assert resp.status_code == 400
    assert settings[SETTING_B] == ""


def test_reset_clears_the_override():
    client, _, settings = _client(stored={SETTING_B: "systemctl", SETTING_V: "restart"})
    resp = client.post("/api/remediation/node-action-allowlist", json={"reset": True})
    assert resp.status_code == 200
    assert resp.get_json()["source"] == "config"
    assert settings[SETTING_B] == ""
    assert settings[SETTING_V] == ""


def test_a_save_that_cannot_reach_the_database_reports_it():
    client, operator, _ = _client()
    operator.kb.set_setting.side_effect = ConnectionError("Database is offline")
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"], "verbs": ["restart"],
    })
    assert resp.status_code == 503


def test_an_admin_save_is_audited_with_before_and_after():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _, _ = _client(store=s, auth_disabled=False)
    _login(client, "root")
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"], "verbs": ["restart"],
    })
    assert resp.status_code == 200
    rows = [e for e in s.recent_audit(limit=20) if e["event"] == EVENT_REMEDIATION_ALLOWLIST]
    assert len(rows) == 1
    assert rows[0]["actor"] == "root"
    assert rows[0]["target"] == "node-action"
    assert rows[0]["detail"]["after"]["effective"]["binaries"] == ["systemctl"]


def test_a_member_may_read_but_not_change():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("m", PASSWORD, role=ROLE_MEMBER)
    client, _, settings = _client(store=s, auth_disabled=False)
    _login(client, "m")
    assert client.get("/api/remediation/node-action-allowlist").status_code == 200
    assert client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"], "verbs": ["restart"],
    }).status_code == 403
    assert settings[SETTING_B] == ""


def test_an_anonymous_caller_gets_nothing():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _, _ = _client(store=s, auth_disabled=False)
    assert client.get("/api/remediation/node-action-allowlist").status_code == 401
    assert client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"], "verbs": ["restart"],
    }).status_code == 401


def test_a_remediate_token_cannot_write():
    """require_role maps remediate → admin (Approve needs that). This list
    is strictly more powerful; Slack's bridge token must not reach it."""
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    admin = s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    _row, secret = s.create_token("slack-bridge", ["remediate"], created_by=admin["id"])
    client, _, settings = _client(store=s, auth_disabled=False)
    resp = client.post(
        "/api/remediation/node-action-allowlist",
        json={"binaries": ["systemctl"], "verbs": ["restart"]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 403
    assert "token" in resp.get_json()["detail"]
    assert settings[SETTING_B] == ""


class _InnerKB:
    def __init__(self, values, raises=False):
        self.values = dict(values)
        self.raises = raises

    def get_setting(self, key, default=None):
        if self.raises:
            raise RuntimeError("db down")
        return self.values.get(key, default)


class _Monitor:
    def __init__(self, healthy=True):
        self._healthy = healthy

    def is_healthy(self):
        return self._healthy

    def mark_unhealthy(self):
        self._healthy = False


def _rkb(*, healthy=True, values=None, inner_raises=False):
    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._kb = _InnerKB(values or {}, raises=inner_raises)
    rkb._health_monitor = _Monitor(healthy)
    return rkb


def test_a_resilient_kb_outage_is_error_not_the_ceiling():
    """CFOP-197: ResilientKB.get_setting returns '' on a blip, which used to
    look like unset. GET must show source=error and an empty effective set,
    not the ceiling the operator had narrowed away from.
    """
    stored = {SETTING_B: "systemctl", SETTING_V: "restart"}
    client, operator, _ = _client(stored=stored)
    operator.kb = _rkb(healthy=False, values=stored)
    body = client.get("/api/remediation/node-action-allowlist").get_json()
    assert body["source"] == "error"
    assert body["effective"]["binaries"] == []
    assert body["effective"]["verbs"] == []
    assert "chmod" not in body["effective"]["binaries"]


def test_a_resilient_kb_inner_error_is_error_not_the_ceiling():
    stored = {SETTING_B: "systemctl", SETTING_V: "restart"}
    client, operator, _ = _client(stored=stored)
    operator.kb = _rkb(healthy=True, values=stored, inner_raises=True)
    body = client.get("/api/remediation/node-action-allowlist").get_json()
    assert body["source"] == "error"
    assert body["effective"]["binaries"] == []


def test_a_healthy_resilient_kb_still_reports_the_subset():
    stored = {SETTING_B: "systemctl", SETTING_V: "restart"}
    client, operator, _ = _client(stored=stored)
    operator.kb = _rkb(healthy=True, values=stored)
    body = client.get("/api/remediation/node-action-allowlist").get_json()
    assert body["source"] == "db"
    assert body["effective"]["binaries"] == ["systemctl"]
    assert body["effective"]["verbs"] == ["restart"]


def test_an_admin_session_may_write():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _, settings = _client(store=s, auth_disabled=False)
    _login(client, "root")
    resp = client.post("/api/remediation/node-action-allowlist", json={
        "binaries": ["systemctl"], "verbs": ["restart"],
    })
    assert resp.status_code == 200
    assert settings[SETTING_B] == "systemctl"


ADMIN = REPO_ROOT / "ui" / "admin.html"


def test_the_admin_page_owns_the_allowlist_tab():
    html = ADMIN.read_text(encoding="utf-8")
    assert 'data-tab="allowlist"' in html
    assert 'id="panel-allowlist"' in html
    assert "'allowlist'" in html and "loadAllowlist()" in html
    assert "/api/remediation/node-action-allowlist" in html
    assert "Reset to ceiling" in html
    assert "not operator-editable" in html or "not editable" in html.lower()
    assert "deny" in html.lower()
    assert "metacharacter" in html.lower()


def test_the_allowlist_tab_still_uses_the_shared_header():
    html = ADMIN.read_text(encoding="utf-8")
    assert 'id="cfop-nav"' in html
    assert 'src="/nav.js"' in html
