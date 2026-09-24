"""DQL evidence for Dynatrace alerts (CFOP-205).

Alerts are made by the real ``DynatraceProblemSource`` from the live P-26091
row, so a change to the details the source emits breaks these tests instead of
silently starving the provider. Query answers are trimmed from the live tenant
(2026-09-24).
"""

from __future__ import annotations

import json

import pytest

from cfshared.evidence import BLOCK_LIMIT, EVIDENCE_KEY, collect
from event_runtime.escalation import EscalationLedger
from event_runtime.external_plugins import PluginContext, load_external_plugins
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope
from event_runtime.plugin_manager import PluginManager
from integrations.dynatrace import grail
from integrations.dynatrace.evidence import DynatraceEvidenceProvider, dql_string
from integrations.dynatrace.grail import GrailQueryError, GrailResult
from integrations.dynatrace.problems import DynatraceProblemSource
from integrations.dynatrace.test_problems import P_26091, row

EVENT_IDS = ["5800326029497036406_1790275140000", "-2519149835338629331_1790276580000"]
DAVIS_EVENTS = [
    {"timestamp": "2026-09-24T19:33:42.369000000Z", "event.status": "ACTIVE", "event.name": "No pod ready",
     "event.description": "Workload does not have any ready pods."},
    {"timestamp": "2026-09-24T19:03:01.000000000Z", "event.status": "CLOSED", "event.name": "Backoff event",
     "event.description": "Events with reason 'BackOff' have been detected for pods of this workload."},
]
LOGS = [{"content": "cfop-201: deliberate crash for Davis", "loglevel": "NONE", "n": "5",
         "last": "2026-09-24T19:30:36.890261000Z"}]
RESTARTS = [{"k8s.workload.name": "crashloop", "r": [2.0, 2.0, 1.0, None, 4.0], "total": 23.0}]
HOST_CPU = [{"peak": 4.6384538014729815, "last": 4.408118057250976, "cpu": [4.53, 4.5, None]}]


class ScriptedGrail:
    """Answers each query by the first marker it contains; records every query."""

    def __init__(self, **answers):
        self.answers = {
            "fetch dt.davis.problems": answers.get("problem", [{"dt.davis.event_ids": EVENT_IDS}]),
            "fetch dt.davis.events": answers.get("events", DAVIS_EVENTS),
            "fetch logs": answers.get("logs", LOGS),
            "dt.kubernetes.container.restarts": answers.get("restarts", RESTARTS),
            "dt.host.cpu.usage": answers.get("cpu", HOST_CPU),
        }
        self.queries = []

    def query(self, dql, **kwargs):
        self.queries.append(dql)
        for marker, answer in self.answers.items():
            if marker in dql:
                if isinstance(answer, Exception):
                    raise answer
                return GrailResult(records=answer)
        raise AssertionError(f"unexpected query: {dql}")


