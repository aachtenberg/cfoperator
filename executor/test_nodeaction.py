"""Tests for the node-action execution path — the safety gate is the focus."""

import json
from unittest.mock import patch

import pytest

import entrypoint
import nodeaction
from entrypoint import run
from nodeaction import (
    SSHError, parse_command_plan, validate_command, validate_plan,
)


# ---- the safety gate ---------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "chmod 600 /root/.ssh/config",
    "chown root:root /root/.ssh/config",
    "sudo -n chmod 0600 /root/.ssh/config",
    "systemctl restart sshd",
    "systemctl reload nginx",
    "ln -s /etc/foo /etc/bar",
    "mkdir -p /etc/cfop",
])
def test_validate_command_allows_safe(cmd):
    ok, reason = validate_command(cmd)
    assert ok, reason


@pytest.mark.parametrize("cmd,needle", [
    ("rm -rf /root/.ssh", "denied"),
    ("dd if=/dev/zero of=/dev/sda", "denied"),
    ("reboot", "denied"),
    ("chmod 600 /a && rm -rf /b", "metacharacter"),
    ("chmod 600 /a; reboot", "metacharacter"),
    ("chmod 600 /a | tee /b", "metacharacter"),
    ("chmod 600 $(cat /etc/x)", "metacharacter"),
    ("chmod 600 /etc/ssh/*", "metacharacter"),
    ("cat /etc/shadow", "allowlist"),
    ("systemctl stop sshd", "verb not allowed"),
    ("systemctl mask sshd", "verb not allowed"),
    ("sudo chmod 600 /a", "sudo -n"),
    ("sudo -S chmod 600 /a", "sudo -n"),
    ("", "empty"),
])
def test_validate_command_refuses_unsafe(cmd, needle):
    ok, reason = validate_command(cmd)
    assert not ok
    assert needle in reason


def test_validate_plan_rejects_too_many():
    ok, reason = validate_plan(["chmod 600 /a"] * 5)
    assert not ok and "too many" in reason


def test_validate_plan_rejects_empty():
    ok, reason = validate_plan([])
    assert not ok and "no commands" in reason


def test_validate_plan_rejects_if_any_bad():
    ok, reason = validate_plan(["chmod 600 /a", "rm -rf /b"])
    assert not ok and "denied" in reason


# ---- plan parsing ------------------------------------------------------------

def test_parse_command_plan_fenced():
    reply = 'sure:\n```json\n{"host": "h1", "commands": ["chmod 600 /a"], "explanation": "x"}\n```'
    plan = parse_command_plan(reply)
    assert plan["host"] == "h1" and plan["commands"] == ["chmod 600 /a"]


def test_parse_command_plan_bare():
    plan = parse_command_plan('{"host": "", "commands": ["chmod 600 /a"]}')
    assert plan["commands"] == ["chmod 600 /a"]


@pytest.mark.parametrize("reply", [
    "no json here",
    '{"host": "h1"}',                       # commands missing
    '{"commands": "chmod 600 /a"}',         # commands not a list
    '{"commands": [1, 2]}',                 # commands not strings
    "{ not valid json",
])
def test_parse_command_plan_rejects(reply):
    assert parse_command_plan(reply) is None


# ---- end-to-end run() routing ------------------------------------------------

def _node_order(**payload_extra):
    payload = {"recommendation": "Fix ssh config perms",
               "target": {"host": "controller"}}
    payload.update(payload_extra)
    return {"id": 10, "remediation_class": "node-action", "risk": "low",
            "confidence": 0.95, "payload": payload}


def _env(order, **extra):
    env = {"CFOP_REMEDIATION_JSON": json.dumps(order), "CFOP_NODE_ACTION_ENABLED": "true"}
    env.update(extra)
    return env


class _FixedLLM:
    def __init__(self, reply): self.reply = reply
    def complete(self, prompt): return self.reply


