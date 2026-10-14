"""DiscoveryTools: host reachability probes and the inventory rollup.

subprocess.run is patched everywhere — no pings, no SSH. What matters is the
shape of the dicts the LLM sees (a probe failure must stay a structured result,
never an exception) and the commands built from host config.
"""

import subprocess
from unittest.mock import patch

import pytest

from tools.discovery import _SUDO_CHECKS, DiscoveryTools


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

        assert run.call_args.args[0] == ["ping", "-c", "3", "-i", "0.2", "-W", "2", "10.0.0.1"]
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
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout="SUDO_OK\nSUDO_READS_OK\n")) as run:
            result = tools.verify_sudo("web1")

        assert run.call_args.args[0][-1] == _SUDO_CHECKS
        assert result["sudo_passwordless"] is True
        assert result["sudo_reads"] is True
        assert result["needs_password"] is False
        assert result["message"] == "SUDO_OK"

    def test_forensics_identity_reads_only(self, tools):
        """CFOP-391: the agent's identity is narrow by design -- no full sudo,
        the read allowlist works. Both are reported as they are."""
        stdout = "sudo: a password is required\nSUDO_READS_OK\n"
        with patch("subprocess.run", return_value=_completed(["ssh"], stdout=stdout)):
            result = tools.verify_sudo("web1")
        assert (result["sudo_passwordless"], result["sudo_reads"]) == (False, True)

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
            return _completed(cmd, stdout="SSH_OK\nSUDO_OK\nSUDO_READS_OK\n")

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
        # No sudo verdict without an SSH session: "SSH not accessible" says it.
        assert result["inventory"][0]["issues"] == [
            "Host not responding to ping",
            "SSH not accessible",
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
        assert result["inventory"][0]["issues"] == ["SSH not accessible"]

    def test_empty_config(self):
        result = DiscoveryTools({}).discover_all_hosts()
        assert result["total_hosts"] == 0
        assert result["inventory"] == []

    # --- CFOP-391: ~20s for seven healthy hosts, every sweep ----------------

    def _fleet(self, n):
        return DiscoveryTools({f"h{i}": {"address": f"10.0.1.{i}", "ssh": {"user": "ops"}}
                               for i in range(n)})

    def test_hosts_are_checked_at_once_not_one_after_another(self):
        """Each probe sleeps 0.3s; sequentially eight hosts would take
        8 x (ping + ssh) = 4.8s. At once, the slowest host's 0.6s."""
        import time

        def slow_run(cmd, **kwargs):
            time.sleep(0.3)
            out = PING_OUTPUT if cmd[0] == "ping" else "SSH_OK\nSUDO_READS_OK\n"
            return _completed(cmd, stdout=out)

        tools = self._fleet(8)
        with patch("subprocess.run", side_effect=slow_run):
            t0 = time.time()
            result = tools.discover_all_hosts()
            elapsed = time.time() - t0
        assert result["alive"] == 8 and result["ssh_accessible"] == 8
        assert elapsed < 2.0, f"took {elapsed:.1f}s: hosts are being checked one after another"

    def test_one_ssh_connection_per_host_answers_both_questions(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            out = PING_OUTPUT if cmd[0] == "ping" else "SSH_OK\nsudo: a password is required\nSUDO_READS_OK\n"
            return _completed(cmd, stdout=out)

        with patch("subprocess.run", side_effect=fake_run):
            result = self._fleet(3).discover_all_hosts()
        ssh_calls = [c for c in calls if c[0] == "ssh"]
        assert len(ssh_calls) == 3, "one SSH connection per host, not one per question"
        assert all(c[-1] == "echo SSH_OK; " + _SUDO_CHECKS for c in ssh_calls)
        row = result["inventory"][0]
        assert (row["ssh_accessible"], row["sudo_passwordless"], row["sudo_reads"]) == (True, False, True)

    def test_a_dead_host_does_not_stall_the_others_and_order_is_kept(self):
        """A dead host costs ping timeouts plus an SSH connect timeout; the
        healthy hosts must not wait behind it, and the inventory stays in
        config order whatever finishes first."""
        import time

        def run(cmd, **kwargs):
            dead = cmd[-1] == "10.0.1.0" or "@10.0.1.0" in " ".join(cmd)
            if dead:
                time.sleep(1.0)
                raise subprocess.TimeoutExpired(cmd=cmd[0], timeout=10)
            return _completed(cmd, stdout=PING_OUTPUT if cmd[0] == "ping" else "SSH_OK\nSUDO_READS_OK\n")

        tools = self._fleet(5)
        with patch("subprocess.run", side_effect=run):
            t0 = time.time()
            result = tools.discover_all_hosts()
            elapsed = time.time() - t0
        assert [h["name"] for h in result["inventory"]] == ["h0", "h1", "h2", "h3", "h4"]
        assert result["inventory"][0]["issues"] == [
            "Host not responding to ping", "SSH not accessible"]
        assert result["hosts_with_issues"] == 1
        assert elapsed < 3.0, f"took {elapsed:.1f}s"


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

class TestSudoCheck:
    """The agent's SSH identity is narrow by design (homelab-infra HOMELAB-28):
    no full sudo, NOPASSWD for an allowlist of reads. CFOP-391."""

    def _probe_fleet(self, ssh_stdout):
        def fake_run(cmd, **kwargs):
            return _completed(cmd, stdout=PING_OUTPUT if cmd[0] == "ping" else ssh_stdout)
        tools = DiscoveryTools({f"h{i}": {"address": f"10.0.2.{i}", "ssh": {"user": "cfop-forensics"}}
                                for i in range(2)})
        with patch("subprocess.run", side_effect=fake_run):
            return tools.discover_all_hosts()

    def test_no_full_sudo_is_not_an_issue_for_the_forensics_identity(self):
        """Every host's real state: full sudo refused, the read allowlist
        works. Seven false issues per sweep until this."""
        result = self._probe_fleet("SSH_OK\nsudo: a password is required\nSUDO_READS_OK\n")
        assert result["hosts_with_issues"] == 0
        assert (result["sudo_passwordless"], result["sudo_reads"]) == (0, 2)
        assert all(h["issues"] == [] for h in result["inventory"])

    def test_a_missing_read_allowlist_is_an_issue(self):
        """The drop-in gone or broken: investigations' sudo reads fail."""
        result = self._probe_fleet("SSH_OK\nsudo: a password is required\n")
        assert result["hosts_with_issues"] == 2
        assert result["inventory"][0]["issues"] == [
            "Read-only sudo allowlist unavailable (sudo -n -l /usr/bin/dmesg -T refused)"]
