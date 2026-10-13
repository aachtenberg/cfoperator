"""The single-agent lock against a real Postgres (CFOP-322).

Needs ``CFOP_TEST_PG_DSN``; CI provides it (tests.yml postgres service) and
these fail there without it rather than skipping into a green run. Locally,
without it, they skip.
"""

from repo_paths import REPO_ROOT
import os
import sys

import pytest

sys.path.insert(0, str(REPO_ROOT / "agent"))

from singleton_lock import ACQUIRED, HELD, AgentSingletonLock  # noqa: E402

DSN = os.getenv("CFOP_TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(not DSN and not os.getenv("CI"),
                                reason="set CFOP_TEST_PG_DSN to run the Postgres lock tests")


@pytest.fixture
def locks():
    assert DSN, "CFOP_TEST_PG_DSN is unset. CI must provide it (tests.yml postgres service)"
    made = []

    def make():
        lock = AgentSingletonLock(DSN)
        made.append(lock)
        return lock
    yield make
    for lock in made:
        lock.release()


def test_a_second_agent_is_refused_while_the_first_holds_the_lock(locks):
    first, second = locks(), locks()
    assert first.try_acquire() == ACQUIRED
    assert second.try_acquire() == HELD


def test_releasing_the_lock_lets_the_next_agent_in(locks):
    first, second = locks(), locks()
    assert first.try_acquire() == ACQUIRED
    first.release()
    assert second.try_acquire() == ACQUIRED


def test_the_holder_survives_its_own_check(locks):
    first, second = locks(), locks()
    first.try_acquire()
    assert first.check() == ACQUIRED
    assert second.try_acquire() == HELD


def test_the_lock_session_carries_the_keepalives(locks):
    """The guard against a dead node holding the lock for the kernel's ~2h."""
    lock = locks()
    assert lock.try_acquire() == ACQUIRED
    with lock._conn.cursor() as cur:
        cur.execute("SHOW tcp_keepalives_idle")
        idle = cur.fetchone()[0]
    # Over a Unix socket Postgres reports 0 and ignores the setting; CI connects
    # over TCP, where it must be what was set.
    assert idle in ("30", "30s", "0")
    if "localhost" in DSN or "127.0.0.1" in DSN:
        assert idle in ("30", "30s")
