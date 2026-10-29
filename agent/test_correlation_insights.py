"""CFOP-149: the correlation pass must not write the learnings it confabulated.

Investigation timestamps are not causation, co-occurrence is not a root
cause, a namespace the cluster does not have is refutable at write time,
and the 18th copy of the same sentence is not a new observation.

CFOP-568: a rollout is not a failure, a pair's count is what the window
holds rather than how many sweeps saw it, and the same services in new
words are the same insight.
"""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from knowledge_base import (
    InvestigationLearning,
    KnowledgeBase,
    ServiceCorrelation,
    correlate_events,
    correlation_insight_rejection,
    correlation_insight_subject,
    drift_failed_services,
    service_failure_patterns,
)


def _at(seconds):
    return datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def _inv(i, seconds, trigger):
    return SimpleNamespace(id=i, started_at=_at(seconds), trigger=trigger, outcome="monitoring")


def _drift(i, seconds):
    return SimpleNamespace(id=i, detected_at=_at(seconds), drift_type="state_change",
                           description="probe timeout")


def test_two_investigations_are_not_a_correlation():
    # The live failure: the pass paired its own queue timestamps and the
    # model reported that as camera-api causing plane-api. Restoring the
    # investigation-investigation loop makes this non-empty.
    paired = correlate_events(
        [_inv(1, 0, "camera-api exit 255"), _inv(2, 30, "plane-api readiness")],
        [],
        window_seconds=300,
    )
    assert paired == []


def test_an_investigation_near_a_drift_event_is_a_correlation():
    paired = correlate_events(
        [_inv(1, 0, "camera-api exit 255"), _inv(2, 20, "plane-api readiness")],
        [_drift(9, 10)],
        window_seconds=300,
    )
    assert len(paired) == 2
    assert {p["event_b"]["type"] for p in paired} == {"drift"}
    assert all(p["event_a"]["type"] == "investigation" for p in paired)


def test_root_cause_from_the_correlation_pass_is_refused():
    reason = correlation_insight_rejection({
        "learning_type": "root_cause",
        "title": "camera-api restarts cause plane-api probe timeouts",
        "description": "they happen together",
        "applies_when": "camera-api exits 255 while plane-api probes time out",
    })
    assert reason == "automated correlation cannot claim root_cause"


def test_a_namespace_the_cluster_does_not_have_is_refused():
    reason = correlation_insight_rejection(
        {
            "learning_type": "pattern",
            "title": "camera-api pods in the plane namespace restart",
            "description": "camera-api pods in the plane namespace",
            "applies_when": "camera-api in the plane namespace exits 255",
        },
        known_namespaces=["apps", "kube-system"],
    )
    assert reason is not None and "plane" in reason


def test_namespace_check_is_skipped_when_the_cluster_list_is_unavailable():
    insight = {
        "learning_type": "pattern",
        "title": "camera-api pods in the plane namespace restart",
        "description": "camera-api pods in the plane namespace",
        "applies_when": "camera-api in the plane namespace exits 255",
    }
    assert correlation_insight_rejection(insight, known_namespaces=None) is None
    assert correlation_insight_rejection(insight, known_namespaces=[]) is None


def test_same_namespace_is_not_read_as_a_namespace_name():
    assert correlation_insight_rejection(
        {
            "learning_type": "pattern",
            "title": "two pods in the same namespace restart together",
            "description": "they share a namespace",
            "applies_when": "two pods in the same namespace restart within 5 min",
        },
        known_namespaces=["apps"],
    ) is None


def test_a_repeated_title_is_not_stored_again():
    first = {
        "learning_type": "pattern",
        "title": "camera-api exit 255 correlates with plane-api timeouts",
        "description": "first copy",
        "applies_when": "camera-api exits 255",
    }
    from knowledge_base import correlation_insight_keys
    seen = correlation_insight_keys(first)
    reason = correlation_insight_rejection(
        {
            "learning_type": "pattern",
            "title": "Camera-api exit 255 correlates with plane-api timeouts!",
            "description": "eighteenth copy",
            "applies_when": "a different trigger condition this time",
        },
        seen_keys=seen,
    )
    assert reason == "duplicate correlation learning"


