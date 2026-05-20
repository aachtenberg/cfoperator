#!/usr/bin/env python3
"""Tests for resilient schema initialization.

A transient PostgreSQL outage must not crash agent startup. ResilientKnowledgeBase
buffers writes locally when the DB is down — but schema creation used to delegate
straight to the raw KnowledgeBase, whose create_all() connects unguarded and threw
OperationalError up through startup. initialize_schema() now absorbs that and the
sync loop retries once the DB recovers.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import ResilientKnowledgeBase


class _StubKB:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def initialize_schema(self):
        self.calls += 1
        if self.fail:
            raise Exception("connection refused: database is down")


class _StubMonitor:
    def __init__(self):
        self.marked_unhealthy = False

    def mark_unhealthy(self):
        self.marked_unhealthy = True


def _rkb(fail=False):
    """A ResilientKnowledgeBase with just the bits initialize_schema() touches."""
    rkb = ResilientKnowledgeBase.__new__(ResilientKnowledgeBase)
    rkb._kb = _StubKB(fail=fail)
    rkb._health_monitor = _StubMonitor()
    rkb._schema_lock = threading.Lock()
    rkb._schema_initialized = False
    return rkb


def test_schema_init_succeeds_when_db_up():
    rkb = _rkb(fail=False)
    assert rkb.initialize_schema() is True
    assert rkb._schema_initialized is True
    assert rkb._kb.calls == 1


def test_schema_init_does_not_raise_when_db_down():
    rkb = _rkb(fail=True)
    # the whole point: a DB outage must not propagate out of startup
    assert rkb.initialize_schema() is False
    assert rkb._schema_initialized is False
    assert rkb._health_monitor.marked_unhealthy is True


def test_schema_init_is_idempotent_after_success():
    rkb = _rkb(fail=False)
    rkb.initialize_schema()
    rkb.initialize_schema()
    rkb.initialize_schema()
    assert rkb._kb.calls == 1          # not re-run once it succeeded


def test_schema_init_retries_after_db_recovers():
    rkb = _rkb(fail=True)
    assert rkb.initialize_schema() is False     # DB down at startup
    rkb._kb.fail = False                        # DB comes back
    assert rkb.initialize_schema() is True      # retry succeeds
    assert rkb._schema_initialized is True
    assert rkb._kb.calls == 2


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
