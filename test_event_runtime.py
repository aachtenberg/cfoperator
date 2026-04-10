"""Basic tests for the modular event runtime scaffold."""

from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from event_runtime.engine import EventRuntime
from event_runtime.bootstrap import build_portable_runtime
from event_runtime.dedupe import FileBackedCooldownPolicy
from event_runtime.host_observability import (
    BareMetalHostContextProvider,
    LocalHostStatsProvider,
    PrometheusHostStatsProvider,
    PrometheusK3sProvider,
    SSHHostStatsProvider,
)
from event_runtime.models import Alert, AlertSeverity, ContextEnvelope, Decision, HostObservation, HostTarget, ScheduledTask
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, ContextProvider, DecisionEngine, HostObservabilityProvider, Scheduler
from event_runtime.server import make_handler, serve
from event_runtime.sources import AlertmanagerAlertSource
from event_runtime.state.composite import CompositeStateSink
from event_runtime.state.local_outbox import LocalOutboxStateSink
from event_runtime.state.replay import ReplayingStateSink
from event_runtime.telemetry import render_metrics, telemetry_available
from event_runtime.worker import BackgroundAlertWorker, FileBackedWorkerState, QueueFullError


class StaticContext(ContextProvider):
    name = "static-context"
    capabilities = ("metrics", "logs", "kubernetes")

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        envelope.context["source"] = alert.source
        return envelope


class InvestigateDecision(DecisionEngine):
    name = "investigate-decision"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(
            action="investigate",
            confidence=1.0,
            reasoning="default",
            params={},
            requested_checks=["metrics", "logs"],
        )


class InvestigateAction(ActionHandler):
    name = "investigate-action"
    action_name = "investigate"

    def execute(self, request):
        return __import__("event_runtime.models", fromlist=["ActionResult"]).ActionResult(
            action="investigate",
            success=True,
            message=f"processed {request.alert.summary}",
            details=request.context.context,
        )


class FailingAction(ActionHandler):
    name = "failing-action"
    action_name = "investigate"

    def execute(self, request):
        raise RuntimeError("boom")


class MemoryScheduler(Scheduler):
    name = "memory-scheduler"

    def __init__(self):
        self.tasks = []

    def schedule(self, task: ScheduledTask) -> dict:
        self.tasks.append(task)
        return {
            "success": True,
            "message": f"scheduled {task.name}",
            "task": task.to_dict(),
        }


class ScheduledDecision(DecisionEngine):
    name = "scheduled-decision"

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(
            action="investigate",
            confidence=0.9,
            reasoning="needs a recurring check",
            params={},
            scheduled_tasks=[
                ScheduledTask(
                    name="watch-crashloop-pod",
                    schedule="*/5 * * * *",
                    rationale="Track repeated restarts until stable",
                    target={"kind": "pod", "namespace": "apps", "name": "api"},
                    parameters={"check": "restart_rate"},
                )
            ],
        )


class TrackingDecision(DecisionEngine):
    name = "tracking-decision"

    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def decide(self, envelope: ContextEnvelope) -> Decision:
        return Decision(action="investigate", confidence=1.0, reasoning="tracking", params={})


class TrackingSink(CompositeStateSink):
    def __init__(self, directory: str):
        super().__init__([LocalOutboxStateSink(directory=directory)])
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        super().start()

    def stop(self) -> None:
        self.stop_calls += 1
        super().stop()


class TrackingWorker:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class StaticHostProvider(HostObservabilityProvider):
    name = "static-host-provider"

    def __init__(self):
        self.target = HostTarget(
            name="edge-01",
            provider=self.name,
            address="10.0.0.10",
            aliases=["edge01", "10.0.0.10"],
        )

    def discover_targets(self) -> list[HostTarget]:
        return [self.target]

    def collect(self, target: HostTarget) -> HostObservation:
        return HostObservation(
            provider=self.name,
            target=target.name,
            stats={
                "cpu": {"load_average_1m": 0.42},
                "memory": {"used_percent": 33.0},
                "disk": {"root": {"used_percent": 61.0}},
            },
        )


def _request_json(server, method: str, path: str, payload: dict | None = None):
    connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
    body = None if payload is None else json.dumps(payload)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    connection.close()
    return response.status, json.loads(data)


def _request_raw(server, method: str, path: str, payload: dict | None = None):
    connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=2)
    body = None if payload is None else json.dumps(payload)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type")
    status = response.status
    connection.close()
    return status, content_type, data


def _extract_metric_value(payload: bytes, metric_name: str) -> float:
    for line in payload.decode("utf-8").splitlines():
        if line.startswith(metric_name + " "):
            return float(line.split()[-1])
    raise AssertionError(f"metric not found: {metric_name}")


def test_local_outbox_persists_events(tmp_path: Path):
    sink = LocalOutboxStateSink(directory=str(tmp_path / "outbox"))
    sink.append([{"event_type": "x", "payload": {"ok": True}}])
    recent = sink.recent(limit=10)
    assert recent[0]["event_type"] == "x"


def test_local_outbox_can_restart_after_stop(tmp_path: Path):
    sink = LocalOutboxStateSink(directory=str(tmp_path / "restartable-outbox"))
    sink.append([{"event_type": "first", "payload": {"ok": True}}])
    sink.stop()
    sink.start()
    sink.append([{"event_type": "second", "payload": {"ok": True}}])

    recent = sink.recent(limit=10)
    assert [event["event_type"] for event in recent[:2]] == ["second", "first"]


def test_local_outbox_next_path_avoids_counter_collision(tmp_path: Path):
    outbox_dir = tmp_path / "collision-outbox"
    outbox_dir.mkdir(parents=True)
    (outbox_dir / "events_000001.jsonl").write_text("", encoding="utf-8")
    (outbox_dir / "events_000003.jsonl").write_text("", encoding="utf-8")

    sink = LocalOutboxStateSink(directory=str(outbox_dir))
    sink.append([{"event_type": "x", "payload": {"ok": True}}])
    sink.stop()

    files = sorted(path.name for path in outbox_dir.glob("events_*.jsonl"))
    assert "events_000004.jsonl" in files


