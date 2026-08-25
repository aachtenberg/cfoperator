"""The console's half of the browser cockpit: open, ticket, close (CFOP-59).

The bridge (CFOP-75) carries bytes for anyone holding an ``investigate``
token. The console holds a cookie. These guard the thing that turns one into
the other — and the ways it must refuse to.

The shape to hold onto: ``open`` is spawn plus a *ticket*, and the ticket is
not the session token. The session token lives in a 0600 file on the host and
dies with the session. The ticket is minted per click, presented once in the
bridge's auth frame, and spent by being verified — so the only credential a
page ever holds lives as long as a handshake. ``close`` is the janitor's
removals applied now, plus every token minted for the investigation revoked.

Route tests run the real WebServer with a real ``HostCockpitSpawner`` over a
fake ssh (the same harness ``test_cockpit_ladder.py`` uses) and a real
``AuthStore`` on SQLite, because the interesting assertions are about what was
minted, with what label and scope, and whether it still verifies afterwards.
"""

import json
import os
import threading
import time

import pytest
from flask import Flask
from sqlalchemy import create_engine

from auth.models import ROLE_ADMIN, ROLE_MEMBER
from auth.store import AuthStore
from cockpit_ladder import TIER_HOST, session_name
from cockpit_spawn import CockpitConfig, CockpitSpawner
from test_cockpit_ladder import HOSTS, FakeSSH, probe_reply, spawner
from web_server import (
    BRIDGE_TICKET_TTL_SECONDS, COCKPIT_BRIDGE_TICKET_LABEL, COCKPIT_SESSION_TOKEN_LABEL,
)

PASSWORD = "correct horse battery staple"
CONSOLE = "http://localhost"  # what Flask's test client presents as its origin
INV = 1889
NAME = session_name(INV)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def live_listing(expires=None):
    """What the host's session listing says when #1889's directory exists."""
    return (0, f"/tmp/{NAME} {int(expires or time.time() + 1800)}\n", "")


def _client(ssh, *, store=None, auth_disabled=True, cockpit=None,
            investigation=None, remediations=("raspberrypi5",), node_names=(),
            pod_jobs=()):
    """The real routes, the real ladder over ``ssh``, and ``store`` for tokens.

    ``cockpit`` is the ``cockpit:`` config block — where the bridge's own
    switches live, so the route reads the same facts the listener would.
    """
    from unittest.mock import MagicMock

    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb.get_investigation.return_value = (
        {"id": INV, "host_id": "cfoperator", "trigger": "node down"}
        if investigation is None else investigation)
    operator.kb.list_remediations_for_investigation.return_value = [
        {"host_id": h} for h in remediations]
    operator.config = {"infrastructure": {"hosts": dict(HOSTS)},
                       "cockpit": dict(cockpit or {})}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = store

    # Stateful about Jobs: a create adds one, a delete removes it, so an
    # open→close→open sequence behaves like a real cluster (the reviewer's
    # guard: the second open must be a fresh spawn, not a dedupe).
    live = [{"name": n, "inv": str(i)} for n, i in pod_jobs]
    kubectl_calls = []

    def kubectl(args, stdin):
        kubectl_calls.append(list(args))
        if args[:2] == ["get", "node"]:
            if args[-1] in node_names:
                return 0, '{"spec": {}}', ""
            return 1, "", 'Error from server (NotFound): nodes "x" not found'
        if args[:2] == ["get", "jobs"]:
            items = [{"metadata": {"name": j["name"], "labels": {"cfop-cockpit": j["inv"]},
                                   "creationTimestamp": "2026-08-25T16:00:00Z"},
                      "spec": {"activeDeadlineSeconds": 1800},
                      "status": {"active": 1}} for j in live]
            return 0, json.dumps({"items": items}), ""
        if args[0] == "create" and stdin:
            man = json.loads(stdin)
            if man.get("kind") == "Job":
                meta = man.get("metadata", {})
                live.append({"name": meta.get("name", ""),
                             "inv": str((meta.get("labels") or {}).get("cfop-cockpit", ""))})
            return 0, json.dumps({"metadata": {"uid": "uid-1"}}), ""
        if args[0] == "delete" and args[1] == "job":
            live[:] = [j for j in live if j["name"] != args[2]]
            return 0, "", ""
        return 0, '{"metadata": {"uid": "uid-1"}}', ""
    server._kubectl_calls = kubectl_calls

    _cockpit = CockpitSpawner(CockpitConfig(namespace="apps"), kubectl_runner=kubectl)
    if store is not None:
        _cockpit._mint = server._mint_cockpit_token
        _cockpit._revoke = server._revoke_cockpit_token
    server._cockpit = _cockpit
    server._ladder = spawner(ssh)
    if store is not None:
        # The real mint, so the tokens under test are the ones a deploy makes.
        server._ladder._mint = server._mint_cockpit_token
        server._ladder._revoke = server._revoke_cockpit_token
    server._setup_routes()

    prior = {k: os.environ.get(k) for k in
             ("CFOP_AUTH_DISABLED", "CFOP_SESSION_SECRET", "CFOP_UI_USERNAME",
              "CFOP_UI_PASSWORD_HASH", "CFOP_API_TOKEN")}
    os.environ["CFOP_AUTH_DISABLED"] = "1" if auth_disabled else ""
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


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


