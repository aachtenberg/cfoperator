"""Cockpit tier 1: the ephemeral pod an operator attaches to (CFOP-35).

Two layers, tested at the layer the defect would live in:

* ``cockpit_spawn.CockpitSpawner`` — the manifest and the guards (dedupe,
  concurrency cap, placement fallback, token delivery, rollback).
* ``POST /api/cockpit/spawn`` — through the real Flask app, because a
  pure-policy test leaves the handler deletable.

The load-bearing assertion in the file is
``test_the_session_token_is_never_a_value_in_the_manifest``: a Job manifest is
the most widely readable object in the namespace — the cockpit's own read-only
service account can list it — so a token in ``env[].value`` would hand every
reader the credential the whole design exists to keep short-lived.
"""

import json
import os
import threading

import pytest
from flask import Flask
from sqlalchemy import create_engine

from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore
from cockpit_spawn import (
    COCKPIT_LABEL,
    DEFAULT_TTL_SECONDS,
    JOB_ROLE_LABEL,
    JOB_ROLE_VALUE,
    MAX_TTL_SECONDS,
    TOKEN_ENV,
    CockpitConfig,
    CockpitSpawnError,
    CockpitSpawner,
    build_cockpit_config,
    clamp_ttl,
)

TOKEN_SECRET = "cfop_super_secret_session_value"
PASSWORD = "correct horse battery staple"


# ---- fakes -----------------------------------------------------------------


class _FakeKubectl:
    """Records kubectl invocations; canned responses per verb.

    The spawner must never shell out in a test: CI has no cluster, and a
    launcher tested against the developer's own kubeconfig would be asserting
    the developer's cluster.
    """

    def __init__(self, *, jobs=(), node=None, list_rc=0, job_rc=0, secret_rc=0,
                 node_rc=0, job_uid="job-uid-1"):
        self.calls = []
        self.jobs = list(jobs)          # (name, investigation_id) tuples
        self.node = node                # the `kubectl get node` body, or None
        self.list_rc = list_rc
        self.job_rc = job_rc
        self.secret_rc = secret_rc
        self.node_rc = node_rc
        self.job_uid = job_uid

    def __call__(self, args, stdin):
        args = list(args)
        self.calls.append((args, stdin))
        if args[:2] == ["get", "jobs"]:
            items = [
                {"metadata": {"name": name, "labels": {COCKPIT_LABEL: str(inv)}},
                 "status": {"active": 1}}
                for name, inv in self.jobs
            ]
            return self.list_rc, json.dumps({"items": items}), "listing blew up"
        if args[:2] == ["get", "node"]:
            return self.node_rc, json.dumps(self.node or {}), "no such node"
        if args[0] == "create":
            kind = json.loads(stdin)["kind"]
            if kind == "Job":
                return self.job_rc, json.dumps({"metadata": {"uid": self.job_uid}}), "job denied"
            return self.secret_rc, "", "secret denied"
        if args[0] == "delete":
            return 0, "", ""
        return 1, "", f"unexpected kubectl {args}"

    # -- readers used by the assertions --

    def created(self, kind):
        for args, stdin in self.calls:
            if args and args[0] == "create" and stdin and json.loads(stdin)["kind"] == kind:
                return json.loads(stdin)
        return None

    @property
    def verbs(self):
        return [a[0] for a, _ in self.calls]


def _minter(recorder=None):
    # **kwargs: the ladder (CFOP-36) passes the chosen tier and host through to
    # the mint so the audit row can answer "which runtime did this session get".
    def mint(investigation_id, ttl_seconds, **kwargs):
        if recorder is not None:
            recorder.append((investigation_id, ttl_seconds))
        return {"id": 7, "prefix": "cfop_abcd", "secret": TOKEN_SECRET}
    return mint


def _spawner(**kubectl_kwargs):
    kubectl = _FakeKubectl(**kubectl_kwargs)
    revoked = []
    spawner = CockpitSpawner(
        CockpitConfig(namespace="apps"),
        kubectl_runner=kubectl,
        token_minter=_minter(),
        token_revoker=revoked.append,
    )
    return spawner, kubectl, revoked


# ---- the manifest ----------------------------------------------------------


def _manifest(node=None, ttl=DEFAULT_TTL_SECONDS):
    spawner, _, _ = _spawner()
    return spawner._build_cockpit_manifest(
        1889, job_name="cfop-cockpit-1889-010203", secret_name="cfop-cockpit-1889-010203-token",
        node=node, placement_note="pinned to node raspberrypi4", ttl_seconds=ttl)


