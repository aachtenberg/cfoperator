"""The cockpit PTY bridge, and every way in that it must refuse (CFOP-75).

A browser-reachable PTY is the sharpest object this system holds. What makes
it tolerable is that every layer beneath is scoped and mortal — but only if
the layer *above* actually refuses, so these are mostly tests about saying no.

They exercise ``authorize()`` directly rather than driving a socket. That is
the point of its shape: it takes strings and returns a verdict, so a foreign
origin, a read-scoped token and a tier this phase does not serve are all
unit-testable without a network, and a contributor who adds a fifth way in has
to add it where the tests already look.
"""

import os
import struct
import time
import fcntl
import pty
import termios

import pytest

from cockpit_bridge import (
    AUTH_TIMEOUT_SECONDS, BIND_TIMEOUT_SECONDS, CLOSE_BUSY, CLOSE_FORBIDDEN,
    CLOSE_NO_SESSION, CLOSE_TIER_UNSUPPORTED, CLOSE_UNAUTHENTICATED,
    DEFAULT_BRIDGE_PORT, REQUIRED_SCOPE, BridgeConfig, CockpitBridge,
    PtySession, authorize, build_bridge_config, normalize_origin, parse_path,
)

CONSOLE = "http://cfop.lan:8083"


class Identity:
    """Shaped like ``auth.store.TokenIdentity``, deliberately.

    It spells ``has_scope`` because that is what the real one spells. The
    double used to define ``has``, which meant these tests exercised a
    fallback branch and never the production path — caught in review.
    """

    def __init__(self, scopes=(REQUIRED_SCOPE,), username="aachten"):
        self.scopes = frozenset(scopes)
        self.username = username

    def has_scope(self, scope):
        return scope in self.scopes


def session(tier="host", **over):
    row = {"tier": tier, "host": "raspberrypi5", "investigation_id": 1889,
           "session_name": "cfop-cockpit-1889",
           "attach_argv": ["ssh", "-t", "sre@10.0.0.15", "/tmp/cfop-cockpit-1889/run"]}
    row.update(over)
    return row


def config(**over):
    base = {"enabled": True, "allowed_origins": (CONSOLE,)}
    base.update(over)
    return BridgeConfig(**base)


def call(*, path="/cockpit/1889", origin=CONSOLE, token="cfop_good",
         identity=None, live=True, cfg=None, verifier=None, resolver=None):
    if verifier is None:
        def verifier(_t):
            return identity if identity is not None else Identity()
    if resolver is None:
        def resolver(_id):
            return session() if live else None
    return authorize(path=path, origin=origin, token=token,
                     config=cfg or config(),
                     token_verifier=verifier, resolver=resolver)


# --------------------------------------------------------------------------
# the path names a session, or it is not a cockpit path
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/cockpit/1889", 1889),
    ("/cockpit/1889/", 1889),
    ("/cockpit/1889?x=1", 1889),
    ("/cockpit/abc", None),
    ("/cockpit/", None),
    ("/cockpit/0", None),
    ("/cockpit/-3", None),
    ("/", None),
    ("/cockpitfoo/1", None),
    ("", None),
])
def test_parse_path(path, expected):
    assert parse_path(path) == expected


@pytest.mark.parametrize("raw,expected", [
    ("http://cfop.lan:8083", "http://cfop.lan:8083"),
    ("http://cfop.lan:8083/", "http://cfop.lan:8083"),
    ("HTTP://CFOP.lan:8083", "http://cfop.lan:8083"),
    ("http://cfop.lan:8083/investigations#2272", "http://cfop.lan:8083"),
    ("", ""),
])
def test_normalize_origin(raw, expected):
    """Humans configure this from the address bar; browsers send it exactly.
    A trailing slash must not lock someone out mid-incident."""
    assert normalize_origin(raw) == expected


# --------------------------------------------------------------------------
# the ways in, and the refusals
# --------------------------------------------------------------------------

