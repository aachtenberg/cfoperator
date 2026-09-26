"""The Postgres alert read model against a real Postgres (CFOP-215).

The read model is only worth having if it says what the fold says. These hold
it to that — row for row against ``fold_alert``, page for page against the
outbox store's ``apply_query`` — across the ways it gets written: batches in
any order, replayed duplicates, concurrent appends to one alert, a table that
predates it, and a fold version change.

Needs ``CFOP_TEST_PG_DSN``. CI exports one (the postgres service in
tests.yml) and these FAIL there without it, rather than skipping into a
green run that tested nothing. Locally, without it, they skip.
"""
import json
import os
import random
import threading
import uuid
from datetime import timedelta

import pytest

from alert_events import BASE, alert, event, fleet, lifecycle
from event_runtime import activity as activity_module
from event_runtime.activity import alert_key, fold_alert
from event_runtime.alert_store import AlertQuery, AlertStoreUnavailable, decode_cursor
from event_runtime.state import postgres as pg_module
from event_runtime.state.local_outbox import LocalOutboxStateSink
from event_runtime.state.postgres import PostgresStateSink
from event_runtime.state.replay import ReplayingStateSink

DSN = os.getenv("CFOP_TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(not DSN and not os.getenv("CI"),
                                reason="set CFOP_TEST_PG_DSN to run the Postgres read-model tests")


def test_ci_provides_a_database():
    assert DSN, ("CFOP_TEST_PG_DSN is unset. CI must provide it (tests.yml postgres service); "
                 "without it the read-model tests would pass having tested nothing.")


def _normal(value):
    """JSON round trip: what both stores hand back over HTTP."""
    return json.loads(json.dumps(value, default=str))


def _sql(statement, params=()):
    import psycopg2
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None


@pytest.fixture
def table():
    name = f"t{uuid.uuid4().hex[:10]}_events"
    yield name
    alerts = pg_module.alerts_table_name(name)
    _sql(f'DROP TABLE IF EXISTS "{name}"; DROP TABLE IF EXISTS "{alerts}"')


def _built(table):
    sink = PostgresStateSink(dsn=DSN, table_name=table)
    sink._ensure_schema()
    sink.rebuild_read_model()
    return sink


def _outbox(tmp_path, events):
    outbox = LocalOutboxStateSink(directory=str(tmp_path / f"outbox-{uuid.uuid4().hex[:6]}"))
    outbox.append(list(events))
    return outbox


def _by_alert(events):
    grouped = {}
    for e in events:
        grouped.setdefault(alert_key(e), []).append(e)
    grouped.pop("", None)
    return grouped


def _rows(sink):
    return {alert_id: (version, _normal(activity)) for alert_id, version, activity in
            _sql(f'SELECT alert_id, fold_version, activity FROM "{sink.alerts_table}"')}


def _append_shuffled(sink, events, seed):
    rng = random.Random(seed)
    shuffled = list(events)
    rng.shuffle(shuffled)
    i = 0
    while i < len(shuffled):
        n = rng.randint(1, 25)
        assert sink.append(shuffled[i:i + n])
        i += n
    # The outbox replay re-sends events Postgres already has.
    assert sink.append(rng.sample(shuffled, k=min(30, len(shuffled))))


# ---- the read model says what the fold says -------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_row_is_the_fold_of_its_events(table, seed):
    events = fleet(60)
    sink = _built(table)
    _append_shuffled(sink, events, seed)
    rows = _rows(sink)
    expected = {alert_id: _normal(fold_alert(sorted(evs, key=lambda e: e["created_at"])))
                for alert_id, evs in _by_alert(events).items()}
    assert set(rows) == set(expected)
    for alert_id, (version, activity) in rows.items():
        assert version == activity_module.FOLD_VERSION
        assert activity == expected[alert_id], alert_id