def test_job_name_carries_the_investigation_and_a_stamp():
    """Two cockpits for the same investigation on the same day must not collide
    on a name — an operator re-spawning after a cluster hiccup would otherwise
    get AlreadyExists instead of a pod."""
    spawner, _, _ = _spawner()
    name = spawner._job_name(1889)
    assert name.startswith("cfop-cockpit-1889-")
    assert name != "cfop-cockpit-1889-"
    assert len(name) <= 63, "Job names are DNS labels"


def test_manifest_runs_as_the_read_only_cockpit_identity():
    spec = _manifest()["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "cfoperator-cockpit"
    assert spec["securityContext"] == {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001}
    assert spec["restartPolicy"] == "Never"


def test_manifest_carries_the_ttl_triple():
    """activeDeadlineSeconds is the session; ttlSecondsAfterFinished is the
    backstop that removes the finished Job (and, by ownership, its token);
    backoffLimit 0 stops a dead cockpit being restarted behind nobody."""
    spec = _manifest(ttl=3600)["spec"]
    assert spec["activeDeadlineSeconds"] == 3600
    assert spec["ttlSecondsAfterFinished"] == 3600
    assert spec["backoffLimit"] == 0


def test_manifest_is_interactive():
    """Without stdin/tty there is nothing to attach to, and with stdinOnce the
    session would end the first time a laptop dropped its connection."""
    container = _manifest()["spec"]["template"]["spec"]["containers"][0]
    assert container["stdin"] is True
    assert container["tty"] is True
    assert container["stdinOnce"] is False


def test_manifest_labels_the_investigation_on_job_and_pod():
    manifest = _manifest()
    for labels in (manifest["metadata"]["labels"],
                   manifest["spec"]["template"]["metadata"]["labels"]):
        assert labels[COCKPIT_LABEL] == "1889"
        assert labels[JOB_ROLE_LABEL] == JOB_ROLE_VALUE


def test_node_selector_pins_a_host_level_finding():
    spec = _manifest(node="raspberrypi4")["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "raspberrypi4"}


def test_no_node_selector_when_there_is_no_node():
    """A pod-level finding must not be pinned anywhere, and an empty selector
    would be a scheduling constraint nobody asked for."""
    assert "nodeSelector" not in _manifest(node=None)["spec"]["template"]["spec"]


def test_the_token_is_referenced_from_a_secret():
    env = {e["name"]: e for e in _manifest()["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env[TOKEN_ENV]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "cfop-cockpit-1889-010203-token", "key": TOKEN_ENV}
    assert "value" not in env[TOKEN_ENV]


def test_the_session_token_is_never_a_value_in_the_manifest():
    """The guard this feature turns on.

    Mutation check: put ``{"name": TOKEN_ENV, "value": token}`` in
    ``_build_cockpit_manifest`` instead of the secretKeyRef and this goes red.
    """
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    spawner.spawn(1889, host="", ttl_seconds=3600)

    job = kubectl.created("Job")
    assert TOKEN_SECRET not in json.dumps(job), (
        "the session token appears in the Job manifest — anything that can read "
        "Jobs (including the cockpit's own service account) can read it there")

    # ...and it is in the Secret, or the pod has no credential at all.
    secret = kubectl.created("Secret")
    assert secret["stringData"][TOKEN_ENV] == TOKEN_SECRET


def test_the_manifest_provides_every_variable_the_pod_entrypoint_reads():
    """A third cross-artifact seam.

    ``_build_cockpit_manifest`` ships in the agent image; ``cockpit/entrypoint.sh``
    ships in the cockpit image; they are built and deployed separately and
    nothing links them at runtime. A renamed variable is therefore not a
    compile error but a pod that exits on ``: "${...:?}"`` seconds after an
    operator asked for it, mid-incident.

    Mutation check: rename CFOP_AGENT_URL in either file and this goes red.
    """
    import re
    from pathlib import Path

    entrypoint = (Path(__file__).parent / "cockpit" / "entrypoint.sh").read_text()
    wanted = set(re.findall(r"\$\{(CFOP_[A-Z_]+)", entrypoint))
    assert wanted, "the entrypoint no longer reads any CFOP_ variable — reread it"

    provided = {e["name"] for e in
                _manifest()["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert wanted <= provided, (
        f"the cockpit pod reads {sorted(wanted - provided)} but the manifest "
        "never sets them")


def test_the_token_secret_is_owned_by_the_job():
    """Ownership is the cleanup: TTL deletes the Job, GC deletes the Secret.
    Without it the agent would need `delete` on secrets to tidy up."""
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    spawner.spawn(1889, host="")
    owner = kubectl.created("Secret")["metadata"]["ownerReferences"][0]
    assert owner["kind"] == "Job"
    assert owner["uid"] == "job-uid-1"
    assert owner["blockOwnerDeletion"] is False


# ---- guards ----------------------------------------------------------------


def test_a_second_spawn_for_the_same_investigation_is_deduped():
    spawner, kubectl, _ = _spawner(jobs=[("cfop-cockpit-1889-000001", 1889)])
    result = spawner.spawn(1889, host="")
    assert result["status"] == "existing"
    assert result["job_name"] == "cfop-cockpit-1889-000001"
    assert "create" not in kubectl.verbs, "a deduped spawn must create nothing"


def test_dedupe_does_not_match_a_different_investigation():
    spawner, kubectl, _ = _spawner(jobs=[("cfop-cockpit-1-000001", 1)], node={"spec": {}})
    assert spawner.spawn(1889, host="")["status"] == "spawned"


def test_the_concurrency_cap_refuses_rather_than_queues():
    spawner, kubectl, _ = _spawner(
        jobs=[("cfop-cockpit-1-000001", 1), ("cfop-cockpit-2-000002", 2)])
    with pytest.raises(CockpitSpawnError) as excinfo:
        spawner.spawn(1889, host="")
    assert excinfo.value.status == 429
    assert "create" not in kubectl.verbs


def test_dedupe_wins_over_the_cap():
    """An operator returning to their own cockpit must not be told the cluster
    is full — of, among others, their own cockpit."""
    spawner, _, _ = _spawner(
        jobs=[("cfop-cockpit-1889-000001", 1889), ("cfop-cockpit-2-000002", 2)])
    assert spawner.spawn(1889, host="")["status"] == "existing"


def test_a_failed_job_listing_is_an_error_not_an_empty_list():
    """'I could not check' must never read as 'nothing is running' — that would
    silently disable both the dedupe and the cap."""
    spawner, kubectl, _ = _spawner(list_rc=1)
    with pytest.raises(CockpitSpawnError) as excinfo:
        spawner.spawn(1889, host="")
    assert excinfo.value.status == 502
    assert "create" not in kubectl.verbs


def test_a_cordoned_node_gets_an_adjacent_spawn_that_says_so():
    spawner, kubectl, _ = _spawner(node={"spec": {"unschedulable": True}})
    result = spawner.spawn(1889, host="raspberrypi3")
    assert result["placement"]["node"] == ""
    assert "adjacent" in result["placement"]["note"]
    assert "nodeSelector" not in kubectl.created("Job")["spec"]["template"]["spec"]


def test_a_tainted_node_gets_an_adjacent_spawn_too():
    """A NotReady node carries a NoExecute taint rather than unschedulable, and
    a cockpit tolerates nothing — pinning there would sit Pending forever."""
    spawner, _, _ = _spawner(node={"spec": {"taints": [
        {"key": "node.kubernetes.io/not-ready", "effect": "NoExecute"}]}})
    result = spawner.spawn(1889, host="raspberrypi3")
    assert result["placement"]["node"] == ""
    assert "not-ready" in result["placement"]["note"]


def test_a_healthy_node_is_pinned():
    spawner, kubectl, _ = _spawner(node={"spec": {"taints": [
        {"key": "some/annotation", "effect": "PreferNoSchedule"}]}})
    result = spawner.spawn(1889, host="raspberrypi4")
    assert result["placement"]["node"] == "raspberrypi4"
    assert kubectl.created("Job")["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "raspberrypi4"}


def test_a_host_that_is_not_a_cluster_node_is_not_an_error():
    """Most investigations are about pods, and plenty of hosts are not nodes."""
    spawner, _, _ = _spawner(node_rc=1)
    result = spawner.spawn(1889, host="some-vm")
    assert result["status"] == "spawned"
    assert result["placement"]["node"] == ""


def test_a_failed_job_create_revokes_the_token_it_minted():
    spawner, _, revoked = _spawner(node={"spec": {}}, job_rc=1)
    with pytest.raises(CockpitSpawnError) as excinfo:
        spawner.spawn(1889, host="")
    assert excinfo.value.status == 502
    assert revoked == [7], "a credential minted for a pod that never existed must die"


def test_a_failed_secret_create_tears_the_job_down_again():
    """Otherwise the pod sits in CreateContainerConfigError until its deadline,
    holding a slot against the concurrency cap."""
    spawner, kubectl, revoked = _spawner(node={"spec": {}}, secret_rc=1)
    with pytest.raises(CockpitSpawnError):
        spawner.spawn(1889, host="")
    assert "delete" in kubectl.verbs
    assert revoked == [7]


def test_no_minter_is_a_refusal_not_a_pod_without_a_credential():
    spawner = CockpitSpawner(CockpitConfig(), kubectl_runner=_FakeKubectl())
    with pytest.raises(CockpitSpawnError) as excinfo:
        spawner.spawn(1889, host="")
    assert excinfo.value.status == 503


def test_the_minted_ttl_matches_the_pod_deadline():
    """The credential and the pod are one session; a token outliving the pod is
    exactly the standing credential this replaces."""
    minted = []
    kubectl = _FakeKubectl(node={"spec": {}})
    spawner = CockpitSpawner(CockpitConfig(), kubectl_runner=kubectl,
                             token_minter=_minter(minted))
    spawner.spawn(1889, host="", ttl_seconds=7200)
    assert minted == [(1889, 7200)]
    assert kubectl.created("Job")["spec"]["activeDeadlineSeconds"] == 7200


# ---- config / TTL ----------------------------------------------------------


def test_ttl_is_clamped_and_junk_reads_as_unspecified():
    assert clamp_ttl(None) == DEFAULT_TTL_SECONDS
    assert clamp_ttl("nonsense") == DEFAULT_TTL_SECONDS
    assert clamp_ttl(0) == DEFAULT_TTL_SECONDS, "0 would kill the pod before the attach"
    assert clamp_ttl(-5) == DEFAULT_TTL_SECONDS
    assert clamp_ttl(60) == 60
    assert clamp_ttl(99 * 3600) == MAX_TTL_SECONDS


def test_cockpit_config_inherits_the_agents_model_from_the_loaded_layout():
    """A cockpit talking to a different model than the investigation it is
    about would be a confusing thing to hand someone mid-incident.

    The seam is *the agent's in-memory config*, not what a config.yaml looks
    like. ``cfshared.config.normalize_aliases`` folds the flat getting-started
    keys into ``llm.primary.*`` at load, so a fixture written in the
    pre-collapse shape is green on a build that reads the flat keys and ships
    an empty ``CFOP_COCKPIT_LLM_URL``. This drives the real loader and then
    checks the symptom that empty value actually causes: the Job env the pod
    reads.
    """
    from cfshared.config import normalize_aliases

    loaded = normalize_aliases({"llm": {"backend": "ollama",
                                        "url": "http://ollama:11434",
                                        "model": "gemma4:26b"}})
    assert "url" not in loaded["llm"], (
        "premise moved: the loader no longer collapses llm.url into llm.primary")

    cfg = build_cockpit_config(loaded)
    assert cfg.llm_url == "http://ollama:11434"
    assert cfg.llm_model == "gemma4:26b"

    env = {e["name"]: e.get("value") for e in
           CockpitSpawner(cfg, kubectl_runner=_FakeKubectl())._build_cockpit_manifest(
               1889, job_name="j", secret_name="s", node=None,
               placement_note="", ttl_seconds=60,
           )["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CFOP_COCKPIT_LLM_URL"] == "http://ollama:11434", (
        "the pod would fall back to cfassist's localhost default instead of the "
        "model the investigation ran on")
    assert env["CFOP_COCKPIT_LLM_MODEL"] == "gemma4:26b"


def test_cockpit_config_still_reads_a_config_that_never_met_the_loader():
    """The flat keys stay supported as a fallback — an embedded caller, or a
    dict assembled by hand, has no reason to have been normalised."""
    cfg = build_cockpit_config({"llm": {"url": "http://flat:11434", "model": "m"}})
    assert (cfg.llm_url, cfg.llm_model) == ("http://flat:11434", "m")


def test_the_canonical_llm_block_wins_over_a_stale_flat_key():
    """Both present means someone half-migrated a config; the canonical
    location is the more specific statement of intent, exactly as the loader
    itself resolves it."""
    cfg = build_cockpit_config({"llm": {"url": "http://stale:11434",
                                        "primary": {"url": "http://real:11434"}}})
    assert cfg.llm_url == "http://real:11434"


def test_cockpit_config_survives_a_non_dict_config():
    assert build_cockpit_config(None).service_account == "cfoperator-cockpit"


def test_env_overrides_the_config_block(monkeypatch):
    monkeypatch.setenv("CFOP_COCKPIT_NAMESPACE", "sre")
    cfg = build_cockpit_config({"cockpit": {"namespace": "apps"}})
    assert cfg.namespace == "sre"


# ---- POST /api/cockpit/spawn -----------------------------------------------


def _client(*, investigation=None, spawner=None, store=None, auth_disabled=True):
    """The real WebServer routes against a stub operator.

    Mirrors the console harness in agent/test_remediation_queue.py: auth is
    installed in dev-bypass mode for the handler-behaviour tests, and with a
    real store for the role-gating ones.
    """
    from unittest.mock import MagicMock

    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb.get_investigation.return_value = (
        {"id": 1889, "host_id": "raspberrypi4"} if investigation is None else investigation)
    operator.config = {}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server.sock = None
    server.ws_clients = []
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = store
    if spawner is not None:
        server._cockpit = spawner
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "true" if auth_disabled else ""
    os.environ["CFOP_SESSION_SECRET"] = "test-session-secret"
    for name in ("CFOP_UI_USERNAME", "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN"):
        os.environ[name] = ""
    try:
        install_auth(server.app, ui_dir="ui", store=store)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return server.app.test_client(), server


def test_spawn_endpoint_returns_the_attach_coordinates():
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner)

    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["job_name"].startswith("cfop-cockpit-1889-")
    assert body["attach_command"].startswith("kubectl attach -it -n apps job/")
    assert body["pod_selector"] == "cfop-cockpit=1889"


def test_spawn_endpoint_never_returns_the_token():
    """The pod gets the credential through the Secret. A copy in the HTTP
    response would put it in the operator's shell history and scrollback."""
    spawner, _, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner)
    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert TOKEN_SECRET not in json.dumps(body)
    assert body["token_prefix"] == "cfop_abcd"


def test_spawn_endpoint_never_pins_from_the_investigations_host_id():
    """REGRESSION GUARD (CFOP-36). This test used to assert the opposite, and
    the behaviour it pinned was a defect: ``Investigation.host_id`` is the
    area-of-responsibility field, which ``agent.py`` sets to ``'cfoperator'``
    on every row. Pinning from it meant every spawn asked the cluster for a
    node called ``cfoperator``, got nothing, and reported "not a cluster node
    — spawned anywhere". The nodeSelector never once fired in production.

    Which host the incident is on now comes from the remediation rows fed off
    the investigation (their host_id IS finding-derived) or from the trigger
    text; see cockpit_ladder.resolve_target_host.
    """
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner,
                        investigation={"id": 1889, "host_id": "cfoperator",
                                       "trigger": "Pod immich-kiosk-0 not ready"})
    client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    for args, _stdin in kubectl.calls:
        assert "cfoperator" not in args[-1:], (
            f"the agent's own host_id was used as a node name: {args}")


def test_spawn_endpoint_pins_the_node_the_caller_names():
    """The console button and `--host` both land here, and this is the path
    that produced a real nodeSelector once the ladder started resolving hosts."""
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner, investigation={"id": 1889, "host_id": "cfoperator"})
    resp = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "host": "raspberrypi4"})
    lookups = [a for a, _ in kubectl.calls if a[:2] == ["get", "node"]]
    assert lookups == [["get", "node", "-o", "json", "--", "raspberrypi4"]], (
        "the node must be looked up exactly once: the ladder asks whether the "
        "host is in the cluster and the spawn asks whether to pin to it, and "
        "two answers that disagree put the session somewhere neither meant")
    body = resp.get_json()
    assert body["placement"]["node"] == "raspberrypi4"
    assert "caller" in body["host_provenance"]


def test_spawn_endpoint_answers_with_a_tier_and_an_attach_argv():
    """Every tier answers in the same shape, so the client never has to know
    which runtime it is attaching to (CFOP-36)."""
    spawner, _, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner)
    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == "pod"
    assert body["attach_argv"][0] == "kubectl"
    assert body["attach_command"] == " ".join(body["attach_argv"])
    assert "tier pod" in body["tier_note"]


