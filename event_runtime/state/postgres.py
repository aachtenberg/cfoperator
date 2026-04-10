"""Optional PostgreSQL sink for remote event persistence."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from .base import BaseStateSink


logger = logging.getLogger(__name__)


class PostgresStateSink(BaseStateSink):
    """Persist domain events to PostgreSQL when psycopg2 is available."""

    durable = False

    def __init__(self, dsn: str | None = None, table_name: str = "event_runtime_events"):
        super().__init__(name="postgres")
        self.dsn = dsn or os.getenv("CFOP_EVENT_RUNTIME_PG_DSN", "")
        self.table_name = table_name
        self._last_error: Optional[str] = None
        self._schema_ready = False

    def start(self) -> None:
        if self.dsn:
            self._ensure_schema()

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
                "INSERT INTO {} (event_id, created_at, event_type, payload) "
                "VALUES (%s, %s, %s, %s) "
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
                            )
                            for event in events
                        ],
                    )
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Failed to append events to PostgreSQL sink %s: %s", self.table_name, exc)
            return False

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

    def health(self) -> dict:
        healthy = bool(self.dsn) and self._last_error is None
        details = {
            "configured": bool(self.dsn),
            "table": self.table_name,
        }
        if self._last_error:
            details["last_error"] = self._last_error
        return {
            "name": self.name,
            "healthy": healthy,
            "durable": False,
            **details,
        }

    def _ensure_schema(self) -> None:
        if self._schema_ready or not self.dsn:
            return
        psycopg2, _extras, sql = self._load_driver()
        table_identifier = sql.Identifier(self.table_name)
        index_identifier = sql.Identifier(f"idx_{self.table_name}_created_at")
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
        self._schema_ready = True
        self._last_error = None

    @staticmethod
    def _load_driver():
        import psycopg2
        from psycopg2 import extras, sql

        return psycopg2, extras, sql