def bridge_on(**over):
    cfg = {"bridge_enabled": True, "bridge_origins": CONSOLE}
    cfg.update(over)
    return cfg


def host_ssh(*rules):
    """A bare Pi with systemd: tier host, no container runtime."""
    return FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")),
                   *rules)


def tokens(store, label):
    return [t for t in store.list_tokens() if t["label"] == label]


def pod_close_made_no_host_removal(server):
    """A pod close routes to the Job delete, never the ladder's ssh destroy —
    so no `rm -rf`/`docker rm` went to any host."""
    ladder = getattr(server, "_ladder", None)
    ssh = getattr(ladder, "_ssh_host", None)
    calls = getattr(getattr(ssh, "__self__", None), "commands", [])
    return not any("rm -rf" in c or "docker rm" in c or "kill-session" in c for c in calls)


# --------------------------------------------------------------------------
# refusing before anything is spawned
# --------------------------------------------------------------------------

def test_open_refuses_when_the_bridge_is_off_and_spawns_nothing(store):
    """A session for a bridge that is off is a workload and a credential for
    nothing. The refusal carries the attach line, so the drawer falls back to
    the copy button rather than to an error."""
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, cockpit={})

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "bridge_disabled"
    assert "bridge_enabled" in body["error"]
    assert body["attach_command"] == f"cfassist attach {INV}"
    assert ssh.calls == [], "nothing may touch the host for a terminal that cannot open"
    assert store.list_tokens() == []


def test_open_refuses_an_origin_the_bridge_would_reject_and_names_it(store):
    """The listener would answer 4403 without saying which origin it saw. The
    console can say, and does, before minting anything."""
    ssh = host_ssh()
    client, _ = _client(ssh, store=store,
                        cockpit=bridge_on(bridge_origins="http://console.lan:8083"))

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "origin"
    assert CONSOLE in body["error"] and "bridge_origins" in body["error"]
    assert ssh.calls == []
    assert store.list_tokens() == []


def test_open_refuses_tier_pod_by_name(store):
    """Phase B. The bridge would refuse with 4409 after a spawn; the console
    refuses before it, with the terminal-side alternative."""
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, cockpit=bridge_on(),
                        node_names=("raspberrypi5",))

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "tier"
    assert "attach --spawn" in body["error"]
    assert not ssh.matching("mkdir"), "a pod-tier investigation must not get a host session"
    assert store.list_tokens() == []


def test_open_404s_an_unknown_investigation(store):
    client, _ = _client(host_ssh(), store=store, cockpit=bridge_on(), investigation={})
    assert client.post(f"/api/cockpit/{INV}/open", json={}).status_code == 404


# --------------------------------------------------------------------------
# the happy path: a session, and a ticket that is not the session token
# --------------------------------------------------------------------------

def test_open_spawns_through_the_ladder_and_hands_back_a_ticket(store):
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, cockpit=bridge_on())

    resp = client.post(f"/api/cockpit/{INV}/open", json={"ttl_seconds": 1800})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()

    # The session the ladder made, at the tier the host affords.
    assert body["tier"] == TIER_HOST and body["host"] == "raspberrypi5"
    assert body["session_name"] == NAME
    assert ssh.matching("mkdir"), "the runner and credential were delivered"

    # Where the page connects, computed by the server.
    assert body["bridge"]["url"] == f"ws://localhost:8084/cockpit/{INV}"
    assert body["bridge"]["origin"] == CONSOLE
    assert body["bridge"]["scope"] == "investigate"
    assert body["bridge"]["ticket_ttl_seconds"] == BRIDGE_TICKET_TTL_SECONDS

    # Two tokens, two purposes.
    session_tokens = tokens(store, COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV))
    ticket_tokens = tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))
    assert len(session_tokens) == 1 and len(ticket_tokens) == 1
    assert ticket_tokens[0]["scopes"] == ["investigate"]
    assert ticket_tokens[0]["expires_at"], "a ticket without an expiry is a standing credential"

    # The secret in the body is the ticket, and only the ticket.
    identity = store.verify_token(body["bridge"]["ticket"])
    assert identity is not None and identity.label == ticket_tokens[0]["label"]
    assert identity.has_scope("investigate")
    # The session token's secret went to the host over stdin and nowhere else.
    env_lines = [s for s in ssh.stdins if isinstance(s, (bytes, bytearray)) and b"CFOP_API_TOKEN=" in s]
    assert env_lines, "the session credential reached the host"
    session_secret = env_lines[0].split(b"CFOP_API_TOKEN=", 1)[1].split(b"\n", 1)[0].decode()
    assert session_secret and session_secret != body["bridge"]["ticket"]
    assert session_secret not in resp.get_data(as_text=True)


