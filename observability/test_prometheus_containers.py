"""PrometheusContainers: discovery via Telegraf metrics, actions over SSH.

requests and subprocess are patched — no Prometheus, no hosts. The interesting
behaviour is the host resolution: every action accepts an optional host and
otherwise has to find the container's engine_host in Prometheus first, and must
degrade to an empty result rather than raise when it cannot.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from observability.prometheus_containers import PrometheusContainers


def _metric_result(name, engine_host=None, instance=None, image="", uptime="1000"):
    metric = {"container_name": name, "container_image": image}
    if engine_host is not None:
        metric["engine_host"] = engine_host
    if instance is not None:
        metric["instance"] = instance
    return {"metric": metric, "value": [0, uptime]}


class _FakeResponse:
    def __init__(self, payload, raise_error=None):
        self._payload = payload
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._payload


def _prom_payload(*results):
    return {"status": "success", "data": {"resultType": "vector", "result": list(results)}}


@pytest.fixture
def backend():
    return PrometheusContainers("http://prom:9090/", ssh_user="ops")


class TestInit:
    def test_strips_trailing_slash(self, backend):
        assert backend.prometheus_url == "http://prom:9090"

    def test_default_ssh_user(self):
        assert PrometheusContainers("http://prom:9090").ssh_user == "aachten"


class TestListContainers:
    def test_maps_metrics_to_containers(self, backend):
        payload = _prom_payload(
            _metric_result("nginx", engine_host="web1", image="nginx:1.25", uptime="5000"),
            _metric_result("postgres", engine_host="db1", image="postgres:16", uptime="0"),
        )
        with patch("requests.get", return_value=_FakeResponse(payload)) as get:
            containers = backend.list_containers()

        assert get.call_args.args[0] == "http://prom:9090/api/v1/query"
        assert get.call_args.kwargs["params"] == {
            "query": 'docker_container_status_uptime_ns{container_name!=""}'
        }
        assert containers == [
            {"host": "web1", "name": "nginx", "status": "running", "image": "nginx:1.25"},
            {"host": "db1", "name": "postgres", "status": "stopped", "image": "postgres:16"},
        ]

    def test_host_filter_is_a_regex_match(self, backend):
        with patch("requests.get", return_value=_FakeResponse(_prom_payload())) as get:
            backend.list_containers(host="web1")

        assert get.call_args.kwargs["params"]["query"] == (
            'docker_container_status_uptime_ns{container_name!="", engine_host=~".*web1.*"}'
        )

    def test_falls_back_to_instance_host(self, backend):
        payload = _prom_payload(_metric_result("nginx", instance="web1.local:9273"))
        with patch("requests.get", return_value=_FakeResponse(payload)):
            assert backend.list_containers()[0]["host"] == "web1.local"

    def test_unknown_host_when_metric_has_neither_label(self, backend):
        payload = _prom_payload(_metric_result("nginx"))
        with patch("requests.get", return_value=_FakeResponse(payload)):
            assert backend.list_containers()[0]["host"] == "unknown"

    def test_query_error_returns_empty(self, backend, capsys):
        with patch("requests.get", side_effect=OSError("connection refused")):
            assert backend.list_containers() == []
        assert "Prometheus query error" in capsys.readouterr().out

    def test_http_error_returns_empty(self, backend):
        response = _FakeResponse({}, raise_error=RuntimeError("503"))
        with patch("requests.get", return_value=response):
            assert backend.list_containers() == []

    def test_missing_data_key_returns_empty(self, backend):
        with patch("requests.get", return_value=_FakeResponse({"status": "success"})):
            assert backend.list_containers() == []


class TestInspect:
    def test_runs_docker_inspect_over_ssh(self, backend):
        payload = json.dumps([{"Id": "abc", "Name": "/nginx"}])
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")
        with patch("subprocess.run", return_value=completed) as run:
            assert backend.inspect("nginx", host="web1")["Id"] == "abc"

        assert run.call_args.args[0] == ["ssh", "ops@web1", "docker", "inspect", "nginx"]

    def test_resolves_host_from_prometheus(self, backend):
        payload = _prom_payload(_metric_result("nginx", engine_host="web1"))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([{"Id": "abc"}]), stderr=""
        )
        with patch("requests.get", return_value=_FakeResponse(payload)), \
                patch("subprocess.run", return_value=completed) as run:
            backend.inspect("nginx")

        assert run.call_args.args[0][1] == "ops@web1"

    def test_unknown_container_returns_empty(self, backend):
        with patch("requests.get", return_value=_FakeResponse(_prom_payload())):
            assert backend.inspect("ghost") == {}

    def test_ssh_failure_returns_empty(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="denied")
        with patch("subprocess.run", return_value=completed):
            assert backend.inspect("nginx", host="web1") == {}

    def test_unparseable_output_returns_empty(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        with patch("subprocess.run", return_value=completed):
            assert backend.inspect("nginx", host="web1") == {}

    def test_ssh_timeout_returns_empty(self, backend, capsys):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30)):
            assert backend.inspect("nginx", host="web1") == {}
        assert "SSH command error" in capsys.readouterr().out


class TestGetLogs:
    def test_builds_tail_and_since(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="line1\n", stderr="")
        with patch("subprocess.run", return_value=completed) as run:
            assert backend.get_logs("nginx", tail=50, since="10m", host="web1") == "line1\n"

        assert run.call_args.args[0] == [
            "ssh", "ops@web1", "docker", "logs", "--tail", "50", "--since", "10m", "nginx",
        ]

    def test_default_tail_without_since(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed) as run:
            backend.get_logs("nginx", host="web1")

        cmd = run.call_args.args[0]
        assert cmd[-3:] == ["--tail", "100", "nginx"]
        assert "--since" not in cmd

    def test_resolves_host_from_prometheus(self, backend):
        payload = _prom_payload(_metric_result("nginx", engine_host="web1"))
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="line1\n", stderr="")
        with patch("requests.get", return_value=_FakeResponse(payload)), \
                patch("subprocess.run", return_value=completed) as run:
            backend.get_logs("nginx")

        assert run.call_args.args[0][1] == "ops@web1"

    def test_unknown_container_returns_empty_string(self, backend):
        with patch("requests.get", return_value=_FakeResponse(_prom_payload())):
            assert backend.get_logs("ghost") == ""


class TestRestart:
    def test_success(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="nginx\n", stderr="")
        with patch("subprocess.run", return_value=completed) as run:
            assert backend.restart("nginx", host="web1") is True

        assert run.call_args.args[0] == ["ssh", "ops@web1", "docker", "restart", "nginx"]

    def test_failed_ssh_is_false(self, backend):
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no such container")
        with patch("subprocess.run", return_value=completed):
            assert backend.restart("nginx", host="web1") is False

    def test_resolves_host_from_prometheus(self, backend):
        payload = _prom_payload(_metric_result("nginx", engine_host="web1"))
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="nginx\n", stderr="")
        with patch("requests.get", return_value=_FakeResponse(payload)), \
                patch("subprocess.run", return_value=completed) as run:
            assert backend.restart("nginx") is True

        assert run.call_args.args[0][1] == "ops@web1"

    def test_unknown_container_is_false_without_ssh(self, backend):
        with patch("requests.get", return_value=_FakeResponse(_prom_payload())), \
                patch("subprocess.run") as run:
            assert backend.restart("ghost") is False
        run.assert_not_called()