def test_runtime_processes_warning_without_database(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "runtime-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_context_provider(StaticContext())
    plugins.register_action_handler(InvestigateAction())

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.WARNING, summary="pod crashloop")
    )

    assert result["success"] is True
    events = runtime.recent_events(limit=10)
    event_types = [event["event_type"] for event in events]
    assert "action_completed" in event_types


def test_alert_from_dict_parses_wire_payload_fields():
    alert = Alert.from_dict(
        {
            "source": "test",
            "severity": "warning",
            "summary": "normalized",
            "details": {"host": "edge-01"},
            "occurred_at": "2026-04-07T12:00:00+00:00",
            "alert_id": "alert-123",
        }
    )

    assert alert.alert_id == "alert-123"
    assert alert.occurred_at == datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)


def test_runtime_reraises_action_handler_errors(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "failing-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(FailingAction())
    runtime = EventRuntime(plugins)

    with pytest.raises(RuntimeError, match="boom"):
        runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="will fail"))

    event_types = [event["event_type"] for event in runtime.recent_events(limit=10)]
    assert "decision_made" in event_types
    assert "action_completed" not in event_types


def test_local_host_stats_provider_collects_baremetal_stats():
    provider = LocalHostStatsProvider()
    targets = provider.discover_targets()

    assert targets
    observation = provider.collect(targets[0]).to_dict()
    assert observation["provider"] == "local-host-stats"
    assert "cpu" in observation["stats"]
    assert "memory" in observation["stats"]
    assert "disk" in observation["stats"]


def test_baremetal_context_provider_matches_requested_host():
    provider = StaticHostProvider()
    context_provider = BareMetalHostContextProvider([provider])
    alert = Alert(
        source="manual",
        severity=AlertSeverity.WARNING,
        summary="edge host hot",
        details={"host": "edge01"},
    )

    envelope = context_provider.provide(alert, ContextEnvelope(alert=alert))

    assert "host_observability" in envelope.context
    observations = envelope.context["host_observability"]["observations"]
    assert observations[0]["provider"] == provider.name
    assert observations[0]["target"] == "edge-01"


def test_baremetal_context_provider_refreshes_discovery_when_interval_is_zero():
    class DynamicHostProvider(HostObservabilityProvider):
        name = "dynamic-host-provider"

        def __init__(self):
            self.calls = 0

        def discover_targets(self) -> list[HostTarget]:
            self.calls += 1
            name = "edge-01" if self.calls == 1 else "edge-02"
            address = "10.0.0.10" if self.calls == 1 else "10.0.0.11"
            return [HostTarget(name=name, provider=self.name, address=address, aliases=[name.replace("-", "")])]

        def collect(self, target: HostTarget) -> HostObservation:
            return HostObservation(provider=self.name, target=target.name, stats={"cpu": {"load_average_1m": 0.1}})

    provider = DynamicHostProvider()
    context_provider = BareMetalHostContextProvider([provider], refresh_interval_seconds=0)

    first_alert = Alert(source="manual", severity=AlertSeverity.WARNING, summary="first", details={"host": "edge01"})
    second_alert = Alert(source="manual", severity=AlertSeverity.WARNING, summary="second", details={"host": "edge02"})

    first = context_provider.provide(first_alert, ContextEnvelope(alert=first_alert))
    second = context_provider.provide(second_alert, ContextEnvelope(alert=second_alert))

    assert first.context["host_observability"]["observations"][0]["target"] == "edge-01"
    assert second.context["host_observability"]["observations"][0]["target"] == "edge-02"
    assert provider.calls >= 2