def test_a_good_connection_is_allowed_and_carries_the_argv():
    v = call()
    assert v.ok
    assert v.session["attach_argv"][0] == "ssh"
    assert v.actor == "aachten"


def test_a_foreign_origin_is_refused():
    v = call(origin="http://evil.example")
    assert not v.ok and v.code == CLOSE_FORBIDDEN


def test_the_origin_is_checked_before_the_token():
    """A page that cannot reach this must not make the agent do database work,
    and must not learn whether a token was good."""
    calls = []

    def verifier(token):
        calls.append(token)
        return Identity()

    v = call(origin="http://evil.example", verifier=verifier)
    assert not v.ok
    assert calls == [], "the token was verified for a rejected origin"


def test_the_offered_origin_is_not_echoed_back():
    """The reason string reaches a terminal; the origin is attacker-controlled
    text."""
    v = call(origin="http://evil.example/\x1b]0;pwned\x07")
    assert "evil.example" not in v.reason
    assert "\x1b" not in v.reason


def test_no_configured_origin_refuses_everything():
    """Closed by default. An unset allowlist is not 'allow any'.

    The reason has to be the *specific* one. An empty allowlist would fail the
    membership check below it anyway, so the refusal is not in doubt — what is
    in doubt is whether the operator is told to fix their config or left
    believing their browser is at fault. Asserting only the close code cannot
    tell those apart, which is how this test was wrong before mutation
    testing caught it.
    """
    v = call(cfg=config(allowed_origins=()))
    assert not v.ok and v.code == CLOSE_FORBIDDEN
    assert "no console origin is configured" in v.reason


def test_a_foreign_origin_and_an_unset_allowlist_read_differently():
    """Two configuration problems, two different things to go and do."""
    unset = call(cfg=config(allowed_origins=()))
    foreign = call(origin="http://evil.example")
    assert unset.reason != foreign.reason


def test_a_missing_token_is_unauthenticated():
    v = call(token="")
    assert not v.ok and v.code == CLOSE_UNAUTHENTICATED


def test_an_invalid_token_is_unauthenticated():
    v = call(verifier=lambda _t: None)
    assert not v.ok and v.code == CLOSE_UNAUTHENTICATED


def test_a_broken_auth_store_does_not_read_as_a_bad_token():
    """Distinguishable on purpose: 'sign in again' is the wrong advice when
    the database is down."""
    def verifier(_t):
        raise RuntimeError("postgres is unreachable")

    v = call(verifier=verifier)
    assert not v.ok and v.code == CLOSE_UNAUTHENTICATED
    assert "could not be verified" in v.reason


def test_a_read_scoped_token_cannot_open_a_terminal():
    """`read` works an incident from the console. A shell on the host is the
    `investigate` line, the same one `cfassist attach` mints against."""
    v = call(identity=Identity(scopes=("read",)))
    assert not v.ok and v.code == CLOSE_FORBIDDEN
    assert REQUIRED_SCOPE in v.reason


def test_a_remediate_token_is_accepted():
    """Scopes imply downward in this system; a stronger token is not a
    stranger. The store expands implied scopes at verify time, so an
    `investigate`-carrying identity is what actually arrives here."""
    assert call(identity=Identity(scopes=("remediate", "investigate", "read"))).ok


def test_the_real_identity_shape_is_the_one_that_is_checked():
    """`TokenIdentity` defines has_scope(). If the check only understood some
    other spelling, every production connection would fall through to the
    frozenset — which happens to work, and would hide the mismatch until
    something changed."""
    class RealShape:
        scopes = frozenset({REQUIRED_SCOPE})
        username = "aachten"
        asked = []

        def has_scope(self, scope):
            RealShape.asked.append(scope)
            return scope in self.scopes

    assert call(identity=RealShape()).ok
    assert RealShape.asked == [REQUIRED_SCOPE], (
        "has_scope() was never called; the scope check is using a fallback")


