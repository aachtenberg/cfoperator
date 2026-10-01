"""The database driver, named rather than inherited (CFOP-248).

Every SQLAlchemy engine in this project used to be built from a bare
``postgresql://`` URL, so the driver was whatever SQLAlchemy defaulted to.
SQLAlchemy 2.1 moved that default from psycopg2 to psycopg (v3); a routine
rebuild pulled 2.1 while only psycopg2 was installed, and the agent and
event_runtime crash-looped on boot (2026-10-01). Naming the driver at every
``create_engine`` means the next change to a default cannot pick one for us.

Raw-driver connections (event_runtime's state sink, the timescale tool) take a
plain libpq ``postgresql://`` URI and are unaffected.
"""

from __future__ import annotations

DRIVER = "postgresql+psycopg"

# Spellings that mean "PostgreSQL, some driver": the bare scheme, the libpq
# alias, and the psycopg2 driver this project ran on until CFOP-248.
_POSTGRES_SCHEMES = ("postgresql", "postgres", "postgresql+psycopg2")


def sqlalchemy_url(url):
    """``url`` with the PostgreSQL driver named explicitly as psycopg 3.

    Takes a string or a SQLAlchemy ``URL``. Only the bare scheme, ``postgres``
    and ``postgresql+psycopg2`` are rewritten; another explicitly named driver
    (``postgresql+asyncpg``) and other backends (sqlite in tests) are returned
    unchanged — naming one deliberately is the point.
    """
    if hasattr(url, "drivername"):  # a sqlalchemy.engine.URL
        if url.drivername in _POSTGRES_SCHEMES:
            return url.set(drivername=DRIVER)
        return url
    scheme, sep, rest = str(url).partition("://")
    if sep and scheme in _POSTGRES_SCHEMES:
        return f"{DRIVER}://{rest}"
    return url
