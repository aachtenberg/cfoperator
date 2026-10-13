"""One active agent per database (CFOP-322).

The agent's in-process state assumes it is alone: the investigation lock, the
alert dedupe, the remediation drainer and the schedulers all live in memory. Two
agents against one database means duplicate investigations, duplicate
remediation rows and duplicate PRs. The chart pins ``replicas: 1`` and
``Recreate``, but ``kubectl scale``, a second compose stack pointed at the same
database, or a deploy repo that drops Recreate all get past a chart.

So the agent holds a session-level Postgres advisory lock for its lifetime.
Postgres releases it when the session ends, so a crashed agent cannot leave a
stale claim behind the way a lease row can.

The one way it can is a session the server has not noticed is dead: a node that
loses power leaves its TCP connection open on the server until keepalive reaps
it, and the kernel default is about two hours. Every lock session therefore sets
the server-side keepalives, which bounds a replacement agent's wait to about a
minute instead.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# (classid, objid) for pg_try_advisory_lock(int4, int4). event_runtime's
# per-alert recomputes use class 0x0CF0; this must stay distinct from it.
LOCK_CLASS = 0x0CF1
LOCK_KEY = 1

ACQUIRED = 'acquired'        # this process holds the lock
HELD = 'held'                # another session holds it
UNAVAILABLE = 'unavailable'  # the database could not be asked

# Server-side keepalives for the lock session: idle 30s, then 3 probes 10s apart,
# so a dead peer's session (and its lock) is dropped about a minute after it died.
_KEEPALIVES = (
    "SET tcp_keepalives_idle = 30",
    "SET tcp_keepalives_interval = 10",
    "SET tcp_keepalives_count = 3",
)


def _psycopg_connect(dsn: str):
    import psycopg
    return psycopg.connect(dsn, autocommit=True, connect_timeout=10)


class AgentSingletonLock:
    """The single-agent claim, held on a dedicated connection.

    ``connect`` takes a libpq DSN and returns an autocommit DB-API connection;
    tests inject one.
    """

    def __init__(self, dsn: str, connect: Optional[Callable[[str], Any]] = None):
        self._dsn = dsn
        self._connect = connect or _psycopg_connect
        self._conn = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def try_acquire(self) -> str:
        """Take the lock if it is free. Returns ACQUIRED, HELD or UNAVAILABLE."""
        if self._conn is not None:
            return ACQUIRED
        try:
            conn = self._connect(self._dsn)
        except Exception as e:
            logger.warning(f"Single-agent lock: database unreachable ({type(e).__name__}: {e})")
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                for stmt in _KEEPALIVES:
                    cur.execute(stmt)
                cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASS, LOCK_KEY))
                got = bool(cur.fetchone()[0])
        except Exception as e:
            logger.warning(f"Single-agent lock: could not take the lock ({type(e).__name__}: {e})")
            _close_quietly(conn)
            return UNAVAILABLE
        if not got:
            _close_quietly(conn)
            return HELD
        self._conn = conn
        return ACQUIRED

    def check(self) -> str:
        """Confirm the claim still stands, re-taking it if the session dropped.

        A live session still holds its session-level lock, so a round trip is
        the whole check. A dropped one (database restart, network blip) lost
        the lock with it, and another agent may have taken it in the meantime:
        that comes back as HELD.
        """
        if self._conn is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                return ACQUIRED
            except Exception as e:
                logger.warning(f"Single-agent lock: session lost ({type(e).__name__}: {e}); re-taking")
                self.release()
        return self.try_acquire()

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            _close_quietly(conn)


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass
