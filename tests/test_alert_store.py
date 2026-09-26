"""The per-alert query surface (CFOP-215), on the outbox store.

The old /activity folded a window of recent events and filtered afterwards,
so a filter only saw alerts whose events happened to fit. These pin the
properties that replaced it — a filter sees every alert, paging visits each
alert exactly once, and ties in time do not reorder or drop rows — rather
than any particular output.
"""
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer

import pytest

from alert_events import BASE, alert, fleet, lifecycle
from event_runtime.alert_store import (
    AlertQuery,
    AlertStoreUnavailable,
    decode_cursor,
    encode_cursor,
    parse_alert_query,
)
from event_runtime.defaults import OpenReasoningDecisionEngine
from event_runtime.engine import EventRuntime
from event_runtime.plugin_manager import PluginManager
from event_runtime.server import make_handler
from event_runtime.state.base import BaseStateSink
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink


def _runtime(tmp_path, events=()):
    outbox = LocalOutboxStateSink(directory=str(tmp_path / "outbox"))
    if events:
        outbox.append(list(events))
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([outbox]))
    plugins.register_decision_engine(OpenReasoningDecisionEngine())
    return EventRuntime(plugins)


def _walk(runtime, query):
    """Every alert id the query yields, following cursors to the end."""
    ids, after = [], None
    for _ in range(1000):
        page = runtime.list_alerts(AlertQuery(**{**query.__dict__, "after": after}))
        ids += [row["alert_id"] for row in page.alerts]
        if page.next_cursor is None:
            return ids
        after = decode_cursor(page.next_cursor)
    raise AssertionError("paging did not terminate")


# ---- the regression this replaces -----------------------------------------


def test_a_status_filter_sees_every_alert_not_a_window(tmp_path):
    """Five failures older than 300 newer alerts. The window fold looked at the
    newest max(limit*12, 100) events and found none of them."""
    events = []
    for i in range(5):
        events += lifecycle(alert(i), BASE + timedelta(minutes=i), "failed")
    for i in range(5, 305):
        events += lifecycle(alert(i), BASE + timedelta(hours=1, minutes=i), "completed")
    runtime = _runtime(tmp_path, events)

    failed = runtime.list_alerts(AlertQuery(status="failed", limit=50))
    assert sorted(row["alert_id"] for row in failed.alerts) == [f"alert-{i:05d}" for i in range(5)]
    # /activity is served from the same model now.
    assert len(runtime.recent_activity(limit=25, status="failed")) == 5


# ---- paging ---------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 7, 50])
def test_paging_visits_every_alert_exactly_once_in_order(tmp_path, limit):
    runtime = _runtime(tmp_path, fleet(40))
    everything = runtime.list_alerts(AlertQuery(limit=200))
    expected = [row["alert_id"] for row in everything.alerts]
    assert len(expected) == 40 and everything.next_cursor is None

    walked = _walk(runtime, AlertQuery(limit=limit))
    assert walked == expected


def test_ties_in_time_are_broken_by_alert_id(tmp_path):
    runtime = _runtime(tmp_path, fleet(40))
    rows = runtime.list_alerts(AlertQuery(limit=200)).alerts
    keys = [(row["latest_event_at"], row["alert_id"]) for row in rows]
    assert len({row["latest_event_at"] for row in rows}) < len(rows), "the fleet must contain ties"
    assert keys == sorted(keys, reverse=True)


def test_paging_with_a_filter_stays_inside_the_filter(tmp_path):
    runtime = _runtime(tmp_path, fleet(60))
    walked = _walk(runtime, AlertQuery(limit=3, source="cfoperator-sweep"))
    full = [row["alert_id"] for row in runtime.list_alerts(AlertQuery(limit=200)).alerts
            if row["source"] == "cfoperator-sweep"]
    assert walked == full and walked


# ---- filters --------------------------------------------------------------


def test_filters_combine(tmp_path):
    runtime = _runtime(tmp_path, fleet(60))
    rows = runtime.list_alerts(AlertQuery(limit=200)).alerts
    since = BASE + timedelta(minutes=20)
    until = BASE + timedelta(minutes=40)
    got = runtime.list_alerts(AlertQuery(limit=200, severity="critical", since=since, until=until,
                                         q="disk PRESSURE")).alerts
    want = [row for row in rows
            if row["severity"] == "critical"
            and since.isoformat() <= row["latest_event_at"] < until.isoformat()
            and "disk pressure" in row["summary"].lower()]
    assert [r["alert_id"] for r in got] == [r["alert_id"] for r in want] and want