def test_the_verdict_does_not_share_the_ladders_argv_list():
    """dict() is shallow, so the copy still handed out the same list the
    ladder holds — and the next thing that happens to it is a subprocess."""
    row = session()
    v = call(resolver=lambda _id: row)
    v.session["attach_argv"].append("; rm -rf /")
    assert row["attach_argv"] == ["ssh", "-t", "sre@10.0.0.15",
                                  "/tmp/cfop-cockpit-1889/run"]


def test_no_live_cockpit_is_refused_rather_than_spawned():
    """Spawning is a workload and a minted credential. It stays the
    admin-gated console route; the bridge only attaches."""
    v = call(live=False)
    assert not v.ok and v.code == CLOSE_NO_SESSION


def test_a_failed_session_lookup_does_not_read_as_no_session():
    def resolver(_id):
        raise RuntimeError("ssh timed out probing the host")

    v = call(resolver=resolver)
    assert not v.ok and v.code == CLOSE_NO_SESSION
    assert "lookup failed" in v.reason


def test_a_pod_tier_cockpit_is_refused_by_name():
    """Phase B. Its argv is `kubectl attach` and no identity here holds
    pods/attach — refused with a reason rather than exec'd into an unreadable
    permissions error."""
    v = call(resolver=lambda _id: session(tier="pod"))
    assert not v.ok and v.code == CLOSE_TIER_UNSUPPORTED
    assert "cfassist attach --spawn" in v.reason


@pytest.mark.parametrize("argv", [None, [], "ssh -t host", {}])
def test_a_session_without_usable_argv_is_refused(argv):
    """The bridge runs argv the ladder produced. Anything else is not a
    session it knows how to attach to."""
    v = call(resolver=lambda _id: session(attach_argv=argv))
    assert not v.ok and v.code == CLOSE_NO_SESSION


def test_the_verdict_carries_a_copy_of_the_session():
    """The resolver's row is not the bridge's to mutate — it may be the
    ladder's own cached dict."""
    row = session()
    v = call(resolver=lambda _id: row)
    v.session["tier"] = "tampered"
    assert row["tier"] == "host"


# --------------------------------------------------------------------------
# config: closed by default, and it does not open a port by accident
# --------------------------------------------------------------------------

def test_disabled_by_default():
    cfg = build_bridge_config({})
    assert cfg.enabled is False
    assert cfg.port == DEFAULT_BRIDGE_PORT
    assert cfg.allowed_origins == ()


def test_config_comes_from_the_cockpit_block():
    cfg = build_bridge_config({"cockpit": {
        "bridge_enabled": True, "bridge_port": 9999,
        "bridge_origins": "http://cfop.lan:8083/, http://10.0.0.14:8083"}})
    assert cfg.enabled is True
    assert cfg.port == 9999
    assert cfg.allowed_origins == ("http://cfop.lan:8083", "http://10.0.0.14:8083")


def test_origins_may_be_a_list():
    cfg = build_bridge_config({"cockpit": {"bridge_origins": [CONSOLE]}})
    assert cfg.allowed_origins == (CONSOLE,)


def test_env_wins_over_the_file(monkeypatch):
    monkeypatch.setenv("CFOP_COCKPIT_BRIDGE_PORT", "7000")
    cfg = build_bridge_config({"cockpit": {"bridge_port": 9999}})
    assert cfg.port == 7000


def test_a_disabled_bridge_does_not_start():
    bridge = CockpitBridge(config(enabled=False), resolver=lambda _i: None,
                           token_verifier=lambda _t: None)
    assert bridge.start() is False


def test_an_enabled_bridge_with_no_origins_refuses_to_listen():
    """Listening while rejecting every browser is the worst of both: the port
    is open and nothing works. Fail at startup, where someone reads logs."""
    bridge = CockpitBridge(config(enabled=True, allowed_origins=()),
                           resolver=lambda _i: None,
                           token_verifier=lambda _t: None)
    assert bridge.start() is False


