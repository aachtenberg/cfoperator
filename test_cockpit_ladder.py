"""The cockpit runtime ladder for non-k8s hosts — tiers 2/3 (CFOP-36).

These guard the *class* of regression the ladder can produce, which is not "the
argv changed" but "a session landed somewhere nobody meant it to, or outlived
its credential". Three of them are mutation-checked in the issue's record:

* the credential must never reach argv (a process table is world-readable);
* ``investigation.host_id`` must never be the answer to "which host" (it is the
  agent's own identity, and reading it is what made tier 1's nodeSelector a
  no-op in production);
* a forced ``--tier`` that is unavailable must fail rather than degrade.

Nothing here shells out: the ssh runner and the release fetcher are injected,
so the tests assert the exact commands rather than the runner's environment.
"""

import re
import time
from pathlib import Path

import pytest

from cockpit_ladder import (
    COCKPIT_ENTRYPOINT,
    CONTAINER_ARCHES,
    DEFAULT_CFASSIST_VERSION,
    EXPIRES_LABEL,
    TIER_CONTAINER,
    TIER_HOST,
    TIER_POD,
    TIER_SSH,
    HostCapabilities,
    HostCockpitSpawner,
    HostLadderConfig,
    host_agent_url,
    build_ladder_config,
    choose_tier,
    normalize_arch,
    parse_probe,
    prepare_ssh_identity,
    resolve_target_host,
    session_name,
)
from cockpit_spawn import CockpitConfig, CockpitSpawnError

ROOT = Path(__file__).parent
SECRET = "cfop_s3cret_do_not_leak"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def probe_reply(arch="aarch64", docker="no", podman="no", systemd_run="no",
                user_systemd="no", tmux="no", sudo="no") -> str:
    return (f"arch={arch}\ndocker={docker}\npodman={podman}\n"
            f"systemd-run={systemd_run}\nuser_systemd={user_systemd}\n"
            f"tmux={tmux}\nsudo={sudo}\n")


class FakeSSH:
    """Records every ssh invocation; answers by matching the remote command."""

    def __init__(self, *rules):
        self.calls = []  # (argv, stdin)
        self.rules = list(rules)  # (needle, (code, out, err))

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        remote = argv[-1]
        for needle, result in self.rules:
            if needle in remote:
                return result
        return (0, "", "")

    @property
    def commands(self):
        return [argv[-1] for argv, _stdin in self.calls]

    @property
    def stdins(self):
        return [stdin for _argv, stdin in self.calls]

    def matching(self, needle):
        return [c for c in self.commands if needle in c]


def minter(recorder=None):
    def mint(investigation_id, ttl_seconds, **kwargs):
        if recorder is not None:
            recorder.append({"investigation_id": investigation_id,
                             "ttl_seconds": ttl_seconds, **kwargs})
        return {"id": 42, "prefix": "cfop_abcd", "secret": SECRET}
    return mint


HOSTS = {
    "raspberrypi5": {"address": "10.0.0.15", "ssh": {"user": "sre",
                                                     "key_path": "/keys/id_rsa"}},
    "ubuntu-llm-01": {"address": "10.0.0.20", "ssh": {"user": "aachten"}},
}


def spawner(ssh, *, revoked=None, minted=None, binary=b"ELF-cfassist",
            **overrides) -> HostCockpitSpawner:
    cfg = HostLadderConfig(**{
        "image": "ghcr.io/aachtenberg/cfoperator-cockpit:main",
        # The realistic pair: tier 1's URL is cluster DNS (it is what the pod
        # calls) and the host tiers get their own, because a Pi cannot resolve
        # the first one.
        "agent_url": "http://cfoperator.apps.svc.cluster.local:8083",
        "host_agent_url": "http://10.0.0.14:8083",
        "llm_url": "http://ollama:11434", "llm_model": "gemma4:26b",
        "hosts": dict(HOSTS),
        **overrides})
    return HostCockpitSpawner(
        cfg,
        ssh_runner=ssh,
        fetcher=lambda url: binary,
        token_minter=minter(minted),
        token_revoker=(revoked.append if revoked is not None else None),
    )


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------

def test_probe_reads_every_capability():
    caps = parse_probe(probe_reply(arch="x86_64", docker="yes", systemd_run="yes",
                                   user_systemd="yes", tmux="yes", sudo="yes"))
    assert (caps.arch, caps.docker, caps.podman) == ("amd64", True, False)
    assert (caps.systemd_run, caps.user_systemd, caps.tmux, caps.sudo) == (True,) * 4
    assert caps.ok


def test_probe_tolerates_a_login_banner():
    """Real hosts print MOTDs, warnings and 'Last login' lines before the
    script runs. None of that may read as 'this host has no docker'."""
    noisy = ("Welcome to Ubuntu 24.04 LTS\n"
             "  * Support: https://ubuntu.com/pro\n"
             "Last login: Tue Aug 19 09:12:41 2026\n"
             + probe_reply(docker="yes") +
             "\n\nsome trailing junk\n")
    caps = parse_probe(noisy)
    assert caps.docker and caps.arch == "arm64"


def test_a_probe_that_says_nothing_is_an_error_not_an_empty_host():
    """'I could not ask' and 'the answer is no' must not be the same value:
    the second picks a tier, the first must refuse to."""
    ssh = FakeSSH(("uname", (0, "-bash: line 1: syntax error\n", "")))
    caps = spawner(ssh).probe("raspberrypi5")
    assert not caps.ok and "no readable output" in caps.error


def test_a_failed_probe_carries_the_ssh_error():
    ssh = FakeSSH(("uname", (255, "", "ssh: connect to host 10.0.0.15: No route to host")))
    caps = spawner(ssh).probe("raspberrypi5")
    assert not caps.ok and "No route to host" in caps.error


def test_the_probe_is_cached_and_refreshable():
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    s = spawner(ssh)
    s.probe("raspberrypi5")
    s.probe("raspberrypi5")
    assert len(ssh.matching("uname")) == 1, "a cached probe must not re-ssh"
    s.invalidate("raspberrypi5")
    s.probe("raspberrypi5")
    assert len(ssh.matching("uname")) == 2


def test_the_probe_runs_once_per_host_not_once_per_fleet():
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    s = spawner(ssh)
    s.probe("raspberrypi5")
    s.probe("ubuntu-llm-01")
    assert len(ssh.matching("uname")) == 2


@pytest.mark.parametrize("reported,want", [
    ("x86_64", "amd64"), ("aarch64", "arm64"), ("armv7l", "arm"),
    ("arm64", "arm64"), ("riscv64", "riscv64"), ("", ""),
])
def test_arch_normalisation(reported, want):
    assert normalize_arch(reported) == want


def test_the_probe_never_asks_the_docker_daemon():
    """`docker info` blocks for seconds when docker is installed but stopped,
    and this probe runs inline on a spawn someone is waiting for."""
    from cockpit_ladder import PROBE_SCRIPT
    assert "command -v" in PROBE_SCRIPT
    assert "docker info" not in PROBE_SCRIPT and "podman info" not in PROBE_SCRIPT


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------

def test_a_cluster_node_is_tier_one_whatever_else_it_has():
    caps = parse_probe(probe_reply(docker="yes", systemd_run="yes", user_systemd="yes"))
    tier, note = choose_tier(caps, is_cluster_node=True, has_host=True)
    assert tier == TIER_POD and "cluster node" in note


def test_no_resolved_host_still_gets_the_cockpit_tier_one_always_built():
    """Most investigations name no machine, and CFOP-35's answer for those —
    an unpinned pod — is a good one. The ladder must not turn "I do not know
    where" into "no cockpit for you"."""
    tier, note = choose_tier(None)
    assert tier == TIER_POD and "no affected host" in note


def test_a_host_that_is_neither_in_the_cluster_nor_the_inventory_falls_back_to_a_pod():
    """A VM nobody configured, or an alert label that is not a hostname. There
    is nowhere to ssh, and a pod beside it beats a refusal."""
    tier, note = choose_tier(None, has_host=True)
    assert tier == TIER_POD and "configured ssh host" in note


def test_an_unreachable_host_degrades_to_a_pod_and_names_the_reason():
    """A session *on* an unreachable box is impossible; a pod next to it still
    investigates it. But "I am on the Pi" and "I am next to the Pi" are
    different facts to debug from, so the note has to say which."""
    caps = HostCapabilities(error="Permission denied (publickey)")
    tier, note = choose_tier(caps, has_host=True)
    assert tier == TIER_POD
    assert "could not be probed" in note and "publickey" in note


def test_docker_on_a_supported_arch_is_tier_two():
    caps = parse_probe(probe_reply(arch="aarch64", docker="yes"))
    tier, note = choose_tier(caps, has_host=True)
    assert tier == TIER_CONTAINER and "docker" in note