QUERIES = [
    AlertQuery(limit=200),
    AlertQuery(limit=200, status="failed"),
    AlertQuery(limit=200, action="log_only"),
    AlertQuery(limit=200, source="cfoperator-sweep", severity="warning"),
    AlertQuery(limit=200, since=BASE + timedelta(minutes=10), until=BASE + timedelta(minutes=30)),
    AlertQuery(limit=200, q="disk pressure"),
    AlertQuery(limit=200, q="100%_\\"),
    AlertQuery(limit=5, full=True),
]


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: repr({k: v for k, v in q.__dict__.items() if v}))
def test_queries_answer_like_the_outbox(table, tmp_path, query):
    events = fleet(60)
    sink = _built(table)
    _append_shuffled(sink, events, seed=7)
    outbox = _outbox(tmp_path, events)
    got, want = sink.list_alerts(query), outbox.list_alerts(query)
    assert _normal(got.alerts) == _normal(want.alerts)
    assert got.next_cursor == want.next_cursor
    assert got.store == "postgres" and want.store == "outbox"


@pytest.mark.parametrize("limit", [1, 4, 13])
def test_paging_matches_the_outbox_through_ties(table, tmp_path, limit):
    events = fleet(45)
    sink = _built(table)
    sink.append(events)
    outbox = _outbox(tmp_path, events)

    def walk(store):
        ids, after = [], None
        while True:
            page = store.list_alerts(AlertQuery(limit=limit, after=after))
            ids += [row["alert_id"] for row in page.alerts]
            if page.next_cursor is None:
                return ids
            after = decode_cursor(page.next_cursor)

    assert walk(sink) == walk(outbox)
    assert len(walk(sink)) == 45


def test_detail_is_the_same_from_either_store(table, tmp_path):
    events = fleet(12)
    sink = _built(table)
    sink.append(events)
    outbox = _outbox(tmp_path, events)
    for alert_id in _by_alert(events):
        assert _normal(sink.get_alert(alert_id)) == _normal(outbox.get_alert(alert_id))
    assert sink.get_alert("no-such-alert") is None


# ---- how it gets built ----------------------------------------------------


def test_a_table_that_predates_the_read_model_is_backfilled(table):
    """The production shape on first deploy: events with no alert_id column."""
    events = fleet(30) + [event("scheduled_task_created", BASE, task={"name": "nightly"})]
    _sql(f'CREATE TABLE "{table}" (event_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, '
         f'event_type TEXT NOT NULL, payload JSONB NOT NULL)')
    for e in events:
        _sql(f'INSERT INTO "{table}" VALUES (%s, %s, %s, %s)',
             (e["event_id"], e["created_at"], e["event_type"], json.dumps(e["payload"])))

    sink = _built(table)
    assert _sql(f'SELECT count(*) FROM "{table}" WHERE alert_id IS NULL')[0][0] == 0
    assert _sql(f"SELECT count(*) FROM \"{table}\" WHERE alert_id = ''")[0][0] == 1
    assert len(_rows(sink)) == 30
    status = sink.health()["read_model"]
    assert status["ready"] and status["backfilled_events"] == len(events) and status["rebuilt_alerts"] == 30


def test_a_fold_version_change_refolds_every_row(table, monkeypatch):
    sink = _built(table)
    sink.append(fleet(20))
    monkeypatch.setattr(pg_module, "FOLD_VERSION", activity_module.FOLD_VERSION + 1)
    assert sink.rebuild_read_model()["rebuilt_alerts"] == 20
    assert {version for version, _ in _rows(sink).values()} == {activity_module.FOLD_VERSION + 1}
    # And a second run finds nothing left to do.
    assert sink.rebuild_read_model()["rebuilt_alerts"] == 0


def test_it_refuses_to_answer_until_built(table):
    sink = PostgresStateSink(dsn=DSN, table_name=table)
    sink._ensure_schema()
    with pytest.raises(AlertStoreUnavailable, match="still being built"):
        sink.list_alerts(AlertQuery())
    sink.rebuild_read_model()
    assert sink.list_alerts(AlertQuery()).store == "postgres"


# ---- what can go wrong ----------------------------------------------------


def test_concurrent_appends_to_one_alert_do_not_lose_an_event(table):
    """Two appends to one alert, each folding without the other's uncommitted
    event, would race to write a row missing one of them. The per-alert lock
    makes the second wait and see both."""
    sink = _built(table)
    for round_ in range(5):
        a = alert(9000 + round_)
        events = [event("alert_received", BASE, alert=a)] + [
            event("checks_requested", BASE + timedelta(milliseconds=n), alert=a, checks=[f"c{n}"])
            for n in range(1, 40)]
        barrier = threading.Barrier(len(events))

        def put(e):
            barrier.wait()
            assert sink.append([e])

        threads = [threading.Thread(target=put, args=(e,)) for e in events]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _version, activity = _rows(sink)[a["alert_id"]]
        assert activity["event_count"] == len(events), f"round {round_}: row folded {activity['event_count']}"


