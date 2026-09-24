"""Davis problems as event runtime alerts (CFOP-204).

``P_26091`` is a row as a live tenant returned it on 2026-09-24 (a crash-looping
deployment on the itx dev cluster), with the description shortened.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from event_runtime.escalation import EscalationLedger
from event_runtime.external_plugins import PluginContext, load_external_plugins
from event_runtime.models import AlertSeverity
from event_runtime.plugin_manager import PluginManager
from integrations.dynatrace import grail
from integrations.dynatrace.grail import GrailQueryError, GrailResult
from integrations.dynatrace.problems import DynatraceProblemSource, lookback_seconds

P_26091 = {
    "event.id": "-6597882896083206449_1790268660000V2",
    "display_id": "P-26091",
    "event.name": "Backoff event",
    "event.status": "ACTIVE",
    "event.category": "ERROR",
    "event.start": "2026-09-24T16:51:00.000000000Z",
    "event.description": "# Backoff event\nFollowing ...",
    "affected_entity_ids": ["CLOUD_APPLICATION-E5476E922805A03C"],
    "affected_entity_names": ["crashloop"],
    "affected_entity_types": ["dt.entity.cloud_application"],
    "root_cause_entity_id": None,
    "root_cause_entity_name": None,
    "k8s.cluster.name": ["eks-k8s-2026-09-24"],
    "k8s.namespace.name": ["dt-chaos"],
    "k8s.workload.kind": ["deployment"],
    "k8s.workload.name": ["crashloop"],
    "k8s.pod.name": None,
    "host.name": None,
    "dt.davis.is_duplicate": False,
    "dt.davis.mute.status": "NOT_MUTED",
    "maintenance.is_under_maintenance": False,
}
FP = "dynatrace:-6597882896083206449_1790268660000V2"


def row(**changes):
    """P_26091 with some fields changed; keys use underscores for dots."""
    updated = dict(P_26091)
    for key, value in changes.items():
        updated[key.replace("__", ".")] = value
    return updated


class FakeClient:
    """Answers each query with the next scripted records list (or raises)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.queries = []

    def query(self, dql, **kwargs):
        self.queries.append(dql)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, GrailResult) else GrailResult(records=answer)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def source(*answers, ledger=None, poll_seconds=60, lookback="7d"):
    clock = Clock()
    client = FakeClient(*answers)
    src = DynatraceProblemSource(client, escalation_ledger=ledger, poll_seconds=poll_seconds,
                                 lookback=lookback, clock=clock)
    return src, client, clock


def poll(src, clock, advance=60):
    alerts = list(src.poll())
    clock.now += advance
    return alerts


def test_a_new_active_problem_becomes_one_alert_the_agent_can_act_on():
    src, client, clock = source([P_26091])
    (alert,) = poll(src, clock)

    assert alert.source == "dynatrace"
    assert alert.fingerprint == FP
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.summary == "Dynatrace P-26091: Backoff event on dt-chaos/crashloop"
    assert (alert.namespace, alert.resource_type, alert.resource_name) == ("dt-chaos", "deployment", "crashloop")
    assert alert.occurred_at == datetime(2026, 9, 24, 16, 51, tzinfo=timezone.utc)
    assert alert.details["alertname"] == "Backoff event"
    assert alert.details["dynatrace"]["display_id"] == "P-26091"
    assert alert.details["dynatrace"]["affected_entities"] == [
        {"id": "CLOUD_APPLICATION-E5476E922805A03C", "name": "crashloop", "type": "dt.entity.cloud_application"}
    ]
    assert alert.details["labels"] == {
        "cluster": "eks-k8s-2026-09-24", "namespace": "dt-chaos",
        "workload": "crashloop", "workload_kind": "deployment",
    }
    assert "fetch dt.davis.problems, from:-7d" in client.queries[0]