def test_node_action_disabled_routes_to_human():
    env = _env(_node_order())
    env["CFOP_NODE_ACTION_ENABLED"] = "false"
    payload = run(env)
    assert payload["status"] == "needs-human" and "not enabled" in payload["detail"]


def test_node_action_unparseable_plan():
    with patch.object(entrypoint, "make_llm", return_value=_FixedLLM("nope")):
        payload = run(_env(_node_order()))
    assert payload["status"] == "needs-human" and "parseable" in payload["detail"]


def test_node_action_gate_rejects_dangerous_plan():
    reply = '{"host": "controller", "commands": ["rm -rf /root/.ssh"]}'
    with patch.object(entrypoint, "make_llm", return_value=_FixedLLM(reply)):
        payload = run(_env(_node_order()))
    assert payload["status"] == "needs-human" and "safety gate" in payload["detail"]
    assert payload["result"]["proposed_commands"] == ["rm -rf /root/.ssh"]


def test_node_action_success_marks_resolved():
    reply = ('{"host": "", "commands": ["sudo -n chmod 600 /root/.ssh/config", '
             '"sudo -n chown root:root /root/.ssh/config"], "explanation": "fix perms"}')
    runs = [{"command": "c1", "returncode": 0, "stdout": "", "stderr": ""},
            {"command": "c2", "returncode": 0, "stdout": "", "stderr": ""}]
    with patch.object(entrypoint, "make_llm", return_value=_FixedLLM(reply)), \
         patch.object(entrypoint, "run_ssh_plan", return_value=runs) as ssh:
        payload = run(_env(_node_order()))
    # host empty in plan + target -> falls through; target host 'controller' used.
    assert ssh.call_args[0][0] == "controller"
    assert payload["status"] == "resolved" and "2 command(s) on controller" in payload["detail"]


def test_node_action_command_failure_routes_to_human():
    reply = '{"host": "controller", "commands": ["sudo -n chmod 600 /root/.ssh/config"]}'
    runs = [{"command": "c1", "returncode": 1, "stdout": "", "stderr": "Operation not permitted"}]
    with patch.object(entrypoint, "make_llm", return_value=_FixedLLM(reply)), \
         patch.object(entrypoint, "run_ssh_plan", return_value=runs):
        payload = run(_env(_node_order()))
    assert payload["status"] == "needs-human" and "exited 1" in payload["detail"]


def test_node_action_no_host_routes_to_human():
    order = _node_order()
    order["payload"]["target"] = {}
    reply = '{"host": "", "commands": ["sudo -n chmod 600 /root/.ssh/config"]}'
    with patch.object(entrypoint, "make_llm", return_value=_FixedLLM(reply)):
        payload = run(_env(order))  # no CFOP_NODE_ACTION_HOST set
    assert payload["status"] == "needs-human" and "no target host" in payload["detail"]


def test_run_ssh_plan_raises_on_connect_failure():
    class _Proc:
        returncode = 255; stdout = ""; stderr = "Connection timed out"
    with patch.object(nodeaction.subprocess, "run", return_value=_Proc()):
        with pytest.raises(SSHError):
            nodeaction.run_ssh_plan("h", ["chmod 600 /a"], {})


def test_prepare_ssh_copies_keys_at_0600(tmp_path):
    secret = tmp_path / "secret"; secret.mkdir()
    (secret / "id_ed25519").write_text("PRIVATEKEY")
    (secret / ".hidden").write_text("skip me")
    ssh_dir = tmp_path / "dotssh"
    nodeaction.prepare_ssh(secret, ssh_dir)
    key = ssh_dir / "id_ed25519"
    assert key.read_text() == "PRIVATEKEY"
    assert (key.stat().st_mode & 0o777) == 0o600
    assert not (ssh_dir / ".hidden").exists()  # dot-entries skipped


def test_prepare_ssh_missing_dir_is_not_fatal(tmp_path):
    nodeaction.prepare_ssh(tmp_path / "nope", tmp_path / "dotssh")  # no raise
