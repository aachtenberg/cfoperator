"""CFOP-149: the correlation pass must not write the learnings it confabulated.

Investigation timestamps are not causation, co-occurrence is not a root
cause, a namespace the cluster does not have is refutable at write time,
and the 18th copy of the same sentence is not a new observation.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator
from knowledge_base import correlate_events, correlation_insight_rejection


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
    assert "automated" in stored[0]["tags"]
