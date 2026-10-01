"""Boot every database layer against a real Postgres and round-trip a write.

Usage:

    python scripts/db_smoke.py postgresql://postgres:pw@localhost:5432/postgres
    # or with CFOP_TEST_PG_DSN set

The DSN must be able to CREATE DATABASE and CREATE EXTENSION vector (a CI
service superuser; never point this at production). A throwaway database is
created, every layer is exercised with whatever driver this image has
installed, and the database is dropped. Exit 0 when all pass; otherwise 1,
naming the step that failed.

Why it exists (CFOP-249): on 2026-10-01 a rebuild pulled SQLAlchemy 2.1, whose
default PostgreSQL driver is not the one installed, and the agent and
event_runtime crash-looped on boot. Nothing between merge and deploy had
started the built image against a database. This runs on every PR
(tests/test_db_rollout.py) and inside the just-built image before the deploy
bump (build-cfoperator-main.yml, db-smoke), so an image that cannot talk to
its database is pushed but never deployed.

Engines are built through cfshared.db.sqlalchemy_url, exactly as the layers
build their own, so a driver the code names but the image lacks fails here.
"""

import os
import sys
import traceback
import uuid
from pathlib import Path

# The agent's modules import each other bare (``from embedding_service import
# ...``), exactly as the image's PYTHONPATH (/app/agent:/app) allows. Run as a
# script, so this path change stays in this process.
ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "agent"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from cfshared.db import sqlalchemy_url  # noqa: E402

EMBEDDING_DIM = 768  # nomic-embed-text, the column the knowledge base creates


def _create_database(admin_url, name):
    engine = create_engine(sqlalchemy_url(admin_url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    engine = create_engine(sqlalchemy_url(admin_url.set(database=name)), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            # Production's knowledge-base database ships pgvector
            # (pgvector/pgvector:pg15); the embeddings tables need it.
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()


def _drop_database(admin_url, name):
    engine = create_engine(sqlalchemy_url(admin_url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def _libpq_dsn(url):
    """The same database as a libpq URI, for the layers that use the raw driver."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def check_driver(url):
    engine = create_engine(sqlalchemy_url(url))
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
        return f"{engine.dialect.driver} -> PostgreSQL {version}"
    finally:
        engine.dispose()


def check_knowledge_base(url):
    from knowledge_base import KnowledgeBase

    kb = KnowledgeBase(db_url=url.render_as_string(hide_password=False), host_id="db-smoke")
    try:
        assert kb.initialize_schema() is True, "initialize_schema() reported a constraint it could not apply"
        kb.set_setting("db_smoke", "ok")
        assert kb.get_setting("db_smoke") == "ok", "setting did not round-trip"
        inv_id = kb.start_investigation("db smoke")
        kb.update_investigation(inv_id, findings={"response": "db smoke"}, outcome="monitoring",
                                duration_seconds=0.1, tool_calls_count=0)
        assert kb.store_investigation_embedding(
            inv_id, [0.001 * i for i in range(EMBEDDING_DIM)], "db-smoke", "db smoke") is True, \
            "the vector embedding was not stored"
        learning_id = kb.store_learning({"learning_type": "insight", "title": "db smoke",
                                         "description": "the knowledge base round-trips a write"})
        assert learning_id, "store_learning returned no id"
        return f"schema ok, investigation {inv_id}, embedding, learning {learning_id}"
    finally:
        kb.engine.dispose()


def check_auth_store(url):
    from auth.store import AuthStore

    store = AuthStore(db_url=url.render_as_string(hide_password=False))
    try:
        store.ensure_schema()  # what auth/bootstrap.py runs at startup
        user = store.create_user("db-smoke", "db-smoke-password-1")
        assert store.get_user(user["id"])["username"] == "db-smoke", "user did not round-trip"
        return f"user {user['id']}"
    finally:
        store.engine.dispose()


def check_event_runtime(url):
    from datetime import datetime, timezone

    from event_runtime.alert_store import AlertQuery
    from event_runtime.state.postgres import PostgresStateSink

    sink = PostgresStateSink(dsn=_libpq_dsn(url), table_name="db_smoke_events")
    sink._ensure_schema()
    sink.rebuild_read_model()
    now = datetime.now(timezone.utc).isoformat()
    alert = {"alert_id": "db-smoke-1", "source": "db-smoke", "severity": "info",
             "summary": "db smoke", "details": {}, "namespace": "smoke", "resource_type": "pod",
             "resource_name": "smoke", "fingerprint": None, "occurred_at": now}
    events = [{"event_id": str(uuid.uuid4()), "event_type": "alert_received",
               "created_at": now, "payload": {"alert": alert}}]
    assert sink.append(events), "append returned False"
    assert sink.get_alert("db-smoke-1"), "the alert did not fold back"
    page = sink.list_alerts(AlertQuery(limit=5))
    assert [row["alert_id"] for row in page.alerts] == ["db-smoke-1"], f"list_alerts returned {page}"
    return "append, get_alert, list_alerts"


def check_timescale_tool(url):
    from tools.timescale import TimescaleTools

    tool = TimescaleTools(host=url.host, port=url.port or 5432, database=url.database,
                          user=url.username, password=url.password or "")
    result = tool.query("SELECT 1 AS one")
    assert result.get("success") and result.get("rows") == [{"one": 1}], f"query returned {result}"
    return "query with statement_timeout"


CHECKS = (
    ("driver", check_driver),
    ("knowledge base", check_knowledge_base),
    ("auth store", check_auth_store),
    ("event runtime", check_event_runtime),
    ("timescale tool", check_timescale_tool),
)


def run(admin_dsn):
    """Run every check in a throwaway database; return [(step, ok, detail)]."""
    admin_url = make_url(admin_dsn)
    name = f"cfop_db_smoke_{uuid.uuid4().hex[:10]}"
    results = []
    try:
        # Inside the cleanup: CREATE DATABASE can succeed and CREATE EXTENSION
        # then fail, and that database must still be dropped (DROP ... IF
        # EXISTS covers the case where nothing was created).
        try:
            _create_database(admin_url, name)
        except Exception as exc:
            results.append(("create database", False, f"{type(exc).__name__}: {exc}"))
            return results
        url = admin_url.set(database=name)
        for step, check in CHECKS:
            try:
                results.append((step, True, check(url)))
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if os.getenv("CFOP_DB_SMOKE_TRACEBACK"):
                    detail += "\n" + traceback.format_exc()
                results.append((step, False, detail))
    finally:
        try:
            _drop_database(admin_url, name)
        except Exception as exc:
            # Cleanup, not a property of the image: a throwaway database left
            # in a CI service container is harmless, so it warns, not fails.
            print(f"warn drop database {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return results


def main(argv):
    dsn = argv[1] if len(argv) > 1 else os.getenv("CFOP_TEST_PG_DSN", "")
    if not dsn:
        print("usage: db_smoke.py <admin-dsn>  (or set CFOP_TEST_PG_DSN)", file=sys.stderr)
        return 2
    results = run(dsn)
    for step, ok, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'} {step}: {detail}")
    failed = [step for step, ok, _ in results if not ok]
    print("db smoke: " + ("passed" if not failed else "FAILED at " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
