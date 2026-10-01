"""The project names its database driver, and that driver is installed.

Every engine used to be built from a bare ``postgresql://`` URL, so the
driver was whatever SQLAlchemy defaulted to. 2.1 changed that default from
psycopg2 to psycopg (v3); a routine rebuild pulled 2.1 with only psycopg2
installed, and the agent and event_runtime crashed on boot with ``No module
named 'psycopg'`` (2026-10-01, CFOP-248). The driver is now named at every
engine through ``cfshared.db.sqlalchemy_url``.
"""
import ast

import pytest
import sqlalchemy

from cfshared.db import DRIVER, sqlalchemy_url
from repo_paths import REPO_ROOT


@pytest.mark.parametrize("given", [
    "postgresql://u:p@h:5432/db",
    "postgres://u:p@h:5432/db",
    "postgresql+psycopg2://u:p@h:5432/db",
    "postgresql+psycopg://u:p@h:5432/db",
])
def test_every_postgres_spelling_names_psycopg3(given):
    assert sqlalchemy_url(given) == "postgresql+psycopg://u:p@h:5432/db"


def test_other_backends_and_url_objects():
    assert sqlalchemy_url("sqlite:///:memory:") == "sqlite:///:memory:"
    url = sqlalchemy.engine.make_url("postgresql://u:p@h/db")
    assert sqlalchemy_url(url).drivername == DRIVER
    assert sqlalchemy_url(sqlalchemy.engine.make_url("sqlite://")).drivername == "sqlite"


def test_the_named_driver_is_installed():
    """No connection needed: create_engine imports the DBAPI module."""
    engine = sqlalchemy.create_engine(sqlalchemy_url("postgresql://u:p@localhost:5432/db"))
    assert engine.dialect.driver == "psycopg", engine.dialect.driver
    engine.dispose()


def test_every_engine_names_its_driver():
    """A create_engine fed a URL that did not go through sqlalchemy_url is how
    a library default comes back. Any new call site fails here until it does."""
    sites = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in (".venv", "venv", "node_modules", ".git") or rel.name.startswith("test_") \
                or rel.parts[0] in ("tests", "demo", "benchmarks"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "create_engine":
                first = node.args[0] if node.args else None
                named = (isinstance(first, ast.Call)
                         and getattr(first.func, "id", getattr(first.func, "attr", "")) == "sqlalchemy_url")
                sites.append((f"{rel}:{node.lineno}", named))
    assert len(sites) >= 3, f"found only {sites} — is REPO_ROOT right?"
    unnamed = [where for where, named in sites if not named]
    assert not unnamed, "create_engine without sqlalchemy_url(...): " + ", ".join(unnamed)
