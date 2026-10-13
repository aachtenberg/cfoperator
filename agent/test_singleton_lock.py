"""The single-agent lock (CFOP-322), against a fake connection.

The real-Postgres behaviour (a second session is refused, release lets it in,
keepalives land on the session) is tests/test_agent_singleton_pg.py. These pin
the decisions around it: what each answer from the database turns into, and
that the agent starts nothing while another agent holds the lock.
"""

import pytest

import singleton_lock
from singleton_lock import ACQUIRED, HELD, UNAVAILABLE, AgentSingletonLock


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.conn.dead:
            raise OSError("server closed the connection")
        self.conn.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            assert params == (singleton_lock.LOCK_CLASS, singleton_lock.LOCK_KEY)
            self._row = (self.conn.grant,)
        else:
            self._row = (1,)

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, grant=True):
        self.grant = grant
        self.dead = False
        self.closed = False
        self.statements = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _lock(*conns):
    made = list(conns)

    def connect(dsn):
        nxt = made.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    return AgentSingletonLock("postgresql://x", connect=connect)


def test_free_lock_is_acquired_and_its_session_kept_open():
    conn = FakeConn(grant=True)
    lock = _lock(conn)
    assert lock.try_acquire() == ACQUIRED
    assert lock.held and not conn.closed


def test_keepalives_are_set_before_the_lock_is_taken():
    """Without them a dead node's session holds the lock for the kernel's ~2h."""
    conn = FakeConn(grant=True)
    _lock(conn).try_acquire()
    lock_at = next(i for i, s in enumerate(conn.statements) if "pg_try_advisory_lock" in s)
    before = " ".join(conn.statements[:lock_at])
    for setting in ("tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count"):
        assert setting in before


def test_a_lock_held_elsewhere_is_reported_and_the_connection_closed():
    conn = FakeConn(grant=False)
    lock = _lock(conn)
    assert lock.try_acquire() == HELD
    assert conn.closed and not lock.held


def test_an_unreachable_database_is_unavailable_not_held():
    assert _lock(OSError("connection refused")).try_acquire() == UNAVAILABLE


def test_check_on_a_live_session_keeps_the_lock_without_reconnecting():
    conn = FakeConn(grant=True)
    lock = _lock(conn)  # a second connect would raise IndexError
    lock.try_acquire()
    assert lock.check() == ACQUIRED


def test_a_dropped_session_is_retaken_or_reported_lost():
    first = FakeConn(grant=True)
    lock = _lock(first, FakeConn(grant=False))
    lock.try_acquire()
    first.dead = True
    assert lock.check() == HELD, "someone else got in while the session was down"
    assert not lock.held


# --- the agent's use of it -------------------------------------------------

class _Seq:
    """A lock double answering try_acquire() from a script."""
    def __init__(self, *states):
        self.states = list(states)

    def try_acquire(self):
        return self.states.pop(0)


@pytest.fixture
def bare_agent(monkeypatch):
    import sys
    from agent import CFOperator
    # The module CFOperator lives in, whichever way `agent` resolved
    # (bare module under CI's PYTHONPATH, package from the repo root).
    agent_mod = sys.modules[CFOperator.__module__]
    op = CFOperator.__new__(CFOperator)
    op._db_params = {"host": "x"}
    return agent_mod, op


def test_a_held_lock_keeps_the_agent_passive_until_it_frees(bare_agent, monkeypatch):
    agent_mod, op = bare_agent
    seq = _Seq(HELD, HELD, UNAVAILABLE, ACQUIRED)
    monkeypatch.setattr(agent_mod, "AgentSingletonLock", lambda dsn: seq)
    sleeps = []
    op._claim_singleton(sleep=sleeps.append)
    assert len(sleeps) == 3, "an unreachable database mid-wait is not permission to start"
    assert seq.states == []


def test_the_agent_hands_the_lock_keywords_not_a_url(bare_agent, monkeypatch):
    """A raw password in a URL is misparsed by libpq, and the lock then reads
    as "unreachable" and the agent runs unguarded (CFOP-383, production)."""
    agent_mod, op = bare_agent
    op._db_params = {"host": "db", "port": 5432, "dbname": "kb",
                     "user": "sre_agent", "password": "p@ss/w:rd"}
    seen = []
    monkeypatch.setattr(agent_mod, "AgentSingletonLock",
                        lambda conninfo: seen.append(conninfo) or _Seq(ACQUIRED))
    op._claim_singleton(sleep=lambda s: None)
    assert seen == [op._db_params]


def test_an_unreachable_database_at_boot_does_not_block_startup(bare_agent, monkeypatch):
    agent_mod, op = bare_agent
    monkeypatch.setattr(agent_mod, "AgentSingletonLock", lambda dsn: _Seq(UNAVAILABLE))
    sleeps = []
    op._claim_singleton(sleep=sleeps.append)
    assert sleeps == []


def test_run_claims_the_lock_before_starting_any_worker(bare_agent, monkeypatch):
    agent_mod, op = bare_agent

    class Stop(Exception):
        pass

    started = []
    monkeypatch.setattr(op, "_get_alert_check_interval", lambda: 10, raising=False)
    monkeypatch.setattr(op, "_get_sweep_interval", lambda: 1800, raising=False)
    op._reactive_poll_enabled = True

    def claim():
        raise Stop
    monkeypatch.setattr(op, "_claim_singleton", claim, raising=False)
    for name in ("_start_investigation_worker", "_start_remediation_worker", "_start_cockpit_bridge"):
        monkeypatch.setattr(op, name, lambda n=name: started.append(n), raising=False)
    op.web_server = None
    with pytest.raises(Stop):
        op.run()
    assert started == []


def test_losing_the_lock_to_another_agent_ends_the_process(bare_agent):
    agent_mod, op = bare_agent

    class Lost:
        def check(self):
            return HELD
    op._singleton = Lost()
    op.last_singleton_check = 0
    with pytest.raises(SystemExit):
        op._check_singleton()


def test_the_loop_check_is_rate_limited(bare_agent):
    agent_mod, op = bare_agent
    calls = []

    class Counting:
        def check(self):
            calls.append(1)
            return ACQUIRED
    op._singleton = Counting()
    op.last_singleton_check = 0
    op._check_singleton()
    op._check_singleton()
    assert calls == [1]