def test_ssh_host_stats_provider_executes_script_over_stdin(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, check=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                [
                    "hostname=edge-01",
                    "load_1m=0.5",
                    "load_5m=0.4",
                    "load_15m=0.3",
                    "uptime_seconds=3600",
                    "cpu_cores=4",
                    "mem_total_bytes=1000",
                    "mem_available_bytes=250",
                    "disk_total_bytes=2000",
                    "disk_used_bytes=1000",
                    "disk_available_bytes=1000",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("event_runtime.host_observability.subprocess.run", fake_run)

    provider = SSHHostStatsProvider(
        [HostTarget(name="edge-01", provider="ssh-host-stats", address="10.0.0.10", metadata={"user": "cfoperator"})]
    )
    observation = provider.collect(provider.discover_targets()[0]).to_dict()

    assert captured["cmd"][-1] == "sh"
    assert "disk_total_bytes=" in captured["input"]
    assert observation["stats"]["memory"]["used_percent"] == 75.0
    assert observation["stats"]["disk"]["root"]["used_percent"] == 50.0


def test_prometheus_host_stats_provider_discovers_targets_and_collects_metrics():
    responses = {
        'up{job=~"node-exporter|node_exporter"} == 1': {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {
                            "instance": "edge-01:9100",
                            "job": "node-exporter",
                            "nodename": "edge-01",
                        },
                        "value": [1712448000, "1"],
                    }
                ]
            },
        },
        'node_load1{instance="edge-01:9100"}': {"status": "success", "data": {"result": [{"value": [1712448000, "0.5"]}]}},
        'node_time_seconds{instance="edge-01:9100"} - node_boot_time_seconds{instance="edge-01:9100"}': {"status": "success", "data": {"result": [{"value": [1712448000, "3600"]}]}},
        '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",instance="edge-01:9100"}[5m])))': {"status": "success", "data": {"result": [{"value": [1712448000, "12.5"]}]}},
        'node_memory_MemTotal_bytes{instance="edge-01:9100"}': {"status": "success", "data": {"result": [{"value": [1712448000, "1000"]}]}},
        'node_memory_MemAvailable_bytes{instance="edge-01:9100"}': {"status": "success", "data": {"result": [{"value": [1712448000, "400"]}]}},
        'node_filesystem_size_bytes{instance="edge-01:9100",mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}': {"status": "success", "data": {"result": [{"value": [1712448000, "2000"]}]}},
        'node_filesystem_avail_bytes{instance="edge-01:9100",mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}': {"status": "success", "data": {"result": [{"value": [1712448000, "500"]}]}},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            query = parse_qs(parsed.query).get("query", [""])[0]
            payload = responses.get(query, {"status": "success", "data": {"result": []}})
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = PrometheusHostStatsProvider(url=f"http://127.0.0.1:{server.server_address[1]}")
        targets = provider.discover_targets()
        assert targets[0].name == "edge-01"
        observation = provider.collect(targets[0]).to_dict()
        assert observation["stats"]["cpu"]["utilization_percent_5m"] == 12.5
        assert observation["stats"]["memory"]["used_percent"] == 60.0
        assert observation["stats"]["disk"]["root"]["used_percent"] == 75.0
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_k3s_provider_discovers_nodes_and_collects_metrics():
    responses = {
        "kube_node_info": {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {
                            "node": "raspberrypi",
                            "internal_ip": "192.168.0.167",
                            "kernel_version": "6.1.0-rpi7-rpi-v8",
                            "kubelet_version": "v1.31.4+k3s1",
                            "os_image": "Debian GNU/Linux 12",
                            "container_runtime_version": "containerd://1.7.23-k3s2",
                        },
                        "value": [1712448000, "1"],
                    }
                ]
            },
        },
        'kube_node_status_condition{node="raspberrypi",condition="Ready",status="true"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "1"]}]},
        },
        'kube_node_status_condition{node="raspberrypi",condition="MemoryPressure",status="true"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "0"]}]},
        },
        'kube_node_status_condition{node="raspberrypi",condition="DiskPressure",status="true"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "0"]}]},
        },
        'kube_node_status_condition{node="raspberrypi",condition="PIDPressure",status="true"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "0"]}]},
        },
        'count(kube_pod_info{node="raspberrypi"}) by (namespace)': {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"namespace": "monitoring"}, "value": [1712448000, "5"]},
                    {"metric": {"namespace": "apps"}, "value": [1712448000, "3"]},
                ]
            },
        },
        'count(kube_pod_status_phase{node="raspberrypi",phase=~"Running|Pending|Failed|Succeeded|Unknown"}) by (phase)': {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"phase": "Running"}, "value": [1712448000, "7"]},
                    {"metric": {"phase": "Succeeded"}, "value": [1712448000, "1"]},
                ]
            },
        },
        'topk(10, sum(kube_pod_container_status_restarts_total{node="raspberrypi"}) by (namespace, pod))': {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"namespace": "apps", "pod": "api-7b4f9"}, "value": [1712448000, "12"]},
                ]
            },
        },
        'sum(rate(container_cpu_usage_seconds_total{node="raspberrypi",container!=""}[5m]))': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "0.45"]}]},
        },
        'sum(container_memory_working_set_bytes{node="raspberrypi",container!=""})': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "524288000"]}]},
        },
        'kube_node_status_allocatable{node="raspberrypi",resource="cpu"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "4"]}]},
        },
        'kube_node_status_allocatable{node="raspberrypi",resource="memory"}': {
            "status": "success",
            "data": {"result": [{"value": [1712448000, "8000000000"]}]},
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            query = parse_qs(parsed.query).get("query", [""])[0]
            payload = responses.get(query, {"status": "success", "data": {"result": []}})
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = PrometheusK3sProvider(url=f"http://127.0.0.1:{server.server_address[1]}")
        targets = provider.discover_targets()
        assert len(targets) == 1
        assert targets[0].name == "raspberrypi"
        assert targets[0].metadata["kubelet_version"] == "v1.31.4+k3s1"

        observation = provider.collect(targets[0])
        assert observation is not None
        stats = observation.to_dict()["stats"]

        assert stats["conditions"]["Ready"] is True
        assert stats["conditions"]["MemoryPressure"] is False
        assert stats["pods"]["total"] == 8
        assert stats["pods"]["by_namespace"]["monitoring"] == 5
        assert stats["pods"]["by_phase"]["Running"] == 7
        assert stats["restarts"][0]["pod"] == "api-7b4f9"
        assert stats["restarts"][0]["restarts"] == 12
        assert stats["resources"]["cpu"]["usage_cores"] == 0.45
        assert stats["resources"]["cpu"]["allocatable_cores"] == 4.0
        assert stats["resources"]["cpu"]["utilization_percent"] == 11.25
        assert stats["resources"]["memory"]["usage_bytes"] == 524288000.0
        assert stats["resources"]["memory"]["utilization_percent"] == 6.55
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_portable_runtime_registers_host_observability_from_env(tmp_path: Path):
    config_path = tmp_path / "host-observability.json"
    config_path.write_text(json.dumps({"providers": [{"type": "local"}]}), encoding="utf-8")
    previous_path = os.environ.get("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH")
    try:
        os.environ["CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH"] = str(config_path)
        runtime = build_portable_runtime()
        health = runtime.health()
        assert "local-host-stats" in health["host_observability_providers"]
        assert "baremetal-host-context" in health["context_providers"]
    finally:
        if previous_path is None:
            os.environ.pop("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH", None)
        else:
            os.environ["CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH"] = previous_path


def test_portable_runtime_loads_host_observability_from_yaml_config(monkeypatch, tmp_path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "event_runtime": {
                    "host_observability": {
                        "refresh_interval_seconds": 0,
                        "providers": [{"type": "local"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_HOST_OBSERVABILITY_JSON", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))

    runtime = build_portable_runtime()
    health = runtime.health()

    assert "local-host-stats" in health["host_observability_providers"]
    context_provider = next(
        provider for provider in runtime.plugins.context_providers if provider.name == "baremetal-host-context"
    )
    assert context_provider.refresh_interval_seconds == 0


def test_baremetal_context_provider_emits_host_metrics():
    if not telemetry_available():
        return

    provider = StaticHostProvider()
    context_provider = BareMetalHostContextProvider([provider], refresh_interval_seconds=0)
    context_provider.start()

    alert = Alert(
        source="manual",
        severity=AlertSeverity.WARNING,
        summary="edge host hot",
        details={"host": "edge01"},
    )
    context_provider.provide(alert, ContextEnvelope(alert=alert))

    payload, _content_type = render_metrics()
    assert b"cfoperator_event_runtime_host_discovery_runs_total" in payload
    assert b"cfoperator_event_runtime_host_discovered_targets" in payload
    assert b"cfoperator_event_runtime_host_observation_runs_total" in payload


def test_render_metrics_contains_runtime_series(tmp_path: Path):
    if not telemetry_available():
        return
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "metrics-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())

    runtime = EventRuntime(plugins)
    runtime.start()
    try:
        runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="metric alert"))
        payload, content_type = render_metrics()
        assert "text/plain" in content_type
        assert b"cfoperator_event_runtime_alerts_received_total" in payload
        assert b"cfoperator_event_runtime_alert_processing_seconds" in payload
        assert b"cfoperator_event_runtime_events_recorded_total" in payload
    finally:
        runtime.stop()


def test_runtime_start_and_stop_plugins_once(tmp_path: Path):
    sink = TrackingSink(directory=str(tmp_path / "tracking-outbox"))
    decision = TrackingDecision()
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(decision)

    runtime = EventRuntime(plugins)
    runtime.start()
    runtime.start()
    runtime.stop()
    runtime.stop()

    assert sink.start_calls == 1
    assert sink.stop_calls == 1
    assert decision.start_calls == 1
    assert decision.stop_calls == 1


def test_runtime_logs_info_without_action(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "info-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.INFO, summary="heartbeat")
    )

    assert result["action"] == "log_only"
    assert result["success"] is True


def test_runtime_can_schedule_follow_up_tasks(tmp_path: Path):
    scheduler = MemoryScheduler()
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "schedule-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(ScheduledDecision())
    plugins.register_action_handler(InvestigateAction())
    plugins.register_scheduler(scheduler)

    runtime = EventRuntime(plugins)
    result = runtime.handle_alert(
        Alert(source="test", severity=AlertSeverity.WARNING, summary="restart storm")
    )

    assert result["success"] is True
    assert result["scheduled_tasks"]
    assert result["scheduled_tasks"][0]["success"] is True
    assert scheduler.tasks[0].name == "watch-crashloop-pod"


def test_file_backed_cooldown_policy_suppresses_duplicates(tmp_path: Path):
    policy = FileBackedCooldownPolicy(path=str(tmp_path / "dedupe.json"), cooldown_seconds=300)
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="same alert")

    allowed_first, reason_first = policy.evaluate(alert)
    allowed_second, reason_second = policy.evaluate(alert)

    assert allowed_first is True
    assert reason_first is None
    assert allowed_second is False
    assert "duplicate suppressed" in str(reason_second)


def test_runtime_suppresses_duplicate_alerts(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "dedupe-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    plugins.register_alert_policy(
        FileBackedCooldownPolicy(path=str(tmp_path / "dedupe-state.json"), cooldown_seconds=300)
    )

    runtime = EventRuntime(plugins)
    first = runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="dupe"))
    second = runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="dupe"))

    assert first["success"] is True
    assert second["status"] == "suppressed"