def test_podman_counts_as_a_container_runtime():
    caps = parse_probe(probe_reply(podman="yes"))
    assert choose_tier(caps, has_host=True)[0] == TIER_CONTAINER


def test_a_32bit_host_degrades_past_the_container_tier():
    """The cockpit image is published amd64+arm64 only. A 32-bit Pi with docker
    would pull a manifest that has nothing for it — better to drop a rung."""
    caps = parse_probe(probe_reply(arch="armv7l", docker="yes", systemd_run="yes",
                                   user_systemd="yes"))
    tier, note = choose_tier(caps, has_host=True)
    assert tier == TIER_HOST
    assert "arm" not in CONTAINER_ARCHES


def test_systemd_run_without_a_manager_to_own_the_unit_is_not_tier_three():
    """systemd-run being on PATH is not enough: as a non-root ssh user with no
    user manager and no sudo, creating the transient unit fails — and a tier
    whose cleanup silently does not exist is worse than the honest 3b."""
    caps = parse_probe(probe_reply(systemd_run="yes"))
    tier, note = choose_tier(caps, has_host=True)
    assert tier == TIER_SSH and "transient unit" in note


def test_sudo_reaches_tier_three_only_when_the_deployment_permits_it():
    caps = parse_probe(probe_reply(systemd_run="yes", sudo="yes"))
    assert choose_tier(caps, has_host=True)[0] == TIER_SSH
    assert choose_tier(caps, has_host=True, allow_sudo=True)[0] == TIER_HOST


def test_the_bottom_rung_says_what_it_is_missing():
    caps = parse_probe(probe_reply())
    tier, note = choose_tier(caps, has_host=True)
    assert tier == TIER_SSH
    assert "no container runtime" in note and "no systemd-run" in note


def test_a_forced_tier_that_is_unavailable_is_an_error_not_a_downgrade():
    """Someone who types --tier container wants the container boundary. Giving
    them a bare process instead, silently, is the worst possible answer."""
    caps = parse_probe(probe_reply(systemd_run="yes", user_systemd="yes"))
    with pytest.raises(CockpitSpawnError) as exc:
        choose_tier(caps, requested=TIER_CONTAINER, has_host=True)
    assert exc.value.status == 409
    assert "neither docker nor podman" in str(exc.value)
    assert TIER_HOST in str(exc.value), "the refusal should name what IS available"


def test_a_forced_tier_that_is_available_wins_over_a_higher_one():
    caps = parse_probe(probe_reply(docker="yes"))
    tier, note = choose_tier(caps, requested=TIER_SSH, is_cluster_node=True, has_host=True)
    assert tier == TIER_SSH and "requested" in note


def test_an_unknown_tier_is_a_client_error():
    with pytest.raises(CockpitSpawnError) as exc:
        choose_tier(parse_probe(probe_reply()), requested="kubernetes")
    assert exc.value.status == 400


def test_a_failed_probe_leaves_only_the_pod_tier():
    """Nothing below tier 1 can be chosen on evidence that was never gathered:
    forcing one has to be refused rather than attempted blind."""
    caps = HostCapabilities(error="no route to host")
    assert choose_tier(caps, is_cluster_node=True, has_host=True)[0] == TIER_POD
    for tier in (TIER_CONTAINER, TIER_HOST, TIER_SSH):
        with pytest.raises(CockpitSpawnError) as exc:
            choose_tier(caps, requested=tier, has_host=True)
        assert exc.value.status == 409 and "no route" in str(exc.value)


# --------------------------------------------------------------------------
# which host
# --------------------------------------------------------------------------

def test_the_investigations_own_host_id_is_never_the_answer():
    """MUTATION GUARD. ``Investigation.host_id`` is the area-of-responsibility
    field: the agent sets it to its own id ('cfoperator') on every row. Reading
    it is exactly how tier 1's nodeSelector came to ask the cluster for a node
    named 'cfoperator' and conclude, every single time, that no finding was
    host-level. Point ``resolve_target_host`` back at it and this fails."""
    host, why = resolve_target_host(
        investigation={"host_id": "cfoperator", "trigger": "Pod immich-kiosk-0 not ready"},
        known_hosts=["raspberrypi5", "cfoperator"],
    )
    assert host != "cfoperator", "host_id names the agent, not the affected machine"
    assert host == "" and "no affected host" in why


def test_the_remediation_row_is_where_the_affected_host_survives():
    host, why = resolve_target_host(
        remediation_hosts=["raspberrypi5"],
        investigation={"host_id": "cfoperator", "trigger": "disk pressure"},
        known_hosts=["raspberrypi5"],
    )
    assert host == "raspberrypi5" and "remediation" in why


def test_placeholder_hosts_on_a_remediation_row_are_skipped():
    """queue_remediation defaults host_id to 'default' when the finding named
    no host — a real column value that is not a real machine."""
    host, _why = resolve_target_host(
        remediation_hosts=["default", "", "ubuntu-llm-01"],
        known_hosts=["ubuntu-llm-01"],
    )
    assert host == "ubuntu-llm-01"


def test_an_explicit_request_beats_everything():
    host, why = resolve_target_host(
        requested="ubuntu-llm-01",
        remediation_hosts=["raspberrypi5"],
        known_hosts=["raspberrypi5", "ubuntu-llm-01"],
    )
    assert host == "ubuntu-llm-01" and "caller" in why


def test_a_host_named_in_the_trigger_is_matched_against_the_inventory():
    host, why = resolve_target_host(
        investigation={"trigger": "InstanceDown: instance=raspberrypi5:9100 job=node"},
        known_hosts=["raspberrypi5", "raspberrypi4"],
    )
    assert host == "raspberrypi5" and "named in" in why


def test_findings_are_searched_as_well_as_the_trigger():
    host, _why = resolve_target_host(
        investigation={"trigger": "etcd latency",
                       "findings": {"hypothesis": "the NIC on ubuntu-cm5-01 has hung again"}},
        known_hosts=["ubuntu-cm5-01"],
    )
    assert host == "ubuntu-cm5-01"


def test_a_host_that_is_only_a_prefix_of_another_is_not_matched():
    """'pi' must not match 'raspberrypi5', and 'raspberrypi4' must not match
    'raspberrypi45'. A wrong match puts the session on the wrong machine."""
    host, _why = resolve_target_host(
        investigation={"trigger": "raspberrypi45 is unreachable"},
        known_hosts=["raspberrypi4", "pi"],
    )
    assert host == ""


def test_only_configured_hosts_are_guessed_at():
    """Guessing beyond the inventory has no upside: an unconfigured name has no
    address or credential, so the spawn would fail one step later anyway."""
    host, _why = resolve_target_host(
        investigation={"trigger": "node headless-gpu NotReady"}, known_hosts=["raspberrypi5"])
    assert host == ""


# --------------------------------------------------------------------------
# tier 2 — the container
# --------------------------------------------------------------------------

def container_spawn(ssh=None, **kw):
    ssh = ssh or FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                         ("docker run", (0, "9f2ac0ffee\n", "")))
    s = spawner(ssh, **kw)
    result = s.spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=14400)
    return ssh, result


def test_the_container_is_detached_so_it_survives_the_spawn_call():
    """The issue proposed `docker run --rm`. Spawn and attach are different ssh
    connections, so a foreground container would be destroyed the moment the
    spawn call returned — the same reason tier 1 creates a Job and attaches
    separately."""
    ssh, result = container_spawn()
    run = ssh.matching("docker run")[0]
    assert "--detach" in run and "--rm" not in run
    assert result["tier"] == TIER_CONTAINER and result["status"] == "spawned"


def test_the_container_carries_the_labels_the_janitor_finds_it_by():
    ssh, _result = container_spawn()
    run = ssh.matching("docker run")[0]
    assert "cfop.dev/role=cockpit" in run
    assert "cfop-cockpit=1889" in run
    assert re.search(rf"{re.escape(EXPIRES_LABEL)}=\d{{10}}", run), \
        "the janitor compares an integer expiry, not a rendered date"


def test_the_session_token_never_reaches_the_container_argv():
    """MUTATION GUARD. argv lands in the host's process table, in this agent's
    own subprocess logging, and in anyone's `ps`. Pass the secret as `-e
    CFOP_API_TOKEN=...` instead of over stdin and this fails."""
    ssh, _result = container_spawn()
    for command in ssh.commands:
        assert SECRET not in command
    run = ssh.matching("docker run")[0]
    assert "--env-file /dev/stdin" in run or "--env-file '/dev/stdin'" in run
    assert any(stdin and SECRET in stdin.decode() for stdin in ssh.stdins), \
        "the credential must still reach the container, just not through argv"