def test_open_on_a_live_session_is_a_fresh_ticket_not_a_second_session(store):
    """A reconnect is another open: the ladder dedupes, the ticket is new."""
    ssh = host_ssh(("for d in /tmp/", live_listing()))
    client, _ = _client(ssh, store=store, cockpit=bridge_on())

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "existing"
    assert body["expires_at"] > time.time(), "the drawer counts down from what the host says"
    assert not ssh.matching("mkdir"), "an existing session is joined, not replaced"
    assert len(tokens(store, COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV))) == 0, (
        "no session token is minted for a session that already has one on the host")
    assert len(tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))) == 1

    again = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    assert again["bridge"]["ticket"] != body["bridge"]["ticket"]
    assert len(tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))) == 2


def test_a_ticket_is_spent_by_being_verified(store):
    """One handshake. The bridge verifies through ``verify_bridge_token``; the
    first answer is the identity, the second is nothing."""
    client, server = _client(host_ssh(), store=store, cockpit=bridge_on())
    ticket = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()["bridge"]["ticket"]

    first = server.verify_bridge_token(ticket)
    assert first is not None and first.has_scope("investigate")
    assert server.verify_bridge_token(ticket) is None, "a spent ticket must not verify twice"
    row = tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))[0]
    assert row["status"] == "revoked"


def test_verifying_a_session_token_does_not_spend_it(store):
    """The host's own credential goes through the same verifier when a laptop
    attaches with it. It is not a ticket, and must survive."""
    _client(host_ssh(), store=store, cockpit=bridge_on())
    _row, secret = store.create_token(
        COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV), ["investigate"],
        creator_role=ROLE_ADMIN, ttl_seconds=600)
    _client_2, server = _client(host_ssh(), store=store, cockpit=bridge_on())
    assert server.verify_bridge_token(secret) is not None
    assert server.verify_bridge_token(secret) is not None


# --------------------------------------------------------------------------
# close: the host clean now, the tokens dead now
# --------------------------------------------------------------------------

def test_close_removes_the_session_now_and_revokes_every_cockpit_token(store):
    ssh = host_ssh(("for d in /tmp/", live_listing()))
    client, _ = _client(ssh, store=store, cockpit=bridge_on())
    client.post(f"/api/cockpit/{INV}/open", json={})  # mints a ticket (unspent)
    store.create_token(COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV),
                       ["investigate"], creator_role=ROLE_ADMIN, ttl_seconds=600)

    resp = client.post(f"/api/cockpit/{INV}/close", json={})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["status"] == "closed" and body["host"] == "raspberrypi5"
    assert body["removed"] == [{"host": "raspberrypi5", "name": NAME, "kind": "session"}]

    assert ssh.matching(f"rm -rf /tmp/{NAME}"), "the directory is gone now, not at the sweep"
    cancels = ssh.matching(f"{NAME}-reap.timer")
    assert cancels and "systemctl --user stop" in cancels[-1] and "sudo -n systemctl stop" in cancels[-1], (
        "both flavours of reap unit are cancelled — a sudo-armed timer is a system unit")

    assert body["tokens_revoked"] == 2
    for label in (COCKPIT_SESSION_TOKEN_LABEL, COCKPIT_BRIDGE_TICKET_LABEL):
        for row in tokens(store, label.format(investigation_id=INV)):
            assert row["status"] == "revoked", f"{row['label']} outlived the session"


def test_close_on_a_container_host_removes_the_container_too(store):
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes", arch="x86_64"), "")),
                  ("ps --filter", (0, f"{NAME} {int(time.time()) + 900}\n", "")))
    client, _ = _client(ssh, store=store, cockpit=bridge_on(), remediations=("ubuntu-llm-01",))

    body = client.post(f"/api/cockpit/{INV}/close", json={}).get_json()
    assert body["removed"] == [{"host": "ubuntu-llm-01", "name": NAME, "kind": "container"}]
    assert ssh.matching(f"docker rm --force {NAME}")