def test_q_matches_resource_namespace_and_id(tmp_path):
    runtime = _runtime(tmp_path, fleet(10))
    assert [r["alert_id"] for r in runtime.list_alerts(AlertQuery(q="pod-3")).alerts] == ["alert-00003"]
    assert [r["alert_id"] for r in runtime.list_alerts(AlertQuery(q="ALERT-00004")).alerts] == ["alert-00004"]


# ---- shapes ---------------------------------------------------------------


def test_list_rows_leave_out_the_timeline_and_detail_has_the_events(tmp_path):
    runtime = _runtime(tmp_path, fleet(5))
    row = runtime.list_alerts(AlertQuery(limit=1)).alerts[0]
    assert "timeline" not in row and "event_types" not in row
    detail = runtime.get_alert(row["alert_id"])
    assert detail["alert"]["alert_id"] == row["alert_id"]
    assert detail["alert"]["timeline"]
    assert [e["event_type"] for e in detail["events"]] == detail["alert"]["event_types"]
    assert runtime.get_alert("no-such-alert") is None


# ---- parameters -----------------------------------------------------------


@pytest.mark.parametrize("params, message", [
    ({"statu": "failed"}, "Unknown parameter"),
    ({"limit": "0"}, "limit"),
    ({"limit": "201"}, "limit"),
    ({"limit": "ten"}, "limit"),
    ({"severity": "urgent"}, "severity"),
    ({"since": "yesterday"}, "since"),
    ({"since": "2026-09-02T00:00:00Z", "until": "2026-09-01T00:00:00Z"}, "earlier"),
    ({"cursor": "not-a-cursor"}, "cursor"),
    ({"q": "x" * 201}, "q"),
])
def test_bad_parameters_are_refused(params, message):
    with pytest.raises(ValueError, match=message):
        parse_alert_query(params)


def test_a_cursor_round_trips():
    stamp = BASE + timedelta(microseconds=123456)
    assert decode_cursor(encode_cursor(stamp, "alert-00001")) == (stamp, "alert-00001")


def test_blank_parameters_mean_unset():
    assert parse_alert_query({"status": "", "q": "  ", "limit": ""}) == AlertQuery()


# ---- over HTTP ------------------------------------------------------------


@pytest.fixture
def served(tmp_path):
    def serve(runtime):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"
    servers = []
    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_list_and_detail(tmp_path, served):
    base = served(_runtime(tmp_path, fleet(12)))
    status, body = _get(f"{base}/v1/alerts?limit=5&source=alertmanager")
    assert status == 200 and body["store"] == "outbox" and len(body["alerts"]) == 4
    assert all(row["source"] == "alertmanager" for row in body["alerts"])

    alert_id = body["alerts"][0]["alert_id"]
    status, detail = _get(f"{base}/v1/alerts/{alert_id}")
    assert status == 200 and detail["alert"]["alert_id"] == alert_id and detail["events"]

    assert _get(f"{base}/v1/alerts/nope")[0] == 404
    assert _get(f"{base}/v1/alerts/{alert_id}/deeper")[0] == 404
    assert _get(f"{base}/v1/alerts?statu=failed")[0] == 400


class _WindowOnlySink(BaseStateSink):
    """A sink that keeps events but no read model, like a third-party one."""
    durable = True

    def __init__(self):
        super().__init__(name="window-only")

    def append(self, events):
        return True

    def recent(self, limit=50):
        return []

    def health(self):
        return {"name": self.name, "healthy": True, "durable": True}


def test_a_sink_without_a_read_model_is_a_503_not_a_window(tmp_path, served):
    plugins = PluginManager()
    plugins.register_state_sink(CompositeStateSink([_WindowOnlySink()]))
    plugins.register_decision_engine(OpenReasoningDecisionEngine())
    runtime = EventRuntime(plugins)
    with pytest.raises(AlertStoreUnavailable):
        runtime.list_alerts(AlertQuery())
    base = served(runtime)
    assert _get(f"{base}/v1/alerts")[0] == 503
    assert _get(f"{base}/v1/alerts/x")[0] == 503