def test_the_container_deadline_is_the_session_ttl():
    """Docker has no activeDeadlineSeconds, so the entrypoint is wrapped."""
    ssh, _result = container_spawn()
    run = ssh.matching("docker run")[0]
    assert "--entrypoint timeout" in run
    assert " 14400 " in run


def test_the_container_runs_the_same_entrypoint_as_the_pod():
    """The image is the invariant across the ladder: a tier changes the
    isolation, never what the session is."""
    ssh, _result = container_spawn()
    run = ssh.matching("docker run")[0]
    assert "cfoperator-cockpit:main" in run
    assert COCKPIT_ENTRYPOINT in run


def test_the_wrapped_entrypoint_is_the_one_the_image_actually_installs():
    """The image and this module ship separately, and tier 2 has to name the
    entrypoint because it wraps it in `timeout`. A path that drifts is a
    container that exits immediately with "no such file", on a host nobody is
    watching — which is how this was caught in the first place."""
    dockerfile = (ROOT / "cockpit" / "Dockerfile").read_text()
    declared = re.search(r'ENTRYPOINT\s*\[\s*"([^"]+)"', dockerfile)
    assert declared, "cockpit/Dockerfile no longer declares an exec-form ENTRYPOINT"
    assert COCKPIT_ENTRYPOINT == declared.group(1)


def test_the_container_attach_is_argv_over_the_operators_own_ssh():
    _ssh, result = container_spawn()
    assert result["attach_argv"][:2] == ["ssh", "-t"]
    assert result["attach_argv"][2] == "sre@10.0.0.15"
    assert result["attach_argv"][-3:] == ["docker", "attach", "cfop-cockpit-1889"]
    assert result["attach_command"] == " ".join(result["attach_argv"])


def test_podman_is_used_when_that_is_what_the_host_has():
    ssh = FakeSSH(("uname", (0, probe_reply(podman="yes"), "")),
                  ("podman run", (0, "abc123\n", "")))
    _ssh, result = container_spawn(ssh=ssh)
    assert result["runtime"] == "podman"
    assert result["attach_argv"][-3:] == ["podman", "attach", "cfop-cockpit-1889"]


def test_a_container_that_will_not_start_revokes_the_token_it_minted():
    """A credential whose session never existed is the orphan CFOP-32 exists to
    prevent."""
    revoked = []
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  ("docker run", (125, "", "no such image")))
    with pytest.raises(CockpitSpawnError) as exc:
        container_spawn(ssh=ssh, revoked=revoked)
    assert exc.value.status == 502 and "no such image" in str(exc.value)
    assert revoked == [42]


def test_live_session_finds_the_cockpit_that_is_already_there():
    """The lookup half of spawn, for the browser bridge (CFOP-75)."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  ("docker ps", (0, "cfop-cockpit-1889\n", "")))
    s = spawner(ssh)
    row = s.live_session(1889, host="raspberrypi5")
    assert row["status"] == "existing"
    assert row["tier"] == TIER_CONTAINER
    assert row["attach_argv"][0] == "ssh"


def test_live_session_never_starts_one():
    """The bridge attaches; it does not spawn. Spawning is a workload plus a
    minted credential and stays the admin-gated console route."""
    minted = []
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    s = spawner(ssh, minted=minted)
    assert s.live_session(1889, host="raspberrypi5") is None
    assert minted == []
    assert not ssh.matching("docker run")


def test_live_session_is_none_for_a_host_not_in_the_inventory():
    ssh = FakeSSH()
    assert spawner(ssh).live_session(1889, host="somebody-elses-box") is None
    assert ssh.calls == [], "an unknown host must not be reached out to"


def test_an_unreachable_host_is_not_reported_as_no_session():
    """probe() does not raise — it returns caps carrying an error — so without
    a check this reads as "there is no cockpit for #1889" when the truth is
    that the machine is down. During an incident whose subject is frequently
    that the machine is down, that is the wrong sentence.

    Mutation check: drop the `caps.error` guard in live_session and this goes
    red with None instead of raising.
    """
    ssh = FakeSSH(("uname", (255, "", "ssh: connect to host 10.0.0.15: No route to host")))
    with pytest.raises(CockpitSpawnError) as exc:
        spawner(ssh).live_session(1889, host="raspberrypi5")
    assert "could not be probed" in str(exc.value)
    assert "No route to host" in str(exc.value)


def test_an_existing_container_is_reported_not_duplicated():
    """Re-running the command the alert told you to run must land you back in
    your own cockpit, not beside it and not against a busy-fleet error."""
    minted = []
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  ("docker ps", (0, "cfop-cockpit-1889\n", "")))
    s = spawner(ssh, minted=minted)
    result = s.spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=14400)
    assert result["status"] == "existing"
    assert minted == [], "an existing cockpit must not mint a second credential"
    assert not ssh.matching("docker run")


# --------------------------------------------------------------------------
# tiers 3 / 3b — the process
# --------------------------------------------------------------------------

def host_spawn(ssh=None, tier=TIER_HOST, **kw):
    ssh = ssh or FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    s = spawner(ssh, **kw)
    result = s.spawn(1889, host="raspberrypi5", tier=tier, ttl_seconds=14400)
    return ssh, result


def test_the_binary_is_pushed_by_the_agent_not_pulled_by_the_host():
    """An incident host may have no route out — and the box that has lost its
    network is exactly the one someone wants a cockpit on."""
    ssh, _result = host_spawn()
    delivery = [i for i, c in enumerate(ssh.commands) if "cat > /tmp/cfop-cockpit-1889/cfassist" in c]
    assert delivery, f"no binary delivery in {ssh.commands}"
    assert ssh.stdins[delivery[0]] == b"ELF-cfassist"
    assert not any("curl" in c or "wget" in c for c in ssh.commands)


def test_the_delivered_binary_is_the_pinned_release():
    fetched = []
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    s = HostCockpitSpawner(
        HostLadderConfig(hosts=dict(HOSTS)),
        ssh_runner=ssh,
        fetcher=lambda url: (fetched.append(url), b"ELF")[1],
        token_minter=minter(),
    )
    s.spawn(1889, host="raspberrypi5", tier=TIER_HOST, ttl_seconds=60)
    assert fetched == [
        f"https://github.com/aachtenberg/cfoperator/releases/download/"
        f"cfassist-v{DEFAULT_CFASSIST_VERSION}/cfassist-linux-arm64"]


def test_the_pinned_cfassist_version_tracks_the_go_tree():
    """A Version bump without a matching release tag leaves tier 3 fetching a
    tag that does not exist. That failure is loud (a 404 naming the asset), but
    it is better caught here, at the bump."""
    go_version = re.search(r'var Version = "([^"]+)"',
                           (ROOT / "cfassist-go" / "internal" / "config" / "config.go").read_text())
    assert go_version, "cfassist-go no longer declares Version where this test looks"
    assert DEFAULT_CFASSIST_VERSION == go_version.group(1), (
        "cockpit_ladder.DEFAULT_CFASSIST_VERSION must match cfassist-go's Version, "
        "and cfassist-v<version> must be tagged before a cockpit can use it")


def test_the_credential_lands_in_a_file_never_in_argv():
    """MUTATION GUARD, tier 3 half: this is the tier with no isolation left, so
    the token IS the security model. Interpolate it into the ssh command and
    this fails."""
    ssh, _result = host_spawn()
    for command in ssh.commands:
        assert SECRET not in command
    assert any(stdin and SECRET in stdin.decode() for stdin in ssh.stdins)
    install = [c for c in ssh.commands if "cat > payload" in c]
    assert install and "chmod 600 env" in install[0]


def test_the_session_directory_is_created_under_a_umask():
    """chmod after the fact is a window: the directory holds a live credential
    for however long that window is."""
    ssh, _result = host_spawn()
    setup = [c for c in ssh.commands if "mkdir -p" in c][0]
    assert setup.startswith("umask 077")


def test_the_runner_reads_the_token_from_the_file_and_mints_nothing():
    ssh, result = host_spawn()
    payload = [s for s in ssh.stdins if s and b"#!/bin/sh" in s][0].decode()
    runner = payload.split("----")[0]
    assert ". ./env" in runner
    assert "--no-session-token" in runner, (
        "the session already holds a credential that dies with it; minting a "
        "second would create one whose revoke-on-exit never runs")
    assert "attach 1889" in runner
    assert "timeout --foreground 14400" in runner, (
        "the deadline wrapper lost --foreground; see the interactive guard below")
    assert "trap 'rm -rf /tmp/cfop-cockpit-1889' EXIT" in runner


def timer_commands(ssh):
    """The self-destruct invocations only. The probe script mentions
    systemd-run too — it is looking for it — so a bare-word match would find
    the probe and pass whether or not a timer was ever armed."""
    return [c for c in ssh.commands if "--on-active" in c]


def armed_timer(ssh):
    """Just the `systemd-run` that arms the timer, with the cancel prefix that
    now precedes it stripped off."""
    return timer_commands(ssh)[0].split("; ")[-1]


def test_the_transient_timer_expires_the_session_even_if_nobody_attaches():
    ssh, result = host_spawn()
    timer = timer_commands(ssh)
    assert timer, f"tier host must arm a self-destruct timer; got {ssh.commands}"
    assert "--user" in timer[0]
    assert "--on-active=14400s" in timer[0]
    assert "--unit=cfop-cockpit-1889-reap" in timer[0]
    assert "/tmp/cfop-cockpit-1889" in timer[0]
    assert "transient timer" in result["placement"]["note"]


def test_sudo_owns_the_timer_when_there_is_no_user_manager():
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", sudo="yes"), "")))
    _ssh, result = host_spawn(ssh=ssh, allow_sudo=True)
    assert armed_timer(ssh).startswith("sudo -n systemd-run")


def test_tier_3b_has_no_timer_and_says_so():
    """That absence *is* tier 3b. Claiming a cleanup that does not exist would
    be worse than the honest note plus a janitor."""
    ssh, result = host_spawn(tier=TIER_SSH)
    assert not timer_commands(ssh)
    assert "janitor" in result["placement"]["note"]


def test_a_timer_that_cannot_be_armed_downgrades_the_note_not_the_session():
    """An operator mid-incident must not lose their cockpit to a systemd quirk;
    the janitor covers the case either way."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")),
                  ("systemd-run", (1, "", "Failed to connect to bus")))
    _ssh, result = host_spawn(ssh=ssh)
    assert result["status"] == "spawned"
    assert "refused" in result["placement"]["note"]
    assert "janitor" in result["placement"]["note"]