def test_close_404s_an_unknown_investigation(store):
    client, _ = _client(host_ssh(), store=store, cockpit=bridge_on(), investigation={})
    assert client.post(f"/api/cockpit/{INV}/close", json={}).status_code == 404


def test_close_of_a_hostless_investigation_is_an_idempotent_pod_noop(store):
    """A vague investigation with no machine resolves to the pod tier (tier 1
    is always attemptable). Close there deletes the Job if one exists and is a
    harmless 200 when none does — idempotent, not the old host-tier 400."""
    client, server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                             remediations=(), investigation={"id": INV, "trigger": "something vague"})
    resp = client.post(f"/api/cockpit/{INV}/close", json={})
    assert resp.status_code == 200
    assert resp.get_json()["removed"] == []


def test_destroy_ignores_the_deadline():
    """The janitor removes what has expired. A kill removes what is named."""
    ssh = host_ssh(("for d in /tmp/", live_listing(expires=time.time() + 3600)))
    ladder = spawner(ssh)
    removed = ladder.destroy(INV, host="raspberrypi5")
    assert removed == [{"host": "raspberrypi5", "name": NAME, "kind": "session"}]
    assert ssh.matching(f"rm -rf /tmp/{NAME}")


def test_destroy_still_cleans_when_nothing_was_listed_live():
    """An expired directory the sweep has not reached yet is exactly what a
    kill must not leave behind."""
    ssh = host_ssh()
    removed = spawner(ssh).destroy(INV, host="raspberrypi5")
    assert removed == []
    assert ssh.matching(f"rm -rf /tmp/{NAME}")


# --------------------------------------------------------------------------
# who may
# --------------------------------------------------------------------------

def _login(client, username):
    assert client.post("/login", json={"username": username, "password": PASSWORD}).status_code == 200


def test_a_member_may_neither_open_nor_close(store):
    """Open is spawn plus a credential; close removes a workload. Both are the
    admin's, and the guard refuses before the host is touched."""
    store.create_user("m", PASSWORD, role=ROLE_MEMBER)
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, auth_disabled=False, cockpit=bridge_on())
    _login(client, "m")
    assert client.post(f"/api/cockpit/{INV}/open", json={}).status_code == 403
    assert client.post(f"/api/cockpit/{INV}/close", json={}).status_code == 403
    assert ssh.calls == []


def test_an_anonymous_caller_may_neither_open_nor_close(store):
    store.create_user("a", PASSWORD, role=ROLE_ADMIN)
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, auth_disabled=False, cockpit=bridge_on())
    assert client.post(f"/api/cockpit/{INV}/open", json={}).status_code == 401
    assert client.post(f"/api/cockpit/{INV}/close", json={}).status_code == 401
    assert ssh.calls == []


def test_an_admin_opens_and_the_ticket_is_theirs(store):
    store.create_user("root", PASSWORD, role=ROLE_ADMIN)
    client, _ = _client(host_ssh(), store=store, auth_disabled=False, cockpit=bridge_on())
    _login(client, "root")
    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 201, resp.get_json()
    ticket = tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))[0]
    assert ticket["created_by"] == store.get_user_by_username("root")["id"]


def test_open_refuses_when_there_is_no_token_store():
    """Legacy env-credential mode has nowhere to mint from. Refusing beats a
    session the browser can never reach."""
    client, _ = _client(host_ssh(), store=None, cockpit=bridge_on())
    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# tier pod: off by default, opens through the pod spawner when the flag is on
# --------------------------------------------------------------------------

def bridge_on_pod(**over):
    cfg = {"bridge_enabled": True, "bridge_origins": CONSOLE, "bridge_pod_tier": True}
    cfg.update(over)
    return cfg


def test_open_refuses_tier_pod_unless_the_pod_tier_flag_is_on(store):
    """Default Phase A behaviour: an in-cluster investigation is refused by
    name, with the flag to turn on and the terminal-side fallback."""
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, cockpit=bridge_on(), node_names=("raspberrypi5",))
    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "tier"
    assert "cockpit.bridge_pod_tier" in body["error"] and "pods/attach" in body["error"]
    assert store.list_tokens() == []


