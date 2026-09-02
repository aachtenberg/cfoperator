"""Guards for the console-managed repo registry (CFOP-77).

Linking a repo used to mean editing ``git.repos`` in config.yaml — a read-only
ConfigMap in k8s, so a deploy commit plus a rollout restart. The registry now
lives in the ``git_repos`` setting and resolves DB-over-file, with a console
tab in front of it.

What these pin is the part that would fail quietly:

* precedence, including the two "fall back to the file" cases (unset, and a
  value that will not decode) — the alternative reading of a corrupt value is
  an empty registry, which unlinks every repo without saying so;
* the write path *through the real Flask routes*, not a policy helper: the
  CFOP-49 lesson is that pure-policy tests leave the handler deletable;
* that a config-seeded entry's ``ssh`` block survives an edit made through a
  form that cannot render it;
* who may write.

The live-refresh half (unregistering the tools of an unlinked repo) is pinned
next to the code that does it, in ``tools/test_git_registry.py``.
"""

from __future__ import annotations

from repo_paths import REPO_ROOT
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy import create_engine

from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore
from cfshared import repos as shared_repos

PASSWORD = "correct horse battery staple"

CONFIG_REPOS = [
    {
        "name": "homelab-infra",
        "github": "aachtenberg/homelab-infra",
        "branch": "main",
        "hosts": ["raspberrypi3"],
        "services": ["apps"],
        # Not rendered by the console, and must survive an edit made there.
        "ssh": {"user": "aachten", "address": "10.0.0.5", "key_path": "/root/.ssh/id_ed25519"},
    },
]


# ---- resolution ------------------------------------------------------------


def test_an_unset_setting_leaves_config_in_charge():
    repos, source = shared_repos.resolve(CONFIG_REPOS, "")
    assert source == "config"
    assert [r["name"] for r in repos] == ["homelab-infra"]
    assert shared_repos.resolve(CONFIG_REPOS, None)[1] == "config"


def test_a_stored_list_replaces_the_file_entirely():
    stored = json.dumps([{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}])
    repos, source = shared_repos.resolve(CONFIG_REPOS, stored)
    assert source == "db"
    assert [r["name"] for r in repos] == ["esp-sensor-hub"]


def test_an_operator_may_store_an_empty_registry():
    """Distinct from unset: '[]' means "I unlinked everything", and reverting
    to the file is a separate, explicit action."""
    repos, source = shared_repos.resolve(CONFIG_REPOS, "[]")
    assert (repos, source) == ([], "db")


@pytest.mark.parametrize("raw", ["{not json", '{"repos": []}', "null", "17"])
def test_an_undecodable_setting_falls_back_to_the_file(raw):
    """The other reading — treat it as empty — would silently unlink every
    repo, which is the one outcome nobody would think to check for."""
    repos, source = shared_repos.resolve(CONFIG_REPOS, raw)
    assert source == "config"
    assert [r["name"] for r in repos] == ["homelab-infra"]


def test_config_entries_the_write_path_would_reject_still_load():
    """sanitize() runs over config.yaml, which predates the console."""
    repos, _ = shared_repos.resolve([{"github": "owner/legacy"}, "junk", {}], "")
    assert [r["name"] for r in repos] == ["owner/legacy"]


# ---- input validation ------------------------------------------------------


def test_hosts_and_services_accept_a_typed_list_or_a_pasted_string():
    _, fields = shared_repos.parse_repo_input(
        {"name": "esp", "github": "o/esp", "hosts": "pi3, pi4", "services": ["mosquitto"]})
    assert fields["hosts"] == ["pi3", "pi4"]
    assert fields["services"] == ["mosquitto"]


@pytest.mark.parametrize("bad", [
    {"name": "esp", "github": "not-a-slug"},
    {"name": "esp", "github": "owner/repo/extra"},
    {"name": "", "github": "o/r"},
    {"name": "has space", "github": "o/r"},
    {"name": "../../etc", "github": "o/r"},
    {"name": "esp", "github": "o/r", "branch": "bad branch"},
])
def test_bad_input_is_refused(bad):
    with pytest.raises(shared_repos.RepoError):
        shared_repos.parse_repo_input(bad)