def test_an_undeliverable_binary_revokes_the_token():
    revoked = []
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")),
                  ("cat > /tmp/cfop-cockpit-1889/cfassist", (1, "", "No space left on device")))
    with pytest.raises(CockpitSpawnError) as exc:
        host_spawn(ssh=ssh, revoked=revoked)
    assert "No space left" in str(exc.value)
    assert revoked == [42]


def test_an_arch_with_no_release_asset_is_refused_before_anything_is_minted():
    minted = []
    ssh = FakeSSH(("uname", (0, probe_reply(arch="riscv64", systemd_run="yes",
                                            user_systemd="yes"), "")))
    with pytest.raises(CockpitSpawnError) as exc:
        host_spawn(ssh=ssh, minted=minted)
    assert exc.value.status == 409 and "riscv64" in str(exc.value)


# --------------------------------------------------------------------------
# refusals that are not tier decisions
# --------------------------------------------------------------------------

def test_an_unconfigured_host_names_the_ones_that_are():
    ssh = FakeSSH()
    with pytest.raises(CockpitSpawnError) as exc:
        spawner(ssh).spawn(1, host="unknown-box", tier=TIER_SSH, ttl_seconds=60)
    assert exc.value.status == 400
    assert "raspberrypi5" in str(exc.value) and "ubuntu-llm-01" in str(exc.value)
    assert not ssh.calls, "an unreachable target must not be sshed to"


def test_no_host_at_all_is_a_client_error_not_a_crash():
    with pytest.raises(CockpitSpawnError) as exc:
        spawner(FakeSSH()).spawn(1, host="", tier=TIER_SSH, ttl_seconds=60)
    assert exc.value.status == 400


def test_no_minter_is_a_refusal_not_a_session_without_a_credential():
    s = HostCockpitSpawner(HostLadderConfig(hosts=dict(HOSTS)), ssh_runner=FakeSSH())
    with pytest.raises(CockpitSpawnError) as exc:
        s.spawn(1, host="raspberrypi5", tier=TIER_SSH, ttl_seconds=60)
    assert exc.value.status == 503


def test_the_mint_is_told_which_tier_and_host_it_is_for():
    """"Which runtime did this session get, and on what" is the first question
    asked of a cockpit after the fact, and the mint is the one event every tier
    shares."""
    minted = []
    container_spawn(minted=minted)
    assert minted[0]["tier"] == TIER_CONTAINER
    assert minted[0]["host"] == "raspberrypi5"
    assert minted[0]["ttl_seconds"] == 14400


# --------------------------------------------------------------------------
# the janitor
# --------------------------------------------------------------------------

def janitor_ssh(containers="", sessions="", **caps):
    caps.setdefault("docker", "yes")
    return FakeSSH(
        ("uname", (0, probe_reply(**caps), "")),
        ("docker ps", (0, containers, "")),
        ("for d in /tmp/cfop-cockpit-", (0, sessions, "")),
    )


def test_the_janitor_reaps_what_expired_and_spares_what_has_not():
    now = 1_800_000_000
    ssh = janitor_ssh(containers=(f"cfop-cockpit-11 {now - 60}\n"
                                  f"cfop-cockpit-22 {now + 3600}\n"))
    reaped = spawner(ssh).reap(["raspberrypi5"], now=now)
    assert [r["name"] for r in reaped] == ["cfop-cockpit-11"]
    removals = [c for c in ssh.commands if "docker rm" in c]
    assert removals == ["docker rm --force cfop-cockpit-11"]


def test_the_janitor_reaps_session_directories_too():
    now = 1_800_000_000
    ssh = janitor_ssh(sessions=(f"/tmp/cfop-cockpit-11 {now - 1}\n"
                                f"/tmp/cfop-cockpit-22 {now + 999}\n"))
    reaped = spawner(ssh).reap(["raspberrypi5"], now=now)
    assert [r["name"] for r in reaped] == ["cfop-cockpit-11"]
    # The directory removal now rides with a tmux kill (CFOP-59), so match the
    # substring rather than a whole command.
    assert any("rm -rf /tmp/cfop-cockpit-11" in c for c in ssh.commands)
    assert not [c for c in ssh.commands if "rm -rf /tmp/cfop-cockpit-22" in c]


def test_a_reaped_session_also_clears_a_failed_timer_unit():
    """A failed unit is not collected, and it would block the next spawn of the
    same name — which is the same investigation, later."""
    now = 1_800_000_000
    ssh = janitor_ssh(sessions=f"/tmp/cfop-cockpit-11 {now - 1}\n")
    spawner(ssh).reap(["raspberrypi5"], now=now)
    resets = [c for c in ssh.commands if "reset-failed" in c]
    assert resets and "cfop-cockpit-11-reap.service" in resets[0]


def test_an_unreadable_expiry_still_gets_reaped():
    """A spawn that died between mkdir and the install leaves a directory with
    no expiry. Sparing it forever is the leak the janitor exists to close."""
    now = 1_800_000_000
    ssh = janitor_ssh(sessions="/tmp/cfop-cockpit-11 not-a-number\n"
                               "/tmp/cfop-cockpit-12\n")
    reaped = spawner(ssh).reap(["raspberrypi5"], now=now)
    assert {r["name"] for r in reaped} == {"cfop-cockpit-11", "cfop-cockpit-12"}


def test_the_janitor_only_touches_its_own_prefix():
    now = 1_800_000_000
    ssh = janitor_ssh(sessions=f"/tmp/somebody-elses-tmpdir {now - 1}\n")
    assert spawner(ssh).reap(["raspberrypi5"], now=now) == []
    assert not [c for c in ssh.commands if "rm -rf" in c]


def test_the_janitor_finds_orphans_no_registry_would_remember():
    """It enumerates by convention rather than tracking what this process
    spawned: an agent that restarted — or a previous instance — leaves sessions
    nothing of ours remembers, and those are the tier-3b leak."""
    now = 1_800_000_000
    ssh = janitor_ssh(sessions=f"/tmp/cfop-cockpit-999 {now - 10}\n")
    fresh = spawner(ssh)  # has never spawned anything
    assert [r["name"] for r in fresh.reap(["raspberrypi5"], now=now)] == ["cfop-cockpit-999"]


def test_one_unreachable_host_does_not_stop_the_sweep():
    """The host that is down is frequently the one with the orphan on it."""
    now = 1_800_000_000

    class Selective(FakeSSH):
        def __call__(self, argv, stdin):
            if any("10.0.0.15" in a for a in argv):
                raise OSError("no route to host")
            return super().__call__(argv, stdin)

    ssh = Selective(("uname", (0, probe_reply(docker="yes"), "")),
                    ("docker ps", (0, f"cfop-cockpit-7 {now - 1}\n", "")))
    reaped = spawner(ssh).reap(["raspberrypi5", "ubuntu-llm-01"], now=now)
    assert [r["host"] for r in reaped] == ["ubuntu-llm-01"]


