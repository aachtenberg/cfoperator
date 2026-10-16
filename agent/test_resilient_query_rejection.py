"""A statement the database rejects is not an outage (CFOP-430).

ResilientKnowledgeBase used to treat every exception from the wrapped
KnowledgeBase as "the database is down": mark the connection unhealthy, and
for writes, fall back to the local outbox. A query bug therefore poisoned the
health monitor on every call — find_learnings(tags=...) failed on every sweep
with `operator does not exist: jsonb ?| jsonb` — and any write in the next
ten seconds was buffered for replay. Combined with the never-deleted buffer
file (test_local_buffer.py) that produced 868 duplicate investigations.

These tests pin the split: a DBAPIError that did not invalidate the
connection leaves the monitor alone; an OperationalError, an invalidated
connection, or a non-database exception still marks it unhealthy. The
compiled SQL of the tag filter is checked too, so the `?|` right-hand side
cannot silently go back to being bound as JSONB.
"""

import os
import sys
import threading

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import (
    DataError, IntegrityError, InterfaceError, OperationalError, ProgrammingError,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import ResilientKnowledgeBase, _is_query_rejection, _jsonb_has_any


def _dbapi(cls, msg="boom", invalidated=False):
    """Build a SQLAlchemy-wrapped driver error the way the engine would."""
    exc = cls("SELECT 1", {}, Exception(msg), connection_invalidated=invalidated)
    return exc


# ----------------------------------------------------------------------------
# _is_query_rejection
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    _dbapi(ProgrammingError, "operator does not exist: jsonb ?| jsonb"),
    _dbapi(DataError, "invalid input syntax for type integer"),
    _dbapi(IntegrityError, "duplicate key value violates unique constraint"),
])
def test_rejected_statement_is_not_an_outage(exc):
    assert _is_query_rejection(exc) is True


@pytest.mark.parametrize("exc", [
    _dbapi(OperationalError, "connection refused"),
    _dbapi(InterfaceError, "connection already closed"),
    _dbapi(ProgrammingError, "server closed the connection", invalidated=True),
    Exception("connection refused: database is down"),   # the stub shape the schema tests use
    ConnectionError("Database is offline"),
])
def test_unreachable_database_is_an_outage(exc):
    assert _is_query_rejection(exc) is False


# ----------------------------------------------------------------------------
# The wrappers
# ----------------------------------------------------------------------------

class _StubKB:
    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def find_learnings(self, **kwargs):
        self.calls.append(("find_learnings", kwargs))
        raise self.exc

    def start_investigation(self, trigger, alert_id=None):
        self.calls.append(("start_investigation", trigger))
        raise self.exc


class _StubMonitor:
    def __init__(self):
        self.unhealthy_marks = 0

    def is_healthy(self):
        return True

    def mark_unhealthy(self):
        self.unhealthy_marks += 1


class _StubBuffer:
    def __init__(self):
        self.events = []

    def buffer_event(self, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)


def _rkb(exc):
    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._kb = _StubKB(exc)
    rkb._health_monitor = _StubMonitor()
    rkb._buffer = _StubBuffer()
    rkb._local_id_lock = threading.Lock()
    rkb._local_inv_id_counter = 0
    return rkb


def test_find_learnings_rejection_returns_empty_without_marking_unhealthy():
    rkb = _rkb(_dbapi(ProgrammingError, "operator does not exist: jsonb ?| jsonb"))
    assert rkb.find_learnings(tags=["automated"]) == []
    assert rkb._health_monitor.unhealthy_marks == 0


def test_find_learnings_outage_still_marks_unhealthy():
    rkb = _rkb(_dbapi(OperationalError, "connection refused"))
    assert rkb.find_learnings(tags=["automated"]) == []
    assert rkb._health_monitor.unhealthy_marks == 1


def test_start_investigation_rejection_buffers_but_does_not_poison_health():
    """The write still goes to the outbox (nothing is dropped silently),
    but the next unrelated write must not be sent there too."""
    rkb = _rkb(_dbapi(ProgrammingError, "column does not exist"))
    local_id = rkb.start_investigation("trigger", alert_id="a1")
    assert local_id < 0
    assert rkb._buffer.events == [("start_investigation", {
        "trigger": "trigger", "alert_id": "a1", "local_id": local_id,
    })]
    assert rkb._health_monitor.unhealthy_marks == 0


def test_start_investigation_outage_marks_unhealthy_and_buffers():
    rkb = _rkb(_dbapi(OperationalError, "connection refused"))
    assert rkb.start_investigation("trigger") < 0
    assert rkb._health_monitor.unhealthy_marks == 1
    assert [e[0] for e in rkb._buffer.events] == ["start_investigation"]


# ----------------------------------------------------------------------------
# The tag / service filter compiles to a text[] right-hand side
# ----------------------------------------------------------------------------

def test_jsonb_has_any_binds_text_array_not_jsonb():
    t = Table("t", MetaData(), Column("id", Integer), Column("tags", JSONB))
    stmt = select(t.c.id).where(_jsonb_has_any(t.c.tags, ["automated", "manual"]))
    compiled = stmt.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "t.tags ?| CAST(ARRAY['automated', 'manual'] AS TEXT[])" in sql
    assert "JSONB" not in sql.upper().replace("CAST", "")
