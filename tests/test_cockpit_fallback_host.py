"""The browser cockpit's last rung: a control host when the target cannot have
one (CFOP-177).

Three investigations get no terminal at all without this — one that resolves
to no host (the live shape: #2397/#2398/#2399 all logged ``host=''``), one
naming a machine with no ``infrastructure.hosts`` entry, and one whose probe
failed. Tier 1 is not an answer to any of them: the bridge cannot attach to a
pod unless Phase B is on *and* the chart granted ``pods/attach``, and with no
affected host "the pod" is somewhere in the cluster anyway.

So ``cockpit.fallback_host`` names a machine to put the shell on instead. What
these guard is the class, not the wording: that it engages only where the
drawer would otherwise refuse, that it never answers a question the caller
asked explicitly, that the session it opens is the one the bridge finds and
the one kill removes, and that nothing about it can be mistaken for a session
on the affected box.
"""

import pytest
from sqlalchemy import create_engine

from auth.store import AuthStore
from cockpit.ladder import TIER_HOST, build_ladder_config, session_name
from cockpit.spawn import CockpitConfig
from test_cockpit_ladder import FakeSSH, probe_reply
from test_cockpit_open import CONSOLE, INV, _client, live_listing

#: In HOSTS, and standing in for the control node an install would name.
FALLBACK = "ubuntu-llm-01"
FALLBACK_ADDRESS = "10.0.0.20"
TARGET_ADDRESS = "10.0.0.15"  # raspberrypi5

NOWHERE = {"id": INV, "trigger": "TargetDown: 3 targets are down"}


@pytest.fixture
def store():
    s = AuthStore(engine=create_engine("sqlite://"))
    s.ensure_schema()
    return s


class PerHostSSH(FakeSSH):
    """FakeSSH that can answer differently per host.

    The fallback tests need one machine to be unreachable while another is
    fine, which a rule set keyed only by the remote command cannot express.
    The address is in the argv (``user@address``), which is exactly how a real
    ssh tells the two apart.
    """

    def __init__(self, by_address, *rules):
        super().__init__(*rules)
        self.by_address = dict(by_address)

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        target = argv[-2] if len(argv) >= 2 else ""
        address = target.split("@")[-1]
        for needle, result in self.by_address.get(address, ()):
            if needle in argv[-1]:
                return result
        for needle, result in self.rules:
            if needle in argv[-1]:
                return result
        return (0, "", "")

    def hosts_touched(self):
        return {argv[-2].split("@")[-1] for argv, _ in self.calls if len(argv) >= 2}


def usable():
    """A probe reply from a box that can hold a login shell."""
    return (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")


UNREACHABLE = (255, "", "ssh: connect to host port 22: No route to host")


def reachable_fallback(*rules):
    """Only the fallback answers; the affected host, if any, is down."""
    return PerHostSSH({FALLBACK_ADDRESS: [("uname", usable()), *rules],
                       TARGET_ADDRESS: [("uname", UNREACHABLE)]})


def bridge():
    return {"bridge_enabled": True, "bridge_origins": CONSOLE}


def opens_on_fallback(**over):
    kwargs = {"cockpit": bridge(), "ladder_over": {"fallback_host": FALLBACK}}
    kwargs.update(over)
    return kwargs


# --------------------------------------------------------------------------
# the three cases that had no terminal at all
# --------------------------------------------------------------------------

def test_a_hostless_investigation_lands_on_the_fallback(store):
    """The live shape. Nothing names a machine, so the ladder says "a pod
    somewhere in the cluster" — and the drawer, which cannot serve one, used to
    stop there."""
    ssh = reachable_fallback()
    client, server = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                             **opens_on_fallback())

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["tier"] == TIER_HOST and body["host"] == FALLBACK
    assert not any(c[0] == "create" for c in server._kubectl_calls), "no Job was spawned"
    assert ssh.hosts_touched() == {FALLBACK_ADDRESS}


def test_an_unreachable_affected_host_falls_back_and_says_which_one(store):
    """raspberrypi5 is configured and down. The session goes to the control
    host, and the note carries the probe failure — "I am on the Pi" and "I am
    next to the Pi" have to stay different facts."""
    ssh = reachable_fallback()
    client, _ = _client(ssh, store=store, **opens_on_fallback())

    body = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    assert body["host"] == FALLBACK
    note = body["tier_note"]
    assert FALLBACK in note and "cockpit.fallback_host" in note
    assert "raspberrypi5" in note and "No route to host" in note
    assert "beside the incident" in note


def test_a_node_with_no_inventory_entry_falls_back(store):
    """The other refusal CFOP-98 left in place: a cluster node nobody put in
    infrastructure.hosts. There is still nowhere on it to put a shell — but
    there is somewhere else."""
    ssh = reachable_fallback()
    client, _ = _client(ssh, store=store, remediations=("raspberrypi9",),
                        node_names=("raspberrypi9",), **opens_on_fallback())

    body = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    assert body["host"] == FALLBACK
    assert "raspberrypi9" in body["tier_note"], "why the real target was not used"


