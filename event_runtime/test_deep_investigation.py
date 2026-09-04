"""Tests for the deep-investigation tier: routing wrapper + Job handler."""

import json

from event_runtime.deep_investigation import (
    DEEP_INVESTIGATE_ACTION,
    DeepInvestigationActionHandler,
    DeepInvestigationConfig,
    EscalationRoutingDecisionEngine,
    JOB_FINGERPRINT_LABEL,
    JOB_ROLE_LABEL,
    JOB_ROLE_VALUE,
    _bounded_json,
)
from event_runtime.models import (
    ActionRequest,
    Alert,
    AlertSeverity,
    ContextEnvelope,
    Decision,
)
from event_runtime.plugins import DecisionEngine


# ---- helpers ---------------------------------------------------------------


def _host_alert(**overrides) -> Alert:
    payload = {
        "source": "alertmanager",
        "severity": "critical",
        "summary": "NodeUnreachable raspberrypi3",
        "resource_type": "node",
        "resource_name": "raspberrypi3",
        "details": {"labels": {"node": "raspberrypi3"}, "host": "raspberrypi3"},
    }
    payload.update(overrides)
    return Alert.from_dict(payload)


def _pod_alert(**overrides) -> Alert:
    payload = {
        "source": "alertmanager",
        "severity": "warning",
        "summary": "PodCrashLooping grafana",
        "resource_type": "pod",
        "resource_name": "grafana-0",
        "namespace": "monitoring",
        "details": {"labels": {"pod": "grafana-0", "namespace": "monitoring"}},
    }
    payload.update(overrides)
    return Alert.from_dict(payload)


class _StubEngine(DecisionEngine):
    name = "stub"

    def __init__(self, decision: Decision):
        self._decision = decision

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return self._decision


def _route(alert: Alert, decision: Decision, **kwargs) -> Decision:
    engine = EscalationRoutingDecisionEngine(_StubEngine(decision), **kwargs)
    return engine.decide(ContextEnvelope(alert=alert))


class _FakeKubectl:
    """Records kubectl invocations; returns canned responses per verb."""

    def __init__(self, active_jobs=None, create_rc=0, create_stderr="", list_rc=0, list_stderr=""):
        self.calls = []
        self.active_jobs = active_jobs or []
        self.create_rc = create_rc
        self.create_stderr = create_stderr
        self.list_rc = list_rc
        self.list_stderr = list_stderr

    def __call__(self, args, stdin):
        self.calls.append((list(args), stdin))
        if args[0] == "get":
            items = [
                {
                    "metadata": {"name": name, "labels": {JOB_FINGERPRINT_LABEL: fp}},
                    "status": {"active": 1},
                }
                for name, fp in self.active_jobs
            ]
            return self.list_rc, json.dumps({"items": items}), self.list_stderr
        if args[0] == "create":
            return self.create_rc, "", self.create_stderr
        raise AssertionError(f"unexpected kubectl verb: {args}")

    @property
    def created_manifest(self):
        for args, stdin in self.calls:
            if args[0] == "create":
                return json.loads(stdin)
        return None


def _handler(tmp_path, kubectl, **config_overrides) -> DeepInvestigationActionHandler:
    config = DeepInvestigationConfig(
        enabled=True,
        completion_base_url="http://event-runtime:8000",
        agent_url="http://agent:8083",
        budget_state_path=str(tmp_path / "budget.json"),
        # ssh_user required since CFOP-137 -- the handler refuses without it
        # rather than spending a budget slot on a worker that would render
        # "ssh @host". Overridable so the refusal itself can be tested.
        **{"ssh_user": "someoperator", **config_overrides},
    )
    return DeepInvestigationActionHandler(config, kubectl_runner=kubectl)


def _request(alert: Alert, decision: Decision | None = None) -> ActionRequest:
    decision = decision or Decision(action=DEEP_INVESTIGATE_ACTION, confidence=0.3, reasoning="test")
    return ActionRequest(alert=alert, decision=decision, context=ContextEnvelope(alert=alert))


