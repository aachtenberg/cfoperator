"""The chat tool surface honours who is asking and what the turn is for (CFOP-124).

Every mutating tool in the registry was reachable by any logged-in member
through chat, and by any drawer hand-off that asked for a "check" — the
ROLE_ADMIN decorators gated the console's buttons, not the capability. These
pin the registry boundary: mutating tools are marked next to their own
definition, a policy withholds them from the model and refuses them on
execution, and k8s_exec_pod never opens a shell in the agent's own datastore,
whoever asks and whatever that datastore is called.
"""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools as tools_module
from tools import ToolPolicy, ToolRegistry, _service_from_host
from tools.ssh import ssh_mutation_reason
from tools.github import GitHubTools
from tools.k8s import K8sTools
from tools.ssh import SSHTools

HOSTS = {"box1": {"address": "10.9.8.7", "user": "ops"}}

# Tools that change the system, or what it will act on. A name here the
# registry does not mark fails; a tool whose name is write-shaped and is not
# marked fails the pattern test, so the next write tool cannot land open.
KNOWN_MUTATING = {
    "ssh_execute", "ssh_restart_service", "ssh_docker_restart",
    "k8s_rollout_restart", "k8s_exec_pod",
    "update_sweep_finding", "resolve_remediation", "store_learning",
    "triage_investigation", "queue_gitops_patch",
}
# Mutating AND needing a named admin behind the turn: an internal caller
# (policy=None) is refused these, unlike every other mutating tool. See
# _SCHEMA_MARKERS in tools/__init__.py for why the two are not the same gate.
KNOWN_HUMAN_ONLY = {"queue_gitops_patch"}
# 'triage_' is in the pattern because triage_investigation is the first write
# tool whose name carries none of the other verbs — an unmarked one would have
# landed open (CFOP-138).
# 'queue_' joins them for queue_gitops_patch (CFOP-160): it enqueues work an
# executor will run, which is a write even though nothing changes at the
# moment it returns.
WRITE_SHAPED = re.compile(
    r"restart|_exec|create_|resolve_|update_|store_|delete_|patch|apply_|triage_|queue_")

MEMBER = ToolPolicy(actor_role="member")
ADMIN = ToolPolicy(actor_role="admin")
VERIFY = ToolPolicy(actor_role="admin", verify_only=True)


def _registry(config_extra=None):
    op = MagicMock()
    op.config = {"infrastructure": {"hosts": dict(HOSTS)}, "search": {}}
    if config_extra:
        op.config.update(config_extra)
    return op, ToolRegistry(op)


def _names(schemas):
    return {s["function"]["name"] for s in schemas}


# --------------------------------------------------------------------------
# the marker lives next to the tool
# --------------------------------------------------------------------------

def test_known_mutating_tools_are_marked():
    _, reg = _registry()
    for name in KNOWN_MUTATING:
        assert name in reg.tools, f"{name} is not registered"
        assert reg.is_mutating(name), f"{name} is not marked mutating"


def test_a_write_shaped_tool_cannot_land_unmarked():
    _, reg = _registry()
    unmarked = [n for n in reg.tools if WRITE_SHAPED.search(n) and not reg.is_mutating(n)]
    assert not unmarked, f"write-shaped tools registered without 'mutating': {unmarked}"


def test_reads_are_not_marked():
    _, reg = _registry()
    for name in ("k8s_get_pods", "k8s_rollout_status", "ssh_check_service", "ssh_get_logs",
                 "ssh_list_services", "find_learnings", "ping_host", "verify_sudo",
                 "get_remediation", "list_remediations"):
        assert name in reg.tools and not reg.is_mutating(name), name