def test_a_host_that_looks_like_a_flag_stays_an_argument():
    """host_id is alert-derived, so it is attacker-adjacent input. Without the
    `--` terminator a value beginning with `-` is parsed by kubectl as a flag
    rather than as the node name."""
    spawner, kubectl, _ = _spawner(node_rc=1)
    spawner.spawn(1889, host="--all-namespaces")
    args = next(a for a, _ in kubectl.calls if a[:2] == ["get", "node"])
    assert args[-2:] == ["--", "--all-namespaces"], (
        f"the node name is not terminator-protected: {args}")


def test_spawn_endpoint_404s_an_unknown_investigation():
    """A cockpit for an id that does not exist is a pod whose first act is to
    fail fetching its own briefing."""
    spawner, kubectl, _ = _spawner()
    client, _ = _client(spawner=spawner, investigation=False)
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 4242})
    assert resp.status_code == 404
    assert kubectl.calls == []


@pytest.mark.parametrize("body", [{}, {"investigation_id": "abc"}, [1, 2], "note"])
def test_spawn_endpoint_400s_without_a_usable_investigation_id(body):
    spawner, _, _ = _spawner()
    client, _ = _client(spawner=spawner)
    resp = client.post("/api/cockpit/spawn", json=body)
    assert resp.status_code == 400