# ---- routing: EscalationRoutingDecisionEngine ------------------------------


def test_host_shaped_escalate_reroutes_with_deep_context():
    decision = Decision(
        action="escalate", confidence=0.9, reasoning="node is gone",
        params={"triage_backend": "ollama", "triage_model": "qwen"},
    )
    routed = _route(_host_alert(), decision)
    assert routed.action == DEEP_INVESTIGATE_ACTION
    assert routed.params["routed_by"] == "escalation-routing"
    ctx = routed.params["deep_context"]
    assert ctx["original_action"] == "escalate"
    assert ctx["original_confidence"] == 0.9
    assert ctx["triage_reasoning"] == "node is gone"
    assert ctx["triage_backend"] == "ollama"
    assert "rerouted from escalate" in routed.reasoning


def test_host_shaped_low_confidence_investigate_reroutes():
    decision = Decision(action="investigate", confidence=0.2, reasoning="unsure")
    routed = _route(_host_alert(), decision)
    assert routed.action == DEEP_INVESTIGATE_ACTION
    assert routed.params["deep_context"]["original_action"] == "investigate"


def test_confidence_zero_triage_failure_does_not_reroute():
    # confidence == 0.0 means the triage call FAILED, not that triage was
    # unsure — don't burn deep-investigation budget on a transient outage.
    decision = Decision(action="investigate", confidence=0.0, reasoning="triage call failed")
    routed = _route(_host_alert(), decision)
    assert routed.action == "investigate"


def test_high_confidence_investigate_passes_through():
    decision = Decision(action="investigate", confidence=0.8, reasoning="clear case")
    routed = _route(_host_alert(), decision)
    assert routed.action == "investigate"
    assert "deep_context" not in routed.params


def test_confidence_at_threshold_passes_through():
    decision = Decision(action="investigate", confidence=0.4, reasoning="boundary")
    routed = _route(_host_alert(), decision, confidence_threshold=0.4)
    assert routed.action == "investigate"


def test_non_host_alert_decisions_untouched():
    decision = Decision(action="investigate", confidence=0.1, reasoning="pod thing")
    routed = _route(_pod_alert(), decision)
    assert routed.action == "investigate"
    assert "deep_context" not in routed.params


def test_non_host_escalate_falls_back_to_notify():
    decision = Decision(action="escalate", confidence=0.9, reasoning="workload escalate")
    routed = _route(_pod_alert(), decision)
    assert routed.action == "notify"
    assert routed.params["original_action"] == "escalate"
    assert routed.params["routed_by"] == "escalation-routing"


def test_route_escalate_can_be_disabled():
    decision = Decision(action="escalate", confidence=0.9, reasoning="x")
    routed = _route(_host_alert(), decision, route_escalate=False)
    assert routed.action == "escalate"


def test_route_low_confidence_can_be_disabled():
    decision = Decision(action="investigate", confidence=0.1, reasoning="x")
    routed = _route(_host_alert(), decision, route_low_confidence_investigate=False)
    assert routed.action == "investigate"


# ---- handler: Job manifest --------------------------------------------------