def test_the_response_carries_a_line_the_drawer_can_render(store):
    """`host_provenance` and `tier_note` are API detail the page does not draw:
    on success an operator reads `tier@host` and nothing else. So the move gets
    its own short sentence, and `test_console_cockpit.py` holds the page to
    putting it beside the button."""
    client, _ = _client(reachable_fallback(), store=store, remediations=(),
                        investigation=NOWHERE, **opens_on_fallback())

    body = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    note = body["placement_note"]
    assert f"session placed on {FALLBACK}" in note and "cockpit.fallback_host" in note
    assert "beside the incident, not on it" in note
    assert "names no host" in note, "why the affected machine was not used"
    assert note in body["host_provenance"]


def test_a_session_that_did_not_move_carries_no_placement_note(store):
    """An always-present key that is usually empty is a key the page learns to
    ignore."""
    ssh = PerHostSSH({TARGET_ADDRESS: [("uname", usable())],
                      FALLBACK_ADDRESS: [("uname", usable())]})
    client, _ = _client(ssh, store=store, **opens_on_fallback())

    body = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    assert "placement_note" not in body


# --------------------------------------------------------------------------
# what it must not do
# --------------------------------------------------------------------------

def test_a_resolved_reachable_host_is_never_moved(store):
    """The fallback is the last rung, not a preference. A host that can hold
    the session holds it."""
    ssh = PerHostSSH({TARGET_ADDRESS: [("uname", usable())],
                      FALLBACK_ADDRESS: [("uname", usable())]})
    client, _ = _client(ssh, store=store, **opens_on_fallback())

    body = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()
    assert body["host"] == "raspberrypi5"
    assert FALLBACK not in body["tier_note"]
    assert ssh.hosts_touched() == {TARGET_ADDRESS}


def test_an_explicit_pod_request_is_refused_not_redirected(store):
    """Someone who typed --tier pod asked a question. Answering a different
    one with a shell on another machine is worse than saying no."""
    ssh = reachable_fallback()
    client, _ = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                        **opens_on_fallback())

    resp = client.post(f"/api/cockpit/{INV}/open", json={"tier": "pod"})
    assert resp.status_code == 409
    body = resp.get_json()
    assert "cockpit.bridge_pod_tier is off" in body["error"]
    assert FALLBACK not in body["error"]
    assert ssh.hosts_touched() == set(), "the fallback was not even probed"
    assert store.list_tokens() == []


def test_an_explicitly_requested_host_is_answered_on_its_own_terms(store):
    """--host names the machine to look at. If it cannot hold a session the
    operator needs to know that, not to be quietly put somewhere else."""
    ssh = reachable_fallback()
    client, _ = _client(ssh, store=store, **opens_on_fallback())

    resp = client.post(f"/api/cockpit/{INV}/open", json={"host": "raspberrypi5"})
    assert resp.status_code == 409
    body = resp.get_json()
    assert "raspberrypi5 could not be given a host-tier cockpit" in body["error"]
    assert FALLBACK_ADDRESS not in ssh.hosts_touched()


def test_the_terminal_spawn_path_never_falls_back(store):
    """`cfassist attach --spawn` has always been able to use tier 1, so a pod
    is a real answer there and the fallback would be a surprise. The rule is
    the browser's reading, not the ladder's."""
    ssh = reachable_fallback()
    client, server = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                             **opens_on_fallback())

    resp = client.post("/api/cockpit/spawn", json={"investigation_id": INV})
    assert resp.status_code in (200, 201), resp.get_json()
    assert resp.get_json()["tier"] == "pod"
    assert any(c[0] == "create" for c in server._kubectl_calls)


# --------------------------------------------------------------------------
# when the fallback itself is no good
# --------------------------------------------------------------------------

def test_an_unreachable_fallback_refuses_and_names_the_gap(store):
    ssh = PerHostSSH({FALLBACK_ADDRESS: [("uname", UNREACHABLE)],
                      TARGET_ADDRESS: [("uname", UNREACHABLE)]})
    client, _ = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                        **opens_on_fallback())

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    error = resp.get_json()["error"]
    assert f"cockpit.fallback_host ({FALLBACK}) could not take the session either" in error
    assert "No route to host" in error


def test_a_fallback_that_is_not_in_the_inventory_refuses_and_says_so(store):
    client, _ = _client(reachable_fallback(), store=store, remediations=(),
                        investigation=NOWHERE, cockpit=bridge(),
                        ladder_over={"fallback_host": "controlplane"})

    error = client.post(f"/api/cockpit/{INV}/open", json={}).get_json()["error"]
    assert "cockpit.fallback_host is controlplane" in error
    assert "no infrastructure.hosts entry" in error


def test_with_no_fallback_configured_the_refusal_offers_one(store):
    """The pre-CFOP-177 behaviour, plus the cheaper of the two ways out: the
    other one is a chart grant and a restart of every workload."""
    ssh = reachable_fallback()
    client, _ = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                        cockpit=bridge())

    resp = client.post(f"/api/cockpit/{INV}/open", json={})
    assert resp.status_code == 409
    error = resp.get_json()["error"]
    assert "no affected host could be resolved" in error
    assert "naming a control host in cockpit.fallback_host" in error
    assert not ssh.calls, "nothing to probe, so nothing was probed"