# --------------------------------------------------------------------------
# the pty itself
# --------------------------------------------------------------------------

def _winsize(fd):
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return cols, rows


@pytest.mark.parametrize("cols,rows,expected", [
    (120, 40, (120, 40)),
    (0, 0, (1, 1)),
    (-5, -5, (1, 1)),
    (99999, 99999, (1000, 1000)),
])
def test_resize_clamps_what_the_browser_sends(cols, rows, expected):
    """These arrive from a browser and the ioctl takes unsigned shorts — 65536
    would wrap to 0 rather than being obviously wrong."""
    master, slave = pty.openpty()
    try:
        term = PtySession(["true"])
        term.fd = master
        term.resize(cols, rows)
        assert _winsize(master) == expected
    finally:
        os.close(master)
        os.close(slave)


def test_resize_on_a_dead_session_is_not_an_error():
    """A resize can arrive after the far end has gone; that is a race, not a
    fault."""
    term = PtySession(["true"])
    term.resize(80, 24)  # fd is -1


def test_close_is_safe_twice():
    """The TTL, the janitor and the socket closing can all end the same
    session."""
    term = PtySession(["true"])
    term.close()
    term.close()


def test_resize_survives_junk_from_a_browser():
    """`{"type":"resize","cols":null}` is a browser bug, not a reason to drop
    someone's session mid-incident — an unhandled TypeError here takes the
    reader task down with it."""
    master, slave = pty.openpty()
    try:
        term = PtySession(["true"])
        term.fd = master
        for cols, rows in [(None, 24), ("80", "24"), ({}, []), (80, None)]:
            term.resize(cols, rows)
        assert _winsize(master)[0] >= 1
    finally:
        os.close(master)
        os.close(slave)


def test_close_reaps_the_child_rather_than_leaving_a_zombie():
    """One SIGHUP plus one WNOHANG missed a child that had not exited yet, and
    the agent then held the zombie for its whole life. The far side outliving
    its TTL is the normal case, which is exactly when that fired."""
    term = PtySession(["sleep", "60"])
    term.start()
    pid = term.pid
    term.close()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_a_session_cap_exists_and_defaults_low():
    """Each terminal is an ssh child on the agent. Without a cap an
    `investigate` token is a licence to fork as many as the box will take."""
    assert build_bridge_config({}).max_sessions == 2
    assert build_bridge_config(
        {"cockpit": {"bridge_max_sessions": 5}}).max_sessions == 5


def test_the_cap_can_never_land_below_one():
    """A cap of 0 would be a listening bridge that refuses everyone — the same
    trap as an empty origin list, reached from the other side.

    Note what 0 actually does: every numeric key in this config family is read
    with `env or block or default`, so a configured 0 is falsy and reads as
    "unset", giving the default. That is the repo-wide convention and not worth
    diverging from for one key. The clamp is what catches a negative, which
    does get through.
    """
    assert build_bridge_config({"cockpit": {"bridge_max_sessions": -4}}).max_sessions == 1
    assert build_bridge_config({"cockpit": {"bridge_max_sessions": 0}}).max_sessions == 2


def test_start_waits_for_the_bind_rather_than_assuming_it():
    """`start()` returning True is what makes the agent log "listening on
    :8084". If it returns before the socket is bound, address-already-in-use
    arrives seconds later, in a different line, below the one an operator
    actually reads.

    The failure has to arrive *late* for this to test anything: a bind error
    set up front would be seen even by a start() that never waited.
    """
    bridge = CockpitBridge(config(), resolver=lambda _i: None,
                           token_verifier=lambda _t: None)

    def slow_failure():
        time.sleep(0.2)
        bridge._bind_error = OSError("address already in use")
        bridge._ready.set()

    bridge._run = slow_failure
    assert bridge.start() is False, (
        "start() reported success before the listener had bound")