def test_handler_builds_job_manifest(tmp_path):
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl)
    alert = _host_alert()
    decision = Decision(
        action=DEEP_INVESTIGATE_ACTION, confidence=0.3, reasoning="r",
        params={"deep_context": {"original_action": "escalate"}, "deep_model": "claude-sonnet-4-6"},
    )

    result = handler.execute(_request(alert, decision))

    assert result.success is True
    assert result.quiet is True
    manifest = kubectl.created_manifest
    assert manifest is not None
    fp12 = alert.effective_fingerprint()[:12]
    assert manifest["metadata"]["name"].startswith(f"cfop-deep-{fp12}-")
    assert manifest["metadata"]["labels"][JOB_ROLE_LABEL] == JOB_ROLE_VALUE
    assert manifest["metadata"]["labels"][JOB_FINGERPRINT_LABEL] == fp12

    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 900
    assert spec["ttlSecondsAfterFinished"] == 21600
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "cfoperator-worker"
    assert pod["restartPolicy"] == "Never"

    env = {entry["name"]: entry for entry in pod["containers"][0]["env"]}
    # Secrets are injected by k8s, never inlined.
    assert "valueFrom" in env["ANTHROPIC_API_KEY"]
    assert "value" not in env["ANTHROPIC_API_KEY"]
    assert env["CFOP_COMPLETION_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "CFOP_COMPLETION_SHARED_SECRET"
    assert env["CFOP_COMPLETION_URL"]["value"] == (
        f"http://event-runtime:8000/v1/investigations/{alert.alert_id}/complete"
    )
    assert env["CFOP_TARGET_HOST"]["value"] == "raspberrypi3"
    assert env["CLAUDE_MODEL"]["value"] == "claude-sonnet-4-6"  # decision param wins
    assert json.loads(env["CFOP_ALERT_JSON"]["value"])["alert_id"] == alert.alert_id
    assert json.loads(env["CFOP_DEEP_CONTEXT_JSON"]["value"])["original_action"] == "escalate"

    assert result.details["job_name"] == manifest["metadata"]["name"]
    assert result.details["host"] == "raspberrypi3"


def test_handler_strips_port_from_instance_host(tmp_path):
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl)
    alert = _host_alert(
        resource_type="instance",
        resource_name="raspberrypi5:9100",
        details={"labels": {"instance": "raspberrypi5:9100"}},
    )
    result = handler.execute(_request(alert))
    assert result.details["host"] == "raspberrypi5"


def test_handler_fails_loudly_without_target_host(tmp_path):
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl)
    alert = _pod_alert()  # no host hint at all

    result = handler.execute(_request(alert))

    assert result.success is False
    assert result.quiet is False
    assert result.details["outcome"] == "failed"
    assert kubectl.created_manifest is None


# ---- handler: guards ---------------------------------------------------------


def test_handler_dedupes_active_fingerprint_job(tmp_path):
    alert = _host_alert()
    fp12 = alert.effective_fingerprint()[:12]
    kubectl = _FakeKubectl(active_jobs=[("cfop-deep-x", fp12)])
    handler = _handler(tmp_path, kubectl)

    result = handler.execute(_request(alert))

    assert result.success is True
    assert result.quiet is True
    assert result.details["outcome"] == "deduped"
    assert kubectl.created_manifest is None


def test_handler_defers_loudly_at_concurrency_cap(tmp_path):
    kubectl = _FakeKubectl(active_jobs=[("j1", "aaa"), ("j2", "bbb")])
    handler = _handler(tmp_path, kubectl, max_concurrent=2)

    result = handler.execute(_request(_host_alert()))

    assert result.success is True
    assert result.quiet is False  # operator must see the deferral
    assert result.details["outcome"] == "needs_action"
    assert result.details["deferred"] is True
    assert kubectl.created_manifest is None


def test_daily_budget_blocks_and_resets_next_day(tmp_path):
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl, daily_budget=1)

    first = handler.execute(_request(_host_alert()))
    assert first.quiet is True

    second = handler.execute(_request(_host_alert(summary="another node alert")))
    assert second.quiet is False
    assert second.details["outcome"] == "needs_action"
    assert "budget" in second.details["reason"]

    # Simulate the day rolling over: stored date no longer matches today.
    budget_path = tmp_path / "budget.json"
    state = json.loads(budget_path.read_text())
    state["date"] = "2000-01-01"
    budget_path.write_text(json.dumps(state))

    third = handler.execute(_request(_host_alert(summary="next day alert")))
    assert third.quiet is True