# --------------------------------------------------------------------------
# one rule, three callers (the CFOP-98 invariant, extended to the fallback)
# --------------------------------------------------------------------------

def test_the_bridge_resolves_the_session_on_the_fallback(store):
    """The bridge re-derives host and tier itself — it authenticates a person,
    not a target. If it landed on the old answer the terminal the drawer just
    opened would close with 4409."""
    ssh = reachable_fallback(("for d in /tmp/", live_listing()))
    _client_, server = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                               **opens_on_fallback())

    live = server.resolve_cockpit_session(INV)
    assert live and live["tier"] == TIER_HOST and live["host"] == FALLBACK


def test_close_removes_the_session_from_the_fallback_host(store):
    """Kill has to reach the machine open used. Before the host travelled with
    the tier, close would have ssh'd the affected box — or nothing at all —
    and left the session running until its TTL."""
    ssh = reachable_fallback(("for d in /tmp/", live_listing()))
    client, _ = _client(ssh, store=store, remediations=(), investigation=NOWHERE,
                        **opens_on_fallback())

    resp = client.post(f"/api/cockpit/{INV}/close", json={})
    assert resp.status_code == 200, resp.get_json()
    removals = [argv for argv, _ in ssh.calls
                if f"/tmp/{session_name(INV)}" in argv[-1] and "rm -rf" in argv[-1]]
    assert removals, "the session directory was never removed"
    assert all(argv[-2].endswith(FALLBACK_ADDRESS) for argv in removals)


# --------------------------------------------------------------------------
# ... including after the affected host comes back
# --------------------------------------------------------------------------

def recovering(*rules):
    """The Pi is down at open and answers by the time anyone hits kill.

    The ordinary incident shape, and the one the tier decision cannot see: a
    failed probe is cached for about a connect timeout, so re-deriving later
    lands on a *different* answer than the one the session was created under.
    """
    return PerHostSSH({FALLBACK_ADDRESS: [("uname", usable()), *rules],
                       TARGET_ADDRESS: [("uname", UNREACHABLE)]})


def recovers(ssh, server, *rules):
    ssh.by_address[TARGET_ADDRESS] = [("uname", usable()), *rules]
    # What the cache expiring does, without the wait: a failed probe is kept
    # only about a connect timeout (`_cache_ttl`).
    server._ladder.invalidate("raspberrypi5")


def test_close_still_reaches_the_fallback_after_the_affected_host_recovers(store):
    ssh = recovering(("for d in /tmp/", live_listing()))
    client, server = _client(ssh, store=store, **opens_on_fallback())
    assert client.post(f"/api/cockpit/{INV}/open", json={}).get_json()["host"] == FALLBACK

    recovers(ssh, server)
    resp = client.post(f"/api/cockpit/{INV}/close", json={})
    assert resp.status_code == 200, resp.get_json()
    removals = [argv for argv, _ in ssh.calls
                if f"/tmp/{session_name(INV)}" in argv[-1] and "rm -rf" in argv[-1]]
    assert any(argv[-2].endswith(FALLBACK_ADDRESS) for argv in removals), (
        "the shell on the control node was left running until its TTL")


def test_the_bridge_still_finds_the_fallback_session_after_the_host_recovers(store):
    """Otherwise the terminal the operator is typing into closes with 4404 the
    moment the affected box answers a ping again."""
    ssh = recovering(("for d in /tmp/", live_listing()))
    _client_, server = _client(ssh, store=store, **opens_on_fallback())

    recovers(ssh, server)   # raspberrypi5 is up, and has no session on it
    live = server.resolve_cockpit_session(INV)
    assert live and live["host"] == FALLBACK and live["tier"] == TIER_HOST


def test_a_host_that_is_down_at_resolve_time_never_reaches_the_lookup(store):
    """The mirror of the test above, and why the lookup needs no rescue for it:
    an unreachable derived host is exactly the case the tier decision sends to
    the fallback itself, so `live_session` is asked about the control node in
    the first place."""
    ssh = reachable_fallback(("for d in /tmp/", live_listing()))
    _client_, server = _client(ssh, store=store, **opens_on_fallback())

    live = server.resolve_cockpit_session(INV)
    assert live and live["host"] == FALLBACK


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_fallback_host_comes_from_the_cockpit_block_and_env_wins(monkeypatch):
    monkeypatch.delenv("CFOP_COCKPIT_FALLBACK_HOST", raising=False)
    assert build_ladder_config({}, CockpitConfig()).fallback_host == ""
    assert build_ladder_config({"cockpit": {"fallback_host": "raspberrypi"}},
                               CockpitConfig()).fallback_host == "raspberrypi"
    monkeypatch.setenv("CFOP_COCKPIT_FALLBACK_HOST", "ubuntu-cm5-01")
    assert build_ladder_config({"cockpit": {"fallback_host": "raspberrypi"}},
                               CockpitConfig()).fallback_host == "ubuntu-cm5-01"