def test_the_janitor_sweeps_every_configured_host_by_default():
    now = 1_800_000_000
    ssh = janitor_ssh(containers=f"cfop-cockpit-3 {now - 1}\n")
    reaped = spawner(ssh).reap(now=now)
    assert sorted(r["host"] for r in reaped) == ["raspberrypi5", "ubuntu-llm-01"]


def test_a_host_that_cannot_be_probed_is_skipped_not_guessed_at():
    ssh = FakeSSH(("uname", (255, "", "Permission denied (publickey)")))
    assert spawner(ssh).reap(["raspberrypi5"], now=time.time()) == []
    assert not [c for c in ssh.commands if "rm" in c]


# --------------------------------------------------------------------------
# ssh plumbing
# --------------------------------------------------------------------------

def test_ssh_never_prompts_and_never_trusts_on_first_use():
    """This runs in a pod with no terminal: a passphrase or host-key prompt
    there is a hung spawn rather than an error."""
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    spawner(ssh).probe("raspberrypi5")
    argv = ssh.calls[0][0]
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=no" in argv
    assert argv[-2] == "sre@10.0.0.15"


def test_the_per_host_ssh_user_and_key_win_over_the_fleet_default():
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    s = spawner(ssh)
    s.probe("raspberrypi5")
    s.probe("ubuntu-llm-01")
    assert "/keys/id_rsa" in ssh.calls[0][0]
    assert ssh.calls[1][0][-2] == "aachten@10.0.0.20"


def test_the_ssh_identity_is_staged_with_key_safe_permissions(tmp_path):
    """A secret volume is root-owned and group-readable, which ssh refuses for
    a private key with an error that reads like a network problem."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE KEY")
    (secret / "..data").mkdir()  # secret volumes use dot-dir indirection
    home = tmp_path / "home"

    assert prepare_ssh_identity(str(secret), str(home / ".ssh"))
    staged = home / ".ssh" / "id_rsa"
    assert staged.read_text() == "PRIVATE KEY"
    assert oct(staged.stat().st_mode & 0o777) == "0o600"
    assert not (home / ".ssh" / "..data").exists()


def test_a_missing_ssh_secret_is_reported_not_crashed_on(tmp_path):
    assert prepare_ssh_identity(str(tmp_path / "nope")) is False


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_the_ladder_inherits_the_image_and_model_from_tier_one():
    """A container on a Pi and a pod in the cluster must brief the same session
    against the same model; two resolutions of that is how they drift."""
    cockpit = CockpitConfig(image="ghcr.io/x/cockpit:v1", agent_url="http://agent:8083",
                            llm_url="http://ollama:11434", llm_model="gemma4:26b")
    cfg = build_ladder_config({"infrastructure": {"hosts": HOSTS}}, cockpit)
    assert cfg.image == "ghcr.io/x/cockpit:v1"
    assert cfg.llm_model == "gemma4:26b"
    assert sorted(cfg.hosts) == ["raspberrypi5", "ubuntu-llm-01"]


def test_an_install_with_no_ssh_inventory_simply_has_no_host_tiers():
    """A cluster-only install has no infrastructure.hosts, and must not fail
    building a ladder it will never climb."""
    cfg = build_ladder_config({}, CockpitConfig())
    assert cfg.hosts == {}
    assert HostCockpitSpawner(cfg).known_host_names() == []


def test_sudo_is_off_unless_the_deployment_turns_it_on():
    assert build_ladder_config({}, CockpitConfig()).allow_sudo is False
    assert build_ladder_config({"cockpit": {"allow_sudo": True}},
                               CockpitConfig()).allow_sudo is True


def test_the_session_name_is_stable_across_spawns():
    """Unlike the tier-1 Job name, which carries a timestamp: a Job is addressed
    through the API, but a container and a directory are addressed by
    convention, and the dedupe, the attach and the janitor must all agree."""
    assert session_name(1889) == "cfop-cockpit-1889" == session_name(1889)


# --------------------------------------------------------------------------
# POST /api/cockpit/spawn, through the ladder
# --------------------------------------------------------------------------

def _ladder_client(ssh, *, investigation=None, hosts=None, remediations=(),
                   node_names=()):
    """The real WebServer routes with a real HostCockpitSpawner behind them.

    ``node_names`` is which of the configured hosts are ALSO cluster nodes.
    Empty (the default) is a cluster that contains none of them, which is what
    makes the endpoint take the ladder at all. Naming one gives the
    dual-membership shape most of a real fleet has — a machine that is both a
    Kubernetes node and an ``infrastructure.hosts`` entry — which is where the
    forced-tier bug lived.
    """
    import os
    import threading
    from unittest.mock import MagicMock

    from flask import Flask

    from cockpit_spawn import CockpitSpawner
    from web_auth import install_auth
    from web_server import WebServer

    operator = MagicMock()
    operator.kb.get_investigation.return_value = (
        {"id": 1889, "host_id": "cfoperator", "trigger": "node down"}
        if investigation is None else investigation)
    operator.kb.list_remediations_for_investigation.return_value = [
        {"host_id": h} for h in remediations]
    operator.config = {"infrastructure": {"hosts": hosts if hosts is not None else HOSTS}}

    server = WebServer.__new__(WebServer)
    server.operator = operator
    server.host, server.port = "localhost", 0
    server.app = Flask(__name__)
    server.sock = None
    server.ws_clients = []
    server._chat_sessions = {}
    server._sessions_lock = threading.Lock()
    server.auth_store = None
    # A cluster that exists but contains none of these hosts: `get node` is a
    # NotFound, everything else works. That is what makes the endpoint take the
    # ladder rather than pinning, while tier 1 stays spawnable for the
    # investigations that name no machine at all.
    def kubectl(args, stdin):
        if args[:2] == ["get", "node"]:
            if args[-1] in node_names:
                return 0, '{"spec": {}}', ""
            return 1, "", 'Error from server (NotFound): nodes "x" not found'
        if args[:2] == ["get", "jobs"]:
            return 0, '{"items": []}', ""
        return 0, '{"metadata": {"uid": "uid-1"}}', ""

    server._cockpit = CockpitSpawner(
        CockpitConfig(namespace="apps"), kubectl_runner=kubectl, token_minter=minter())
    server._ladder = spawner(ssh)
    server._setup_routes()

    prior = os.environ.get("CFOP_AUTH_DISABLED")
    os.environ["CFOP_AUTH_DISABLED"] = "1"
    try:
        install_auth(server.app, store=None)
    finally:
        if prior is None:
            os.environ.pop("CFOP_AUTH_DISABLED", None)
        else:
            os.environ["CFOP_AUTH_DISABLED"] = prior
    return server.app.test_client(), server


def test_the_endpoint_takes_the_ladder_for_a_host_outside_the_cluster():
    """The end-to-end shape of the issue: a needs_human investigation whose
    remediation names a bare Pi produces a session on that Pi, not a pod."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    client, _server = _ladder_client(ssh, remediations=["raspberrypi5"])

    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == TIER_HOST
    assert body["host"] == "raspberrypi5"
    assert body["attach_argv"][0] == "ssh"
    assert "remediation" in body["host_provenance"]
    assert "transient unit" in body["tier_note"]


def test_the_endpoint_still_spawns_a_pod_when_no_host_is_named():
    """The common case, and CFOP-35's behaviour: most investigations name no
    machine, and an unpinned pod is the right answer for them."""
    ssh = FakeSSH()
    client, _server = _ladder_client(ssh, investigation={"id": 1889, "host_id": "cfoperator",
                                                         "trigger": "Pod x not ready"})
    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == TIER_POD
    assert not ssh.calls, "a cluster spawn must not probe the fleet over ssh"


def test_the_endpoint_refuses_an_unavailable_forced_tier_with_409():
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    client, _server = _ladder_client(ssh, remediations=["raspberrypi5"])
    resp = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": "container"})
    assert resp.status_code == 409
    assert "docker" in resp.get_json()["error"]


def test_an_unknown_host_lands_in_the_cluster_rather_than_failing():
    """docs/cockpit.md's troubleshooting table promises this exact shape: not
    knowing how to reach a host is not an error, because a cockpit next to the
    problem beats no cockpit at all."""
    ssh = FakeSSH()
    client, _server = _ladder_client(ssh, remediations=["a-vm-nobody-configured"])
    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == TIER_POD
    assert "neither a cluster node nor a configured ssh host" in body["tier_note"]
    assert not ssh.calls, "an unreachable target must not be sshed to"