def test_portable_runtime_bootstrap_uses_local_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "portable"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    runtime = build_portable_runtime()

    health = runtime.health()
    sink = health["sink"]
    assert sink["healthy"] is True
    assert sink["durable"] is True

    result = runtime.handle_alert(
        Alert(source="portable", severity=AlertSeverity.WARNING, summary="portable run")
    )
    assert result["success"] is True


def test_fastapi_adapter_module_can_be_imported_without_fastapi_installed():
    module = __import__("event_runtime.fastapi_app", fromlist=["build_app"])
    assert hasattr(module, "create_app")
    assert hasattr(module, "build_app")


def test_fastapi_adapter_exposes_metrics_when_fastapi_is_available(tmp_path: Path):
    try:
        from fastapi.testclient import TestClient
    except (ImportError, RuntimeError):
        return

    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "fastapi-metrics-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    module = __import__("event_runtime.fastapi_app", fromlist=["create_app"])
    app = module.create_app(runtime=runtime, worker=None)
    client = TestClient(app)

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")


class MemoryRemoteSink:
    durable = False
    name = "memory-remote"

    def __init__(self):
        self.events = []
        self.append_calls = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def append(self, events):
        self.append_calls += 1
        known = {event["event_id"] for event in self.events}
        for event in events:
            if event["event_id"] not in known:
                self.events.append(event)
        return True

    def recent(self, limit: int = 50):
        return list(reversed(self.events[-limit:]))

    def health(self):
        return {"name": self.name, "healthy": True, "durable": False}


def test_replaying_sink_replays_outbox_events(tmp_path: Path):
    local_sink = LocalOutboxStateSink(directory=str(tmp_path / "replay-outbox"))
    remote_sink = MemoryRemoteSink()
    sink = ReplayingStateSink(
        local_sink=local_sink,
        remote_sinks=[remote_sink],
        replay_interval_seconds=3600,
        replay_batch_size=10,
        checkpoint_path=str(tmp_path / "replay-outbox" / "checkpoints.json"),
    )

    event = {
        "event_id": "evt-1",
        "created_at": "2026-04-07T00:00:00+00:00",
        "event_type": "alert_received",
        "payload": {"ok": True},
    }
    assert sink.append([event]) is True
    replay = sink.replay_once()

    assert replay["success"] is True
    assert replay["replayed"] == 0
    assert remote_sink.events
    assert remote_sink.events[0]["event_id"] == "evt-1"


class FlakyRemoteSink(MemoryRemoteSink):
    name = "flaky-remote"

    def __init__(self, fail_calls: int = 1):
        super().__init__()
        self.fail_calls = fail_calls

    def append(self, events):
        self.append_calls += 1
        if self.fail_calls > 0:
            self.fail_calls -= 1
            return False
        return super().append(events)