def test_the_marker_has_one_home_and_the_wrong_home_fails_registration():
    """Family schemas and inline tools both carry 'mutating' INSIDE the schema.
    An entry-level key reads as False through is_mutating — a write tool left
    open, the defect this issue is about — so it stops registration instead."""
    _, reg = _registry()
    for name in KNOWN_MUTATING:
        assert reg.tools[name]["schema"].get("mutating") is True, f"{name} marks the wrong home"
        assert "mutating" not in reg.tools[name], f"{name} still marks the entry"
    reg.tools["invented_tool"] = {"function": lambda: None, "mutating": True,
                                  "schema": {"name": "invented_tool"}}
    with pytest.raises(ValueError, match="belong inside the tool's schema"):
        reg._check_marker_placement()
    del reg.tools["invented_tool"]
    reg.tools["invented_tool"] = {"function": lambda: None, "human_only": True,
                                  "schema": {"name": "invented_tool"}}
    with pytest.raises(ValueError, match="belong inside the tool's schema"):
        reg._check_marker_placement()


def test_family_schemas_carry_the_marker_and_the_model_never_sees_it():
    by_name = {s["name"]: s for s in SSHTools(HOSTS).get_schemas()}
    assert all(by_name[n].get("mutating") for n in ("ssh_execute", "ssh_restart_service", "ssh_docker_restart"))
    assert not by_name["ssh_check_service"].get("mutating")
    by_name = {s["name"]: s for s in K8sTools().get_schemas()}
    assert all(by_name[n].get("mutating") for n in ("k8s_rollout_restart", "k8s_exec_pod"))
    assert not by_name["k8s_rollout_status"].get("mutating")
    gh = GitHubTools.__new__(GitHubTools)
    gh.repos = {}
    by_name = {s["name"]: s for s in gh.get_schemas()}
    assert all(by_name[n].get("mutating") for n in ("github_create_pr", "github_create_issue_comment"))
    assert not by_name["github_get_pr"].get("mutating")
    # Stripped on the way into the registry: an OpenAI-shaped function schema
    # with a stray key is a 400 on the stricter providers.
    _, reg = _registry()
    assert not any("mutating" in s["function"] for s in reg.get_schemas())


# --------------------------------------------------------------------------
# both layers: what the model is offered, and what runs if it asks anyway
# --------------------------------------------------------------------------

def test_a_member_is_offered_reads_only():
    _, reg = _registry()
    offered = _names(reg.get_schemas(policy=MEMBER))
    assert {"k8s_get_pods", "ssh_check_service", "find_learnings", "ping_host", "get_remediation"} <= offered
    assert not (offered & KNOWN_MUTATING)


def test_admin_and_internal_callers_are_unchanged():
    """Unchanged for everything except the human_only tools, which an internal
    caller never had."""
    _, reg = _registry()
    everything = _names(reg.get_schemas())
    assert (KNOWN_MUTATING - KNOWN_HUMAN_ONLY) <= everything
    assert _names(reg.get_schemas(policy=ADMIN)) == everything | KNOWN_HUMAN_ONLY
    assert _names(reg.get_schemas(policy=None)) == everything


# --------------------------------------------------------------------------
# human_only: mutating is not the same question as "did a person ask"
# --------------------------------------------------------------------------

def test_a_human_only_tool_is_withheld_from_internal_callers():
    """policy=None is the sweep, the investigation and the morning summary.
    They are trusted to restart a service, and are still not a person — a tool
    that treats its caller's request AS the human approval must not be
    reachable from one (CFOP-160, caught in review)."""
    _, reg = _registry()
    for name in KNOWN_HUMAN_ONLY:
        assert name not in _names(reg.get_schemas(policy=None)), name
        assert name not in _names(reg.get_schemas())


def test_a_human_only_tool_is_offered_to_a_named_admin():
    _, reg = _registry()
    assert KNOWN_HUMAN_ONLY <= _names(reg.get_schemas(policy=ADMIN))


@pytest.mark.parametrize("policy", [None, MEMBER, VERIFY], ids=["internal", "member", "verify"])
def test_a_human_only_tool_named_anyway_is_refused_before_it_runs(policy):
    op, reg = _registry()
    for name in KNOWN_HUMAN_ONLY:
        out = reg.execute(name, {"recommendation": "x" * 60}, policy=policy)
        assert out.get("refused") is True, out
        op.kb.queue_remediation.assert_not_called()