def test_what_an_investigation_needs_survives_the_agents_prompt_cut():
    """The agent shows the model json.dumps(alert)[:1000] (agent.run_investigation).

    A big problem -- many affected entities, a long description -- must not
    push the title, the display id or where it is out of that window.
    """
    import json

    many = [f"CLOUD_APPLICATION-{i:016X}" for i in range(40)]
    big = row(**{
        "affected_entity_ids": many,
        "affected_entity_names": [f"workload-{i}" for i in range(40)],
        "affected_entity_types": ["dt.entity.cloud_application"] * 40,
        "event.description": "x" * 5000,
    })
    src, _, clock = source([big])
    (alert,) = poll(src, clock)
    visible = json.dumps(alert.to_dict(), default=str)[:1000]
    for needed in ('"alertname": "Backoff event"', '"namespace": "dt-chaos"', '"workload": "crashloop"',
                   '"workload_kind": "deployment"', '"display_id": "P-26091"', "Dynatrace P-26091"):
        assert needed in visible, f"{needed} fell outside the first 1000 characters"
    assert alert.details["dynatrace"]["affected_entity_count"] == 40
    assert len(alert.details["dynatrace"]["affected_entities"]) == 10


def test_without_a_filter_the_query_is_unscoped():
    src, client, clock = source([P_26091])
    poll(src, clock)
    assert "| filter" not in client.queries[0]


def test_a_filter_scopes_the_query_before_anything_else_runs_on_it():
    clock = Clock()
    client = FakeClient([P_26091])
    src = DynatraceProblemSource(client, clock=clock, problem_filter='in("dev", k8s.cluster.name) or x == 1')
    poll(src, clock)
    query = client.queries[0]
    assert query.index('| filter (in("dev", k8s.cluster.name) or x == 1)\n') < query.index("| sort timestamp desc")


def test_a_rejected_query_is_an_error_that_names_the_filter(caplog):
    ledger = EscalationLedger()
    clock = Clock()
    client = FakeClient([P_26091], GrailQueryError("HTTP 400 PARSE_ERROR: `|` isn't allowed here", status=400))
    src = DynatraceProblemSource(client, clock=clock, escalation_ledger=ledger, problem_filter="in(")
    poll(src, clock)
    ledger.mark(FP)
    with caplog.at_level("WARNING"):
        assert poll(src, clock) == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and "CFOP_DYNATRACE_PROBLEM_FILTER='in('" in errors[0].getMessage()
    assert ledger.take(FP) is True             # the open problem was kept, not resolved


def test_other_failures_stay_warnings(caplog):
    clock = Clock()
    src = DynatraceProblemSource(FakeClient(GrailQueryError("cannot reach", status=None)), clock=clock,
                                 problem_filter='in("dev", k8s.cluster.name)')
    with caplog.at_level("WARNING"):
        poll(src, clock)
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_a_retitled_problem_does_not_alert_again():
    src, _, clock = source([P_26091], [row(event__name="Multiple Kubernetes problems")])
    assert len(poll(src, clock)) == 1
    assert poll(src, clock) == []


def test_a_quiet_problem_that_leaves_the_window_is_not_resolved():
    """Absence is not a clear: no 'Resolved:' and no second alert when it reappears."""
    ledger = EscalationLedger()
    src, _, clock = source([P_26091], [], [P_26091], ledger=ledger)
    poll(src, clock)
    ledger.mark(FP)
    assert poll(src, clock) == []          # gone from the result
    assert poll(src, clock) == []          # back, still ACTIVE
    assert ledger.take(FP) is True         # the escalation was never consumed


def test_a_closed_escalated_problem_gets_one_resolution():
    ledger = EscalationLedger()
    src, _, clock = source([P_26091], [row(event__status="CLOSED")], [row(event__status="CLOSED")], ledger=ledger)
    poll(src, clock)
    ledger.mark(FP)
    (resolved,) = poll(src, clock)
    assert resolved.summary == "Resolved: Dynatrace P-26091: Backoff event on dt-chaos/crashloop"
    assert resolved.fingerprint == FP + ":resolved"
    assert resolved.severity is AlertSeverity.INFO
    assert resolved.details["resolution"] is True
    assert poll(src, clock) == []          # once


