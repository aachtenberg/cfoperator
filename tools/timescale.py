"""
TimescaleDB Tools for CFOperator
=================================

Read-only SQL access to the telemetry TimescaleDB (`sensors` database).
This is where telegraf lands every MQTT message (tagged with device_id /
device_name / topic), so questions like "which device is emitting the most
telemetry?" are a single SELECT here instead of a spelunking expedition
through Prometheus aggregates and broker logs.

Safety model (LLM-facing tool, so belt and braces):
- connects as a dedicated read-only role (no write grants at the DB level)
- default_transaction_read_only=on at the connection level
- single-statement SELECT/WITH only, enforced before execution
- statement_timeout + row cap so a runaway query can't wedge the agent
"""

from typing import Dict, Any, List
import datetime
import decimal
import logging
import re

import psycopg2
import psycopg2.extras

logger = logging.getLogger("cfoperator.tools.timescale")

# Tables telegraf fills from MQTT, surfaced in the tool description so the
# LLM doesn't burn calls rediscovering the schema every session.
SCHEMA_CHEAT_SHEET = (
    "MQTT telemetry tables (one row per message; all have time, topic, and device or device_id/device_name): "
    "esp_temperature(celsius, humidity, battery_percent), "
    "esp_weather(temperature, humidity, pressure), "
    "esp_status(uptime_seconds, wifi_rssi, free_heap, battery_voltage), "
    "esp_gps(gps_latitude, gps_longitude, gps_satellites), "
    "surveillance(device, ip, motion_count, capture_count, framerate, wifi_rssi), "
    "esphome_display(device, value), lora_gateway(gateway_id, status, rssi), "
    "solar_battery(voltage, current, soc), solar_mppt(mppt1_pv_power, mppt2_pv_power). "
    "Example - loudest MQTT device last hour: "
    "SELECT device, count(*) FROM (SELECT device, time FROM esp_temperature UNION ALL "
    "SELECT device, time FROM esp_weather UNION ALL SELECT device, time FROM esp_status UNION ALL "
    "SELECT device, time FROM surveillance UNION ALL SELECT device, time FROM esphome_display UNION ALL "
    "SELECT device, time FROM solar_battery UNION ALL SELECT device, time FROM solar_mppt) t "
    "WHERE time > now() - interval '1 hour' GROUP BY device ORDER BY 2 DESC; "
    "Also present: river/flood data (wsc_readings, river_readings, dam_releases, dam_levels, "
    "reservoir_readings, swe_daily, orrpb_river_levels) and eccc_climate_daily."
)

# One statement, must read like a query. Comments are stripped before this
# check so '-- comment\nSELECT ...' passes and 'DELETE ... -- SELECT' fails.
_ALLOWED_PREFIX = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    """Remove line and block comments (naive but safe: we only ever reject more)."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def validate_readonly_sql(sql: str) -> str:
    """
    Return the cleaned single statement, or raise ValueError.

    This is a first gate for fast, helpful errors - the real enforcement is
    the read-only role + read-only transaction on the connection.
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL query")

    cleaned = _strip_comments(sql).strip()
    # Allow trailing semicolons; reject anything multi-statement.
    cleaned = re.sub(r"[;\s]+$", "", cleaned)
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed - send one SELECT per call")
    if not _ALLOWED_PREFIX.match(cleaned):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed - this tool is read-only")
    return cleaned


def _jsonable(value):
    """Coerce DB types the JSON serializer chokes on."""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    return value


class TimescaleTools:
    """Read-only query access to the telemetry TimescaleDB."""

    def __init__(self, host: str, port: int, database: str, user: str,
                 password: str, statement_timeout_ms: int = 15000):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.statement_timeout_ms = statement_timeout_ms

    def query(self, sql: str, max_rows: int = 200) -> Dict[str, Any]:
        """
        Run one read-only SQL statement and return rows as dicts.

        Args:
            sql: A single SELECT / WITH statement.
            max_rows: Row cap (1-1000); larger result sets are truncated.
        """
        try:
            cleaned = validate_readonly_sql(sql)
        except ValueError as e:
            return {"error": str(e)}

        max_rows = max(1, min(int(max_rows or 200), 1000))

        conn = None
        try:
            conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                dbname=self.database,
                user=self.user,
                password=self.password,
                connect_timeout=5,
                options=(
                    f"-c default_transaction_read_only=on "
                    f"-c statement_timeout={self.statement_timeout_ms}"
                ),
            )
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(cleaned)
                rows = cur.fetchmany(max_rows)
                truncated = cur.fetchone() is not None
            result_rows = [
                {k: _jsonable(v) for k, v in row.items()} for row in rows
            ]
            out = {
                "success": True,
                "row_count": len(result_rows),
                "rows": result_rows,
            }
            if truncated:
                out["truncated"] = True
                out["hint"] = (
                    f"Result exceeded max_rows={max_rows}; aggregate (GROUP BY / count) "
                    "or add LIMIT instead of paging raw rows."
                )
            return out
        except psycopg2.Error as e:
            # primary error line only - psycopg2 messages carry multi-line detail
            msg = str(e).strip().split("\n")[0]
            return {"error": f"Query failed: {msg}"}
        finally:
            if conn is not None:
                conn.close()

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Tool schemas for LLM function calling."""
        return [
            {
                "name": "timescale_query",
                "description": (
                    "Run a read-only SQL query against the telemetry TimescaleDB "
                    "(database 'sensors') where telegraf stores every MQTT message and "
                    "the flood-monitoring history. USE THIS for questions about IoT/MQTT "
                    "device activity, message rates, last-seen times, sensor readings, or "
                    "river/dam data - one SQL query replaces many Prometheus/Loki calls. "
                    + SCHEMA_CHEAT_SHEET
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": (
                                "A single SELECT (or WITH ... SELECT) statement. "
                                "No writes, no multiple statements. Prefer aggregates "
                                "(count, max(time), avg) over raw row dumps."
                            ),
                        },
                        "max_rows": {
                            "type": "integer",
                            "description": "Maximum rows to return (default 200, cap 1000)",
                            "default": 200,
                        },
                    },
                    "required": ["sql"],
                },
            }
        ]
