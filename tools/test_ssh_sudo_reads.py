"""ssh_execute sends docker/crictl through ``sudo -n``, and a failed tool result
is not logged as a success (CFOP-437).

The forensics SSH user is kept out of the docker group (HOMELAB-28) and holds
a passwordless sudo allowlist of read subcommands instead. A model composing
``docker ps -a`` by hand got "permission denied while trying to connect to the
docker API", concluded it had no access to Docker, and told the operator so.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from tools import ToolRegistry
from tools.ssh import SSHTools, ssh_mutation_reason, with_sudo_for_socket_reads


@pytest.mark.parametrize("sent, ran", [
    ("docker ps -a", "sudo -n docker ps -a"),
    ("docker ps -a --filter name=square --format '{{.Names}}\t{{.Status}}'",
     "sudo -n docker ps -a --filter name=square --format '{{.Names}}\t{{.Status}}'"),
    ("crictl ps -a", "sudo -n crictl ps -a"),
    ("/usr/bin/docker images", "sudo -n /usr/bin/docker images"),
    ("  docker ps", "  sudo -n docker ps"),
    ("DOCKER_HOST=unix:///var/run/docker.sock docker ps", "DOCKER_HOST=unix:///var/run/docker.sock sudo -n docker ps"),
    # only the first simple command; the pipeline after it is the model's
    ("docker ps -a | grep square | wc -l", "sudo -n docker ps -a | grep square | wc -l"),
    ("docker ps -a && echo done", "sudo -n docker ps -a && echo done"),
])
def test_bare_docker_and_crictl_get_sudo(sent, ran):
    assert with_sudo_for_socket_reads(sent) == ran


@pytest.mark.parametrize("sent", [
    "sudo -n docker ps -a",
    "sudo docker ps",
    "doas docker ps",
    "ls -la /var/run/docker.sock",
    "systemctl status docker",
    "journalctl -u docker -n 20",
    "grep docker /etc/group",
    "echo x | docker ps",            # docker is not the first simple command
    "dockerd --version",             # not the docker client
    "",
])
def test_everything_else_is_left_alone(sent):
    assert with_sudo_for_socket_reads(sent) == sent


def test_execute_runs_the_rewritten_command_and_reports_it():
    tools = SSHTools({"box": {"address": "10.0.0.1", "ssh": {"user": "cfop-forensics"}}})
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="square-postgres\n", stderr=""))
    with patch("tools.ssh.subprocess.run", run):
        out = tools.execute("box", "docker ps -a --filter name=square")
    argv = run.call_args.args[0]
    assert argv[-1] == "sudo -n docker ps -a --filter name=square"
    assert argv[-2] == "cfop-forensics@10.0.0.1"
    assert out["success"] is True
    # The operator reading the result sees what actually ran.
    assert out["command"] == "sudo -n docker ps -a --filter name=square"


def test_the_rewrite_does_not_open_a_write():
    # The host refuses `sudo -n docker rm` (not on the sudoers allowlist), and
    # the mutation gate on a gated turn classifies it as a write regardless of
    # the sudo prefix, exactly as it classified the bare form.
    rewritten = with_sudo_for_socket_reads("docker rm square-postgres square-redis")
    assert rewritten == "sudo -n docker rm square-postgres square-redis"
    assert ssh_mutation_reason(rewritten)
    assert ssh_mutation_reason("docker rm square-postgres square-redis")
    assert ssh_mutation_reason("sudo -n docker ps -a") is None


def test_ssh_execute_description_states_the_contract():
    tools = SSHTools({"box": {"address": "10.0.0.1", "ssh": {"user": "cfop-forensics"}}})
    desc = next(s["description"] for s in tools.get_schemas() if s["name"] == "ssh_execute")
    assert "sudo -n" in desc and "docker" in desc and "unprivileged" in desc


def _registry():
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": {
        "box": {"address": "10.0.0.1", "ssh": {"user": "cfop-forensics"}}}}, "search": {}}
    return ToolRegistry(op)


def test_a_failed_tool_result_is_not_logged_as_success(caplog):
    reg = _registry()
    reg.tools["ssh_execute"]["function"] = lambda host, command, timeout=30: {
        "success": False, "stdout": "", "exit_code": 1, "host": host, "command": command,
        "stderr": "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock\n",
    }
    with caplog.at_level(logging.INFO, logger="cfoperator.tools"):
        out = reg.execute("ssh_execute", {"host": "box", "command": "docker ps"})
    assert out["success"] is False
    assert "completed successfully" not in caplog.text
    assert "Tool ssh_execute returned failure: permission denied" in caplog.text


def test_a_plain_data_result_still_logs_success(caplog):
    reg = _registry()
    reg.tools["ssh_execute"]["function"] = lambda host, command, timeout=30: {
        "success": True, "stdout": "ok\n", "stderr": "", "exit_code": 0}
    with caplog.at_level(logging.INFO, logger="cfoperator.tools"):
        reg.execute("ssh_execute", {"host": "box", "command": "uptime"})
    assert "Tool ssh_execute completed successfully" in caplog.text
