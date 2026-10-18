"""Host writes on a turn that may change things go to a confirmer (CFOP-442).

The class of regression these guard: a write-shaped SSH command on an admin
turn running unseen (as before, as the read identity) or being refused when
an admin is there to confirm it; and a turn that must not change things
(member, verify, unattended) suddenly being offered a way to.
"""

from unittest.mock import MagicMock

import pytest

from tools import UNATTENDED, ToolPolicy, ToolRegistry

HOSTS = {"box1": {"address": "10.9.8.7", "ssh": {"user": "cfop-forensics"}}}


def _registry():
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": dict(HOSTS)}, "search": {}}
    reg = ToolRegistry(op)
    ran = []

    def fake(name):
        def run(**kw):
            ran.append((name, kw))
            return {"success": True, "stdout": "ran", "exit_code": 0}
        return run

    for name in ("ssh_execute", "ssh_docker_restart", "ssh_restart_service"):
        reg.tools[name]["function"] = fake(name)
    return reg, ran


class _Confirmer:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"success": True, "exit_code": 0, "confirmed_by": "aachten"}

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.result


def _admin(confirmer=None):
    return ToolPolicy(actor_role="admin", confirm_host_write=confirmer)


def test_a_write_on_an_admin_turn_goes_to_the_confirmer_not_the_host():
    reg, ran = _registry()
    confirm = _Confirmer()
    out = reg.execute("ssh_execute", {"host": "box1", "command": "docker stop square-redis"},
                      policy=_admin(confirm))
    assert ran == []  # nothing ran as the read identity
    assert out == confirm.result
    (call,) = confirm.calls
    # The command is in its final form: what the admin is shown is what runs.
    assert call["command"] == "sudo -n docker stop square-redis"
    assert call["host"] == "box1" and call["tool"] == "ssh_execute"
    assert call["reason"]


def test_a_read_on_an_admin_turn_runs_directly_and_asks_nobody():
    reg, ran = _registry()
    confirm = _Confirmer()
    reg.execute("ssh_execute", {"host": "box1", "command": "docker ps -a"}, policy=_admin(confirm))
    assert confirm.calls == []
    assert ran == [("ssh_execute", {"host": "box1", "command": "docker ps -a"})]


@pytest.mark.parametrize("tool,args,command", [
    ("ssh_docker_restart", {"host": "box1", "container": "web; rm -rf /"},
     "sudo -n docker restart 'web; rm -rf /'"),
    ("ssh_restart_service", {"host": "box1", "service": "nginx"},
     "sudo -n systemctl restart nginx"),
])
def test_the_fixed_verb_tools_always_confirm_and_quote_their_argument(tool, args, command):
    """They used to run as the read identity, whose sudoers refuses both, so
    an admin was offered two tools that could not succeed."""
    reg, ran = _registry()
    confirm = _Confirmer()
    reg.execute(tool, args, policy=_admin(confirm))
    assert ran == []
    assert confirm.calls[0]["command"] == command


def test_an_admin_turn_nobody_can_confirm_is_refused_with_the_command():
    """A remediate token is an admin with nobody at a console. Its write is
    neither run unseen nor silently dropped: it is refused, naming what would
    have run, so the operator behind the token can do it themselves."""
    reg, ran = _registry()
    out = reg.execute("ssh_execute", {"host": "box1", "command": "systemctl stop nginx"},
                      policy=_admin(None))
    assert ran == []
    assert out["refused"] is True
    assert "systemctl stop nginx" in out["error"] and "API token" in out["error"]


def test_an_unknown_host_is_an_error_before_anyone_is_asked():
    reg, ran = _registry()
    confirm = _Confirmer()
    out = reg.execute("ssh_execute", {"host": "nope", "command": "reboot"}, policy=_admin(confirm))
    assert confirm.calls == [] and ran == []
    assert "Unknown host" in out["error"]


def test_a_member_turn_is_still_refused_whatever_it_carries():
    """The role gate comes first. A confirmer on a member's policy changes
    nothing: a member is refused the tool outright."""
    reg, ran = _registry()
    confirm = _Confirmer()
    out = reg.execute("ssh_execute", {"host": "box1", "command": "docker stop x"},
                      policy=ToolPolicy(actor_role="member", confirm_host_write=confirm))
    assert confirm.calls == [] and ran == []
    assert out["refused"] is True


def test_a_verify_turn_still_refuses_the_write():
    reg, ran = _registry()
    confirm = _Confirmer()
    out = reg.execute("ssh_execute", {"host": "box1", "command": "docker stop x"},
                      policy=ToolPolicy(actor_role="admin", verify_only=True,
                                        confirm_host_write=confirm))
    assert confirm.calls == [] and ran == []
    assert out["refused"] is True


def test_an_unattended_run_cannot_carry_a_confirmer():
    with pytest.raises(ValueError):
        ToolPolicy(unattended=True, confirm_host_write=_Confirmer())
    reg, ran = _registry()
    out = reg.execute("ssh_execute", {"host": "box1", "command": "docker stop x"}, policy=UNATTENDED)
    assert out["refused"] is True and ran == []


def test_the_confirmer_is_not_part_of_policy_equality():
    assert _admin(_Confirmer()) == _admin(None)


def test_an_internal_caller_with_no_policy_is_unchanged():
    reg, ran = _registry()
    reg.execute("ssh_execute", {"host": "box1", "command": "docker stop x"})
    assert ran == [("ssh_execute", {"host": "box1", "command": "docker stop x"})]