def test_a_member_naming_a_mutating_tool_is_refused_before_it_runs():
    _, reg = _registry()
    run = MagicMock()
    reg.tools["ssh_execute"]["function"] = run
    out = reg.execute("ssh_execute", {"host": "box1", "command": "reboot"}, policy=MEMBER)
    assert out["refused"] is True and "needs an admin" in out["error"]
    run.assert_not_called()


def test_a_verification_turn_refuses_with_its_own_message():
    _, reg = _registry()
    run = MagicMock()
    reg.tools["ssh_restart_service"]["function"] = run
    # ssh_execute is the one documented exception (it is command-gated instead,
    # so a verification turn can still run its checks); everything else goes.
    assert (_names(reg.get_schemas(policy=VERIFY)) & KNOWN_MUTATING) == {"ssh_execute"}
    out = reg.execute("ssh_restart_service", {"host": "box1", "service": "x"}, policy=VERIFY)
    assert out["refused"] is True and "verification pass" in out["error"]
    run.assert_not_called()


def test_a_read_runs_under_a_restrictive_policy():
    _, reg = _registry()
    reg.tools["k8s_get_pods"]["function"] = lambda **kw: {"pods": []}
    assert reg.execute("k8s_get_pods", {}, policy=MEMBER) == {"pods": []}
    assert reg.execute("k8s_get_pods", {}, policy=VERIFY) == {"pods": []}


def test_an_admin_runs_a_mutating_tool():
    _, reg = _registry()
    reg.tools["ssh_execute"]["function"] = lambda **kw: {"stdout": "ok"}
    assert reg.execute("ssh_execute", {"host": "box1", "command": "id"}, policy=ADMIN) == {"stdout": "ok"}
    assert reg.execute("ssh_execute", {"host": "box1", "command": "id"}) == {"stdout": "ok"}


# --------------------------------------------------------------------------
# k8s_exec_pod and the agent's own datastore — derived, never named
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host, expected", [
    ("kb-db.aweoriujwoedf.svc.cluster.local", ("kb-db", "aweoriujwoedf", False)),
    ("kb-db.aweoriujwoedf.svc", ("kb-db", "aweoriujwoedf", False)),
    # Two labels are ambiguous until the cluster is asked — see the class below.
    ("kb-db.aweoriujwoedf", ("kb-db", "aweoriujwoedf", True)),
    ("timescale.local", ("timescale", "local", True)),
    ("kb-db.lan", ("kb-db", "lan", True)),
    ("KB-DB.Aweoriujwoedf.svc.cluster.local.", ("kb-db", "aweoriujwoedf", False)),
    ("kb-db.aweoriujwoedf:5432", ("kb-db", "aweoriujwoedf", True)),
    ("kb-db", ("kb-db", None, False)),
    ("10.0.0.5", None),
    ("::1", None),
    ("[::1]:5432", None),
    ("localhost", None),
    ("db.example.com", None),
    ("", None),
    (None, None),
])
def test_service_from_host(host, expected):
    assert _service_from_host(host) == expected


