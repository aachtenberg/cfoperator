"""DiscoveryTools: host reachability probes and the inventory rollup.

subprocess.run is patched everywhere — no pings, no SSH. What matters is the
shape of the dicts the LLM sees (a probe failure must stay a structured result,
never an exception) and the commands built from host config.
"""

import subprocess
from unittest.mock import patch

import pytest

from tools.discovery import DiscoveryTools


HOSTS = {
    "web1": {
        "address": "10.0.0.1",
        "role": "web",
        "monitoring": ["prometheus"],
        "ssh": {"user": "ops", "key_path": "/keys/id_ed25519"},
    },
    "db1": {
        "address": "10.0.0.2",
        "ssh": {"user": "dba"},  # no key_path — agent/default key
    },
}

PING_OUTPUT = (
    "PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.\n"
    "--- 10.0.0.1 ping statistics ---\n"
    "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
    "rtt min/avg/max/mdev = 0.271/0.412/0.533/0.107 ms\n"
)


@pytest.fixture
def tools():
    return DiscoveryTools(HOSTS)


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class TestPingHost:
    def test_unknown_host(self, tools):
        assert tools.ping_host("nope") == {"success": False, "error": "Unknown host: nope"}

    def test_alive_parses_average_latency(self, tools):
        with patch("subprocess.run", return_value=_completed(["ping"], stdout=PING_OUTPUT)) as run:
            result = tools.ping_host("web1")

        assert run.call_args.args[0] == ["ping", "-c", "3", "-W", "2", "10.0.0.1"]
        assert result["success"] is True
        assert result["alive"] is True
        assert result["latency_ms"] == 0.412
        assert result["address"] == "10.0.0.1"

    def test_dead_host_has_no_latency(self, tools):
        with patch("subprocess.run", return_value=_completed(["ping"], returncode=1, stdout="")):
            result = tools.ping_host("web1")

        assert result["success"] is True
        assert result["alive"] is False
        assert result["latency_ms"] is None

    def test_alive_but_unparseable_output(self, tools):
        with patch("subprocess.run", return_value=_completed(["ping"], stdout="3 received\n")):
            result = tools.ping_host("web1")

        assert result["alive"] is True
        assert result["latency_ms"] is None

    def test_timeout_returns_structured_failure(self, tools):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ping", timeout=10)):
            result = tools.ping_host("web1")

        assert result["success"] is False
        assert result["host"] == "web1"
        assert "error" in result


class TestVerifySsh:
    def test_unknown_host(self, tools):
        assert tools.verify_ssh("nope")["success"] is False

    def test_accessible_uses_configured_key_and_user(self, tools):
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout="SSH_OK\n")) as run:
            result = tools.verify_ssh("web1")

        cmd = run.call_args.args[0]
        assert cmd[-2:] == ["ops@10.0.0.1", "echo SSH_OK"]
        assert cmd[cmd.index("-i") + 1] == "/keys/id_ed25519"
        assert result["ssh_accessible"] is True
        assert result["error"] is None

    def test_omits_key_flag_when_unset(self, tools):
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout="SSH_OK\n")) as run:
            tools.verify_ssh("db1")

        cmd = run.call_args.args[0]
        assert "-i" not in cmd
        assert cmd[-2] == "dba@10.0.0.2"

    def test_denied_surfaces_stderr(self, tools):
        with patch("subprocess.run", return_value=_completed(["ssh"], returncode=255, stderr="Permission denied")):
            result = tools.verify_ssh("web1")

        assert result["ssh_accessible"] is False
        assert result["error"] == "Permission denied"

    def test_timeout_returns_structured_failure(self, tools):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10)):
            result = tools.verify_ssh("web1")

        assert result == {
            "success": False,
            "host": "web1",
            "address": "10.0.0.1",
            "ssh_accessible": False,
            "error": result["error"],
        }


class TestVerifySudo:
    def test_unknown_host(self, tools):
        assert tools.verify_sudo("nope")["success"] is False

    def test_passwordless(self, tools):
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout="SUDO_OK\n")) as run:
            result = tools.verify_sudo("web1")

        assert run.call_args.args[0][-1] == "sudo -n echo SUDO_OK 2>&1"
        assert result["sudo_passwordless"] is True
        assert result["needs_password"] is False
        assert result["message"] == "SUDO_OK"

    def test_password_required_is_detected(self, tools):
        stdout = "sudo: a password is required\n"
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout=stdout)):
            result = tools.verify_sudo("web1")

        assert result["sudo_passwordless"] is False
        assert result["needs_password"] is True

    def test_timeout_returns_structured_failure(self, tools):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10)):
            result = tools.verify_sudo("web1")

        assert result["success"] is False
        assert result["host"] == "web1"


class TestDiscoverAllHosts:
    def test_healthy_fleet_has_no_issues(self, tools):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ping":
                return _completed(cmd, stdout=PING_OUTPUT)
            return _completed(cmd, stdout="SSH_OK\nSUDO_OK\n")

        with patch("subprocess.run", side_effect=fake_run):
            result = tools.discover_all_hosts()

        assert result["total_hosts"] == 2
        assert (result["alive"], result["ssh_accessible"], result["sudo_passwordless"]) == (2, 2, 2)
        assert result["hosts_with_issues"] == 0
        assert [h["name"] for h in result["inventory"]] == ["web1", "db1"]
        assert result["inventory"][0]["role"] == "web"
        assert result["inventory"][0]["monitoring"] == ["prometheus"]

    def test_missing_role_and_monitoring_default(self, tools):
        with patch("subprocess.run", return_value=_completed(["x"], returncode=1)):
            inventory = tools.discover_all_hosts()["inventory"]

        db1 = next(h for h in inventory if h["name"] == "db1")
        assert db1["role"] == "unknown"
        assert db1["monitoring"] == []

    def test_each_failed_probe_adds_an_issue(self, tools):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ping":
                return _completed(cmd, returncode=1)
            return _completed(cmd, returncode=255, stderr="denied")

        with patch("subprocess.run", side_effect=fake_run):
            result = tools.discover_all_hosts()

        assert result["hosts_with_issues"] == 2
        assert result["inventory"][0]["issues"] == [
            "Host not responding to ping",
            "SSH not accessible",
            "Sudo requires password",
        ]

    def test_alive_but_no_ssh(self, tools):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ping":
                return _completed(cmd, stdout=PING_OUTPUT)
            return _completed(cmd, returncode=255)

        with patch("subprocess.run", side_effect=fake_run):
            result = tools.discover_all_hosts()

        assert result["alive"] == 2
        assert result["ssh_accessible"] == 0
        assert result["inventory"][0]["issues"] == ["SSH not accessible", "Sudo requires password"]

    def test_empty_config(self):
        result = DiscoveryTools({}).discover_all_hosts()
        assert result["total_hosts"] == 0
        assert result["inventory"] == []


class TestSchemas:
    def test_covers_every_tool_method(self, tools):
        names = [schema["name"] for schema in tools.get_schemas()]
        assert names == ["ping_host", "verify_ssh", "verify_sudo", "discover_all_hosts"]
        for schema in tools.get_schemas():
            assert schema["parameters"]["type"] == "object"

    def test_ping_schema_lists_known_hosts(self, tools):
        ping = tools.get_schemas()[0]
        description = ping["parameters"]["properties"]["host"]["description"]
        assert "web1" in description and "db1" in description
        assert ping["parameters"]["required"] == ["host"]