def test_an_edit_keeps_the_fields_it_did_not_mention():
    updated = shared_repos.upsert(CONFIG_REPOS, "homelab-infra", {"branch": "release"})
    entry = shared_repos.find(updated, "homelab-infra")
    assert entry["branch"] == "release"
    assert entry["ssh"]["user"] == "aachten"
    assert entry["github"] == "aachtenberg/homelab-infra"


def test_an_empty_field_clears_it():
    updated = shared_repos.upsert(
        [{"name": "esp", "github": "o/esp", "path": "/srv/esp"}], "esp", {"path": None})
    assert "path" not in shared_repos.find(updated, "esp")


def test_a_new_entry_needs_a_slug():
    with pytest.raises(shared_repos.RepoError):
        shared_repos.upsert([], "esp", {"branch": "main"})


def test_the_ssh_block_is_never_echoed_to_the_console():
    view = shared_repos.public_view(CONFIG_REPOS[0])
    assert view["has_ssh"] is True
    assert "10.0.0.5" not in json.dumps(view)
    assert "key_path" not in json.dumps(view)


# ---- the HTTP surface ------------------------------------------------------


def _client(*, config_repos=None, stored="", store=None, auth_disabled=True):
    """The real WebServer routes against a stub operator.

    Same harness as test_cockpit_spawn.py: dev-bypass auth for the
    handler-behaviour tests, a real store for the role-gating ones.
    """
    from web_auth import install_auth
    from web_server import WebServer

    settings = {shared_repos.SETTING_KEY: stored}
    file_repos = shared_repos.sanitize(
        CONFIG_REPOS if config_repos is None else config_repos)

    operator = MagicMock()
    operator.config = {"git": {"repos": list(file_repos), "github": {"token": "ghp_test"}}}
    operator.file_git_repos.side_effect = lambda: list(file_repos)
    operator.kb.get_setting.side_effect = lambda key, default=None: settings.get(key, default)
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
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN", "GITHUB_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "true" if auth_disabled else ""
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN", "GITHUB_TOKEN"):
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


def test_get_reports_the_file_list_and_its_source():
    client, _, _ = _client()
    body = client.get("/api/git/repos").get_json()
    assert body["source"] == "config"
    assert [r["name"] for r in body["repos"]] == ["homelab-infra"]
    assert body["shadowed"] == []
    assert body["github_token_configured"] is True


def test_get_names_the_config_entries_the_console_list_is_hiding():
    """Shadowing is the cost of whole-list semantics; an operator who edits
    config.yaml afterwards has to be told it is being ignored."""
    stored = json.dumps([{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}])
    client, _, _ = _client(stored=stored)
    body = client.get("/api/git/repos").get_json()
    assert body["source"] == "db"
    assert body["shadowed"] == ["homelab-infra"]
    assert [r["name"] for r in body["config_repos"]] == ["homelab-infra"]