def test_start_gives_up_if_the_listener_never_answers():
    """A thread that neither binds nor dies must not hold the agent's startup
    open forever."""
    bridge = CockpitBridge(config(), resolver=lambda _i: None,
                           token_verifier=lambda _t: None)
    bridge._run = lambda: time.sleep(BIND_TIMEOUT_SECONDS + 5)
    import cockpit_bridge as mod
    original, mod.BIND_TIMEOUT_SECONDS = mod.BIND_TIMEOUT_SECONDS, 0.2
    try:
        assert bridge.start() is False
    finally:
        mod.BIND_TIMEOUT_SECONDS = original


def test_the_bind_wait_is_bounded():
    """A listener that never answers must not hold up the agent's startup."""
    assert 0 < BIND_TIMEOUT_SECONDS <= 30


def test_the_auth_window_is_short():
    """An unauthenticated connection holding a slot is the cheapest possible
    nuisance."""
    assert 0 < AUTH_TIMEOUT_SECONDS <= 30


def test_every_refusal_has_its_own_code():
    """The console has to say which wall it hit: 'sign in again', 'ask for
    investigate scope' and 'this one is in the cluster' are three different
    next actions."""
    codes = [CLOSE_UNAUTHENTICATED, CLOSE_FORBIDDEN, CLOSE_NO_SESSION,
             CLOSE_TIER_UNSUPPORTED, CLOSE_BUSY]
    assert len(set(codes)) == len(codes)
    assert all(4000 <= c <= 4999 for c in codes), (
        "close codes outside 4000-4999 are not application codes and browsers "
        "may replace them")


def test_the_lookup_failure_reason_reaches_the_operator():
    """"raspberrypi5 could not be probed" is a different problem from "there
    is no session", and mid-incident it is frequently *the* problem. Flattening
    both to one message sends someone looking in the wrong place."""
    def resolver(_id):
        raise RuntimeError("raspberrypi5 could not be probed (No route to host)")

    v = call(resolver=resolver)
    assert not v.ok and v.code == CLOSE_NO_SESSION
    assert "No route to host" in v.reason


def test_a_reason_cannot_carry_an_escape_sequence():
    """Part of that message comes off the far host — ssh stderr, a login
    banner — and it is rendered by a console that may be an xterm."""
    def resolver(_id):
        raise RuntimeError("probe failed \x1b]0;pwned\x07 on raspberrypi5")

    v = call(resolver=resolver)
    assert "\x1b" not in v.reason and "\x07" not in v.reason
    assert "raspberrypi5" in v.reason


# --------------------------------------------------------------------------
# the server-side half: what the bridge is given, and what it is never given
# --------------------------------------------------------------------------

def _server(*, investigation=None, tier="host", live=None, store=None):
    from unittest.mock import MagicMock

    from web_server import WebServer

    server = WebServer.__new__(WebServer)
    server.operator = MagicMock()
    server.operator.kb.get_investigation.return_value = (
        {"id": 1889, "trigger": "mount hung"} if investigation is None else investigation)
    server.auth_store = store
    server._resolve_cockpit_host = lambda _id, _inv, _req: ("raspberrypi5", "trigger text")
    server._choose_cockpit_tier = lambda host, requested, **_kw: (tier, "note", None)
    ladder = MagicMock()
    ladder.live_session.return_value = live
    server._cockpit_ladder = lambda: ladder
    return server, ladder


def test_the_target_is_derived_server_side_not_supplied():
    """The bridge authenticates a person, not a target. A caller-supplied host
    would let anyone holding a token aim a terminal at any machine in the
    inventory."""
    server, ladder = _server(live=session())
    row = server.resolve_cockpit_session(1889)
    assert row["attach_argv"][0] == "ssh"
    ladder.live_session.assert_called_once_with(1889, host="raspberrypi5")


def test_a_pod_investigation_resolves_to_a_tier_stub():
    """So the refusal names the tier instead of saying "no session" — which
    would send someone hunting for a cockpit that is deliberately not offered
    here yet."""
    server, ladder = _server(tier="pod")
    row = server.resolve_cockpit_session(1889)
    assert row["tier"] == "pod"
    ladder.live_session.assert_not_called()


