"""The chat tool surface honours who is asking and what the turn is for (CFOP-124).

Every mutating tool in the registry was reachable by any logged-in member
through chat, and by any drawer hand-off that asked for a "check" — the
ROLE_ADMIN decorators gated the console's buttons, not the capability. These
pin the registry boundary: mutating tools are marked next to their own
definition, a policy withholds them from the model and refuses them on
execution, and k8s_exec_pod never opens a shell in the agent's own datastore,
whoever asks and whatever that datastore is called.
"""

import ast
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools as tools_module
from tools import ToolPolicy, ToolRegistry, _service_from_host
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
}
WRITE_SHAPED = re.compile(r"restart|_exec|create_|resolve_|update_|store_|delete_|patch_|apply_")

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


def test_the_role_string_matches_the_console():
    # tools/ mirrors auth.models.ROLE_ADMIN rather than importing it; read the
    # original from source so the copy cannot drift.
    src = (pathlib.Path(__file__).resolve().parent.parent / "auth" / "models.py").read_text()
    literal = [
        ast.literal_eval(node.value)
        for node in ast.parse(src).body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ROLE_ADMIN" for t in node.targets)
    ]
    assert literal == [tools_module._ROLE_ADMIN]


# --------------------------------------------------------------------------
# both layers: what the model is offered, and what runs if it asks anyway
# --------------------------------------------------------------------------

def test_a_member_is_offered_reads_only():
    _, reg = _registry()
    offered = _names(reg.get_schemas(policy=MEMBER))
    assert {"k8s_get_pods", "ssh_check_service", "find_learnings", "ping_host", "get_remediation"} <= offered
    assert not (offered & KNOWN_MUTATING)


def test_admin_and_internal_callers_are_unchanged():
    _, reg = _registry()
    everything = _names(reg.get_schemas())
    assert KNOWN_MUTATING <= everything
    assert _names(reg.get_schemas(policy=ADMIN)) == everything
    assert _names(reg.get_schemas(policy=None)) == everything


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
    assert not (_names(reg.get_schemas(policy=VERIFY)) & KNOWN_MUTATING)
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
    ("kb-db.aweoriujwoedf.svc.cluster.local", ("kb-db", "aweoriujwoedf")),
    ("kb-db.aweoriujwoedf.svc", ("kb-db", "aweoriujwoedf")),
    ("kb-db.aweoriujwoedf", ("kb-db", "aweoriujwoedf")),
    ("KB-DB.Aweoriujwoedf.svc.cluster.local.", ("kb-db", "aweoriujwoedf")),
    ("kb-db.aweoriujwoedf:5432", ("kb-db", "aweoriujwoedf")),
    ("kb-db", ("kb-db", None)),
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

    def _reg(self, monkeypatch, db_url=None, ts_host=None, extra=None, own_ns=None):
        monkeypatch.delenv("POD_NAMESPACE", raising=False)
        monkeypatch.delenv("CFOP_NAMESPACE", raising=False)
        monkeypatch.setattr(tools_module, "_NAMESPACE_FILE", "/nonexistent/serviceaccount/namespace")
        if own_ns:
            monkeypatch.setenv("POD_NAMESPACE", own_ns)
        op, reg = _registry({"chat": {"protected_exec_hosts": list(extra or [])}})
        op.kb = SimpleNamespace(db_url=db_url) if db_url else SimpleNamespace()
        reg.timescale_tools = SimpleNamespace(host=ts_host) if ts_host else None
        calls = []
        reg.k8s_tools = SimpleNamespace(exec_pod=lambda **kw: calls.append(kw) or {"success": True})
        return reg, calls

    @staticmethod
    def _exec(reg, ns, pod, policy=None):
        return reg.execute("k8s_exec_pod", {"namespace": ns, "pod_name": pod, "command": "id"}, policy=policy)

    @pytest.mark.parametrize("pod", ["kb-db-0", "kb-db-7f9c6b-x2k9", "kb-db"])
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

    def test_it_holds_for_admins_too(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db.aweoriujwoedf.svc:5432/kb")
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0", policy=ADMIN)["refused"] is True
        assert calls == []

    def test_a_bare_service_name_resolves_in_the_agents_own_namespace(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, db_url="postgresql://u:p@kb-db:5432/kb", own_ns="aweoriujwoedf")
        assert self._exec(reg, "aweoriujwoedf", "kb-db-0")["refused"] is True
        assert self._exec(reg, "joeblowxxxx", "kb-db-0")["success"] is True

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

    def test_config_can_add_a_target_but_never_defines_the_default(self, monkeypatch):
        reg, calls = self._reg(monkeypatch, extra=["metrics-db.joeblowxxxx"])
        out = self._exec(reg, "joeblowxxxx", "metrics-db-0")
        assert out["refused"] is True and "chat.protected_exec_hosts" in out["error"]
        assert self._exec(reg, "joeblowxxxx", "web-0")["success"] is True


# --------------------------------------------------------------------------
# CFOP-123 residual: the hand-off asks for approve / resolve / reject
# --------------------------------------------------------------------------

class TestApproveFromChat:
    def _row(self, status="needs-human"):
        return {"id": 7, "status": status, "claimed_at": None, "completed_at": None,
                "payload": {}, "remediation_class": "k8s-action"}

    def test_approved_queues_the_row_like_the_console(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        op.kb.remediation_approve_conflict.return_value = None
        op.kb.update_remediation_status.return_value = True
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved"})
        assert out["success"] is True
        op.kb.update_remediation_status.assert_called_once_with(7, "queued")

    def test_the_consoles_approve_policy_applies(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        op.kb.remediation_approve_conflict.return_value = "manual-class rows are human-only work"
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved", "note": "go"})
        assert "human-only" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    @pytest.mark.parametrize("status", ["claimed", "executing"])
    def test_a_leased_row_cannot_be_approved(self, status):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row(status)
        out = reg.execute("resolve_remediation", {"remediation_id": 7, "status": "approved"})
        assert "still running" in out["error"]
        op.kb.update_remediation_status.assert_not_called()

    def test_a_note_is_optional_to_approve_but_still_required_to_close(self):
        op, reg = _registry()
        op.kb.get_remediation.return_value = self._row()
        assert "note is required" in reg.execute("resolve_remediation", {"remediation_id": 7})["error"]
        assert "note is required" in reg.execute(
            "resolve_remediation", {"remediation_id": 7, "status": "rejected"})["error"]

    def test_the_schema_advertises_approved(self):
        _, reg = _registry()
        params = reg.tools["resolve_remediation"]["schema"]["parameters"]
        assert params["properties"]["status"]["enum"] == ["resolved", "rejected", "approved"]
        assert params["required"] == ["remediation_id"]