def test_replaying_sink_persists_checkpoints_across_instances(tmp_path: Path):
    outbox_dir = tmp_path / "replay-persist-outbox"
    checkpoint_path = tmp_path / "replay-persist-outbox" / "checkpoints.json"
    local_sink = LocalOutboxStateSink(directory=str(outbox_dir))
    remote_sink = MemoryRemoteSink()
    sink = ReplayingStateSink(
        local_sink=local_sink,
        remote_sinks=[remote_sink],
        replay_interval_seconds=3600,
        replay_batch_size=10,
        checkpoint_path=str(checkpoint_path),
    )

    for index in range(2):
        assert sink.append(
            [
                {
                    "event_id": f"evt-{index}",
                    "created_at": "2026-04-07T00:00:00+00:00",
                    "event_type": "alert_received",
                    "payload": {"index": index},
                }
            ]
        ) is True

    local_sink_2 = LocalOutboxStateSink(directory=str(outbox_dir))
    remote_sink_2 = MemoryRemoteSink()
    sink_2 = ReplayingStateSink(
        local_sink=local_sink_2,
        remote_sinks=[remote_sink_2],
        replay_interval_seconds=3600,
        replay_batch_size=10,
        checkpoint_path=str(checkpoint_path),
    )
    second = sink_2.replay_once()

    assert second["success"] is True
    assert second["replayed"] == 0
    assert remote_sink_2.events == []


def test_replaying_sink_replays_backlog_before_advancing_inline_checkpoint(tmp_path: Path):
    outbox_dir = tmp_path / "replay-flaky-outbox"
    local_sink = LocalOutboxStateSink(directory=str(outbox_dir))
    remote_sink = FlakyRemoteSink(fail_calls=1)
    sink = ReplayingStateSink(
        local_sink=local_sink,
        remote_sinks=[remote_sink],
        replay_interval_seconds=3600,
        replay_batch_size=10,
        checkpoint_path=str(outbox_dir / "checkpoints.json"),
    )

    for index in range(2):
        assert sink.append(
            [
                {
                    "event_id": f"evt-{index}",
                    "created_at": "2026-04-07T00:00:00+00:00",
                    "event_type": "alert_received",
                    "payload": {"index": index},
                }
            ]
        ) is True

    replay = sink.replay_once()

    assert replay["success"] is True
    assert replay["replayed"] == 2
    assert sorted(event["event_id"] for event in remote_sink.events) == ["evt-0", "evt-1"]


def test_postgres_sink_module_can_be_imported_without_connecting():
    module = __import__("event_runtime.state.postgres", fromlist=["PostgresStateSink"])
    sink = module.PostgresStateSink(dsn="")
    assert sink.health()["configured"] is False


def test_runtime_can_record_explicit_events(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "events-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())

    runtime = EventRuntime(plugins)
    runtime.record_event("manual_event", ok=True)

    events = runtime.recent_events(limit=5)
    assert events[0]["event_type"] == "manual_event"


def test_runtime_activity_summarizes_completed_and_logged_alerts(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "activity-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="warning path"))
    runtime.handle_alert(Alert(source="test", severity=AlertSeverity.INFO, summary="info path"))

    activities = runtime.recent_activity(limit=10)
    warning = next(activity for activity in activities if activity["summary"] == "warning path")
    info = next(activity for activity in activities if activity["summary"] == "info path")

    assert warning["status"] == "completed"
    assert warning["action"] == "investigate"
    assert warning["decision"]["action"] == "investigate"
    assert "action_completed" in warning["event_types"]

    assert info["status"] == "logged"
    assert info["action"] == "log_only"
    assert info["reason"] == "severity_gate"

    skipped = runtime.recent_events(limit=5, event_type="alert_skipped", alert_id=info["alert_id"])
    assert len(skipped) == 1
    assert skipped[0]["event_type"] == "alert_skipped"


def test_server_exposes_activity_feed_and_html(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "activity-server-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)
    runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="api degraded"))

    handler = make_handler(runtime)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request_json(server, "GET", "/activity?limit=5")
        assert status == 200
        assert payload["activities"][0]["summary"] == "api degraded"

        status, content_type, data = _request_raw(server, "GET", "/activity.html?limit=5")
        assert status == 200
        assert "text/html" in content_type
        assert b"Event Runtime Activity" in data
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_background_worker_processes_job(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "worker-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())

    runtime = EventRuntime(plugins)
    worker = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=10)
    worker.start()
    try:
        queued = worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="queued alert"))
        job = worker.wait_for_job(queued["job_id"], timeout=2.0)
        assert job is not None
        assert job["status"] == "completed"
        assert job["result"]["success"] is True
    finally:
        worker.stop()

    metrics = worker.health()["metrics"]
    assert metrics["average_queue_delay_seconds"] >= 0.0
    assert metrics["average_processing_duration_seconds"] >= 0.0


def test_background_worker_restores_persisted_queued_jobs(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "restore-outbox"))])
    state = FileBackedWorkerState(path=str(tmp_path / "queue" / "jobs.json"))

    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    worker1 = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=10, state=state)
    queued = worker1.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="restore me"))

    worker2 = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=10, state=state)
    worker2.start()
    try:
        job = worker2.wait_for_job(queued["job_id"], timeout=2.0)
        assert job is not None
        assert job["status"] == "completed"
        assert job["result"]["success"] is True
    finally:
        worker2.stop()


def test_background_worker_rejects_when_queue_is_full(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "full-outbox"))])
    state = FileBackedWorkerState(path=str(tmp_path / "queue" / "jobs.json"))
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    worker = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=1, state=state)
    worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="first"))

    try:
        with pytest.raises(QueueFullError):
            worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="second"))
    finally:
        persisted = state.load_jobs()
        assert len(persisted) == 1