def test_an_unknown_investigation_resolves_to_none():
    server, ladder = _server(investigation=None)
    server.operator.kb.get_investigation.return_value = None
    assert server.resolve_cockpit_session(9999) is None
    ladder.live_session.assert_not_called()


def test_no_auth_store_refuses_rather_than_allowing():
    """No store means auth is disabled, which is a local-development posture.
    A development posture must not silently become "anyone may open a terminal
    on a production host"."""
    server, _ladder = _server(store=None)
    assert server.verify_bridge_token("cfop_anything") is None


def test_the_pod_stub_is_refused_by_the_bridge_end_to_end():
    """The two halves agree: what the server resolves for a tier-1
    investigation is what authorize() turns into the tier refusal."""
    server, _ladder = _server(tier="pod")
    v = authorize(path="/cockpit/1889", origin=CONSOLE, token="cfop_good",
                  config=config(), token_verifier=lambda _t: Identity(),
                  resolver=server.resolve_cockpit_session)
    assert not v.ok and v.code == CLOSE_TIER_UNSUPPORTED


# --------------------------------------------------------------------------
# tier pod: refused by default, served only when the flag is on (CFOP-59 Phase B)
# --------------------------------------------------------------------------

def pod_session(**over):
    row = {"tier": "pod", "host": "", "investigation_id": 2242,
           "job_name": "cfop-cockpit-2242-abc",
           "attach_argv": ["kubectl", "attach", "-it", "-n", "apps",
                           "job/cfop-cockpit-2242-abc"]}
    row.update(over)
    return row


def test_a_pod_cockpit_is_refused_by_name_when_the_pod_tier_is_off():
    v = authorize(path="/cockpit/2242", origin=CONSOLE, token="t",
                  config=config(),  # pod_tier defaults False
                  token_verifier=lambda _t: Identity(),
                  resolver=lambda _i: pod_session())
    assert not v.ok
    assert v.code == CLOSE_TIER_UNSUPPORTED
    assert "bridge_pod_tier" in v.reason and "attach --spawn" in v.reason


def test_a_pod_cockpit_is_served_when_the_pod_tier_is_on():
    v = authorize(path="/cockpit/2242", origin=CONSOLE, token="t",
                  config=config(pod_tier=True),
                  token_verifier=lambda _t: Identity(),
                  resolver=lambda _i: pod_session())
    assert v.ok, v.reason
    assert v.session["attach_argv"][:2] == ["kubectl", "attach"]


def test_the_pod_tier_flag_does_not_loosen_any_other_gate():
    """Turning on the pod tier must not become a way past origin or scope."""
    foreign = authorize(path="/cockpit/2242", origin="http://evil.example", token="t",
                        config=config(pod_tier=True),
                        token_verifier=lambda _t: Identity(),
                        resolver=lambda _i: pod_session())
    assert not foreign.ok and foreign.code == CLOSE_FORBIDDEN

    readonly = authorize(path="/cockpit/2242", origin=CONSOLE, token="t",
                        config=config(pod_tier=True),
                        token_verifier=lambda _t: Identity(scopes=("read",)),
                        resolver=lambda _i: pod_session())
    assert not readonly.ok and readonly.code == CLOSE_FORBIDDEN


def test_build_bridge_config_reads_the_pod_tier_flag():
    assert build_bridge_config({"cockpit": {}}).pod_tier is False
    assert build_bridge_config({"cockpit": {"bridge_pod_tier": True}}).pod_tier is True


def test_env_overrides_the_pod_tier_flag(monkeypatch):
    monkeypatch.setenv("CFOP_COCKPIT_BRIDGE_POD_TIER", "1")
    assert build_bridge_config({"cockpit": {"bridge_pod_tier": False}}).pod_tier is True