def test_forcing_a_host_tier_on_an_unknown_host_refuses_instead():
    """The other half of that promise: once you have asked for something
    specific, quietly giving you a pod would be the wrong answer."""
    client, _server = _ladder_client(FakeSSH(), remediations=["a-vm-nobody-configured"])
    resp = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": TIER_SSH})
    assert resp.status_code == 409
    assert "is not in infrastructure.hosts" in resp.get_json()["error"]


def test_an_unreachable_host_lands_in_the_cluster_and_names_the_reason():
    ssh = FakeSSH(("uname", (255, "", "Permission denied (publickey)")))
    client, _server = _ladder_client(ssh, remediations=["raspberrypi5"])
    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == TIER_POD
    assert "could not be probed" in body["tier_note"]
    assert "publickey" in body["tier_note"]


def test_an_explicit_host_override_reaches_the_resolver():
    """`--host` is the documented escape hatch for a heuristic that guessed
    wrong, so it has to beat everything the heuristic would have found."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    client, _server = _ladder_client(ssh, remediations=["raspberrypi5"])
    body = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "host": "ubuntu-llm-01"}).get_json()
    assert body["host"] == "ubuntu-llm-01"
    assert body["host_provenance"] == "requested by the caller"


def test_the_endpoint_400s_an_unknown_tier():
    client, _server = _ladder_client(FakeSSH())
    resp = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": "kubernetes"})
    assert resp.status_code == 400


def test_a_remediation_lookup_failure_costs_precision_not_the_spawn():
    """The queue is one input to "which host", not a dependency of spawning."""
    ssh = FakeSSH()
    client, server = _ladder_client(ssh)
    server.operator.kb.list_remediations_for_investigation.side_effect = RuntimeError("db down")
    resp = client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert resp.status_code == 201
    assert resp.get_json()["tier"] == TIER_POD


def test_reap_cockpits_is_a_no_op_when_nothing_is_configured():
    """A cluster-only install has no infrastructure.hosts and the janitor tick
    must cost it nothing — it runs on the agent's worker loop forever."""
    ssh = FakeSSH()
    _client, server = _ladder_client(ssh, hosts={})
    server._ladder = HostCockpitSpawner(HostLadderConfig(hosts={}), ssh_runner=ssh)
    assert server.reap_cockpits() == 0
    assert not ssh.calls


def test_reap_cockpits_never_raises_into_the_worker_loop():
    """It runs beside the remediation drainer; an exception here would take
    that thread's tick with it."""
    def explode(argv, stdin):
        raise RuntimeError("ssh vanished")

    _client, server = _ladder_client(FakeSSH())
    server._ladder = HostCockpitSpawner(HostLadderConfig(hosts=dict(HOSTS)),
                                        ssh_runner=explode)
    assert server.reap_cockpits() == 0


# --------------------------------------------------------------------------
# the tier-3 runner, actually run
# --------------------------------------------------------------------------

def _materialise_session(tmp_path, ttl=5, tier=TIER_HOST):
    """Write the generated runner and env into a real directory with a stub
    cfassist, so the script can be executed rather than pattern-matched."""
    import os
    import stat as stat_mod

    directory = tmp_path / "cfop-cockpit-1889"
    directory.mkdir()
    s = HostCockpitSpawner(HostLadderConfig(agent_url="http://agent:8083"))
    (directory / "run").write_text(
        s._runner_script(1889, str(directory), ttl, tier=tier))
    (directory / "run").chmod(0o700)
    (directory / "env").write_bytes(
        s._env_file(1889, {"secret": SECRET}))
    (directory / "env").chmod(0o600)
    (directory / "expires").write_text("1")
    cfassist = directory / "cfassist"
    cfassist.write_text('#!/bin/sh\necho "argv: $*"\necho "token: $CFOP_API_TOKEN"\n')
    cfassist.chmod(0o700)
    return directory


def test_the_runner_actually_removes_the_session_on_exit(tmp_path):
    """REGRESSION GUARD. The first version of this script ended in `exec`,
    which replaces the shell — and a replaced shell runs no traps, so the
    credential and the binary survived every ordinary exit. "Leaves nothing
    behind" is the promise the whole tier rests on, and pattern-matching the
    script would not have caught it. So: run it, and look."""
    import subprocess

    directory = _materialise_session(tmp_path)
    proc = subprocess.run(["/bin/sh", str(directory / "run")],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "argv: attach 1889" in proc.stdout
    assert SECRET in proc.stdout, "the session must inherit the dying credential"
    assert not directory.exists(), (
        f"the session directory survived the session: {proc.stdout}{proc.stderr}")


def test_the_runner_reports_a_timed_out_session_rather_than_swallowing_it(tmp_path):
    """`timeout` firing is exit 124, and a session that hit its TTL is a normal
    outcome to pass back — but it must still clean up on the way out."""
    import subprocess

    directory = _materialise_session(tmp_path, ttl=1)
    # `exec`, so the thing being timed out IS timeout's direct child — which is
    # the real shape: cfassist is a binary, not a shell wrapping one. It matters
    # because --foreground (see the guards below) trades whole-group kills for a
    # usable terminal, so timeout signals the direct child only.
    (directory / "cfassist").write_text('#!/bin/sh\nexec sleep 30\n')
    (directory / "cfassist").chmod(0o700)
    proc = subprocess.run(["/bin/sh", str(directory / "run")],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 124
    assert not directory.exists()


def test_the_runner_is_posix_sh_not_bash(tmp_path):
    """A Pi's /bin/sh is dash, and the runner is invoked as /bin/sh — a
    bashism here would fail on the hosts this tier exists for."""
    import subprocess

    directory = _materialise_session(tmp_path)
    assert subprocess.run(["/bin/sh", "-n", str(directory / "run")]).returncode == 0


# --------------------------------------------------------------------------
# review round 1 — the four ways the documented path broke
# --------------------------------------------------------------------------

def test_a_probe_failure_is_not_cached_for_the_success_ttl():
    """REGRESSION GUARD. docs/cockpit.md's setup step 3 is: run the smoke test,
    see `Permission denied (publickey)`, mount the key, retry. Caching that
    failure for fifteen minutes makes the fix invisible — the retry answers
    from the cache until the agent restarts, and the operator concludes the
    mount did not work.

    A capability is a fact about a host and keeps; a failure to reach one is a
    fact about a moment.
    """
    replies = iter([(255, "", "Permission denied (publickey)"),
                    (0, probe_reply(docker="yes"), "")])

    def ssh(argv, stdin):
        return next(replies)

    s = spawner(ssh, probe_cache_seconds=900, ssh_connect_timeout=5)
    failed = s.probe("raspberrypi5")
    assert not failed.ok
    assert s._cache_ttl(failed) <= 5, "a failure must not be kept for the success TTL"

    # The helm upgrade lands and the operator retries a few seconds later.
    s._probes["raspberrypi5"].probed_at -= 10
    assert s.probe("raspberrypi5").docker, (
        "the retry answered from a stale failure; the fix would look like it "
        "had not worked")


def test_a_successful_probe_is_still_cached():
    """The negative-caching fix must not turn every spawn into a probe storm:
    a capability is a fact about a host and keeps."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    s = spawner(ssh, probe_cache_seconds=900)
    s.probe("raspberrypi5")
    s.probe("raspberrypi5")
    assert len(ssh.matching("uname")) == 1


def test_a_forced_tier_reprobes_rather_than_trusting_a_cached_failure():
    """`--tier` is what an operator reaches for after fixing something. It has
    to look at the host again, not at what the host looked like before the fix."""
    replies = iter([(255, "", "Permission denied (publickey)"),
                    (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")])

    def ssh(argv, stdin):
        return next(replies)

    s = spawner(ssh, probe_cache_seconds=900)
    s.probe("raspberrypi5")
    assert s.probe("raspberrypi5", refresh=True).systemd_run


# ---- the agent URL a host can actually resolve -----------------------------

def test_a_cluster_only_agent_url_is_refused_before_anything_is_created():
    """REGRESSION GUARD. `cockpit.agent_url` is documented as "what the POD
    calls" and defaults to cluster DNS. Inheriting it for a host tier puts an
    unresolvable name on the Pi, and the failure lands *after* the operator has
    attached — a briefed session with no briefing. Better to refuse at spawn
    and name the key to set."""
    minted, ssh = [], FakeSSH(("uname", (0, probe_reply(systemd_run="yes"), "")))
    s = spawner(ssh, minted=minted, host_agent_url="")
    with pytest.raises(CockpitSpawnError) as exc:
        s.spawn(1889, host="raspberrypi5", tier=TIER_SSH, ttl_seconds=60)
    assert exc.value.status == 400
    assert "cockpit.host_agent_url" in str(exc.value)
    assert minted == [], "nothing may be minted for a session that cannot brief itself"
    assert not ssh.calls, "and nothing may be created on the host either"


@pytest.mark.parametrize("url", [
    "http://cfoperator.apps.svc.cluster.local:8083",
    "http://cfoperator.apps.svc:8083",
])
def test_every_shape_of_in_cluster_name_is_caught(url):
    with pytest.raises(CockpitSpawnError):
        host_agent_url(HostLadderConfig(agent_url=url))


def test_the_host_url_wins_over_the_pod_url_without_changing_it():
    """One knob cannot serve both runtimes — and CFOP_COCKPIT_AGENT_URL would
    have retargeted the pod as a side effect of fixing the Pi."""
    cfg = HostLadderConfig(agent_url="http://cfoperator.apps.svc.cluster.local:8083",
                           host_agent_url="http://10.0.0.14:8083")
    assert host_agent_url(cfg) == "http://10.0.0.14:8083"
    assert cfg.agent_url == "http://cfoperator.apps.svc.cluster.local:8083", (
        "tier 1 must keep calling cluster DNS; that is the address a pod can use")


def test_the_session_is_told_the_host_reachable_url():
    ssh, _result = host_spawn()
    env = [s for s in ssh.stdins if s and b"CFOP_AGENT_URL" in s][0].decode()
    assert "CFOP_AGENT_URL=http://10.0.0.14:8083" in env
    assert "svc.cluster.local" not in env


# ---- re-running the alert command after `exit` -----------------------------

def test_an_exited_container_does_not_block_the_next_spawn():
    """REGRESSION GUARD. The ordinary path is: spawn, work, `exit`, run the
    alert command again. `exit` from a docker attach *stops* the container,
    which keeps the name — so the second `docker run --name` 502s and the
    operator is locked out for the whole TTL, which is precisely what the
    how-to promises they will never have to think about."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  # `docker ps` (running only) is empty: the corpse is stopped.
                  ("docker ps", (0, "", "")),
                  ("docker run", (0, "9f2ac0ffee\n", "")))
    s = spawner(ssh)
    result = s.spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=14400)
    assert result["status"] == "spawned"
    run = ssh.matching("docker run")[0]
    assert run.startswith("docker rm cfop-cockpit-1889 >/dev/null 2>&1;"), (
        f"the exited namesake is never cleared: {run}")