def test_spawn_endpoint_reports_the_cap_as_429():
    spawner, _, _ = _spawner(
        jobs=[("cfop-cockpit-1-000001", 1), ("cfop-cockpit-2-000002", 2)])
    client, _ = _client(spawner=spawner)
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 429


def test_spawn_endpoint_returns_200_for_an_existing_cockpit():
    """201 would tell a client it created something it did not."""
    spawner, _, _ = _spawner(jobs=[("cfop-cockpit-1889-000001", 1889)])
    client, _ = _client(spawner=spawner)
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "existing"


def test_spawn_endpoint_clamps_a_caller_supplied_ttl():
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner)
    client.post("/api/cockpit/spawn", json={"investigation_id": 1889, "ttl_seconds": 10 ** 9})
    assert kubectl.created("Job")["spec"]["activeDeadlineSeconds"] == MAX_TTL_SECONDS


# ---- who may spawn ---------------------------------------------------------


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


def _login(client, username):
    assert client.post("/login", json={"username": username, "password": PASSWORD}).status_code == 200


def test_a_member_may_not_spawn_a_cockpit(store):
    """Spawning creates a workload and mints a credential. Members read the
    console; they do not get to put pods on the fleet."""
    store.create_user("m", PASSWORD, role=ROLE_MEMBER)
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner, store=store, auth_disabled=False)

    _login(client, "m")
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 403
    assert kubectl.calls == [], "the guard must refuse before any cluster call"