class TestExecPodRefusesTheDatastore:
    """Session 21 (2026-08-28) resolved two queue rows with psql inside the
    knowledge-base pod, reached through k8s_exec_pod. The protected set is
    derived from the connections the agent opens — nothing here knows what
    the database is called or where it lives."""

    def _reg(self, monkeypatch, db_url=None, ts_host=None, extra=None, own_ns=None,
             namespaces=("aweoriujwoedf", "joeblowxxxx"), ns_fails=False):
        monkeypatch.delenv("POD_NAMESPACE", raising=False)
        monkeypatch.delenv("CFOP_NAMESPACE", raising=False)
        monkeypatch.setattr(tools_module, "_NAMESPACE_FILE", "/nonexistent/serviceaccount/namespace")
        if own_ns:
            monkeypatch.setenv("POD_NAMESPACE", own_ns)
        op, reg = _registry({"chat": {"protected_exec_hosts": list(extra or [])}})
        op.kb = SimpleNamespace(db_url=db_url) if db_url else SimpleNamespace()
        reg.timescale_tools = SimpleNamespace(host=ts_host) if ts_host else None
        calls = []
        self.ns_calls = []

        def get_namespaces():
            self.ns_calls.append(1)
            if ns_fails:
                raise RuntimeError("kubectl unavailable")
            return {"success": True, "namespaces": [{"name": n} for n in namespaces]}
        reg.k8s_tools = SimpleNamespace(
            exec_pod=lambda **kw: calls.append(kw) or {"success": True},
            get_namespaces=get_namespaces)
        return reg, calls

    @staticmethod
    def _exec(reg, ns, pod, policy=None):
        return reg.execute("k8s_exec_pod", {"namespace": ns, "pod_name": pod, "command": "id"}, policy=policy)

    @pytest.mark.parametrize("pod", ["kb-db-0", "kb-db-12", "kb-db-7f9c6b4d8-x2k9p", "kb-db"])
    def test_pods_backing_the_kb_service_are_refused(self, monkeypatch, pod):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc.cluster.local:5432/kb")
        out = self._exec(reg, "aweoriujwoedf", pod)
        assert out["refused"] is True and "knowledge base" in out["error"] and calls == []

    def test_other_pods_and_other_namespaces_still_work(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc.cluster.local:5432/kb")
        assert self._exec(reg, "aweoriujwoedf", "web-0")["success"] is True
        assert self._exec(reg, "joeblowxxxx", "kb-db-0")["success"] is True
        assert self._exec(reg, "aweoriujwoedf", "kb-database-0")["success"] is True  # not <service>-
        assert len(calls) == 3

    @pytest.mark.parametrize("pod", ["kb-database-0", "kb-cache", "kb-db-migrations", "kb-db-backup-nightly"])
    def test_a_prefix_alone_is_not_a_match(self, monkeypatch, pod):
        """`startswith(service + '-')` over-matches: with service `kb`, the
        unrelated `kb-database-0` would be refused. Only a controller-shaped
        suffix — StatefulSet ordinal, ReplicaSet hash + pod suffix — counts."""
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb.aweoriujwoedf.svc:5432/kb")
        assert self._exec(reg, "aweoriujwoedf", pod)["success"] is True
        assert len(calls) == 1

    def test_it_holds_for_admins_too(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc:5432/kb")
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0", policy=ADMIN)["refused"] is True
        assert calls == []

    def test_a_bare_service_name_resolves_in_the_agents_own_namespace(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db:5432/kb", own_ns="aweoriujwoedf")
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0")["refused"] is True
        assert self._exec(reg, "joeblowxxxx", "kb-db-0")["success"] is True

    def test_an_omitted_namespace_resolves_the_way_kubectl_would(self, monkeypatch):
        # kubectl exec with no -n uses the context's default namespace, which
        # in-cluster is the agent's own. Treating "" as a wildcard refused the
        # pod name in every namespace, including ones it could never reach.
        reg, calls = self._reg(monkeypatch,
                               db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc:5432/kb",
                               own_ns="joeblowxxxx")
        assert self._exec(reg, "", "kb-db-0")["success"] is True
        reg2, _ = self._reg(monkeypatch,
                            db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc:5432/kb",
                            own_ns="aweoriujwoedf")
        assert self._exec(reg2, "", "kb-db-0")["refused"] is True

    def test_a_bare_service_name_with_no_namespace_known_is_protected_everywhere(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db:5432/kb")
        assert self._exec(reg, "joeblowxxxx", "kb-db-0")["refused"] is True
        assert self._exec(reg, "joeblowxxxx", "web-0")["success"] is True

    @pytest.mark.parametrize("db_url", [
        "postgresql://u:p@10.0.0.5:5432/kb",
        "postgresql://u:p@localhost:5432/kb",
        "postgresql://u:p@db.example.com:5432/kb",
    ])
    def test_a_database_outside_the_cluster_is_a_no_op(self, monkeypatch, db_url):
        # Compose and trial installs: nothing exec_pod could reach, so nothing
        # is protected and nothing errors.
        reg, calls = self._reg(monkeypatch, db_url=db_url)
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0")["success"] is True
        assert len(calls) == 1

    def test_no_knowledge_base_at_all_is_a_no_op(self, monkeypatch):
        reg, calls = self._reg(monkeypatch)
        assert self._exec(reg, "aweoriujwoedf", "anything-0")["success"] is True

    def test_the_timescale_host_is_protected_the_same_way(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, ts_host="tsdb.aweoriujwoedf.svc.cluster.local")
        out = self._exec(reg, "aweoriujwoedf", "tsdb-0")
        assert out["refused"] is True and "TimescaleDB" in out["error"]

    def test_a_two_label_host_is_cluster_dns_when_that_namespace_exists(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf:5432/kb")
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0")["refused"] is True
        assert self._exec(reg, "joeblowxxxx", "kb-db-0")["success"] is True

    def test_a_two_label_lan_name_is_not_cluster_dns(self, monkeypatch):
        # `timescale.local` parses exactly like `svc.ns`, and treating it as one
        # protected a fictional `local` namespace while leaving the real pod
        # open. Only the cluster can tell them apart.
        reg, calls = self._reg(monkeypatch, ts_host="timescale.local")
        assert self._exec(reg, "local", "timescale-0")["success"] is True
        assert self._exec(reg, "aweoriujwoedf", "timescale-0")["success"] is True

    def test_the_namespace_lookup_is_cached(self, monkeypatch):
        reg, _ = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf:5432/kb")
        for _ in range(4):
            self._exec(reg, "aweoriujwoedf", "kb-db-0")
        assert len(self.ns_calls) == 1, "the namespace list is re-fetched on every exec"

    def test_an_unanswerable_namespace_lookup_keeps_the_target(self, monkeypatch):
        # Fail closed: an unnecessary refusal is recoverable, a datastore left
        # open is the hole this exists to close.
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf:5432/kb",
                               ns_fails=True)
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0")["refused"] is True

    def test_config_can_add_a_target_but_never_defines_the_default(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, extra=["metrics-db.joeblowxxxx"])
        out = self._exec(reg, "joeblowxxxx", "metrics-db-0")
        assert out["refused"] is True and "chat.protected_exec_hosts" in out["error"]
        assert self._exec(reg, "joeblowxxxx", "web-0")["success"] is True


# --------------------------------------------------------------------------
# a verification turn keeps ssh_execute, and classifies what it sends
# --------------------------------------------------------------------------

# The checks session 23 actually needed, verbatim from the agent log.
VERIFY_READS = [
    "mountpoint /mnt/router-share || df -h /mnt/router-share; systemctl status 'mnt-router\\x2dshare.mount'",
    "journalctl -u 'mnt-router\\x2dshare.mount' -n 15",
    "sudo dmesg | grep -i cifs | tail -n 10",
    "nc -zv -w 2 192.168.0.1 21 22 80 139 445 8080 2>&1",
    "grep -i router-share /etc/fstab || true",
    "timeout 5 bash -c 'echo > /dev/tcp/192.168.0.1/445'",
    "systemctl list-units --type=service --state=running --no-pager --no-legend",
    "df -h /mnt/nas-backup /mnt/hdd-pictures",
    "cat /etc/fstab",
    "ps aux | grep cfoperator",
    "iptables -L -n",
    "mount -l | grep cifs",
    "curl -s http://localhost:9100/metrics",
    "git -C /home/x/homelab-infra log -1 --oneline",
    "kubectl get pods -n data",
]

VERIFY_WRITES = [
    ("sudo systemctl restart 'mnt-router\\x2dshare.mount'", "systemctl restart"),
    ("grep router-share /etc/fstab; sudo systemctl restart x.mount", "systemctl restart"),
    ("sudo sed -i '/mnt\\/router-share/d' /etc/fstab", "sed -i"),
    ("echo x > /etc/fstab", "redirected"),
    ("cat a >> /etc/fstab", "redirected"),
    ("sudo systemctl daemon-reload", "systemctl daemon-reload"),
    ("bash -c \"systemctl restart nginx\"", "systemctl restart"),
    ("docker restart immich", "docker restart"),
    ("rm -rf /tmp/x", "changes the filesystem"),
    ("sudo -n reboot", "takes the host down"),
    ("sudo umount /mnt/router-share", "unmounts"),
    ("apt-get install -y jq", "installed packages"),
    ("sudo tee /etc/fstab", "tee writes"),
    ("kubectl exec kb-db-0 -n data -- psql", "changes cluster state"),
    ("find /tmp -name x -delete", "changes files"),
    ("curl -X POST http://x/admin", "writes or sends data"),
    ("df -h; sudo systemctl stop nginx", "systemctl stop"),
    ("systemctl restart x", "systemctl restart"),
]


@pytest.mark.parametrize("command", VERIFY_READS)
def test_the_classifier_lets_a_read_through(command):
    assert ssh_mutation_reason(command) is None, command


@pytest.mark.parametrize("command, fragment", VERIFY_WRITES)
def test_the_classifier_names_why_a_write_is_refused(command, fragment):
    reason = ssh_mutation_reason(command)
    assert reason and fragment in reason, (command, reason)


def test_a_verification_turn_can_still_run_the_checks():
    """Withholding ssh_execute outright left a verification pass unable to
    verify — the sweep's own checks are ssh one-liners. The tool stays; the
    command is classified."""
    _, reg = _registry()
    assert "ssh_execute" in _names(reg.get_schemas(policy=VERIFY))
    ran = []
    reg.tools["ssh_execute"]["function"] = lambda **kw: ran.append(kw) or {"stdout": "active"}
    out = reg.execute("ssh_execute", {"host": "box1", "command": "systemctl status x"}, policy=VERIFY)
    assert out == {"stdout": "active"} and len(ran) == 1


def test_a_verification_turn_refuses_a_command_that_writes():
    _, reg = _registry()
    ran = []
    reg.tools["ssh_execute"]["function"] = lambda **kw: ran.append(kw) or {"stdout": ""}
    out = reg.execute("ssh_execute", {"host": "box1", "command": "sudo systemctl restart x"},
                      policy=VERIFY)
    assert out["refused"] is True and "systemctl restart" in out["error"]
    assert ran == []


def test_the_other_ssh_writers_are_still_withheld_on_a_verification_turn():
    # Only the command-gated tool comes back; restart_service has no read form.
    _, reg = _registry()
    offered = _names(reg.get_schemas(policy=VERIFY))
    assert "ssh_execute" in offered
    for name in ("ssh_restart_service", "ssh_docker_restart", "k8s_exec_pod",
                 "k8s_rollout_restart", "resolve_remediation", "store_learning"):
        assert name not in offered


def test_a_member_never_gets_ssh_execute_even_to_verify():
    """The role gate is stricter than the turn's purpose and is not softened
    by it: command-gating is for an admin verifying, not for a member."""
    _, reg = _registry()
    member_verify = ToolPolicy(actor_role="member", verify_only=True)
    assert "ssh_execute" not in _names(reg.get_schemas(policy=member_verify))
    ran = []
    reg.tools["ssh_execute"]["function"] = lambda **kw: ran.append(kw)
    out = reg.execute("ssh_execute", {"host": "box1", "command": "systemctl status x"},
                      policy=member_verify)
    assert out["refused"] is True and "needs an admin" in out["error"]
    assert ran == []