def test_linking_a_repo_persists_it_and_applies_it_live():
    client, operator, settings = _client()
    resp = client.post("/api/git/repos", json={
        "name": "esp-sensor-hub",
        "github": "aachtenberg/esp-sensor-hub",
        "hosts": "raspberrypi3",
        "services": "esp-sensor-hub, mosquitto",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "db"
    assert {r["name"] for r in body["repos"]} == {"homelab-infra", "esp-sensor-hub"}

    saved = json.loads(settings[shared_repos.SETTING_KEY])
    assert {r["name"] for r in saved} == {"homelab-infra", "esp-sensor-hub"}
    # Seeded from the file rather than starting empty: the first console write
    # must not unlink everything config.yaml declared.
    assert shared_repos.find(saved, "homelab-infra")["ssh"]["user"] == "aachten"

    applied = operator.apply_git_repos.call_args[0][0]
    assert {r["name"] for r in applied} == {"homelab-infra", "esp-sensor-hub"}


def test_editing_through_the_console_keeps_the_ssh_block():
    client, _, settings = _client()
    resp = client.post("/api/git/repos", json={
        "name": "homelab-infra", "github": "aachtenberg/homelab-infra", "branch": "release"})
    assert resp.status_code == 200
    entry = shared_repos.find(json.loads(settings[shared_repos.SETTING_KEY]), "homelab-infra")
    assert entry["branch"] == "release"
    assert entry["ssh"]["address"] == "10.0.0.5"


def test_a_bad_slug_is_refused_before_anything_is_written():
    client, operator, settings = _client()
    resp = client.post("/api/git/repos", json={"name": "esp", "github": "aachtenberg"})
    assert resp.status_code == 400
    assert "owner/repo" in resp.get_json()["error"]
    assert settings[shared_repos.SETTING_KEY] == ""
    operator.apply_git_repos.assert_not_called()


def test_a_save_that_cannot_reach_the_database_reports_it():
    client, operator, _ = _client()
    operator.kb.set_setting.side_effect = ConnectionError("Database is offline")
    resp = client.post("/api/git/repos", json={"name": "esp", "github": "o/esp"})
    assert resp.status_code == 503
    operator.apply_git_repos.assert_not_called()


def test_unlinking_a_repo_removes_it_and_leaves_the_rest():
    stored = json.dumps([
        {"name": "homelab-infra", "github": "aachtenberg/homelab-infra"},
        {"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"},
    ])
    client, operator, settings = _client(stored=stored)
    resp = client.delete("/api/git/repos/esp-sensor-hub")
    assert resp.status_code == 200
    assert [r["name"] for r in json.loads(settings[shared_repos.SETTING_KEY])] == ["homelab-infra"]
    assert [r["name"] for r in operator.apply_git_repos.call_args[0][0]] == ["homelab-infra"]


def test_importing_a_shadowed_entry_brings_its_ssh_block_with_it():
    """The console can only see `has_ssh`, so an import that posted back what
    GET showed it would create a new entry with the key path gone — the repo
    would come back GitHub-API-only and nothing would say so."""
    stored = json.dumps([{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}])
    client, _, settings = _client(stored=stored)
    assert client.get("/api/git/repos").get_json()["shadowed"] == ["homelab-infra"]

    resp = client.post("/api/git/repos/import", json={"names": ["homelab-infra"]})
    assert resp.status_code == 200
    assert resp.get_json()["imported"] == ["homelab-infra"]
    assert resp.get_json()["shadowed"] == []

    entry = shared_repos.find(json.loads(settings[shared_repos.SETTING_KEY]), "homelab-infra")
    assert entry["ssh"]["key_path"] == "/root/.ssh/id_ed25519"
    assert entry["hosts"] == ["raspberrypi3"]


def test_importing_with_no_names_takes_everything_the_console_is_missing():
    stored = json.dumps([{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}])
    client, _, settings = _client(stored=stored)
    body = client.post("/api/git/repos/import", json={}).get_json()
    assert body["imported"] == ["homelab-infra"]
    assert {r["name"] for r in body["repos"]} == {"homelab-infra", "esp-sensor-hub"}


def test_importing_something_config_never_declared_is_refused():
    client, _, settings = _client()
    resp = client.post("/api/git/repos/import", json={"names": ["not-in-the-file"]})
    assert resp.status_code == 400
    assert settings[shared_repos.SETTING_KEY] == ""


def test_importing_what_is_already_linked_is_a_no_op():
    client, _, _ = _client()
    body = client.post("/api/git/repos/import", json={"names": ["homelab-infra"]}).get_json()
    assert body["imported"] == []


def test_a_member_may_not_import():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("m", PASSWORD, role=ROLE_MEMBER)
    client, _, _ = _client(store=s, auth_disabled=False)
    _login(client, "m")
    assert client.post("/api/git/repos/import", json={}).status_code == 403


def test_a_repo_whose_name_is_a_slug_can_still_be_unlinked():
    """config.yaml may declare an entry with no name of its own; sanitize()
    falls back to the owner/repo slug, and the default URL converter would
    stop at the slash — leaving that row unremovable from the console."""
    stored = json.dumps([{"github": "aachtenberg/legacy"}])
    client, _, settings = _client(stored=stored)
    assert [r["name"] for r in client.get("/api/git/repos").get_json()["repos"]] == ["aachtenberg/legacy"]
    assert client.delete("/api/git/repos/aachtenberg/legacy").status_code == 200
    assert json.loads(settings[shared_repos.SETTING_KEY]) == []


def test_unlinking_something_that_is_not_linked_is_a_404():
    client, _, settings = _client()
    assert client.delete("/api/git/repos/nope").status_code == 404
    assert settings[shared_repos.SETTING_KEY] == ""


def test_revert_clears_the_setting_rather_than_storing_an_empty_list():
    """'' reads as unset — the settings store has no delete, and a stored []
    means "no repos at all", which is a different and much worse thing."""
    stored = json.dumps([{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}])
    client, operator, settings = _client(stored=stored)
    resp = client.post("/api/git/repos/revert", json={})
    assert resp.status_code == 200
    assert settings[shared_repos.SETTING_KEY] == ""
    assert resp.get_json()["source"] == "config"
    assert [r["name"] for r in resp.get_json()["repos"]] == ["homelab-infra"]
    operator.apply_git_repos.assert_called_once_with(None)


# ---- who may write ---------------------------------------------------------


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


def _login(client, username):
    assert client.post("/login", json={"username": username, "password": PASSWORD}).status_code == 200


def test_a_member_may_read_the_registry_but_not_change_it():
    """Which repos the tools may read — and the executor may target — is a
    behaviour change, so it sits on the admin side of the console's line."""
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("m", PASSWORD, role=ROLE_MEMBER)
    client, _, settings = _client(store=s, auth_disabled=False)
    _login(client, "m")

    assert client.get("/api/git/repos").status_code == 200
    assert client.post("/api/git/repos", json={"name": "e", "github": "o/e"}).status_code == 403
    assert client.delete("/api/git/repos/homelab-infra").status_code == 403
    assert client.post("/api/git/repos/revert", json={}).status_code == 403
    assert settings[shared_repos.SETTING_KEY] == ""


def test_an_anonymous_caller_gets_nothing():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _, _ = _client(store=s, auth_disabled=False)
    assert client.get("/api/git/repos").status_code == 401
    assert client.post("/api/git/repos", json={"name": "e", "github": "o/e"}).status_code == 401


def test_an_admin_may_link_a_repo():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    s.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _, settings = _client(store=s, auth_disabled=False)
    _login(client, "root")
    assert client.post("/api/git/repos", json={
        "name": "esp", "github": "aachtenberg/esp-sensor-hub"}).status_code == 200
    assert "esp" in settings[shared_repos.SETTING_KEY]


# ---- the console page ------------------------------------------------------


ADMIN = REPO_ROOT / "ui" / "admin.html"


def test_the_admin_page_owns_the_repos_tab():
    html = ADMIN.read_text(encoding="utf-8")
    assert 'data-tab="repos"' in html
    assert 'id="panel-repos"' in html
    assert "'repos'" in html and "loadRepos()" in html
    for endpoint in ("/api/git/repos", "/api/git/repos/revert", "/api/git/repos/import"):
        assert endpoint in html
    assert "method:'DELETE'" in html


def test_the_page_says_which_list_is_live_and_what_it_is_shadowing():
    """Whole-list semantics are only safe if the page admits config.yaml has
    stopped being consulted."""
    html = ADMIN.read_text(encoding="utf-8")
    assert "console registry (database)" in html
    assert "config.yaml is not consulted" in html
    assert "Import the shadowed entries" in html
    assert "Revert to config.yaml" in html


def test_the_page_still_mounts_the_shared_header():
    html = ADMIN.read_text(encoding="utf-8")
    assert 'id="cfop-nav"' in html
    assert 'src="/nav.js"' in html
