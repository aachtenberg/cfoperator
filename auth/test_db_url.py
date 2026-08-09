"""Where auth looks for its database.

This is the bug that shipped in the first auth release: the DSN was built from
``KNOWLEDGE_BASE_PG_*``, the pod sets ``POSTGRES_*``, so every lookup missed,
the defaults took over, and the agent spent a release connecting to localhost
and silently falling back to legacy single-user mode. Nothing crashed and
/api/health stayed green, so the only visible symptom was that /users and
/tokens did not exist.

These tests pin the variable names against what the deployment actually
provides, because getting them wrong is not a loud failure.
"""

import pytest

from auth.store import default_db_url, describe_db_target

ALL_VARS = [
    "CFOP_AUTH_DB_URL",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "KNOWLEDGE_BASE_PG_HOST", "KNOWLEDGE_BASE_PG_PORT", "KNOWLEDGE_BASE_PG_DATABASE",
    "KNOWLEDGE_BASE_PG_USER", "KNOWLEDGE_BASE_PG_PASSWORD",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_uses_the_postgres_vars_the_deployment_sets(monkeypatch):
    """k3s and docker-compose both provide POSTGRES_*; this is the real path."""
    monkeypatch.setenv("POSTGRES_HOST", "postgres.apps.svc")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "cfoperator")
    monkeypatch.setenv("POSTGRES_USER", "cfoperator")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")

    assert default_db_url() == "postgresql://cfoperator:s3cret@postgres.apps.svc:5433/cfoperator"


def test_falls_back_to_the_knowledge_base_vars(monkeypatch):
    """knowledge_base.py reads these directly, so a deployment configured that
    way must still work."""
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_HOST", "kb-host")
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_DATABASE", "sre_knowledge")
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_USER", "sre_agent")
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_PASSWORD", "pw")

    assert default_db_url() == "postgresql://sre_agent:pw@kb-host:5432/sre_knowledge"


def test_postgres_vars_win_when_both_are_set(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "primary")
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_HOST", "secondary")
    assert "@primary:" in default_db_url()


def test_explicit_url_overrides_everything(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "ignored")
    monkeypatch.setenv("CFOP_AUTH_DB_URL", "postgresql://u:p@elsewhere:6000/other")
    assert default_db_url() == "postgresql://u:p@elsewhere:6000/other"


def test_default_target_is_the_cluster_service_not_localhost():
    """The old default was localhost, which inside a pod is guaranteed wrong and
    fails as 'connection refused' — an infrastructure-looking error for what is
    actually a missing variable. Aim at the same place agent.py does."""
    url = default_db_url()
    assert "@postgres:5432/cfoperator" in url
    assert "localhost" not in url


def test_password_special_characters_are_escaped(monkeypatch):
    """Sealed-secret passwords contain @ and / often enough that an unescaped
    DSN would connect to the wrong host or fail to parse."""
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_USER", "cfop")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/w:rd#1")

    url = default_db_url()
    # Exactly one @ — the delimiter — so the host is unambiguous.
    assert url.count("@") == 1
    assert url.endswith("@db:5432/cfoperator")
    assert "p%40ss%2Fw%3Ard%231" in url


def test_empty_values_do_not_shadow_the_fallback(monkeypatch):
    """An env var set to "" is how a half-filled secret presents itself; it must
    not count as configured."""
    monkeypatch.setenv("POSTGRES_HOST", "")
    monkeypatch.setenv("KNOWLEDGE_BASE_PG_HOST", "kb-host")
    assert "@kb-host:" in default_db_url()


def test_describe_target_never_leaks_the_password():
    """It goes in the startup log line, which is the whole point of having it."""
    described = describe_db_target("postgresql://svcaccount:hunter2@db.internal:5432/cfoperator")
    assert described == "db.internal:5432/cfoperator"
    assert "hunter2" not in described
    assert "svcaccount" not in described