def test_an_alert_whose_fold_raises_is_skipped_not_fatal(table, monkeypatch):
    sink = _built(table)
    good, bad = alert(1), alert(2)
    real = pg_module.fold_alert

    def fold(events):
        if alert_key(events[0]) == bad["alert_id"]:
            raise RuntimeError("fold bug")
        return real(events)

    monkeypatch.setattr(pg_module, "fold_alert", fold)
    events = lifecycle(good, BASE, "completed") + lifecycle(bad, BASE, "failed")
    assert sink.append(events)
    assert set(_rows(sink)) == {good["alert_id"]}
    assert _sql(f'SELECT count(*) FROM "{table}"')[0][0] == len(events)
    assert sink.health()["read_model"]["fold_failures"] == 1


def test_the_replaying_sink_uses_the_outbox_until_postgres_can_answer(table, tmp_path):
    local = LocalOutboxStateSink(directory=str(tmp_path / "outbox"))
    remote = PostgresStateSink(dsn=DSN, table_name=table)
    remote._ensure_schema()
    replaying = ReplayingStateSink(local_sink=local, remote_sinks=[remote],
                                   checkpoint_path=str(tmp_path / "replay.json"))
    events = fleet(10)
    assert replaying.append(events)

    # Not built yet: the outbox answers, completely.
    page = replaying.list_alerts(AlertQuery(limit=50))
    assert page.store == "outbox" and len(page.alerts) == 10

    remote.rebuild_read_model()
    page = replaying.list_alerts(AlertQuery(limit=50))
    assert page.store == "postgres" and len(page.alerts) == 10 and page.lagging is False

    # An event Postgres has not been sent yet: still Postgres, but it says so,
    # and an alert only the outbox knows is still found.
    late = lifecycle(alert(777), BASE + timedelta(days=1), "completed")
    local.append(late)
    page = replaying.list_alerts(AlertQuery(limit=50))
    assert page.store == "postgres" and page.lagging is True
    assert replaying.get_alert("alert-00777")["alert"]["status"] == "completed"


def test_a_failed_refold_is_repaired_after_its_append_commits(table, monkeypatch):
    """Review of #284. A refold that fails marks its rows stale; the rebuild
    that repairs them must start after the append commits, or it can run
    before the marks are visible and declare the model ready with them."""
    import time
    sink = _built(table)
    a = alert(1)
    first = lifecycle(a, BASE, "received")
    assert sink.append(first)

    real, calls = sink._refresh, []

    def fail_once(cur, ids):
        calls.append(ids)
        if len(calls) == 1:
            raise RuntimeError("simulated database error during refold")
        return real(cur, ids)

    monkeypatch.setattr(sink, "_refresh", fail_once)
    later = [event("alert_skipped", BASE + timedelta(seconds=5), alert=a, reason="severity_gate")]
    assert sink.append(later), "a read-model failure must not fail the append"
    assert _sql(f'SELECT count(*) FROM "{table}"')[0][0] == len(first) + len(later)

    deadline = time.time() + 10
    while not sink._read_model_ready.is_set() and time.time() < deadline:
        time.sleep(0.05)
    version, activity = _rows(sink)[a["alert_id"]]
    assert version == activity_module.FOLD_VERSION
    assert activity["event_count"] == len(first) + len(later) and activity["status"] == "logged"


def test_a_rebuild_repeats_when_rows_went_stale_during_its_pass(table, monkeypatch):
    sink = _built(table)
    sink.append(fleet(6))
    real, passes = sink._fold_stale_alerts, []

    def fold_and_mark(*args):
        passes.append(1)
        if len(passes) == 1:
            sink._stale_generation += 1   # an append marked rows mid-pass
        return real(*args)

    monkeypatch.setattr(sink, "_fold_stale_alerts", fold_and_mark)
    sink.rebuild_read_model()
    assert len(passes) == 2