def test_kubectl_create_failure_is_loud_and_refunds_budget(tmp_path):
    kubectl = _FakeKubectl(create_rc=1, create_stderr="server unavailable")
    handler = _handler(tmp_path, kubectl, daily_budget=1)

    result = handler.execute(_request(_host_alert()))

    assert result.success is False
    assert result.quiet is False
    assert "server unavailable" in result.details["reason"]
    # Budget was refunded, so the next attempt still goes through.
    kubectl_ok = _FakeKubectl()
    handler_ok = _handler(tmp_path, kubectl_ok, daily_budget=1)
    retry = handler_ok.execute(_request(_host_alert()))
    assert retry.quiet is True


def test_job_listing_failure_is_loud(tmp_path):
    kubectl = _FakeKubectl(list_rc=1, list_stderr="forbidden")
    handler = _handler(tmp_path, kubectl)

    result = handler.execute(_request(_host_alert()))

    assert result.success is False
    assert "forbidden" in result.details["reason"]


# ---- env payload bounding ----------------------------------------------------


def test_bounded_json_passes_small_payloads_unchanged():
    payload = {"alert_id": "a", "details": {"host": "h"}}
    assert json.loads(_bounded_json(payload)) == payload


def test_bounded_json_drops_heavyweight_detail_keys():
    payload = {
        "alert_id": "a",
        "details": {
            "host": "raspberrypi3",
            "alertname": "NodeUnreachable",
            "labels": {"node": "raspberrypi3"},
            "annotations": {"blob": "x" * 40000},
        },
    }
    bounded = json.loads(_bounded_json(payload))
    assert len(json.dumps(bounded).encode()) <= 16 * 1024
    assert bounded["details"]["host"] == "raspberrypi3"
    assert bounded["details"]["_truncated"] is True
    assert "annotations" not in bounded["details"]


# ---- bootstrap gating ---------------------------------------------------------