def test_background_worker_prunes_terminal_jobs(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "pruned-outbox"))])
    state = FileBackedWorkerState(path=str(tmp_path / "queue" / "jobs.json"))
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    worker = BackgroundAlertWorker(
        runtime=runtime,
        worker_count=1,
        max_queue_size=10,
        max_terminal_jobs=1,
        state=state,
    )
    worker.start()
    try:
        first = worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="first job"))
        second = worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="second job"))

        assert worker.wait_for_job(first["job_id"], timeout=2.0) is not None
        final_second = worker.wait_for_job(second["job_id"], timeout=2.0)
        assert final_second is not None
        assert worker.get_job(first["job_id"]) is None

        persisted = state.load_jobs()
        assert list(persisted.keys()) == [second["job_id"]]
    finally:
        worker.stop()


def test_stdlib_server_accepts_alert_mode_query_string(tmp_path: Path):
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "server-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)
    worker = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=10)

    runtime.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime, worker=worker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _request_json(
            server,
            "POST",
            "/alert?mode=sync",
            {"source": "test", "severity": "warning", "summary": "query string works"},
        )
        assert status == 200
        assert payload["success"] is True
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        runtime.stop()


def test_stdlib_server_exposes_metrics_endpoint(tmp_path: Path):
    if not telemetry_available():
        return
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "server-metrics-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)

    runtime.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime, worker=None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime.handle_alert(Alert(source="test", severity=AlertSeverity.WARNING, summary="metrics endpoint"))
        status, content_type, payload = _request_raw(server, "GET", "/metrics")
        assert status == 200
        assert "text/plain" in str(content_type)
        assert b"cfoperator_event_runtime_alerts_received_total" in payload
        assert b"cfoperator_event_runtime_up" in payload
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        runtime.stop()


def test_stdlib_metrics_refresh_queue_age_at_scrape_time(tmp_path: Path):
    if not telemetry_available():
        return
    sink = CompositeStateSink([LocalOutboxStateSink(directory=str(tmp_path / "queue-age-outbox"))])
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(InvestigateDecision())
    plugins.register_action_handler(InvestigateAction())
    runtime = EventRuntime(plugins)
    worker = BackgroundAlertWorker(runtime=runtime, worker_count=1, max_queue_size=10)

    runtime.start()
    queued = worker.enqueue(Alert(source="test", severity=AlertSeverity.WARNING, summary="queued age"))
    with worker._jobs_lock:
        worker._jobs[queued["job_id"]].created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime, worker=worker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, content_type, payload = _request_raw(server, "GET", "/metrics")
        assert status == 200
        assert "text/plain" in str(content_type)
        assert _extract_metric_value(payload, "cfoperator_event_runtime_queue_size") >= 1.0
        assert _extract_metric_value(payload, "cfoperator_event_runtime_queue_oldest_age_seconds") >= 4.0
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        runtime.stop()


def test_serve_starts_and_stops_runtime_and_worker(monkeypatch, tmp_path: Path):
    sink = TrackingSink(directory=str(tmp_path / "serve-outbox"))
    decision = TrackingDecision()
    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(decision)
    runtime = EventRuntime(plugins)
    worker = TrackingWorker()
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["address"] = address
            captured["handler"] = handler
            captured["runtime_started_during_serve"] = runtime._started

        def serve_forever(self):
            captured["serve_called"] = True

        def server_close(self):
            captured["closed"] = True

    monkeypatch.setattr("event_runtime.server.ThreadingHTTPServer", FakeServer)

    serve(runtime, host="127.0.0.1", port=8099, worker=worker)

    assert captured["runtime_started_during_serve"] is True
    assert captured["serve_called"] is True
    assert captured["closed"] is True
    assert sink.start_calls == 1
    assert sink.stop_calls == 1
    assert decision.start_calls == 1
    assert decision.stop_calls == 1
    assert worker.start_calls == 1
    assert worker.stop_calls == 1