def test_clearing_a_namesake_will_not_kill_a_running_one():
    """`rm` without --force on purpose. A *running* namesake was already
    returned by the dedupe, so anything left is a corpse — and if the two ever
    race, failing loudly beats killing somebody's live session."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  ("docker ps", (0, "", "")),
                  ("docker run", (0, "abc\n", "")))
    spawner(ssh).spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=60)
    assert "docker rm --force" not in ssh.matching("docker run")[0]


def test_a_still_armed_reap_timer_is_cancelled_before_the_next_is_set():
    """REGRESSION GUARD, and the sharper half of the same bug. The reap unit is
    named per investigation, so after `exit` the previous session's timer is
    still counting down against the same `/tmp` path. Leaving it does two bad
    things at once: `systemd-run` fails because the unit exists, and then the
    OLD timer fires and deletes the NEW session out from under the operator."""
    ssh, _result = host_spawn()
    armed = timer_commands(ssh)[0]
    assert "systemctl --user stop cfop-cockpit-1889-reap.timer" in armed
    assert "reset-failed" in armed, (
        "a failed unit is not collected, so it keeps the name and blocks the "
        "next spawn for the same investigation")
    assert armed.index("stop") < armed.index("--on-active"), "cancel, then arm"


def test_the_janitor_cancels_a_sudo_armed_timer_too():
    """A session armed through sudo leaves a *system* unit that
    `systemctl --user` cannot see, so cancelling only the user one leaves
    exactly the orphan the sweep exists to remove."""
    now = 1_800_000_000
    ssh = janitor_ssh(sessions=f"/tmp/cfop-cockpit-11 {now - 1}\n")
    spawner(ssh).reap(["raspberrypi5"], now=now)
    cancels = [c for c in ssh.commands if "reset-failed" in c]
    assert cancels, "nothing cancelled the reap unit"
    assert "systemctl --user reset-failed" in cancels[0]
    assert "sudo -n systemctl reset-failed" in cancels[0]


# ---- the cap that was only on tier 1 ---------------------------------------

def test_host_tiers_have_a_concurrency_cap_of_their_own():
    """REGRESSION GUARD. The cap lived in CockpitSpawner, so the ladder path
    never asked — and every host spawn mints an `investigate` token onto a
    machine with no cluster-side ceiling above it. "dedupe, concurrency cap,
    token mint, audit — must be central" was true of one tier only."""
    minted = []
    ssh = janitor_ssh(sessions=("/tmp/cfop-cockpit-11 9999999999\n"
                                "/tmp/cfop-cockpit-22 9999999999\n"),
                      docker="no", systemd_run="yes", user_systemd="yes")
    s = spawner(ssh, minted=minted, max_concurrent=2)
    with pytest.raises(CockpitSpawnError) as exc:
        s.spawn(1889, host="raspberrypi5", tier=TIER_HOST, ttl_seconds=60)
    assert exc.value.status == 429
    assert "cfop-cockpit-11" in str(exc.value), "the refusal should name what is holding it"
    assert minted == [], "a refused spawn must not leave a token on the host"


def test_the_cap_counts_containers_and_processes_together():
    """A box can carry both at once, and "how many cockpits are on this host"
    has one answer."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")),
                  ("docker ps", (0, "cfop-cockpit-11\n", "")),
                  ("for d in /tmp/cfop-cockpit-", (0, "/tmp/cfop-cockpit-22 9999999999\n", "")))
    s = spawner(ssh, max_concurrent=2)
    with pytest.raises(CockpitSpawnError) as exc:
        s.spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=60)
    assert exc.value.status == 429


def test_an_expired_directory_does_not_count_against_the_cap():
    """It belongs to the janitor, not to a session. Counting it would lock an
    operator out of a host until the next sweep."""
    ssh = janitor_ssh(sessions="/tmp/cfop-cockpit-11 1\n/tmp/cfop-cockpit-22 1\n",
                      docker="no", systemd_run="yes", user_systemd="yes")
    s = spawner(ssh, max_concurrent=2)
    assert s.spawn(1889, host="raspberrypi5", tier=TIER_HOST,
                   ttl_seconds=60)["status"] == "spawned"


def test_the_operators_own_session_is_never_the_thing_that_blocks_them():
    """Dedupe before cap, same as tier 1: re-running your own command at the
    cap must return your cockpit, not a 429."""
    ssh = janitor_ssh(sessions=("/tmp/cfop-cockpit-1889 9999999999\n"
                                "/tmp/cfop-cockpit-22 9999999999\n"),
                      docker="no", systemd_run="yes", user_systemd="yes")
    s = spawner(ssh, max_concurrent=2)
    assert s.spawn(1889, host="raspberrypi5", tier=TIER_HOST,
                   ttl_seconds=60)["status"] == "existing"


def test_a_container_tier_refuses_a_loopback_model_url():
    """A container has its own network namespace, same as a pod — the agent's
    hostNetwork loopback URL means the container itself there too."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    minted = []
    s = spawner(ssh, minted=minted, llm_url="http://127.0.0.1:11434")
    with pytest.raises(CockpitSpawnError) as exc:
        s.spawn(1889, host="raspberrypi5", tier=TIER_CONTAINER, ttl_seconds=60)
    assert exc.value.status == 400
    assert "CFOP_COCKPIT_LLM_URL" in str(exc.value)
    assert minted == []


def test_a_process_tier_accepts_a_loopback_model_url():
    """Tiers host/ssh run directly on the machine, so loopback is correct there
    whenever ollama is on that host — the rule is about network namespaces."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    s = spawner(ssh, llm_url="http://localhost:11434")
    assert s.spawn(1889, host="raspberrypi5", tier=TIER_HOST,
                   ttl_seconds=60)["status"] == "spawned"


# --------------------------------------------------------------------------
# a host that is BOTH a cluster node and an ssh host
# --------------------------------------------------------------------------

def test_a_host_tier_can_be_forced_on_a_host_that_is_also_a_node():
    """REGRESSION GUARD. Most of this fleet is both a Kubernetes node and an
    infrastructure.hosts entry, and for those "give me a shell on the machine,
    not a pod scheduled onto it" is a real request — raspberrypi4 drives a
    physical kiosk, and a pod on that node cannot see the session, the display
    or the USB devices the incident is about.

    The probe used to be skipped for any host that was a node, so choose_tier
    had no capabilities to honour the request with and 409'd. Correct on the
    evidence it had, and useless."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    client, _server = _ladder_client(ssh, node_names=("raspberrypi5",),
                                     remediations=["raspberrypi5"])

    body = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": TIER_HOST}).get_json()
    assert body["tier"] == TIER_HOST, f"forced host tier was refused: {body}"
    assert body["host"] == "raspberrypi5"
    assert body["attach_argv"][0] == "ssh"
    assert ssh.matching("uname"), "the host was never probed"