def test_analyze_correlations_stores_only_the_insight_that_survives():
    """The write loop is the gate. A model that still emits root_cause and a
    namespace the cluster does not have must not reach store_learning.
    Restoring a blanket store of insights[:3] stores all three and this fails.
    """
    op = CFOperator.__new__(CFOperator)
    op.config = {
        "llm": {"primary": {"url": "http://ollama:11434", "model": "gemma4:26b"}},
        "notifications": {},
    }
    op.llm_timeout = 5
    op.notifications = []
    op.tools = SimpleNamespace(k8s_tools=SimpleNamespace(get_namespaces=lambda: {
        "success": True,
        "namespaces": [{"name": "apps"}, {"name": "kube-system"}],
    }))
    stored = []

    class KB:
        def get_setting(self, key, default=""):
            if key == "selected_backend":
                return "ollama"
            if key == "ollama_selected_model":
                return "gemma4:26b"
            return default

        def get_operational_summary(self, hours=24):
            return {"sweeps": {}, "investigations": {"total": 0}, "learnings": {}}

        def find_learnings(self, **kwargs):
            return [{
                "title": "camera-api exit 255 correlates with plane-api timeouts",
                "applies_when": "camera-api exits 255",
                "tags": ["automated"],
            }]

        def store_learning(self, insight):
            stored.append(dict(insight))
            return 7

        _kb = SimpleNamespace(
            find_correlated_events=lambda **k: [],
            get_service_correlations=lambda **k: [],
        )

    op.kb = KB()
    op._embed_learning = MagicMock()
    body = {"insights": [
        {
            "learning_type": "pattern",
            "title": "ollama on ubuntu-llm-01 times out loading a model",
            "description": "llama-server did not start while the daemon was up",
            "applies_when": "ollama load fails while the daemon is healthy",
            "services": ["ollama"],
            # Volunteered by the model; it must not displace `automated`,
            # which is what keeps the row out of investigation prompts (CFOP-377).
            # A non-string tag is dropped rather than failing the pass.
            "tags": ["ollama", {"nested": "tag"}],
            "category": "resource",
        },
        {
            "learning_type": "root_cause",
            "title": "camera-api restarts cause plane-api probe timeouts",
            "description": "they happen together",
            "applies_when": "camera-api exits 255 while plane-api probes time out",
        },
        {
            "learning_type": "pattern",
            "title": "camera-api pods in the plane namespace restart",
            "description": "camera-api pods in the plane namespace",
            "applies_when": "camera-api in the plane namespace exits 255",
        },
    ]}

    class Resp:
        def json(self):
            return {"message": {"content": json.dumps(body)}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("requests.post", lambda *a, **k: Resp())
        op._analyze_correlations([{"severity": "warning", "finding": "load failed"}], [])

    assert [row["title"] for row in stored] == [
        "ollama on ubuntu-llm-01 times out loading a model"
    ]
    assert stored[0]["learning_type"] == "pattern"
    assert stored[0]["tags"] == ["ollama", "correlation", "automated"]


# --- CFOP-568 ---------------------------------------------------------------

# The four cfoperator Deployments as the 2026-10-29 02:17 drift event listed
# them: every service both removed and added, because a deploy rolled them.
_OLD_PODS = ["cfoperator-5fbfb75fb8-sv7c7", "cfoperator-bridge-bbdf657b6-zqr6j",
             "cfoperator-event-runtime-9c9869c7-q4mzt", "cfoperator-mcp-784554c5b7-h2xkd"]
_NEW_PODS = ["cfoperator-78749f5fcb-wwv24", "cfoperator-bridge-7b495cb4fb-wh2qj",
             "cfoperator-event-runtime-6b8cfcd775-2j2xc", "cfoperator-mcp-749d47bbf-kmgnz"]


def _change(seconds, added=(), removed=()):
    return SimpleNamespace(detected_at=_at(seconds), drift_type="container_change",
                           drift_details={"host": "apps", "added": list(added),
                                          "removed": list(removed)})


def test_a_rollout_is_not_a_failure():
    # Two deploys a couple of minutes apart, twice: before CFOP-568 this made
    # every pair of the four a "cascade_failure" and the pass wrote ~900
    # learnings about it. Counting `added`/`removed` again makes it non-empty.
    assert drift_failed_services("container_change",
                                 {"added": _NEW_PODS, "removed": _OLD_PODS}) == set()
    events = [
        _change(0, added=_NEW_PODS, removed=_OLD_PODS),
        _change(120, added=_OLD_PODS, removed=_NEW_PODS),
        _change(3600, added=_NEW_PODS, removed=_OLD_PODS),
        _change(3720, added=_OLD_PODS, removed=_NEW_PODS),
    ]
    assert service_failure_patterns(events) == []


def test_services_that_go_away_together_are_a_pattern():
    events = [
        _change(0, removed=["immich-server-868f4dd875-p7kcc"]),
        _change(60, removed=["immich-kiosk-6d5f7c9b8d-x2x9z"]),
        _change(3600, removed=["immich-server-868f4dd875-p7kcc"]),
        _change(3660, removed=["immich-kiosk-6d5f7c9b8d-x2x9z"]),
    ]
    assert service_failure_patterns(events) == [{
        "service_a": "immich-kiosk",
        "service_b": "immich-server",
        "co_failure_count": 2,
        "avg_time_delta_seconds": 60.0,
        "correlation_type": "cascade_failure",
    }]


def test_an_appearance_or_a_cronjob_run_is_not_a_failure():
    assert drift_failed_services("container_change", {
        "added": ["searxng-c55b97cbb-ccnj6"],
        "removed": ["freshet-alerter-29676615-6slrn"],
    }) == set()
    # Rows of the drift types nothing writes any more are still read.
    assert drift_failed_services("container_stopped", {"container": "loki-0"}) == {"loki-0"}


class _CorrelationSession:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        assert model is ServiceCorrelation
        return SimpleNamespace(all=lambda: list(self.rows))

    def delete(self, row):
        self.rows.remove(row)

    def add(self, row):
        self.rows.append(row)


def _kb_over(session):
    kb = KnowledgeBase.__new__(KnowledgeBase)

    @contextmanager
    def scope():
        yield session

    kb.session_scope = scope
    return kb


def test_the_table_holds_the_window_not_the_sweep_count():
    session = _CorrelationSession([
        ServiceCorrelation(service_a="cfoperator", service_b="cfoperator-mcp",
                           correlation_type="cascade_failure", occurrence_count=246,
                           avg_time_delta_seconds=272.0, correlation_details={}),
        ServiceCorrelation(service_a="immich-kiosk", service_b="immich-server",
                           correlation_type="co_failure", occurrence_count=9,
                           avg_time_delta_seconds=4.0, correlation_details={}),
    ])
    kb = _kb_over(session)
    window = [{"service_a": "immich-kiosk", "service_b": "immich-server",
               "co_failure_count": 2, "avg_time_delta_seconds": 60.0,
               "correlation_type": "cascade_failure"},
              {"service_a": "loki-0", "service_b": "plane-api",
               "co_failure_count": 3, "avg_time_delta_seconds": 12.0,
               "correlation_type": "co_failure"}]

    # Every sweep reads the same 30 days. Incrementing made the count the
    # number of sweeps; a second sync must leave it where the window put it.
    assert kb.sync_service_correlations(window) == {"updated": 1, "added": 1, "deleted": 1}
    assert kb.sync_service_correlations(window) == {"updated": 2, "added": 0, "deleted": 0}

    held = {(r.service_a, r.service_b): r for r in session.rows}
    assert set(held) == {("immich-kiosk", "immich-server"), ("loki-0", "plane-api")}
    assert held[("immich-kiosk", "immich-server")].occurrence_count == 2
    assert held[("immich-kiosk", "immich-server")].correlation_type == "cascade_failure"
    assert held[("loki-0", "plane-api")].occurrence_count == 3

    # A window with nothing in it holds nothing.
    kb.sync_service_correlations([])
    assert session.rows == []


def test_the_same_services_in_new_words_are_refused():
    seen = {correlation_insight_subject({"services": ["cfoperator", "cfoperator-mcp"]})}
    reworded = {
        "learning_type": "insight",
        "title": "High-frequency cascading failure from cfoperator to cfoperator-mcp",
        "description": "the 21st phrasing",
        "applies_when": "cfoperator fails and cfoperator-mcp follows within 5 min",
        # Case, order and a pod hash do not make it a new subject.
        "services": ["CFOperator-MCP", "cfoperator-78749f5fcb-wwv24"],
    }
    reason = correlation_insight_rejection(reworded, seen_subjects=seen)
    assert reason is not None and "already exists" in reason

    elsewhere = dict(reworded, services=["immich-kiosk", "immich-server"])
    assert correlation_insight_rejection(elsewhere, seen_subjects=seen) is None
    # No services named: nothing to key on, so only the title dedupe applies.
    assert correlation_insight_rejection(dict(reworded, services=[]), seen_subjects=seen) is None


def test_a_deprecated_subject_still_counts():
    """An operator who deprecated ~900 of these meant it. A subject read only
    from live rows would be written straight back the next sweep."""
    calls = []

    class Session:
        def query(self, column):
            assert column is InvestigationLearning.services
            return self

        def filter(self, *clauses):
            calls.extend(str(c) for c in clauses)
            return self

        def all(self):
            return [(["cfoperator", "cfoperator-mcp"],), (None,), (["Loki-0"],)]

    kb = _kb_over(Session())
    assert kb.correlation_learning_subjects() == {
        frozenset({"cfoperator", "cfoperator-mcp"}), frozenset({"loki-0"}),
    }
    assert calls and not any("deprecated" in c for c in calls)


def test_analyze_correlations_writes_one_learning_per_subject():
    op = CFOperator.__new__(CFOperator)
    op.config = {
        "llm": {"primary": {"url": "http://ollama:11434", "model": "gemma4:26b"}},
        "notifications": {},
    }
    op.llm_timeout = 5
    op.notifications = []
    op.tools = SimpleNamespace(k8s_tools=None)
    stored = []

    class KB:
        def get_setting(self, key, default=""):
            return {"selected_backend": "ollama",
                    "ollama_selected_model": "gemma4:26b"}.get(key, default)

        def get_operational_summary(self, hours=24):
            return {"sweeps": {}, "investigations": {"total": 0}, "learnings": {}}

        def find_learnings(self, **kwargs):
            return []

        def store_learning(self, insight):
            stored.append(dict(insight))
            return 7

        _kb = SimpleNamespace(
            find_correlated_events=lambda **k: [],
            get_service_correlations=lambda **k: [],
            # Only on deprecated rows by now; it must still hold.
            correlation_learning_subjects=lambda: {frozenset({"cfoperator", "cfoperator-mcp"})},
        )

    op.kb = KB()
    op._embed_learning = MagicMock()
    body = {"insights": [
        {"learning_type": "pattern",
         "title": "Cascading failure from cfoperator to cfoperator-mcp",
         "description": "cfoperator fails, then cfoperator-mcp",
         "applies_when": "cfoperator and cfoperator-mcp fail within 5 min",
         "services": ["cfoperator", "cfoperator-mcp"]},
        {"learning_type": "pattern",
         "title": "immich-kiosk times out after immich-server goes away",
         "description": "the kiosk's album calls fail while the server is gone",
         "applies_when": "immich-server is gone and immich-kiosk logs timeouts",
         "services": ["immich-server", "immich-kiosk"]},
        {"learning_type": "insight",
         "title": "Kiosk album API timeouts follow immich-server outages",
         "description": "the same pair, reworded in the same response",
         "applies_when": "immich-kiosk album requests time out",
         "services": ["immich-kiosk", "immich-server"]},
    ]}

    class Resp:
        def json(self):
            return {"message": {"content": json.dumps(body)}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("requests.post", lambda *a, **k: Resp())
        op._analyze_correlations([{"severity": "warning", "finding": "kiosk timeouts"}], [])

    assert [row["title"] for row in stored] == [
        "immich-kiosk times out after immich-server goes away"
    ]