def test_alertmanager_source_normalizes_and_deduplicates():
    alertmanager_response = [
        {
            "labels": {
                "alertname": "PodNotReady",
                "severity": "warning",
                "namespace": "kube-system",
                "pod": "helm-install-traefik-crd-hnjqd",
                "instance": "kube-state-metrics.monitoring.svc:8080",
            },
            "annotations": {
                "summary": "Pod kube-system/helm-install-traefik-crd-hnjqd not ready for 30m",
            },
            "startsAt": "2026-03-31T17:26:10Z",
            "status": {"state": "active"},
            "fingerprint": "abc123",
        },
        {
            "labels": {
                "alertname": "NodeNotReady",
                "severity": "critical",
                "node": "raspberrypi3",
            },
            "annotations": {
                "summary": "Node raspberrypi3 not ready for 5m",
            },
            "startsAt": "2026-04-01T10:00:00Z",
            "status": {"state": "active"},
            "fingerprint": "def456",
        },
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(alertmanager_response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = AlertmanagerAlertSource(url=f"http://127.0.0.1:{server.server_address[1]}")

        # First poll — should yield both alerts
        alerts = list(source.poll())
        assert len(alerts) == 2
        assert alerts[0].source == "alertmanager"
        assert alerts[0].severity == AlertSeverity.WARNING
        assert alerts[0].namespace == "kube-system"
        assert alerts[0].resource_type == "pod"
        assert alerts[0].resource_name == "helm-install-traefik-crd-hnjqd"
        assert alerts[0].fingerprint == "abc123"
        assert alerts[1].severity == AlertSeverity.CRITICAL
        assert alerts[1].resource_type == "node"
        assert alerts[1].details.get("host") == "raspberrypi3"

        # Second poll — same fingerprints, should yield nothing
        alerts = list(source.poll())
        assert len(alerts) == 0

        # Simulate resolution then re-fire: change the response
        alertmanager_response.clear()
        alerts = list(source.poll())
        assert len(alerts) == 0  # nothing firing

        # Re-fire the first alert
        alertmanager_response.append({
            "labels": {"alertname": "PodNotReady", "severity": "warning", "namespace": "kube-system", "pod": "helm-install-traefik-crd-hnjqd"},
            "annotations": {"summary": "Pod not ready again"},
            "startsAt": "2026-04-07T12:00:00Z",
            "status": {"state": "active"},
            "fingerprint": "abc123",
        })
        alerts = list(source.poll())
        assert len(alerts) == 1
        assert alerts[0].summary == "Pod not ready again"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_alertmanager_source_registered_via_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "am-source"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_ALERTMANAGER_URL", "http://localhost:9999")

    runtime = build_portable_runtime()


# ---------------------------------------------------------------------------
# Git change context provider tests
# ---------------------------------------------------------------------------


def test_git_context_provider_attaches_recent_changes(monkeypatch, tmp_path):
    """GitChangeContextProvider falls back to local git when no GitHub token/slug is configured."""
    from event_runtime.git_context import GitChangeContextProvider

    fake_log = "abc1234|Alice|2026-04-09 10:00:00 +0000|fix: restart loop\ndef5678|Bob|2026-04-09 09:00:00 +0000|chore: bump version\n"

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=fake_log, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    repos = [{"name": "cfoperator", "path": str(tmp_path), "hosts": ["headless-gpu"], "services": ["cfoperator"]}]
    provider = GitChangeContextProvider(repos=repos)

    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="pod crash", details={"host": "headless-gpu"})
    envelope = ContextEnvelope(alert=alert)
    result = provider.provide(alert, envelope)

    assert "recent_changes" in result.context
    changes = result.context["recent_changes"]
    assert len(changes) == 1
    assert changes[0]["repo"] == "cfoperator"
    assert len(changes[0]["commits"]) == 2
    assert changes[0]["commits"][0]["author"] == "Alice"


def test_git_context_provider_skips_unmatched_host():
    """Provider should not enrich when alert host does not match any repo."""
    from event_runtime.git_context import GitChangeContextProvider

    repos = [{"name": "cfoperator", "hosts": ["headless-gpu"], "services": ["cfoperator"]}]
    provider = GitChangeContextProvider(repos=repos)

    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="disk full", details={"host": "raspberrypi"})
    envelope = ContextEnvelope(alert=alert)
    result = provider.provide(alert, envelope)
    assert "recent_changes" not in result.context


def test_git_context_provider_uses_github_api_primary(monkeypatch):
    """GitHub API is the primary path; local git is the fallback."""
    from event_runtime.git_context import GitChangeContextProvider

    # Simulate local git failure
    def fail_run(cmd, **kwargs):
        return SimpleNamespace(returncode=128, stdout="", stderr="not a git repo")

    monkeypatch.setattr("subprocess.run", fail_run)

    # Simulate GitHub API success
    commit_data = [
        {"sha": "aaa111", "commit": {"author": {"name": "Carol", "date": "2026-04-09T08:00:00Z"}, "message": "deploy fix"}},
    ]

    class FakeResp:
        def __init__(self):
            self.status = 200
        def read(self):
            return json.dumps(commit_data).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda req, **kw: FakeResp())

    repos = [{"name": "test-repo", "path": "/nonexistent", "github": "owner/test-repo", "hosts": ["node1"]}]
    provider = GitChangeContextProvider(repos=repos, github_token="fake-token")

    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test", details={"host": "node1"})
    envelope = ContextEnvelope(alert=alert)
    result = provider.provide(alert, envelope)

    assert "recent_changes" in result.context
    assert result.context["recent_changes"][0]["source"] == "github"
    assert result.context["recent_changes"][0]["commits"][0]["author"] == "Carol"


# ---------------------------------------------------------------------------
# GitHub action handler tests
# ---------------------------------------------------------------------------


def test_investigate_code_handler_uses_context():
    """InvestigateCodeActionHandler should summarize recent_changes from context."""
    from event_runtime.github_actions import InvestigateCodeActionHandler
    from event_runtime.models import ActionRequest

    handler = InvestigateCodeActionHandler(repos=[])
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test alert")
    decision = Decision(action="investigate_code", confidence=0.8, reasoning="check code")
    envelope = ContextEnvelope(alert=alert, context={
        "recent_changes": [
            {"repo": "myrepo", "commits": [
                {"hash": "abc123", "author": "Dev", "date": "2026-04-09", "message": "fix: something"},
            ]},
        ],
    })

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert result.success
    assert "myrepo" in result.details.get("summary", "")
    assert result.details["recent_changes"][0]["repo"] == "myrepo"


def test_investigate_code_handler_no_changes():
    """InvestigateCodeActionHandler should succeed with no changes message."""
    from event_runtime.github_actions import InvestigateCodeActionHandler
    from event_runtime.models import ActionRequest

    handler = InvestigateCodeActionHandler(repos=[])
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test")
    decision = Decision(action="investigate_code", confidence=0.8, reasoning="check")
    envelope = ContextEnvelope(alert=alert)

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert result.success
    assert "No recent code changes" in result.message


def test_open_pr_handler_missing_params():
    """OpenPRActionHandler should fail gracefully with missing params."""
    from event_runtime.github_actions import OpenPRActionHandler
    from event_runtime.models import ActionRequest

    handler = OpenPRActionHandler(token="fake")
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test")
    decision = Decision(action="open_pr", confidence=0.9, reasoning="fix needed", params={})
    envelope = ContextEnvelope(alert=alert)

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert not result.success
    assert "Missing required params" in result.message


def test_comment_issue_handler_missing_params():
    """CommentIssueActionHandler should fail gracefully with missing params."""
    from event_runtime.github_actions import CommentIssueActionHandler
    from event_runtime.models import ActionRequest

    handler = CommentIssueActionHandler(token="fake")
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test")
    decision = Decision(action="comment_issue", confidence=0.9, reasoning="post findings", params={"repo": "owner/repo"})
    envelope = ContextEnvelope(alert=alert)

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert not result.success
    assert "Missing required params" in result.message