def test_an_anonymous_caller_may_not_spawn_a_cockpit(store):
    store.create_user("a", PASSWORD, role=ROLE_ADMIN)
    spawner, kubectl, _ = _spawner(node={"spec": {}})
    client, _ = _client(spawner=spawner, store=store, auth_disabled=False)
    assert client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).status_code == 401
    assert kubectl.calls == []


def test_an_admin_spawns_and_the_mint_goes_through_the_session_token_path(store):
    """The real mint, not the fake one: an admin's spawn must produce a token
    labelled for the investigation, scoped to investigate, and expiring."""
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    kubectl = _FakeKubectl(node={"spec": {}})
    client, server = _client(store=store, auth_disabled=False)
    server._cockpit = CockpitSpawner(
        CockpitConfig(namespace="apps"),
        kubectl_runner=kubectl,
        token_minter=server._mint_cockpit_token,
        token_revoker=server._revoke_cockpit_token,
    )

    _login(client, "root")
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889, "ttl_seconds": 3600})
    assert resp.status_code == 201

    tokens = [t for t in store.list_tokens() if t["label"] == "cockpit-inv-1889"]
    assert len(tokens) == 1, "the cockpit must mint exactly one token"
    assert tokens[0]["scopes"] == ["investigate"], (
        "a cockpit reads; the write path stays the PR/console gate even from "
        "inside a pod on the affected node")
    assert tokens[0]["expires_at"], "a session token without an expiry is a standing credential"

    # The minted secret reached the pod by reference, and only by reference.
    assert resp.get_json()["token_prefix"] == tokens[0]["token_prefix"]
    secret = kubectl.created("Secret")
    assert secret["stringData"][TOKEN_ENV]
    assert secret["stringData"][TOKEN_ENV] not in json.dumps(kubectl.created("Job"))


def test_the_endpoint_refuses_when_there_is_no_token_store():
    """Legacy env-credential mode has nowhere to mint from: refusing beats
    spawning a pod that cannot read its own briefing."""
    kubectl = _FakeKubectl(node={"spec": {}})
    client, server = _client(store=None)
    server._cockpit = CockpitSpawner(
        CockpitConfig(), kubectl_runner=kubectl,
        token_minter=server._mint_cockpit_token)
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 503