def test_a_closed_problem_that_never_escalated_resolves_silently():
    src, _, clock = source([P_26091], [row(event__status="CLOSED")], [P_26091], ledger=EscalationLedger())
    poll(src, clock)
    assert poll(src, clock) == []
    assert len(poll(src, clock)) == 1      # closed then reopened under the same id: a new episode


@pytest.mark.parametrize("noise", [
    {"dt__davis__is_duplicate": True},
    {"dt__davis__mute__status": "MUTED"},
    {"maintenance__is_under_maintenance": True},
])
def test_problems_davis_says_to_ignore_do_not_alert_until_they_stop_being_noise(noise):
    src, _, clock = source([row(**noise)], [P_26091])
    assert poll(src, clock) == []
    assert len(poll(src, clock)) == 1


def test_a_failed_poll_keeps_open_problems_and_backs_off():
    ledger = EscalationLedger()
    src, client, clock = source([P_26091], GrailQueryError("HTTP 503: down"), [P_26091], ledger=ledger)
    poll(src, clock)
    ledger.mark(FP)
    assert poll(src, clock, advance=0) == []          # failure: nothing emitted
    assert poll(src, clock, advance=60) == []         # inside the backoff: not even queried
    assert len(client.queries) == 2
    clock.now += 60
    assert poll(src, clock) == []                     # recovered: still open, no re-alert
    assert ledger.take(FP) is True


def test_polls_are_throttled_to_the_configured_interval():
    src, client, clock = source([], [], poll_seconds=120)
    poll(src, clock, advance=60)
    poll(src, clock, advance=60)
    assert len(client.queries) == 1
    poll(src, clock)
    assert len(client.queries) == 2


def test_a_problem_out_of_every_result_past_the_lookback_is_forgotten():
    src, _, clock = source([P_26091], [], [P_26091], lookback="1h")
    poll(src, clock, advance=3601)
    poll(src, clock)                       # absent for longer than the lookback: dropped
    assert len(poll(src, clock)) == 1      # so its next ACTIVE row is new again


def test_a_limited_result_is_logged_not_hidden(caplog):
    limited = GrailResult(records=[P_26091], metadata={"grail": {"notifications": [
        {"message": "Your result has been limited to 1000.", "notificationType": "API_RECORDS_LIMIT_ADDED"}]}})
    src, _, clock = source(limited)
    with caplog.at_level("WARNING"):
        poll(src, clock)
    assert "Your result has been limited to 1000." in caplog.text


@pytest.mark.parametrize("category,severity", [
    ("AVAILABILITY", AlertSeverity.CRITICAL),
    ("SLOWDOWN", AlertSeverity.WARNING),
    ("INFO", AlertSeverity.INFO),
    ("SOMETHING_NEW", AlertSeverity.WARNING),
    (None, AlertSeverity.WARNING),
])
def test_category_maps_to_severity(category, severity):
    src, _, clock = source([row(event__category=category)])
    assert poll(src, clock)[0].severity is severity


def test_a_host_problem_is_named_by_its_host():
    host_row = row(k8s__namespace__name=None, k8s__workload__name=None, k8s__workload__kind=None,
                   **{"host.name": ["ubuntu-itx-01"], "event.name": "CPU saturation"})
    src, _, clock = source([host_row])
    (alert,) = poll(src, clock)
    assert (alert.resource_type, alert.resource_name) == ("host", "ubuntu-itx-01")
    assert alert.summary == "Dynatrace P-26091: CPU saturation on ubuntu-itx-01"
    assert alert.details["host"] == "ubuntu-itx-01"


@pytest.mark.parametrize("value", ["", "7", "7w", "0d", "-1h"])
def test_a_malformed_lookback_is_refused(value):
    with pytest.raises(ValueError, match="lookback must look like"):
        lookback_seconds(value)


# --- register(), through the real loader ------------------------------------

