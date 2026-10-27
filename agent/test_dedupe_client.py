#!/usr/bin/env python3
"""The agent's half of cfop-dedupe (CFOP-538): the client, the fallback, and
the two agent methods every enqueue site goes through.

test_dedupe_parity.py runs every shared case through the real sites. This
file pins what those cases cannot reach: the client's auth and failure modes,
what the fallback decides and who it says decided, the metrics and the span,
and the agent's snapshot, inventory read and fold recording. The repo-wide
guard that no enqueue escapes the funnel is tests/test_dedupe_funnel.py.
"""

import http.server
import json
import os
import sys
import threading
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
import agent.agent as agent_mod  # noqa: E402
import dedupe_client as dc  # noqa: E402
import dedupe_reference  # noqa: E402
from cfshared import tracing  # noqa: E402

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

# Taken before agent/conftest.py swaps in the service's model.
_DECIDE = dc.decide


def _decisions(path, decision, tier, source):
    return dc.DEDUPE_DECISIONS.labels(path=path, decision=decision, tier=tier,
                                      source=source)._value.get()


def _requests(outcome):
    return dc.DEDUPE_REQUEST_SECONDS.labels(outcome=outcome)._sum.get(), \
        sum(b.get() for b in dc.DEDUPE_REQUEST_SECONDS.labels(outcome=outcome)._buckets)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    reply = (200, {"decision": "new", "dedupe_key": "k", "reason": "no open row covers it"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append((self.path, dict(self.headers), body))
        code, out = _Handler.reply
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def service(monkeypatch):
    """A real HTTP service, reached through the real client."""
    monkeypatch.setattr(dc, "decide", _DECIDE)
    _Handler.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def _header(seen, name):
    return next((v for k, v in seen[1].items() if k.lower() == name.lower()), None)


def test_client_posts_the_request_with_the_shared_secret(service, monkeypatch):
    monkeypatch.setenv(dc.SHARED_SECRET_ENV, " s3cret ")
    _Handler.reply = (200, {"decision": "new", "dedupe_key": "k", "reason": "r"})
    out = dc.decide(service, {"candidate": {"dedupe_key": "k"}})
    assert out["decision"] == "new"
    (seen,) = _Handler.seen
    assert seen[0] == "/v1/decide" and seen[2] == {"candidate": {"dedupe_key": "k"}}
    assert _header(seen, dc.AUTH_HEADER) == "s3cret"


def test_client_sends_no_token_when_none_is_configured(service, monkeypatch):
    monkeypatch.delenv(dc.SHARED_SECRET_ENV, raising=False)
    _Handler.reply = (200, {"decision": "new", "reason": "r"})
    dc.decide(service, {"candidate": {}})
    assert _header(_Handler.seen[0], dc.AUTH_HEADER) is None


@pytest.mark.parametrize("reply, status", [
    ((401, {"error": "Missing X-CFOP-Token header"}), 401),
    ((500, {"error": "boom"}), 500),
    ((200, {"error": "not a decision"}), 0),
    ((200, {"decision": "merge"}), 0),
    # A fold or a repeat names its row; without one there is nothing to apply.
    ((200, {"decision": "fold", "tier": "target"}), 0),
    ((200, {"decision": "suppress", "tier": "key"}), 0),
])
def test_client_refuses_anything_but_a_decision(service, reply, status):
    _Handler.reply = reply
    with pytest.raises(dc.DedupeClientError) as e:
        dc.decide(service, {"candidate": {}})
    assert e.value.status == status


def test_client_reports_an_unreachable_service(monkeypatch):
    monkeypatch.setattr(dc, "decide", _DECIDE)
    with pytest.raises(dc.DedupeClientError) as e:
        dc.decide("http://127.0.0.1:9", {"candidate": {}}, timeout=2)
    assert e.value.status == 0


# ---------------------------------------------------------------------------
# resolve(): who decides
# ---------------------------------------------------------------------------

_ROWS = [{"id": 7, "status": "needs-human", "payload": {"dedupe_key": "k"}},
         {"id": 8, "status": "rejected", "payload": {"dedupe_key": "j"}}]


def test_the_service_decides_when_it_answers():
    out = dc.resolve("http://dedupe.test", "investigation", {"dedupe_key": "k"}, _ROWS, [])
    assert out["decision"] == "suppress" and out["row_id"] == 7 and out["source"] == "service"


@pytest.mark.parametrize("fail", [
    dc.DedupeClientError("decide failed (503): down", status=503),
    dc.DedupeClientError("decide failed: timed out"),
    RuntimeError("a bug in the client is still not the enqueue's problem"),
])
def test_an_outage_leaves_the_exact_key_to_decide(monkeypatch, caplog, fail):
    def down(url, request, **kw):
        raise fail
    monkeypatch.setattr(dc, "decide", down)
    errors_before = _requests("error")[1]
    out = dc.resolve("http://dedupe.test", "chat", {"dedupe_key": "k"}, _ROWS, [])
    assert (out["decision"], out["row_id"], out["tier"], out["source"]) == ("suppress", 7, "key", "fallback")
    assert out["error"] == ("http 503" if getattr(fail, "status", 0) else type(fail).__name__)
    assert _requests("error")[1] == errors_before + 1
    assert any("exact key decides" in r.getMessage() for r in caplog.records)

    new = dc.resolve("http://dedupe.test", "chat", {"dedupe_key": "j"}, _ROWS, [])
    assert (new["decision"], new["dedupe_key"], new["source"]) == ("new", "j", "fallback"), (
        "a rejected row's key is free again, as queue_remediation has it")


def test_no_url_asks_nothing(monkeypatch):
    monkeypatch.setattr(dc, "decide", MagicMock(side_effect=AssertionError("asked")))
    out = dc.resolve("", "console", {"dedupe_key": "x"}, _ROWS, [])
    assert (out["decision"], out["dedupe_key"], out["source"]) == ("new", "x", "unconfigured")


def test_an_unread_snapshot_is_not_asked_about(monkeypatch):
    monkeypatch.setattr(dc, "decide", MagicMock(side_effect=AssertionError("asked")))
    out = dc.resolve("http://dedupe.test", "summary", {"dedupe_key": "k"}, None, [])
    # Nothing to check the key against here; queue_remediation's own check
    # at insert time is the backstop.
    assert (out["decision"], out["source"], out["error"]) == ("new", "fallback", "snapshot")


def test_a_keyless_candidate_is_new_and_unkeyed():
    assert dc.fallback({}, _ROWS)["decision"] == "new"
    assert dc.fallback({}, _ROWS)["dedupe_key"] == ""


def test_count_labels_the_decision_a_site_acted_on():
    before = _decisions("summary", "fold", "node", "service")
    dc.count("summary", {"decision": "fold", "tier": "node", "source": "service", "row_id": 3})
    assert _decisions("summary", "fold", "node", "service") == before + 1
    before = _decisions("console", "new", "", "fallback")
    dc.count("console", {"decision": "new", "source": "fallback"})
    assert _decisions("console", "new", "", "fallback") == before + 1


def test_rows_are_trimmed_to_what_the_tiers_read():
    rows = [{"id": 1, "status": "queued", "investigation_id": 5,
             "payload": {"dedupe_key": "k", "recommendation": "r", "rendered_context": "x" * 5000,
                         "proposed_diff": "d", "targets": [{"kind": "k", "id": "i"}]}}]
    assert dc.trim_rows(rows) == [{"id": 1, "status": "queued", "payload": {
        "dedupe_key": "k", "recommendation": "r", "targets": [{"kind": "k", "id": "i"}]}}]


def test_the_candidate_is_a_copy_of_the_contract_fields():
    details = {"dedupe_key": "k", "host": "h", "report": "long", "confidence": 0.9, "title": None}
    cand = dc.candidate_from(details)
    details["host"] = "rewritten"
    assert cand == {"dedupe_key": "k", "host": "h"}


# ---------------------------------------------------------------------------
# The span, and the trace it hands the service
# ---------------------------------------------------------------------------

@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    yield exporter
    tracing.reset()


def test_the_decision_is_a_span_in_the_enqueuers_trace(spans, service):
    _Handler.reply = (200, {"decision": "fold", "row_id": 41, "tier": "target",
                            "reason": "same FIX target as open row #41 (shared: deploy.yaml)",
                            "absorbed": "nginx crashloops"})
    with tracing.span("investigation"):
        dc.resolve(service, "investigation", {"dedupe_key": "k", "recommendation": "secret text"}, [], [])
    (decide_span,) = [s for s in spans.get_finished_spans() if s.name == "dedupe.decide"]
    (parent,) = [s for s in spans.get_finished_spans() if s.name == "investigation"]
    assert decide_span.parent.span_id == parent.context.span_id
    assert dict(decide_span.attributes) == {
        "cfop.dedupe.path": "investigation", "cfop.dedupe.decision": "fold",
        "cfop.dedupe.tier": "target", "cfop.dedupe.source": "service", "cfop.dedupe.row_id": 41}
    # Shapes, not text: the candidate and the service's reason stay off it.
    assert "secret text" not in json.dumps(dict(decide_span.attributes))
    # The service is handed this trace, so its decision log line can name it.
    traceparent = _header(_Handler.seen[0], "traceparent")
    assert traceparent and f"{decide_span.context.trace_id:032x}" in traceparent


def test_a_fallback_marks_the_span_failed(spans, service):
    _Handler.reply = (503, {"error": "down"})
    dc.resolve(service, "chat", {"dedupe_key": "k"}, [], [])
    (s,) = [s for s in spans.get_finished_spans() if s.name == "dedupe.decide"]
    assert s.attributes["cfop.dedupe.source"] == "fallback"
    assert s.attributes["cfop.dedupe.http_status"] == 503
    assert s.attributes["error.type"] == "DedupeClientError"


# ---------------------------------------------------------------------------
# The agent's two methods
# ---------------------------------------------------------------------------

def _op(rows=(), notready=()):
    op = dedupe_reference.wire(MagicMock())
    op.kb.list_open_remediations_for_dedupe.return_value = list(rows)
    op._notready_nodes = MagicMock(return_value=set(notready))
    return op


def test_the_url_comes_from_env_then_config(monkeypatch):
    op = MagicMock()
    op.config = {"remediation": {"dedupe": {"url": "http://cfg:8093/"}}}
    monkeypatch.delenv("CFOP_DEDUPE_URL", raising=False)
    assert CFOperator._dedupe_url(op) == "http://cfg:8093"
    monkeypatch.setenv("CFOP_DEDUPE_URL", " http://env:8093/ ")
    assert CFOperator._dedupe_url(op) == "http://env:8093"
    monkeypatch.delenv("CFOP_DEDUPE_URL")
    op.config = {"remediation": {"queue_feed": True}}
    assert CFOperator._dedupe_url(op) == ""


def test_decide_sends_the_snapshot_and_the_inventory_the_node_tier_needs():
    rows = [{"id": 5, "status": "queued", "payload": {"dedupe_key": "node-down-raspberrypi5"}}]
    op = _op(rows, notready={"RaspberryPi5"})
    d = CFOperator._dedupe_decide(op, "investigation", {"dedupe_key": "inv-1", "host": "raspberrypi5:9100",
                                                        "trigger": "pod not ready"})
    assert (d["decision"], d["row_id"], d["tier"]) == ("fold", 5, "node")
    (asked,) = dedupe_reference.REQUESTS
    assert asked["vocabulary"] == {"notready_nodes": ["raspberrypi5"]}
    assert asked["open_rows"] == dc.trim_rows(rows)


def test_no_host_means_no_inventory_read():
    op = _op()
    CFOperator._dedupe_decide(op, "chat", {"dedupe_key": "chat-1", "recommendation": "r"})
    op._notready_nodes.assert_not_called()
    assert dedupe_reference.REQUESTS[0]["vocabulary"] == {"notready_nodes": []}


def test_an_unreadable_snapshot_falls_back_without_asking():
    op = _op()
    op.kb.list_open_remediations_for_dedupe.side_effect = RuntimeError("db down")
    d = CFOperator._dedupe_decide(op, "console", {"dedupe_key": "m-1", "host": "raspberrypi5"})
    assert (d["decision"], d["source"]) == ("new", "fallback")
    assert dedupe_reference.REQUESTS == []
    op._notready_nodes.assert_not_called()


def test_decide_counts_unless_told_not_to():
    op = _op()
    before = _decisions("summary", "new", "", "service")
    CFOperator._dedupe_decide(op, "summary", {"dedupe_key": "s-1"}, count=False)
    assert _decisions("summary", "new", "", "service") == before
    CFOperator._dedupe_decide(op, "summary", {"dedupe_key": "s-1"})
    assert _decisions("summary", "new", "", "service") == before + 1


def test_a_fold_records_its_line_and_its_alias(monkeypatch):
    op = _op()
    folded = MagicMock()
    monkeypatch.setattr(agent_mod, "REMEDIATION_FOLDED", folded)
    rid = CFOperator._apply_dedupe_fold(op, {"decision": "fold", "row_id": 9, "tier": "identifier",
                                             "absorbed": "the same PG_URL fix", "alias": "inv-2"})
    assert rid == 9
    op.kb.record_remediation_absorbed.assert_called_once_with(9, "the same PG_URL fix")
    op.kb.record_remediation_alias.assert_called_once_with(9, "inv-2")
    folded.labels.assert_called_once_with(reason="repeat")


def test_a_node_fold_records_no_alias_and_no_fold_count(monkeypatch):
    op = _op()
    folded = MagicMock()
    monkeypatch.setattr(agent_mod, "REMEDIATION_FOLDED", folded)
    CFOperator._apply_dedupe_fold(op, {"decision": "fold", "row_id": 4, "tier": "node",
                                       "absorbed": "pod not ready"})
    op.kb.record_remediation_alias.assert_not_called()
    folded.labels.assert_not_called()


def test_a_fold_that_cannot_be_recorded_is_still_a_fold():
    """Enqueuing a second row because a note failed to save would bring back
    the duplicate the fold exists to stop."""
    op = _op()
    op.kb.record_remediation_absorbed.side_effect = RuntimeError("db blip")
    assert CFOperator._apply_dedupe_fold(op, {"decision": "fold", "row_id": 3, "tier": "target",
                                              "absorbed": "x", "alias": "k"}) == 3