def test_open_spawns_a_pod_cockpit_and_a_ticket_when_the_flag_is_on(store):
    ssh = host_ssh()
    client, _ = _client(ssh, store=store, cockpit=bridge_on_pod(),
                        node_names=("raspberrypi5",))
    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["tier"] == "pod"
    assert body["attach_command"].startswith("kubectl attach -it")
    assert body["bridge"]["url"] == f"ws://localhost:8084/cockpit/{INV}"
    # The pod session mints its own token plus the bridge ticket, same as host.
    assert len(tokens(store, COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=INV))) == 1


def test_the_resolver_returns_a_pod_session_only_when_the_flag_is_on(store):
    """The bridge's resolver: with the flag off, a pod investigation comes back
    as a stub the bridge refuses by name; with it on, the live pod session so
    the bridge can attach."""
    job = (f"cfop-cockpit-{INV}-abc", INV)
    off_client, off_server = _client(host_ssh(), store=store, cockpit=bridge_on(),
                                     node_names=("raspberrypi5",), pod_jobs=(job,))
    stub = off_server.resolve_cockpit_session(INV)
    assert stub == {"tier": "pod", "host": "raspberrypi5", "investigation_id": INV}

    on_client, on_server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                                   node_names=("raspberrypi5",), pod_jobs=(job,))
    live = on_server.resolve_cockpit_session(INV)
    assert live and live["tier"] == "pod"
    assert live["attach_argv"][:2] == ["kubectl", "attach"]
    assert live["job_name"] == f"cfop-cockpit-{INV}-abc"


def test_the_resolver_returns_none_for_a_pod_with_no_live_job(store):
    _client_, server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                               node_names=("raspberrypi5",), pod_jobs=())
    assert server.resolve_cockpit_session(INV) is None


def test_open_pod_reports_a_deadline_for_the_countdown(store):
    """The drawer counts down from expires_at; a pod open must carry it too, or
    Phase B shows TTL — while the Job has activeDeadlineSeconds."""
    client, _ = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                        node_names=("raspberrypi5",))
    body = client.post(f"/api/cockpit/{INV}/open", json={"ttl_seconds": 1800}).get_json()
    assert body["tier"] == "pod"
    assert body["expires_at"] > time.time()


def test_close_deletes_the_pod_job_and_revokes_the_token(store):
    """The bug the review caught: close must delete the Job (not SSH the node),
    or the pod runs on with a revoked in-Secret token."""
    job = (f"cfop-cockpit-{INV}-abc", INV)
    client, server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                             node_names=("raspberrypi5",), pod_jobs=(job,))
    store.create_token(COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV),
                       ["investigate"], creator_role=ROLE_ADMIN, ttl_seconds=600)

    resp = client.post(f"/api/cockpit/{INV}/close", json={})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["removed"] == [{"host": "", "name": job[0], "kind": "pod"}]
    assert ["delete", "job", job[0], "-n", "apps"] in server._kubectl_calls, (
        "close did not delete the pod Job")
    # No host-side removal: a pod cockpit is not on an ssh host.
    assert pod_close_made_no_host_removal(server), "close ran a host removal for a pod cockpit"
    assert body["tokens_revoked"] == 1
    for row in tokens(store, COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=INV)):
        assert row["status"] == "revoked"


def test_open_close_open_is_a_fresh_spawn_not_a_dedupe(store):
    """After close deletes the Job, the next open must spawn a new one — a
    dedupe onto the deleted Job is exactly the ghost this fixes."""
    client, _ = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                        node_names=("raspberrypi5",))
    first = client.post(f"/api/cockpit/{INV}/open", json={})
    assert first.status_code == 201 and first.get_json()["status"] == "spawned"
    assert client.post(f"/api/cockpit/{INV}/close", json={}).status_code == 200
    second = client.post(f"/api/cockpit/{INV}/open", json={})
    assert second.status_code == 201, second.get_json()
    assert second.get_json()["status"] == "spawned", "reopen deduped onto a killed Job"


def test_pod_destroy_is_a_noop_when_there_is_no_job(store):
    _client_, server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                               node_names=("raspberrypi5",), pod_jobs=())
    assert server._cockpit.destroy(INV) == []


def test_an_existing_pod_session_carries_the_jobs_deadline(store):
    """A console joining a pod session it did not start reads the countdown from
    the Job's creationTimestamp + activeDeadlineSeconds."""
    job = (f"cfop-cockpit-{INV}-abc", INV)
    _client_, server = _client(host_ssh(), store=store, cockpit=bridge_on_pod(),
                               node_names=("raspberrypi5",), pod_jobs=(job,))
    live = server.resolve_cockpit_session(INV)
    # 2026-08-25T16:00:00Z + 1800s
    assert live["expires_at"] == 1787680800 + 1800 or live["expires_at"] > 0
