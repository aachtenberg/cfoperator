"""The database URLs this project builds resolve to an installed driver.

Every engine is created from a bare ``postgresql://`` URL, so the driver is
whatever SQLAlchemy defaults to. 2.1 changed that default to psycopg (v3),
which is not installed, and a routine image rebuild pulled 2.1 in: the agent
and event_runtime crashed on boot with ``No module named 'psycopg'``
(2026-10-01). Nothing caught it, because no test ever created an engine.
"""

import sqlalchemy


def test_a_bare_postgresql_url_gets_an_installed_driver():
    engine = sqlalchemy.create_engine("postgresql://u:p@localhost:5432/db")
    assert engine.dialect.driver == "psycopg2", engine.dialect.driver
    engine.dispose()