class Ticks:
    """A clock that moves forward a fixed step each time it is read."""

    def __init__(self, step=0.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def alert_from(problem_row):
    """The alert exactly as DynatraceProblemSource emits it for this row."""
    class _Once:
        def query(self, dql, **kw):
            return GrailResult(records=[problem_row])
    (alert,) = DynatraceProblemSource(_Once()).poll()
    return alert


def gather(alert, client=None, clock=None, **kw):
    client = client or ScriptedGrail()
    provider = DynatraceEvidenceProvider(client, clock=clock or Ticks(), **kw)
    envelope = provider.provide(alert, ContextEnvelope(alert=alert))
    return envelope, client


def text_of(envelope):
    return envelope.context[EVIDENCE_KEY]["dynatrace"]


def test_a_workload_problem_gets_its_events_logs_and_restarts():
    envelope, _ = gather(alert_from(P_26091))
    text = text_of(envelope)
    assert text.startswith("Dynatrace's view of P-26091")
    assert "- 19:33:42 ACTIVE No pod ready: Workload does not have any ready pods." in text
    assert "- x5, last 19:30:36 cfop-201: deliberate crash for Davis" in text   # NONE level not shown
    assert "- 23 in total; per 10 minutes, oldest first: 2 2 1 - 4" in text
    # arrives whole through the core contract
    assert collect(envelope.context) == {"dynatrace": text} and len(text) <= BLOCK_LIMIT


def test_the_queries_are_the_shapes_checked_on_the_tenant():
    _, client = gather(alert_from(P_26091))
    problem_q, events_q, logs_q, restarts_q = client.queries
    assert 'filter event.id == "-6597882896083206449_1790268660000V2"' in problem_q
    assert f'in(event.id, array("{EVENT_IDS[0]}", "{EVENT_IDS[1]}"))' in events_q
    assert 'k8s.namespace.name == "dt-chaos" and k8s.workload.name == "crashloop"' in logs_q
    assert "summarize n = count(), last = max(timestamp), by:{content, loglevel}" in logs_q
    assert "sum(dt.kubernetes.container.restarts)" in restarts_q and "interval:10m" in restarts_q


def test_values_are_escaped_into_dql():
    assert dql_string('we"ird\\name') == '"we\\"ird\\\\name"'
    _, client = gather(alert_from(row(**{"k8s.workload.name": ['we"ird\\name']})))
    assert 'k8s.workload.name == "we\\"ird\\\\name"' in client.queries[2]


def test_a_failed_query_is_said_not_hidden():
    envelope, _ = gather(alert_from(P_26091), ScriptedGrail(logs=GrailQueryError("HTTP 400 DQL-SYNTAX-ERROR: nope")))
    text = text_of(envelope)
    assert "(query failed: HTTP 400 DQL-SYNTAX-ERROR: nope)" in text
    assert "No pod ready" in text and "23 in total" in text      # the others still arrive


def test_an_empty_result_says_none_rather_than_nothing():
    envelope, _ = gather(alert_from(P_26091), ScriptedGrail(logs=[], events=[]))
    text = text_of(envelope)
    assert "newest first:\n(none)" in text


def test_a_problem_without_event_ids_does_not_query_events():
    envelope, client = gather(alert_from(P_26091), ScriptedGrail(problem=[{"dt.davis.event_ids": None}]))
    assert not any("fetch dt.davis.events" in q for q in client.queries)
    assert "Davis events in this problem, newest first:\n(none)" in text_of(envelope)


def test_the_time_budget_skips_what_is_left():
    # Each clock read advances 15s against a 20s budget: the first section runs, the rest are skipped.
    envelope, client = gather(alert_from(P_26091), clock=Ticks(step=15.0), budget_seconds=20)
    text = text_of(envelope)
    assert text.count("(skipped: the 20s evidence budget was used up)") == 2
    assert all("fetch logs" not in q for q in client.queries)


def test_a_host_problem_gets_filtered_logs_and_cpu_instead():
    host_row = row(k8s__namespace__name=None, k8s__workload__name=None, k8s__workload__kind=None,
                   **{"host.name": ["ubuntu-itx-01"]})
    envelope, client = gather(alert_from(host_row))
    text = text_of(envelope)
    logs_q = next(q for q in client.queries if "fetch logs" in q)
    assert 'host.name == "ubuntu-itx-01" and not in(loglevel, array("INFO", "DEBUG", "TRACE", "NONE"))' in logs_q
    assert "- CPU %: peak 4.6, latest 4.4; per 10 minutes, oldest first: 4.5 4.5 -" in text
    assert not any("restarts" in q for q in client.queries)


@pytest.mark.parametrize("alert", [
    Alert(source="alertmanager", severity=AlertSeverity.WARNING, summary="KubePodCrashLooping"),
    # another source carrying a Dynatrace-shaped id is still not ours to query for
    Alert(source="alertmanager", severity=AlertSeverity.WARNING, summary="relabelled",
          details={"dynatrace": {"problem_id": "x"}}),
    Alert(source="dynatrace", severity=AlertSeverity.INFO, summary="Resolved: Dynatrace P-1",
          details={"resolution": True, "dynatrace": {"problem_id": "x"}}),
    Alert(source="dynatrace", severity=AlertSeverity.WARNING, summary="no problem id", details={}),
])
def test_other_alerts_are_left_alone(alert):
    envelope, client = gather(alert)
    assert envelope.context == {} and client.queries == []


# --- through register() and the real runtime -----------------------------------

BASE_ENV = {"DT_ENVIRONMENT_URL": "https://abc12345.apps.dynatrace.com", "DT_PLATFORM_TOKEN": "dt0s16.X.Y"}


def _load(monkeypatch, **env):
    for key in ("CFOP_DYNATRACE_EVIDENCE", "CFOP_DYNATRACE_POLL_SECONDS", "CFOP_DYNATRACE_LOOKBACK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    plugins = PluginManager()
    load_external_plugins(plugins, PluginContext(escalation_ledger=EscalationLedger()), raw="integrations.dynatrace")
    return plugins


def test_register_adds_the_evidence_provider_on_its_own_short_timeout(monkeypatch):
    (provider,) = _load(monkeypatch).context_providers
    assert isinstance(provider, DynatraceEvidenceProvider)
    assert provider._client.timeout == 10


@pytest.mark.parametrize("off", ["0", "false", "off", "no"])
def test_evidence_can_be_switched_off_without_losing_the_problem_source(monkeypatch, off):
    plugins = _load(monkeypatch, CFOP_DYNATRACE_EVIDENCE=off)
    assert plugins.context_providers == [] and len(plugins.alert_sources) == 1


def test_the_evidence_reaches_the_investigate_request_end_to_end(monkeypatch, tmp_path):
    """Problem -> alert -> evidence -> the body POSTed to the agent, through build_portable_runtime."""
    from event_runtime.bootstrap import build_portable_runtime
    from event_runtime.http_actions import HTTPInvestigateActionHandler, HTTPTriageDecisionEngine

    scripted = ScriptedGrail()

    def fake_query(self, dql, **kw):
        if "fields event.id, display_id" in dql:           # the problem source's poll
            return GrailResult(records=[P_26091])
        return scripted.query(dql)

    sent = []
    monkeypatch.setattr(grail.GrailClient, "query", fake_query)
    monkeypatch.setattr(HTTPTriageDecisionEngine, "_call_triage",
                        lambda self, alert: {"action": "investigate", "reason": "test", "confidence": 1.0})
    monkeypatch.setattr(HTTPInvestigateActionHandler, "_post", lambda self, endpoint, body: sent.append(json.loads(body)))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    for key in ("CFOP_EVENT_RUNTIME_PG_DSN", "CFOP_EVENT_RUNTIME_ALERTMANAGER_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CFOP_AGENT_URL", "http://agent.invalid:8083")
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_PLUGINS", "integrations.dynatrace")
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    runtime = build_portable_runtime()
    assert "dynatrace-evidence" in runtime.health()["context_providers"]
    runtime.poll_sources()

    (body,) = sent
    assert body["summary"] == "Dynatrace P-26091: Backoff event on dt-chaos/crashloop"
    evidence = body[EVIDENCE_KEY]["dynatrace"]
    assert "cfop-201: deliberate crash for Davis" in evidence and "No pod ready" in evidence
