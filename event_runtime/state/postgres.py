"""Optional PostgreSQL sink for remote event persistence.

Besides the append-only event table, this keeps a per-alert read model
(CFOP-215): one row per alert holding the fold of all its events, so alerts
can be filtered and paged by their folded state (status, action, ...) instead
of by whatever fits in a window of raw events.

The read model is derived, never authoritative:

- It is maintained by *recomputing* an alert from all of its events inside
  the transaction that inserted one, using the same fold the in-memory path
  uses (``activity.fold_alert``). There is no second definition of status in
  SQL, and a replayed or reordered event heals rather than corrupts it.
- Per-alert advisory locks serialize concurrent recomputes, so two appends to
  one alert cannot each fold without the other's event and race to write.
- ``activity.FOLD_VERSION`` is stored per row. On start, a background rebuild
  folds every alert whose row is missing or at another version, which is also
  how an existing table gets its read model. Until that finishes the sink
  refuses to answer, and the replaying sink answers from the outbox instead.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import timezone
from typing import Iterable, List, Optional

from ..activity import FOLD_VERSION, alert_key, fold_alert
from ..alert_store import AlertPage, AlertQuery, AlertStoreUnavailable, encode_cursor, parse_timestamp
from .base import BaseStateSink


logger = logging.getLogger(__name__)

# Advisory-lock class for per-alert recomputes, so they cannot collide with any
# other advisory lock user of the database. Arbitrary, fixed.
_LOCK_CLASS = 0x0CF0
_REFRESH_CHUNK = 100
_BACKFILL_BATCH = 1000


def alerts_table_name(events_table: str) -> str:
    """``event_runtime_events`` -> ``event_runtime_alerts``."""
    base = events_table[: -len("_events")] if events_table.endswith("_events") else events_table
    return f"{base}_alerts"


class PostgresStateSink(BaseStateSink):
    """Persist domain events to PostgreSQL when psycopg2 is available."""

    durable = True

    def __init__(self, dsn: str | None = None, table_name: str = "event_runtime_events"):
        super().__init__(name="postgres")
        self.dsn = dsn or os.getenv("CFOP_EVENT_RUNTIME_PG_DSN", "")
        self.table_name = table_name
        self.alerts_table = alerts_table_name(table_name)
        self._last_error: Optional[str] = None
        self._schema_ready = False
        # Set once the read model covers every stored event at FOLD_VERSION.
        self._read_model_ready = threading.Event()
        self._rebuild_thread: Optional[threading.Thread] = None
        self._rebuild_guard = threading.Lock()
        self._stop = threading.Event()
        self._read_model_status: dict = {"ready": False}
        self._fold_failures = 0

    def start(self) -> None:
        if not self.dsn:
            return
        self._stop.clear()
        try:
            self._ensure_schema()
        except Exception as exc:
            # The outbox holds every event and replays it later, so Postgres
            # being down at startup is a degrade, not a reason not to start.
            # The rebuild retries the schema with backoff.
            self._last_error = str(exc)
            logger.warning("PostgreSQL sink %s unavailable at start: %s", self.table_name, exc)
        self.start_rebuild()

    def stop(self) -> None:
        self._stop.set()

    # ---- writes ------------------------------------------------------------

    def append(self, events: List[dict]) -> bool:
        if not self.dsn:
            self._last_error = "No PostgreSQL DSN configured"
            return False
        if not events:
            return True

        try:
            psycopg2, extras, sql = self._load_driver()
            self._ensure_schema()
            query = sql.SQL(
                "INSERT INTO {} (event_id, created_at, event_type, payload, alert_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (event_id) DO NOTHING"
            ).format(sql.Identifier(self.table_name))
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        query,
                        [
                            (
                                event["event_id"],
                                event["created_at"],
                                event["event_type"],
                                extras.Json(event.get("payload", {})),
                                alert_key(event),
                            )
                            for event in events
                        ],
                    )
                    self._refresh_in_savepoint(cur, {alert_key(event) for event in events})
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Failed to append events to PostgreSQL sink %s: %s", self.table_name, exc)
            return False

    def _refresh_in_savepoint(self, cur, alert_ids: Iterable[str]) -> None:
        """Recompute these alerts without risking the events' own insert.

        A read-model failure must not roll back the events — that would stall
        the outbox replay behind a derived table. On a database error the
        rows are marked stale instead and the rebuild picks them up.
        """
        ids = sorted({alert_id for alert_id in alert_ids if alert_id})
        if not ids:
            return
        cur.execute("SAVEPOINT read_model")
        try:
            self._refresh(cur, ids)
            cur.execute("RELEASE SAVEPOINT read_model")
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT read_model")
            logger.warning("Alert read model refresh failed for %d alert(s); marking them stale: %s",
                           len(ids), exc)
            _psycopg2, _extras, sql = self._load_driver()
            cur.execute(
                sql.SQL("UPDATE {} SET fold_version = -1 WHERE alert_id = ANY(%s)").format(
                    sql.Identifier(self.alerts_table)),
                (ids,),
            )
            self._read_model_ready.clear()
            self.start_rebuild()

    def _refresh(self, cur, alert_ids: List[str]) -> int:
        """Fold each alert from all of its events and upsert its row."""
        _psycopg2, extras, sql = self._load_driver()
        written = 0
        for start in range(0, len(alert_ids), _REFRESH_CHUNK):
            chunk = alert_ids[start:start + _REFRESH_CHUNK]
            # Sorted order everywhere, so two transactions can never wait on
            # each other's locks in opposite orders.
            for alert_id in chunk:
                cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LOCK_CLASS, alert_id))
            cur.execute(
                sql.SQL(
                    "SELECT event_id, created_at, event_type, payload, alert_id FROM {} "
                    "WHERE alert_id = ANY(%s) ORDER BY created_at, event_id"
                ).format(sql.Identifier(self.table_name)),
                (chunk,),
            )
            grouped = defaultdict(list)
            for row in cur.fetchall():
                grouped[row[4]].append(_event_from_row(row))
            rows = []
            for alert_id, events in grouped.items():
                try:
                    activity = fold_alert(events)
                except Exception as exc:
                    # A fold that raises is a code bug for this alert's data.
                    # Skipping it keeps every other alert current; the count
                    # in health() says something is being left out.
                    self._fold_failures += 1
                    logger.error("Could not fold alert %s (%d events): %s", alert_id, len(events), exc)
                    continue
                rows.append(self._row(alert_id, activity, extras))
            if rows:
                extras.execute_values(cur, sql.SQL(
                    "INSERT INTO {} (alert_id, source, severity, status, action, summary, namespace, "
                    "resource_name, first_event_at, latest_event_at, event_count, fold_version, activity, "
                    "updated_at) VALUES %s "
                    "ON CONFLICT (alert_id) DO UPDATE SET source = EXCLUDED.source, "
                    "severity = EXCLUDED.severity, status = EXCLUDED.status, action = EXCLUDED.action, "
                    "summary = EXCLUDED.summary, namespace = EXCLUDED.namespace, "
                    "resource_name = EXCLUDED.resource_name, first_event_at = EXCLUDED.first_event_at, "
                    "latest_event_at = EXCLUDED.latest_event_at, event_count = EXCLUDED.event_count, "
                    "fold_version = EXCLUDED.fold_version, activity = EXCLUDED.activity, "
                    "updated_at = EXCLUDED.updated_at"
                ).format(sql.Identifier(self.alerts_table)).as_string(cur), rows,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())")
                written += len(rows)
        return written

    @staticmethod
    def _row(alert_id: str, activity: dict, extras) -> tuple:
        return (
            alert_id,
            activity.get("source"),
            activity.get("severity"),
            activity.get("status"),
            activity.get("action"),
            activity.get("summary"),
            activity.get("namespace"),
            activity.get("resource_name"),
            parse_timestamp(activity["first_event_at"]),
            parse_timestamp(activity["latest_event_at"]),
            int(activity.get("event_count") or 0),
            FOLD_VERSION,
            extras.Json(activity, dumps=lambda value: json.dumps(value, default=str)),
        )

    # ---- reads -------------------------------------------------------------

    def recent(self, limit: int = 50) -> List[dict]:
        if not self.dsn:
            return []
        try:
            psycopg2, _extras, sql = self._load_driver()
            self._ensure_schema()
            query = sql.SQL(
                "SELECT event_id, created_at, event_type, payload "
                "FROM {} ORDER BY created_at DESC LIMIT %s"
            ).format(sql.Identifier(self.table_name))
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (limit,))
                    rows = cur.fetchall()
            self._last_error = None
            return [
                {
                    "event_id": row[0],
                    "created_at": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]),
                    "event_type": row[2],
                    "payload": row[3],
                }
                for row in rows
            ]
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Failed to read recent events from PostgreSQL sink %s: %s", self.table_name, exc)
            return []

    def list_alerts(self, query: AlertQuery) -> AlertPage:
        """Answer the query in SQL. Semantics: ``alert_store.apply_query``."""
        self._require_read_model()
        psycopg2, _extras, sql = self._load_driver()
        conditions, params = [], []
        for column in ("status", "action", "source", "severity"):
            value = getattr(query, column)
            if value:
                conditions.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
                params.append(value)
        if query.since:
            conditions.append(sql.SQL("latest_event_at >= %s"))
            params.append(query.since)
        if query.until:
            conditions.append(sql.SQL("latest_event_at < %s"))
            params.append(query.until)
        if query.q:
            pattern = "%" + _like_escape(query.q) + "%"
            conditions.append(sql.SQL(
                "(summary ILIKE %s OR resource_name ILIKE %s OR namespace ILIKE %s OR alert_id ILIKE %s)"))
            params.extend([pattern] * 4)
        if query.after:
            conditions.append(sql.SQL("(latest_event_at, alert_id) < (%s, %s)"))
            params.extend(query.after)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
        body = sql.SQL("activity") if query.full else sql.SQL("activity - 'timeline' - 'event_types'")
        statement = sql.SQL(
            "SELECT {body}, latest_event_at, alert_id FROM {table}{where} "
            "ORDER BY latest_event_at DESC, alert_id DESC LIMIT %s"
        ).format(body=body, table=sql.Identifier(self.alerts_table), where=where)
        params.append(query.limit + 1)
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(statement, params)
                    rows = cur.fetchall()
        except Exception as exc:
            self._last_error = str(exc)
            raise AlertStoreUnavailable(f"postgres: {exc}") from exc
        page = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            next_cursor = encode_cursor(page[-1][1], page[-1][2])
        return AlertPage(alerts=[row[0] for row in page], next_cursor=next_cursor, store="postgres")

    def get_alert(self, alert_id: str) -> Optional[dict]:
        """Fold the alert fresh from its events; the row is for lists."""
        self._require_read_model()
        psycopg2, _extras, sql = self._load_driver()
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "SELECT event_id, created_at, event_type, payload, alert_id FROM {} "
                            "WHERE alert_id = %s ORDER BY created_at, event_id"
                        ).format(sql.Identifier(self.table_name)),
                        (alert_id,),
                    )
                    rows = cur.fetchall()
        except Exception as exc:
            self._last_error = str(exc)
            raise AlertStoreUnavailable(f"postgres: {exc}") from exc
        if not rows:
            return None
        events = [_event_from_row(row) for row in rows]
        return {"alert": fold_alert(events), "events": events}

    def _require_read_model(self) -> None:
        if not self.dsn:
            raise AlertStoreUnavailable("no PostgreSQL DSN configured")
        if not self._read_model_ready.is_set():
            raise AlertStoreUnavailable("alert read model is still being built")

    # ---- rebuild -----------------------------------------------------------

    def start_rebuild(self) -> None:
        """Bring the read model up to date in the background (idempotent)."""
        with self._rebuild_guard:
            if self._rebuild_thread is not None and self._rebuild_thread.is_alive():
                return
            self._rebuild_thread = threading.Thread(
                target=self._rebuild_until_done, daemon=True, name="event-runtime-alerts-rebuild")
            self._rebuild_thread.start()

    def _rebuild_until_done(self) -> None:
        delay = 5.0
        while not self._stop.is_set():
            try:
                self.rebuild_read_model()
                return
            except Exception as exc:
                self._read_model_status = {"ready": False, "last_error": str(exc)}
                logger.warning("Alert read model rebuild failed, retrying in %.0fs: %s", delay, exc)
                self._stop.wait(delay)
                delay = min(delay * 2, 300.0)

    def rebuild_read_model(self) -> dict:
        """Index, backfill ``alert_id``, and fold every stale alert.

        Safe to run at any time and alongside appends: backfill touches only
        rows with no ``alert_id``, and each refold takes the same per-alert
        lock an append does.
        """
        self._ensure_schema()
        self._ensure_alert_id_index()
        backfilled = self._backfill_alert_ids()
        rebuilt = self._fold_stale_alerts()
        self._read_model_status = {"ready": True, "backfilled_events": backfilled,
                                   "rebuilt_alerts": rebuilt, "fold_version": FOLD_VERSION}
        self._read_model_ready.set()
        logger.info("Alert read model ready: %d events backfilled, %d alerts folded", backfilled, rebuilt)
        return dict(self._read_model_status)

    def _ensure_alert_id_index(self) -> None:
        """Build the per-alert index without blocking appends.

        CONCURRENTLY cannot run in a transaction, and a failed concurrent build
        leaves an INVALID index that IF NOT EXISTS would then keep forever — so
        an invalid one is dropped and rebuilt.
        """
        psycopg2, _extras, sql = self._load_driver()
        index = f"idx_{self.table_name}_alert_id"
        conn = psycopg2.connect(self.dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                            "WHERE c.relname = %s", (index,))
                found = cur.fetchone()
                if found is not None and found[0]:
                    return
                if found is not None:
                    cur.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(index)))
                cur.execute(sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON {} (alert_id, created_at)")
                            .format(sql.Identifier(index), sql.Identifier(self.table_name)))
        finally:
            conn.close()

    def _backfill_alert_ids(self) -> int:
        """Fill ``alert_id`` on rows written before the column existed.

        Uses the same ``alert_key`` the fold groups by. "" marks an event with
        no alert, so NULL means only "not classified yet". Keyset on event_id
        keeps each batch an index range, not a rescan.
        """
        psycopg2, extras, sql = self._load_driver()
        last, total = "", 0
        while not self._stop.is_set():
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("SELECT event_id, payload FROM {} WHERE event_id > %s AND alert_id IS NULL "
                                "ORDER BY event_id LIMIT %s").format(sql.Identifier(self.table_name)),
                        (last, _BACKFILL_BATCH),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        return total
                    extras.execute_values(
                        cur,
                        sql.SQL("UPDATE {} AS e SET alert_id = v.alert_id FROM (VALUES %s) AS v(event_id, alert_id) "
                                "WHERE e.event_id = v.event_id").format(
                            sql.Identifier(self.table_name)).as_string(cur),
                        [(event_id, alert_key({"payload": payload})) for event_id, payload in rows],
                    )
            last, total = rows[-1][0], total + len(rows)
        return total

    def _fold_stale_alerts(self) -> int:
        """Refold every alert whose row is missing or at another FOLD_VERSION."""
        psycopg2, _extras, sql = self._load_driver()
        last, total = "", 0
        while not self._stop.is_set():
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "SELECT DISTINCT e.alert_id FROM {events} e "
                            "LEFT JOIN {alerts} a ON a.alert_id = e.alert_id "
                            "WHERE e.alert_id > %s AND (a.alert_id IS NULL OR a.fold_version <> %s) "
                            "ORDER BY e.alert_id LIMIT %s"
                        ).format(events=sql.Identifier(self.table_name), alerts=sql.Identifier(self.alerts_table)),
                        (last, FOLD_VERSION, _REFRESH_CHUNK),
                    )
                    ids = [row[0] for row in cur.fetchall()]
                    if not ids:
                        return total
                    total += self._refresh(cur, ids)
            last = ids[-1]
        return total

    # ---- schema ------------------------------------------------------------

    def health(self) -> dict:
        healthy = bool(self.dsn) and self._last_error is None
        details = {
            "configured": bool(self.dsn),
            "table": self.table_name,
            "alerts_table": self.alerts_table,
            "read_model": {**self._read_model_status, "ready": self._read_model_ready.is_set(),
                           "fold_failures": self._fold_failures},
        }
        if self._last_error:
            details["last_error"] = self._last_error
        return {
            "name": self.name,
            "healthy": healthy,
            "durable": self.durable,
            **details,
        }

    def _ensure_schema(self) -> None:
        if self._schema_ready or not self.dsn:
            return
        psycopg2, _extras, sql = self._load_driver()
        table_identifier = sql.Identifier(self.table_name)
        index_identifier = sql.Identifier(f"idx_{self.table_name}_created_at")
        alerts = sql.Identifier(self.alerts_table)
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        """
                    CREATE TABLE IF NOT EXISTS {} (
                        event_id TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL,
                        event_type TEXT NOT NULL,
                        payload JSONB NOT NULL
                    )
                    """
                    ).format(table_identifier)
                )
                cur.execute(
                    sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (created_at DESC)").format(
                        index_identifier,
                        table_identifier,
                    )
                )
                # CFOP-215. Nullable with no default, so on an existing table
                # this is a catalog change, not a rewrite. COLLATE "C" on both
                # tables: byte order is Python's string order, so SQL paging
                # and ties sort exactly like apply_query, and the join between
                # them never has two collations to choose from.
                cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS alert_id TEXT COLLATE "C"')
                            .format(table_identifier))
                cur.execute(
                    sql.SQL(
                        """
                    CREATE TABLE IF NOT EXISTS {} (
                        alert_id TEXT COLLATE "C" PRIMARY KEY,
                        source TEXT,
                        severity TEXT,
                        status TEXT,
                        action TEXT,
                        summary TEXT,
                        namespace TEXT,
                        resource_name TEXT,
                        first_event_at TIMESTAMPTZ NOT NULL,
                        latest_event_at TIMESTAMPTZ NOT NULL,
                        event_count INTEGER NOT NULL,
                        fold_version INTEGER NOT NULL,
                        activity JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                    ).format(alerts)
                )
                cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (latest_event_at DESC, alert_id DESC)")
                            .format(sql.Identifier(f"idx_{self.alerts_table}_latest"), alerts))
                for column in ("status", "action", "source", "severity"):
                    cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({}, latest_event_at DESC)").format(
                        sql.Identifier(f"idx_{self.alerts_table}_{column}"), alerts, sql.Identifier(column)))
        self._schema_ready = True
        self._last_error = None

    @staticmethod
    def _load_driver():
        import psycopg2
        from psycopg2 import extras, sql

        return psycopg2, extras, sql


def _event_from_row(row) -> dict:
    created = row[1]
    if hasattr(created, "astimezone"):
        created = created.astimezone(timezone.utc).isoformat()
    return {"event_id": row[0], "created_at": str(created), "event_type": row[2], "payload": row[3]}


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
