"""The event runtime's half of the console repo registry (CFOP-77).

This process resolves the registry once, at startup, from the same
``git_repos`` setting the console writes. Everything here is about the failure
direction: a database that cannot be read, has no row, or answers with junk
must leave the YAML file's repos in place. Falling open is the safe answer —
and it is also invisible, which is why the read is pinned rather than trusted
(the table and column are checked against the ORM model in
``agent/test_git_registry.py``, where that model is importable).
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from cfshared import repos as shared_repos
from event_runtime import bootstrap


FILE_YAML = """
git:
  github:
    token: ghp_from_file
  repos:
    - name: homelab-infra
      github: aachtenberg/homelab-infra
"""

DB_REPOS = [{"name": "esp-sensor-hub", "github": "aachtenberg/esp-sensor-hub"}]


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(FILE_YAML, encoding="utf-8")
    return str(path)


def _fake_psycopg2(monkeypatch, *, value=None, row=True, connect_raises=None, calls=None):
    """A psycopg2 stand-in. The real driver is an optional dependency here, and
    this suite must not need a database."""

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            if calls is not None:
                calls.append((sql, params))

        def fetchone(self):
            return (value,) if row else None

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    def connect(dsn, **kwargs):
        if connect_raises is not None:
            raise connect_raises
        if calls is not None:
            calls.append(("connect", dsn, kwargs))
        return _Conn()

    module = types.ModuleType("psycopg2")
    module.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    return module


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("CFOP_GIT_REPOS_JSON", raising=False)
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_PG_DSN", "postgresql://u:p@db:5432/cfoperator")


def test_the_console_registry_wins_over_the_file(monkeypatch, config_file):
    _fake_psycopg2(monkeypatch, value=json.dumps(DB_REPOS))
    cfg = bootstrap._load_git_config(config_file)
    assert [r["name"] for r in cfg["repos"]] == ["esp-sensor-hub"]
    # The github block still comes from the file — the console manages repos,
    # not credentials.
    assert cfg["github"]["token"] == "ghp_from_file"


def test_no_stored_row_leaves_the_file_in_charge(monkeypatch, config_file):
    _fake_psycopg2(monkeypatch, row=False)
    assert [r["name"] for r in bootstrap._load_git_config(config_file)["repos"]] == ["homelab-infra"]


def test_a_database_that_will_not_answer_leaves_the_file_in_charge(monkeypatch, config_file):
    _fake_psycopg2(monkeypatch, connect_raises=OSError("connection refused"))
    assert [r["name"] for r in bootstrap._load_git_config(config_file)["repos"]] == ["homelab-infra"]


def test_a_junk_setting_leaves_the_file_in_charge(monkeypatch, config_file):
    _fake_psycopg2(monkeypatch, value="{not json")
    assert [r["name"] for r in bootstrap._load_git_config(config_file)["repos"]] == ["homelab-infra"]


def test_an_emptied_registry_is_honoured(monkeypatch, config_file):
    """Distinct from unreadable: the operator unlinked everything."""
    _fake_psycopg2(monkeypatch, value="[]")
    assert bootstrap._load_git_config(config_file)["repos"] == []


def test_the_env_var_still_outranks_the_console(monkeypatch, config_file):
    _fake_psycopg2(monkeypatch, value=json.dumps(DB_REPOS))
    monkeypatch.setenv("CFOP_GIT_REPOS_JSON", json.dumps([{"name": "pinned", "github": "o/pinned"}]))
    assert [r["name"] for r in bootstrap._load_git_config(config_file)["repos"]] == ["pinned"]


def test_without_an_explicit_dsn_the_database_block_is_used(monkeypatch, config_file):
    """The registry lives in the agent's own database, so the connection the
    rest of this config already describes is the right one to read it over."""
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_PG_DSN", "")
    calls: list = []
    _fake_psycopg2(monkeypatch, value=json.dumps(DB_REPOS), calls=calls)
    assert [r["name"] for r in bootstrap._load_git_config(config_file)["repos"]] == ["esp-sensor-hub"]
    connects = [c for c in calls if c[0] == "connect"]
    assert connects and connects[0][1].startswith("postgresql://")


def test_a_config_with_no_database_reads_nothing(monkeypatch):
    """No host, no user, no read — not a connection attempt to a guess."""
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_PG_DSN", "")
    calls: list = []
    _fake_psycopg2(monkeypatch, value=json.dumps(DB_REPOS), calls=calls)
    assert bootstrap._load_git_repos_from_db({"database": {}}) is None
    assert calls == []


def test_the_read_is_bounded_and_asks_for_the_registry_key(monkeypatch, config_file):
    calls: list = []
    _fake_psycopg2(monkeypatch, value=json.dumps(DB_REPOS), calls=calls)
    bootstrap._load_git_config(config_file)
    connects = [c for c in calls if c[0] == "connect"]
    assert connects and connects[0][2].get("connect_timeout"), "startup must not hang on a slow DB"
    queries = [c for c in calls if c[0] is bootstrap._SETTINGS_QUERY]
    assert queries and queries[0][1] == (shared_repos.SETTING_KEY,)