def _build_runtime(monkeypatch, tmp_path, **env):
    from event_runtime.bootstrap import build_portable_runtime

    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    for key in (
        "CFOP_DEEP_INVESTIGATION_ENABLED",
        "CFOP_DEEP_COMPLETION_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return build_portable_runtime()


def test_bootstrap_registers_deep_handler_when_enabled(monkeypatch, tmp_path):
    runtime = _build_runtime(
        monkeypatch, tmp_path,
        CFOP_DEEP_INVESTIGATION_ENABLED="true",
        CFOP_DEEP_COMPLETION_BASE_URL="http://event-runtime:8000",
    )
    assert DEEP_INVESTIGATE_ACTION in runtime.plugins.action_handlers
    assert isinstance(runtime.plugins.decision_engine, EscalationRoutingDecisionEngine)


def test_bootstrap_skips_deep_handler_when_disabled(monkeypatch, tmp_path):
    runtime = _build_runtime(monkeypatch, tmp_path)
    assert DEEP_INVESTIGATE_ACTION not in runtime.plugins.action_handlers
    assert not isinstance(runtime.plugins.decision_engine, EscalationRoutingDecisionEngine)


def test_bootstrap_skips_deep_handler_without_completion_url(monkeypatch, tmp_path):
    runtime = _build_runtime(
        monkeypatch, tmp_path,
        CFOP_DEEP_INVESTIGATION_ENABLED="true",
    )
    assert DEEP_INVESTIGATE_ACTION not in runtime.plugins.action_handlers
    assert not isinstance(runtime.plugins.decision_engine, EscalationRoutingDecisionEngine)


# ---- routing: boot-forensics bypass ------------------------------------------


def _boot_alert(**overrides) -> Alert:
    payload = {
        "source": "boot-forensics",
        "severity": "warning",
        "summary": "Unclean reboot detected on raspberrypi5",
        "resource_type": "node",
        "resource_name": "raspberrypi5",
        "fingerprint": "boot-forensics-raspberrypi5-abc123",
        "details": {
            "boot_forensics": True,
            "host": "raspberrypi5",
            "labels": {"node": "raspberrypi5"},
            "previous_boot_id": "abc123",
            "detected_at": "2026-06-11T12:00:00+00:00",
        },
    }
    payload.update(overrides)
    return Alert.from_dict(payload)


class _ExplodingEngine(DecisionEngine):
    """Inner engine that must NOT be consulted for boot-forensics alerts."""

    name = "exploding"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        raise AssertionError("triage should be bypassed for boot-forensics alerts")


def test_boot_forensics_bypasses_triage_and_routes_deep():
    engine = EscalationRoutingDecisionEngine(_ExplodingEngine())
    routed = engine.decide(ContextEnvelope(alert=_boot_alert()))

    assert routed.action == DEEP_INVESTIGATE_ACTION
    assert routed.confidence == 1.0
    assert routed.params["deep_template"] == "boot-forensics"
    assert routed.params["routed_by"] == "escalation-routing"
    ctx = routed.params["deep_context"]
    assert ctx["original_action"] == "boot_forensics"
    assert ctx["previous_boot_id"] == "abc123"


def test_boot_forensics_route_can_be_disabled():
    decision = Decision(action="notify", confidence=0.9, reasoning="triaged")
    engine = EscalationRoutingDecisionEngine(_StubEngine(decision), route_boot_forensics=False)
    routed = engine.decide(ContextEnvelope(alert=_boot_alert()))
    assert routed.action == "notify"  # fell through to normal triage


def test_non_boot_alert_unaffected_by_bypass():
    decision = Decision(action="investigate", confidence=0.9, reasoning="normal")
    engine = EscalationRoutingDecisionEngine(_StubEngine(decision))
    routed = engine.decide(ContextEnvelope(alert=_host_alert()))
    assert routed.action == "investigate"


def test_boot_forensics_handler_uses_template_and_fingerprint(tmp_path):
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl)
    alert = _boot_alert()
    decision = Decision(
        action=DEEP_INVESTIGATE_ACTION, confidence=1.0, reasoning="boot",
        params={"deep_template": "boot-forensics", "deep_context": {"previous_boot_id": "abc123"}},
    )

    result = handler.execute(_request(alert, decision))

    manifest = kubectl.created_manifest
    env = {e["name"]: e for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CFOP_TEMPLATE"]["value"] == "boot-forensics"
    assert result.details["template"] == "boot-forensics"
    # Explicit per-boot fingerprint flows into the dedupe label
    assert manifest["metadata"]["labels"][JOB_FINGERPRINT_LABEL] == alert.effective_fingerprint()[:12]


def test_handler_refuses_before_spending_budget_when_no_ssh_user(tmp_path):
    """CFOP-137: an unset ssh_user must not cost a budget slot or a Job.

    The worker renders "ssh {ssh_user}@{host}" into a billed prompt rather than
    connecting itself, so an empty user is not a fast connect error -- it is a
    Job that flails until claude_timeout. Refuse alongside the host,
    concurrency and budget guards, naming the setting.
    """
    kubectl = _FakeKubectl()
    handler = _handler(tmp_path, kubectl, ssh_user="")
    result = handler.execute(_request(_host_alert()))
    assert result.details.get("outcome") == "failed"
    assert "no SSH user configured" in (result.message or "")
    assert "CFOP_DEEP_SSH_USER" in (result.message or "")
    assert not any("create" in str(c) for c in kubectl.calls), \
        "no Job may be created without an ssh user"


# ---- the reroute counter (CFOP-163) ----------------------------------------

def test_reroute_is_counted_by_the_action_it_replaced():
    import pytest
    from event_runtime import telemetry
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    child = telemetry.DEEP_REROUTES.labels(from_action="escalate")
    before = child._value.get()
    routed = _route(_host_alert(), Decision(action="escalate", confidence=0.9, reasoning="node is gone", params={}))
    assert routed.action == DEEP_INVESTIGATE_ACTION
    assert child._value.get() == before + 1
    # A decision that is NOT rerouted counts nothing.
    _route(_host_alert(), Decision(action="notify", confidence=0.9, reasoning="fine", params={}))
    assert child._value.get() == before + 1