def test_auto_still_prefers_the_pod_for_a_cluster_node_without_probing():
    """The default must not change, and must not cost an ssh round trip: a
    node is tier 1 whatever else it has installed."""
    ssh = FakeSSH(("uname", (0, probe_reply(docker="yes"), "")))
    client, _server = _ladder_client(ssh, node_names=("raspberrypi5",),
                                     remediations=["raspberrypi5"])

    body = client.post("/api/cockpit/spawn", json={"investigation_id": 1889}).get_json()
    assert body["tier"] == TIER_POD
    assert not ssh.calls, "auto probed a cluster node it had already decided about"


def test_forcing_a_tier_the_node_cannot_provide_still_refuses():
    """Probing more does not mean accepting more — a host with no container
    runtime still cannot give you a container."""
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    client, _server = _ladder_client(ssh, node_names=("raspberrypi5",),
                                     remediations=["raspberrypi5"])

    resp = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": TIER_CONTAINER})
    assert resp.status_code == 409
    assert "docker" in resp.get_json()["error"]


def test_forcing_pod_on_a_node_does_not_probe():
    """`--tier pod` is the other override — look at the box from next door —
    and it needs no capabilities from the box."""
    ssh = FakeSSH(("uname", (0, probe_reply(), "")))
    client, _server = _ladder_client(ssh, node_names=("raspberrypi5",),
                                     remediations=["raspberrypi5"])

    body = client.post("/api/cockpit/spawn",
                       json={"investigation_id": 1889, "tier": TIER_POD}).get_json()
    assert body["tier"] == TIER_POD
    assert not ssh.calls


def test_a_forced_tier_busts_a_stale_probe_rather_than_trusting_it():
    """REGRESSION GUARD, and the second half of the publickey-then-retry hole.

    `--tier` is what an operator types *after* changing the box — installing
    docker, mounting a key. A single request against a cold cache is green
    whether or not `refresh=` is passed, so this drives two: the first caches
    "no docker", the operator installs it, and the second must look again.

    Without the refresh the second call answers from the stale capabilities and
    409s on a host that can now do exactly what was asked.
    """
    state = {"docker": "no"}

    def ssh(argv, stdin):
        remote = argv[-1]
        if "uname" in remote:
            return 0, probe_reply(docker=state["docker"]), ""
        if "docker ps" in remote:
            return 0, "", ""
        return 0, "9f2ac0ffee\n", ""

    client, _server = _ladder_client(ssh, node_names=("raspberrypi5",),
                                     remediations=["raspberrypi5"])

    # First spawn: no docker on the box, so the container tier is impossible.
    first = client.post("/api/cockpit/spawn",
                        json={"investigation_id": 1889, "tier": TIER_CONTAINER})
    assert first.status_code == 409
    assert "neither docker nor podman" in first.get_json()["error"]

    # The operator installs docker and immediately retries — inside the
    # fifteen-minute probe TTL, which is the whole point.
    state["docker"] = "yes"
    second = client.post("/api/cockpit/spawn",
                         json={"investigation_id": 1889, "tier": TIER_CONTAINER})
    assert second.status_code == 201, (
        f"the retry answered from a stale probe: {second.get_json()}")
    assert second.get_json()["tier"] == TIER_CONTAINER


def test_auto_still_answers_from_the_cache_on_a_second_request():
    """The refresh is scoped to an explicit --tier: `auto` must not turn every
    spawn into a fresh round trip across the fleet."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes"), "")))
    client, _server = _ladder_client(ssh, remediations=["raspberrypi5"])

    client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    client.post("/api/cockpit/spawn", json={"investigation_id": 1889})
    assert len(ssh.matching("uname")) == 1, (
        "auto re-probed a host it had already asked about")


# --------------------------------------------------------------------------
# the deadline wrapper must not steal the terminal
# --------------------------------------------------------------------------

def test_the_deadline_wrapper_leaves_the_session_in_the_foreground():
    """REGRESSION GUARD, found by attaching to a real tier-3 session.

    Plain `timeout` calls setpgid, putting the command in its own process
    group — which is a BACKGROUND group with respect to the terminal. The
    session then renders its briefing, echoes every keystroke, and responds to
    none of them: reads from the tty raise SIGTTIN, and ctrl-c's SIGINT goes to
    the shell instead. The only way out is killing the ssh.

    Verified on the box: without the flag `PGID != TPGID`, with it they match.
    """
    s = HostCockpitSpawner(HostLadderConfig())
    for tier in (TIER_HOST, TIER_SSH):
        runner = s._runner_script(1889, "/tmp/cfop-cockpit-1889", 14400, tier=tier)
        assert "timeout --foreground " in runner, (
            f"tier {tier}'s session would be uninterruptible and unable to read "
            f"the terminal:\n{runner}")


def test_the_container_deadline_wrapper_is_also_foreground():
    """`docker attach` hands the session a tty too, so the same rule applies to
    the entrypoint wrapper."""
    ssh, _result = container_spawn()
    run = ssh.matching("docker run")[0]
    assert "--entrypoint timeout" in run
    assert "--foreground" in run, f"the container session would be uninterruptible: {run}"
    # order matters: it is timeout's flag, so it precedes the duration
    assert run.index("--foreground") < run.index("14400")


def test_the_deadline_is_still_enforced():
    """--foreground must not have quietly dropped the TTL along with the
    process group."""
    s = HostCockpitSpawner(HostLadderConfig())
    runner = s._runner_script(1889, "/tmp/cfop-cockpit-1889", 900, tier=TIER_HOST)
    assert "timeout --foreground 900 " in runner


# --------------------------------------------------------------------------
# reattach: tmux where the host has it (CFOP-59)
# --------------------------------------------------------------------------

def test_the_runner_wraps_the_session_in_tmux_when_the_host_has_it():
    """A dropped connection must not end the session: the runner's first act is
    to create-or-attach a tmux session named for the investigation, so a second
    ssh rejoins the same TUI."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes",
                                            tmux="yes"), "")))
    _ssh, _result = host_spawn(ssh=ssh)
    runner = [s for s in ssh.stdins if s and b"#!/bin/sh" in s][0].decode().split("----")[0]

    assert "tmux new-session -A -s cfop-cockpit-1889" in runner, (
        "the runner does not create-or-attach a named tmux session")
    assert "CFOP_COCKPIT_TMUX=1" in runner, "nothing stops the created session recursing into tmux"
    assert "command -v tmux" in runner, "a host that lost tmux since the probe should fall through"
    # The session still runs under the deadline, inside tmux.
    assert "timeout --foreground 14400" in runner
    assert "attach 1889" in runner


def test_the_runner_has_no_tmux_when_the_host_lacks_it():
    """Today's behaviour, unchanged, for a host without tmux: a drop ends the
    session and the drawer says so."""
    ssh, _result = host_spawn()  # probe has tmux="no"
    runner = [s for s in ssh.stdins if s and b"#!/bin/sh" in s][0].decode().split("----")[0]
    assert "tmux" not in runner


def test_tier_ssh_with_tmux_still_wraps():
    """Tier 3b has no self-destruct timer, so tmux reattach is exactly where a
    long session most wants to survive a blip."""
    ssh = FakeSSH(("uname", (0, probe_reply(tmux="yes"), "")))
    _ssh, _result = host_spawn(ssh=ssh, tier=TIER_SSH)
    runner = [s for s in ssh.stdins if s and b"#!/bin/sh" in s][0].decode().split("----")[0]
    assert "tmux new-session -A -s cfop-cockpit-1889" in runner


def test_destroy_kills_the_tmux_session_before_removing_the_directory():
    """With reattach the session process lives inside tmux; removing only the
    directory would leave a live tmux session holding a cockpit whose files are
    gone."""
    ssh = FakeSSH(("uname", (0, probe_reply(systemd_run="yes", user_systemd="yes",
                                            tmux="yes"), "")),
                  ("for d in /tmp/", (0, f"/tmp/cfop-cockpit-1889 {int(time.time()) + 1800}\n", "")))
    spawner(ssh).destroy(1889, host="raspberrypi5")
    combined = [c for c in ssh.commands if "kill-session" in c]
    assert combined, "destroy did not kill the tmux session"
    assert "tmux kill-session -t cfop-cockpit-1889" in combined[0]
    assert "rm -rf /tmp/cfop-cockpit-1889" in combined[0], (
        "the kill and the removal must be one round trip, in that order")