def test_open_pr_handler_rejects_invalid_repo_slug():
    """OpenPRActionHandler should reject malformed repo slugs before making a request."""
    from event_runtime.github_actions import OpenPRActionHandler
    from event_runtime.models import ActionRequest

    handler = OpenPRActionHandler(token="fake")
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test")
    decision = Decision(
        action="open_pr",
        confidence=0.9,
        reasoning="fix needed",
        params={"repo": "../bad", "head": "feature-branch"},
    )
    envelope = ContextEnvelope(alert=alert)

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert not result.success
    assert "Invalid repo slug" in result.message


def test_comment_issue_handler_rejects_invalid_repo_slug():
    """CommentIssueActionHandler should reject malformed repo slugs before making a request."""
    from event_runtime.github_actions import CommentIssueActionHandler
    from event_runtime.models import ActionRequest

    handler = CommentIssueActionHandler(token="fake")
    alert = Alert(source="test", severity=AlertSeverity.WARNING, summary="test")
    decision = Decision(
        action="comment_issue",
        confidence=0.9,
        reasoning="post findings",
        params={"repo": "owner/repo/extra", "issue_number": 42},
    )
    envelope = ContextEnvelope(alert=alert)

    result = handler.execute(ActionRequest(alert=alert, decision=decision, context=envelope))
    assert not result.success
    assert "Invalid repo slug" in result.message


def test_build_github_action_handlers_no_token():
    """build_github_action_handlers without token should only include investigate_code."""
    from event_runtime.github_actions import build_github_action_handlers

    handlers = build_github_action_handlers(repos=[], github_token=None)
    assert "investigate_code" in handlers
    assert "open_pr" not in handlers
    assert "comment_issue" not in handlers


def test_build_github_action_handlers_with_token():
    """build_github_action_handlers with token should include all handlers."""
    from event_runtime.github_actions import build_github_action_handlers

    handlers = build_github_action_handlers(repos=[], github_token="fake-token")
    assert "investigate_code" in handlers
    assert "open_pr" in handlers
    assert "comment_issue" in handlers


# ---------------------------------------------------------------------------
# Bootstrap integration: git plugins registered when config present
# ---------------------------------------------------------------------------


def test_bootstrap_registers_git_plugins(monkeypatch, tmp_path):
    """When CFOP_GIT_REPOS_JSON is set, bootstrap should register git plugins."""
    repos = [{"name": "test-repo", "github": "owner/test-repo", "hosts": ["node1"]}]
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "git-bootstrap"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CFOP_GIT_REPOS_JSON", json.dumps(repos))

    runtime = build_portable_runtime()
    provider_names = [p.name for p in runtime.plugins.context_providers]
    assert "git-change-context" in provider_names
    assert "investigate_code" in runtime.plugins.action_handlers


def test_bootstrap_no_git_plugins_without_config(monkeypatch, tmp_path):
    """Without git config, bootstrap should not register git plugins."""
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "no-git"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    monkeypatch.delenv("CFOP_GIT_REPOS_JSON", raising=False)
    monkeypatch.delenv("CFOP_GITHUB_TOKEN", raising=False)

    runtime = build_portable_runtime()
    provider_names = [p.name for p in runtime.plugins.context_providers]
    assert "git-change-context" not in provider_names
    assert "investigate_code" not in runtime.plugins.action_handlers


def test_bootstrap_loads_github_token_from_config_env_file(monkeypatch, tmp_path):
    """Bootstrap should resolve git.github.token from a colocated .env file via config.yaml."""
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "git:\n"
        "  github:\n"
        "    token: ${GITHUB_TOKEN}\n"
        "  repos:\n"
        "    - name: test-repo\n"
        "      github: owner/test-repo\n"
        "      hosts:\n"
        "        - node1\n",
        encoding="utf-8",
    )
    env_path.write_text("GITHUB_TOKEN=from-dotenv\n", encoding="utf-8")

    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "git-config"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    monkeypatch.delenv("CFOP_GIT_REPOS_JSON", raising=False)
    monkeypatch.delenv("CFOP_GITHUB_TOKEN", raising=False)

    runtime = build_portable_runtime(config_path=str(config_path))

    provider_names = [p.name for p in runtime.plugins.context_providers]
    assert "git-change-context" in provider_names
    assert "investigate_code" in runtime.plugins.action_handlers
    assert "open_pr" in runtime.plugins.action_handlers
    assert "comment_issue" in runtime.plugins.action_handlers


def test_bootstrap_derives_postgres_audit_sink_from_database_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database:\n"
        "  host: db.internal\n"
        "  port: 5432\n"
        "  database: cfoperator\n"
        "  user: audit-user\n"
        "  password: p@ss word\n"
        "event_runtime:\n"
        "  persistence:\n"
        "    postgres:\n"
        "      enabled: true\n"
        "      table_name: audit_events\n",
        encoding="utf-8",
    )

    created = {}

    class FakePostgresSink:
        durable = False
        name = "postgres"

        def __init__(self, dsn: str | None = None, table_name: str = "event_runtime_events"):
            created["dsn"] = dsn
            created["table_name"] = table_name
            self.dsn = dsn
            self.table_name = table_name

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def append(self, events):
            return True

        def recent(self, limit: int = 50):
            return []

        def health(self):
            return {"name": self.name, "healthy": True, "durable": False, "configured": True, "table": self.table_name}

    import event_runtime.bootstrap as bootstrap_module

    monkeypatch.setattr(bootstrap_module, "PostgresStateSink", FakePostgresSink)
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "portable"))
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", "0")
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_ENABLED", raising=False)

    runtime = build_portable_runtime(config_path=str(config_path))

    assert isinstance(runtime.plugins.state_sink, ReplayingStateSink)
    assert created["dsn"] == "postgresql://audit-user:p%40ss%20word@db.internal:5432/cfoperator"
    assert created["table_name"] == "audit_events"
    assert runtime.health()["sink"]["remotes"][0]["table"] == "audit_events"