def _load(monkeypatch, **env):
    for key in ("DT_ENVIRONMENT_URL", "DT_PLATFORM_TOKEN", "CFOP_DYNATRACE_POLL_SECONDS", "CFOP_DYNATRACE_LOOKBACK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    plugins, ledger = PluginManager(), EscalationLedger()
    load_external_plugins(plugins, PluginContext(escalation_ledger=ledger), raw="integrations.dynatrace")
    return plugins, ledger


def test_register_needs_both_url_and_token(monkeypatch):
    with pytest.raises(Exception, match="needs DT_ENVIRONMENT_URL and DT_PLATFORM_TOKEN"):
        _load(monkeypatch)
    with pytest.raises(Exception, match="needs DT_PLATFORM_TOKEN"):
        _load(monkeypatch, DT_ENVIRONMENT_URL="https://abc12345.apps.dynatrace.com")


@pytest.mark.parametrize("env,message", [
    ({"CFOP_DYNATRACE_POLL_SECONDS": "5"}, "must be a number of seconds >= 10"),
    ({"CFOP_DYNATRACE_POLL_SECONDS": "often"}, "must be a number of seconds >= 10"),
    ({"CFOP_DYNATRACE_POLL_SECONDS": "nan"}, "must be a number of seconds >= 10"),
    ({"CFOP_DYNATRACE_POLL_SECONDS": "inf"}, "must be a number of seconds >= 10"),
    ({"CFOP_DYNATRACE_POLL_SECONDS": "-inf"}, "must be a number of seconds >= 10"),
    ({"CFOP_DYNATRACE_LOOKBACK": "a week"}, "lookback must look like"),
    ({"DT_ENVIRONMENT_URL": "https://abc12345.live.dynatrace.com"}, "platform host"),
])
def test_register_refuses_bad_settings_at_startup(monkeypatch, env, message):
    base = {"DT_ENVIRONMENT_URL": "https://abc12345.apps.dynatrace.com", "DT_PLATFORM_TOKEN": "dt0s16.X.Y"}
    with pytest.raises(Exception, match=message):
        _load(monkeypatch, **{**base, **env})


@pytest.mark.parametrize("raw,expected", [
    ('in("eks-k8s-2026-09-24", k8s.cluster.name)', '| filter (in("eks-k8s-2026-09-24", k8s.cluster.name))'),
    ("   ", None),
])
def test_register_reads_the_problem_filter(monkeypatch, raw, expected):
    monkeypatch.setenv("CFOP_DYNATRACE_PROBLEM_FILTER", raw)
    plugins, _ = _load(monkeypatch, DT_ENVIRONMENT_URL="https://abc12345.apps.dynatrace.com",
                       DT_PLATFORM_TOKEN="dt0s16.X.Y")
    query = plugins.alert_sources[0]._query
    if expected:
        assert expected in query
    else:
        assert "| filter" not in query


def test_register_adds_the_problem_source_with_the_runtimes_ledger(monkeypatch):
    plugins, ledger = _load(monkeypatch, DT_ENVIRONMENT_URL="https://abc12345.apps.dynatrace.com",
                            DT_PLATFORM_TOKEN="dt0s16.X.Y")
    (src,) = plugins.alert_sources
    assert isinstance(src, DynatraceProblemSource)
    assert src._escalation_ledger is ledger


def test_the_plugin_feeds_the_real_runtime_end_to_end(monkeypatch, tmp_path):
    """CFOP_EVENT_RUNTIME_PLUGINS=integrations.dynatrace through build_portable_runtime."""
    from event_runtime.bootstrap import build_portable_runtime

    monkeypatch.setattr(grail.GrailClient, "query", lambda self, dql, **kw: GrailResult(records=[P_26091]))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    for key in ("CFOP_EVENT_RUNTIME_PG_DSN", "CFOP_AGENT_URL", "CFOP_EVENT_RUNTIME_ALERTMANAGER_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_PLUGINS", "integrations.dynatrace")
    monkeypatch.setenv("DT_ENVIRONMENT_URL", "https://abc12345.apps.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "dt0s16.X.Y")

    runtime = build_portable_runtime()
    assert "dynatrace" in runtime.health()["sources"]
    assert len(runtime.poll_sources()) == 1
    (received,) = runtime.recent_events(limit=10, event_type="alert_received")
    assert received["payload"]["alert"]["summary"] == "Dynatrace P-26091: Backoff event on dt-chaos/crashloop"
    assert received["payload"]["alert"]["fingerprint"] == FP
