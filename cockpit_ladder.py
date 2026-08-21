"""The cockpit runtime ladder for non-k8s hosts — tiers 2/3 (CFOP-36).

Tier 1 (``cockpit_spawn.py``) puts the briefed session in an ephemeral pod. Most
of this fleet is not in the cluster: bare Pis, a GPU box, VMs. The contract has
to be the same everywhere — *briefing in, session out* — and only the isolation
and cleanup mechanism is allowed to degrade:

===========  ==========================  =====================  ==================
tier         runtime                     isolation              cleanup
===========  ==========================  =====================  ==================
``pod``      Kubernetes Job              pod + read-only SA     activeDeadline + GC
``container``docker/podman, detached     container              timeout + janitor
``host``     released binary + systemd   none                   transient timer
``ssh``      released binary + timeout   none                   janitor only
===========  ==========================  =====================  ==================

Three things carry over from tier 1 unchanged, because they are the security
envelope rather than an implementation detail:

* **The credential is the session.** Every tier gets a per-investigation token
  minted at spawn (CFOP-32) whose TTL is the session's TTL. Below ``container``
  there is no isolation left, so the token *is* the security model — which is
  why it is short-lived, scoped to ``investigate``, and never in argv.
* **Spawn is server-side, attach is operator-side.** The agent creates the
  runtime and hands back coordinates; the operator's own ssh (or kubectl)
  opens the terminal. Those are two different connections, which is why nothing
  here runs in the foreground: a session created by the spawn call would die
  with it.
* **Nothing is left behind.** What Kubernetes does for free at tier 1 —
  deadline, ownership GC — has to be built at 2/3, so every artifact carries
  its own expiry and a janitor sweeps whatever outlived it.

The ssh runner is injectable for the same reason ``CockpitSpawner``'s kubectl
runner is: the tests must be able to assert the exact argv without a fleet.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from cockpit_spawn import (
    COCKPIT_LABEL,
    DEFAULT_MAX_CONCURRENT,
    JOB_ROLE_LABEL,
    JOB_ROLE_VALUE,
    MAX_TTL_SECONDS,
    TIER_POD,
    TOKEN_ENV,
    CockpitConfig,
    CockpitSpawnError,
)

logger = logging.getLogger("cfoperator.cockpit.ladder")

# ---- the ladder ------------------------------------------------------------

# TIER_POD comes from cockpit_spawn: tier 1 owns its own name, and re-declaring
# it here is how the two would eventually disagree.
TIER_CONTAINER = "container"
TIER_HOST = "host"
TIER_SSH = "ssh"
TIER_AUTO = "auto"

#: Highest-first. ``choose_tier`` walks this, so the order *is* the ladder.
TIER_ORDER = (TIER_POD, TIER_CONTAINER, TIER_HOST, TIER_SSH)
VALID_TIERS = frozenset(TIER_ORDER) | {TIER_AUTO}

#: Session artifacts are named from this, on every tier and in every runtime:
#: the janitor finds work by convention rather than by consulting a registry
#: (see ``reap``), so the prefix is load-bearing, not cosmetic.
SESSION_PREFIX = "cfop-cockpit"

#: Container/dir label carrying the unix timestamp the session dies at. The
#: janitor compares integers rather than parsing ``docker ps`` date formats,
#: which differ by version and locale.
EXPIRES_LABEL = "cfop.dev/expires"

#: The cockpit image is published for these only (see the build workflow's
#: cockpit stanza). A 32-bit host has no container to run, so it degrades to a
#: process tier instead of failing — that degradation is the feature.
CONTAINER_ARCHES = frozenset({"amd64", "arm64"})

#: ``uname -m`` spellings → the arch names both GHCR and the cfassist release
#: assets use. Anything unrecognised stays as reported, so the refusal names
#: what the host actually said rather than an empty string.
_ARCH_ALIASES = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm", "armv6l": "arm", "armhf": "arm", "arm": "arm",
}

DEFAULT_PROBE_CACHE_SECONDS = 15 * 60
DEFAULT_SSH_CONNECT_TIMEOUT = 5
DEFAULT_SSH_COMMAND_TIMEOUT = 30
DEFAULT_JANITOR_INTERVAL_SECONDS = 15 * 60

#: The release whose ``cfassist-linux-<arch>`` asset tiers 3/3b deliver. Pinned
#: rather than "latest" so a session is reproducible, and asserted against
#: ``cfassist-go/internal/config/config.go`` by the tests: bumping the CLI's
#: Version without tagging the release would otherwise leave tier 3 fetching a
#: tag that does not exist. It fails loudly when that happens (a 404 naming the
#: missing asset), which is the right failure — silently falling back to an
#: older binary would put a cfassist without ``attach`` on the host.
DEFAULT_CFASSIST_VERSION = "0.8.1"

#: Where the cockpit image installs its entrypoint. Tier 2 has to name it
#: because it wraps the session in ``timeout`` (docker has no
#: activeDeadlineSeconds), which means overriding ``--entrypoint`` and passing
#: the real one as an argument. The image and this file ship separately, so the
#: tests assert this against ``cockpit/Dockerfile``'s ENTRYPOINT: getting it
#: wrong is a container that exits immediately with "no such file", on a host
#: nobody is watching.
COCKPIT_ENTRYPOINT = "/usr/local/bin/cockpit-entrypoint"
DEFAULT_RELEASE_BASE = "https://github.com/aachtenberg/cfoperator/releases/download"


@dataclass
class HostCapabilities:
    """What one host can actually run, as reported by the probe.

    ``error`` set means the probe itself failed (host down, no key, no route).
    That is distinct from a host that answered and simply has nothing: the
    first is "I could not ask", the second is "the answer is no", and only the
    second is a tier decision.
    """

    arch: str = ""
    docker: bool = False
    podman: bool = False
    systemd_run: bool = False
    user_systemd: bool = False
    tmux: bool = False
    sudo: bool = False
    probed_at: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def container_runtime(self) -> str:
        """Docker before podman: where both exist, docker is the one the fleet's
        own compose stacks already use, so its images are the warm ones."""
        if self.docker:
            return "docker"
        if self.podman:
            return "podman"
        return ""

    def summary(self) -> str:
        have = [n for n, v in (("docker", self.docker), ("podman", self.podman),
                               ("systemd-run", self.systemd_run), ("tmux", self.tmux))
                if v]
        return f"{self.arch or 'unknown arch'}, " + (", ".join(have) if have else "no runtime")


#: One round trip answers every ladder question. Written in the style of
#: ``event_runtime/host_observability.py``'s remote script — POSIX sh, no
#: pipeline that a busybox userland would refuse, and key=value out so the
#: parser never has to care about ordering or extra lines.
#:
#: ``command -v`` rather than the issue's ``docker info``: info connects to the
#: daemon and blocks for seconds on a host where it is installed but stopped,
#: and this probe runs inline on a spawn an operator is waiting for. A docker
#: that is present but dead surfaces as a loud spawn failure one step later,
#: which is a better place to spend the timeout.
PROBE_SCRIPT = r"""
arch=$(uname -m 2>/dev/null || echo unknown)
printf 'arch=%s\n' "$arch"
for b in docker podman systemd-run tmux; do
  if command -v "$b" >/dev/null 2>&1; then printf '%s=yes\n' "$b"; else printf '%s=no\n' "$b"; fi
done
if [ -d "/run/user/$(id -u 2>/dev/null)/systemd" ]; then
  printf 'user_systemd=yes\n'
else
  printf 'user_systemd=no\n'
fi
if sudo -n true >/dev/null 2>&1; then printf 'sudo=yes\n'; else printf 'sudo=no\n'; fi
"""


def normalize_arch(raw: str) -> str:
    return _ARCH_ALIASES.get((raw or "").strip().lower(), (raw or "").strip().lower())


def parse_probe(output: str) -> HostCapabilities:
    """Read the probe's key=value lines.

    Deliberately tolerant: a login shell that prints a MOTD, a banner, or a
    stray warning before the script runs is normal on real hosts, and none of
    that should read as "this host has no docker".
    """
    caps = HostCapabilities(probed_at=time.time())
    for line in (output or "").splitlines():
        key, _, value = line.strip().partition("=")
        key, value = key.strip(), value.strip().lower()
        if key == "arch":
            caps.arch = normalize_arch(value)
        elif key == "docker":
            caps.docker = value == "yes"
        elif key == "podman":
            caps.podman = value == "yes"
        elif key == "systemd-run":
            caps.systemd_run = value == "yes"
        elif key == "user_systemd":
            caps.user_systemd = value == "yes"
        elif key == "tmux":
            # Probed but unused: a tmux session is what survives a dropped
            # connection, which is what the console drawer's reattach needs
            # (CFOP-59). Recording it now means that issue starts from a fact
            # rather than another fleet-wide probe.
            caps.tmux = value == "yes"
        elif key == "sudo":
            caps.sudo = value == "yes"
    return caps


def choose_tier(
    caps: Optional[HostCapabilities],
    *,
    requested: str = TIER_AUTO,
    is_cluster_node: bool = False,
    has_host: bool = False,
    allow_sudo: bool = False,
) -> Tuple[str, str]:
    """Pick the runtime tier, or explain why the requested one is impossible.

    Returns ``(tier, note)``. The note is operator-facing and always says which
    rung was chosen *and what was missing above it* — "docker host, no cluster
    membership" is a different incident from "bare host, docker gone".

    **Tier 1 is always attemptable.** The cluster is the one runtime that needs
    no host to be resolved first, so an investigation that names no machine
    still gets the cockpit CFOP-35 built — unpinned, and saying so. Tiers 2/3
    are the ones that need a target: without a host there is nowhere to ssh.

    A forced ``requested`` tier that the probe says is unavailable is an error,
    never a quiet downgrade: someone who typed ``--tier container`` because they
    need the container boundary must not silently get a process on the host.
    """
    requested = (requested or TIER_AUTO).strip().lower() or TIER_AUTO
    if requested not in VALID_TIERS:
        raise CockpitSpawnError(
            f"unknown tier {requested!r} (want one of: {', '.join(sorted(VALID_TIERS))})", 400)

    available = _available_tiers(caps, allow_sudo=allow_sudo)

    if requested != TIER_AUTO:
        if requested in available:
            return requested, f"tier {requested} (requested)"
        raise CockpitSpawnError(
            f"tier {requested!r} was requested but is not available on this target "
            f"({_why_not(requested, caps)}); available: "
            f"{', '.join(t for t in TIER_ORDER if t in available)}", 409)

    # Auto. The cluster wins only when the incident is actually in it: pinning
    # to a node is better than a bare host, but a bare host is much better than
    # a pod somewhere else entirely, which is what tier 1 silently gives you for
    # a finding about a Pi.
    if not has_host:
        return TIER_POD, "tier pod — no affected host resolved; spawned anywhere in the cluster"
    if is_cluster_node:
        return TIER_POD, "tier pod — the affected host is a schedulable cluster node"
    if caps is None:
        return TIER_POD, ("tier pod — the affected host is neither a cluster node nor a "
                          "configured ssh host; spawned in the cluster instead")
    if not caps.ok:
        # The box cannot be reached, so a session *on* it is impossible. A pod
        # beside it still investigates it — but the operator has to be told,
        # because "I am on the Pi" and "I am next to the Pi" are different
        # facts to debug from.
        return TIER_POD, (f"tier pod — the affected host could not be probed "
                          f"({caps.error}); spawned in the cluster instead")

    for tier in (TIER_CONTAINER, TIER_HOST, TIER_SSH):
        if tier in available:
            return tier, _auto_note(tier, caps)
    return TIER_POD, "tier pod — nothing else is available on the affected host"


def _available_tiers(caps, *, allow_sudo: bool) -> set:
    """What *could* be done, ignoring what is preferable.

    Tier 1 is unconditional: the agent's own cluster is always a place to put a
    pod, and a spawn it cannot actually make fails loudly at ``kubectl create``
    with a message about RBAC rather than being pre-empted here by a guess.
    """
    available = {TIER_POD}
    if caps is None or not caps.ok:
        return available
    if caps.container_runtime and caps.arch in CONTAINER_ARCHES:
        available.add(TIER_CONTAINER)
    # systemd-run on its own is not enough: as a non-root ssh user it needs
    # either a running user manager to own the transient unit, or passwordless
    # sudo that the deployment has explicitly allowed. Without one of those the
    # timer cannot be created, and a tier whose cleanup silently does not exist
    # is worse than the honest 3b that says so.
    if caps.systemd_run and (caps.user_systemd or (allow_sudo and caps.sudo)):
        available.add(TIER_HOST)
    available.add(TIER_SSH)
    return available


def _auto_note(tier: str, caps: Optional[HostCapabilities]) -> str:
    if tier == TIER_CONTAINER:
        return f"tier container — {caps.container_runtime} on {caps.arch}"
    if tier == TIER_HOST:
        owner = "user systemd" if caps and caps.user_systemd else "sudo systemd"
        return f"tier host — no container runtime; transient unit via {owner}"
    missing = []
    if caps and not caps.container_runtime:
        missing.append("no container runtime")
    elif caps and caps.arch not in CONTAINER_ARCHES:
        missing.append(f"no cockpit image for {caps.arch or 'this arch'}")
    if caps and not caps.systemd_run:
        missing.append("no systemd-run")
    elif caps and not caps.user_systemd:
        missing.append("no systemd manager to own a transient unit")
    return ("tier ssh — best-effort: " + ", ".join(missing)) if missing else "tier ssh — best-effort"


def _why_not(tier: str, caps: Optional[HostCapabilities]) -> str:
    if caps is None:
        return "the host is not in infrastructure.hosts, so it was never probed"
    if not caps.ok:
        return f"the capability probe failed: {caps.error}"
    if tier == TIER_CONTAINER:
        if not caps.container_runtime:
            return "neither docker nor podman is installed"
        return f"the cockpit image is not published for {caps.arch or 'this architecture'}"
    if tier == TIER_HOST:
        if not caps.systemd_run:
            return "systemd-run is not installed"
        return "there is no user systemd manager and passwordless sudo is not permitted"
    return "unavailable"


# ---- resolving *which* host --------------------------------------------------

#: Values that mean "not a real host". ``'cfoperator'`` is the agent's own
#: ``host_id`` and ``'default'`` is the column default.
_NON_HOSTS = frozenset({"", "default", "cfoperator", "none", "null", "unknown"})


def resolve_target_host(
    *,
    requested: str = "",
    remediation_hosts: Sequence[str] = (),
    investigation: Optional[Dict[str, Any]] = None,
    known_hosts: Iterable[str] = (),
) -> Tuple[str, str]:
    """Work out which host the incident is on. Returns ``(host, provenance)``.

    **``investigation['host_id']`` is deliberately never consulted.** It reads
    like the answer and is not: it is the *area-of-responsibility* field for
    multi-agent installs, set from ``KnowledgeBase.host_id``, which the agent
    hardcodes to ``'cfoperator'``. Every investigation in a normal install
    carries the same value. Reading it is how tier 1's ``nodeSelector`` came to
    ask the cluster for a node named ``cfoperator`` and conclude, every single
    time, that the finding was not host-level.

    The finding's own host does exist — on the remediation rows fed from it,
    whose ``host_id`` comes from the classified finding rather than from the
    agent's identity. Failing that, the trigger text names it often enough to
    be worth matching against the hosts we actually know about; guessing beyond
    the configured inventory is not, because the cost of a wrong match is a
    session on the wrong machine mid-incident.
    """
    requested = (requested or "").strip()
    if requested:
        return requested, "requested by the caller"

    for candidate in remediation_hosts:
        host = (candidate or "").strip()
        if host and host.lower() not in _NON_HOSTS:
            return host, "from the remediation queued off this investigation"

    names = sorted({(n or "").strip() for n in known_hosts if (n or "").strip()},
                   key=len, reverse=True)
    if names:
        haystack = _investigation_text(investigation)
        for name in names:
            if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", haystack, re.IGNORECASE):
                return name, "named in the investigation"

    return "", "no affected host could be resolved from the investigation"


def _investigation_text(investigation: Optional[Dict[str, Any]]) -> str:
    """Trigger plus findings, flattened. Both matter: the trigger is where an
    alert puts ``instance=raspberrypi5``, the findings are where the agent's own
    conclusion names the box it could not reach."""
    if not isinstance(investigation, dict):
        return ""
    parts = [str(investigation.get("trigger") or "")]
    findings = investigation.get("findings")
    if isinstance(findings, dict):
        parts.extend(str(v) for v in findings.values())
    elif findings:
        parts.append(str(findings))
    return "\n".join(parts)


# ---- the spawner --------------------------------------------------------------

_SSHRunner = Callable[[Sequence[str], Optional[bytes]], Tuple[int, str, str]]
_Fetcher = Callable[[str], bytes]
_TokenMinter = Callable[..., Dict[str, Any]]
_TokenRevoker = Callable[[int], None]


@dataclass
class HostLadderConfig:
    """Tier 2/3 knobs. Everything deployment-shaped comes from env first, the
    same precedence ``build_cockpit_config`` uses and for the same reason."""

    image: str = ""
    agent_url: str = ""
    host_agent_url: str = ""
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    llm_url: str = ""
    llm_model: str = ""
    ssh_user: str = "sre"
    ssh_key_path: str = ""
    ssh_secret_dir: str = ""
    ssh_connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT
    ssh_command_timeout: int = DEFAULT_SSH_COMMAND_TIMEOUT
    probe_cache_seconds: int = DEFAULT_PROBE_CACHE_SECONDS
    janitor_interval_seconds: int = DEFAULT_JANITOR_INTERVAL_SECONDS
    allow_sudo: bool = False
    cfassist_version: str = DEFAULT_CFASSIST_VERSION
    release_base: str = DEFAULT_RELEASE_BASE
    hosts: Dict[str, Any] = field(default_factory=dict)


def build_ladder_config(agent_config: Any, cockpit: CockpitConfig) -> HostLadderConfig:
    """Ladder config from ``cockpit:``, ``infrastructure.hosts``, env, defaults.

    The image, agent URL and LLM settings are *not* re-derived: they come from
    the tier-1 config that already resolved them, so a container on a Pi and a
    pod in the cluster brief the same session against the same model. Two
    resolutions of "which model does a cockpit talk to" is how they drift.
    """
    cfg = agent_config if isinstance(agent_config, dict) else {}
    block = cfg.get("cockpit") if isinstance(cfg.get("cockpit"), dict) else {}
    infra = cfg.get("infrastructure") if isinstance(cfg.get("infrastructure"), dict) else {}
    hosts = infra.get("hosts") if isinstance(infra.get("hosts"), dict) else {}

    def _str(env: str, key: str, default: str) -> str:
        return str(os.getenv(env) or block.get(key) or default).strip()

    def _int(env: str, key: str, default: int) -> int:
        try:
            return int(os.getenv(env) or block.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _bool(env: str, key: str, default: bool) -> bool:
        raw = os.getenv(env)
        if raw is None:
            raw = block.get(key, default)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    return HostLadderConfig(
        image=cockpit.image,
        agent_url=cockpit.agent_url,
        # Tier 1's agent_url is cluster DNS by default and by design — it is
        # what the POD calls. A Pi cannot resolve it. One knob cannot serve
        # both runtimes, so tiers 2/3 get their own and fall back loudly rather
        # than silently (see host_agent_url()).
        host_agent_url=_str("CFOP_COCKPIT_HOST_AGENT_URL", "host_agent_url", ""),
        max_concurrent=cockpit.max_concurrent,
        llm_url=cockpit.llm_url,
        llm_model=cockpit.llm_model,
        ssh_user=_str("CFOP_COCKPIT_SSH_USER", "ssh_user", "sre"),
        ssh_key_path=_str("CFOP_COCKPIT_SSH_KEY", "ssh_key_path", ""),
        ssh_secret_dir=_str("CFOP_COCKPIT_SSH_SECRET_DIR", "ssh_secret_dir", ""),
        ssh_connect_timeout=_int("CFOP_COCKPIT_SSH_CONNECT_TIMEOUT", "ssh_connect_timeout",
                                 DEFAULT_SSH_CONNECT_TIMEOUT),
        ssh_command_timeout=_int("CFOP_COCKPIT_SSH_TIMEOUT", "ssh_command_timeout",
                                 DEFAULT_SSH_COMMAND_TIMEOUT),
        probe_cache_seconds=_int("CFOP_COCKPIT_PROBE_CACHE_SECONDS", "probe_cache_seconds",
                                 DEFAULT_PROBE_CACHE_SECONDS),
        janitor_interval_seconds=_int("CFOP_COCKPIT_JANITOR_INTERVAL", "janitor_interval_seconds",
                                      DEFAULT_JANITOR_INTERVAL_SECONDS),
        allow_sudo=_bool("CFOP_COCKPIT_ALLOW_SUDO", "allow_sudo", False),
        cfassist_version=_str("CFOP_COCKPIT_CFASSIST_VERSION", "cfassist_version",
                              DEFAULT_CFASSIST_VERSION),
        release_base=_str("CFOP_COCKPIT_RELEASE_BASE", "release_base", DEFAULT_RELEASE_BASE),
        hosts=dict(hosts),
    )


#: Names that only resolve inside a Kubernetes cluster. A session on a bare
#: host cannot fetch its own briefing from one of these, and the failure would
#: land *after* the operator attached — a briefed session that is not briefed.
_CLUSTER_ONLY_SUFFIXES = (".svc", ".svc.cluster.local")


def host_agent_url(config: "HostLadderConfig") -> str:
    """The agent URL a host tier's session should call, or a refusal.

    ``cockpit.agent_url`` is documented as "what the POD calls" and defaults to
    cluster DNS, so inheriting it for tiers 2/3 puts an unresolvable name on
    every Pi. Rather than let that surface as a briefing that quietly fails
    inside the session, it is a spawn-time error naming the key to set.
    """
    url = (config.host_agent_url or config.agent_url).strip()
    hostname = urllib.parse.urlparse(url).hostname or ""
    if hostname.endswith(_CLUSTER_ONLY_SUFFIXES):
        raise CockpitSpawnError(
            f"the cockpit agent URL ({url}) only resolves inside the cluster, so a "
            f"session on a host outside it could not fetch its briefing — set "
            f"cockpit.host_agent_url (or CFOP_COCKPIT_HOST_AGENT_URL) to an "
            f"address the fleet can reach", 400)
    if hostname in ("localhost", "127.0.0.1", "::1"):
        # Not refused: a single-box install where the fleet host *is* the agent
        # host is a real shape. But it is wrong far more often than it is
        # right, so it does not pass silently.
        logger.warning("cockpit: host-tier sessions will call %s, which means "
                       "'the machine the session runs on'. Set "
                       "cockpit.host_agent_url if that is not the agent.", url)
    return url


class HostCockpitSpawner:
    """Spawns (and reaps) cockpit sessions on hosts outside the cluster."""

    def __init__(
        self,
        config: HostLadderConfig,
        *,
        ssh_runner: Optional[_SSHRunner] = None,
        fetcher: Optional[_Fetcher] = None,
        token_minter: Optional[_TokenMinter] = None,
        token_revoker: Optional[_TokenRevoker] = None,
    ):
        self._config = config
        self._ssh = ssh_runner or _run_ssh
        self._fetch = fetcher or _fetch_url
        self._mint = token_minter
        self._revoke = token_revoker
        self._probes: Dict[str, HostCapabilities] = {}
        self._binaries: Dict[str, bytes] = {}
        self._identity_staged = False

    @property
    def config(self) -> HostLadderConfig:
        return self._config

    def known_host_names(self) -> List[str]:
        return sorted(self._config.hosts.keys())

    # ---- probe -------------------------------------------------------------

    def probe(self, host: str, *, refresh: bool = False) -> HostCapabilities:
        """Capabilities for ``host``, from a TTL cache.

        The cache lives in this process rather than on a "host record" because
        there is no such record: the ``hosts`` table registers CFOperator
        instances, not the SSH fleet, and inventing a table for one consumer is
        the mistake CFOP-35 avoided with its service-account mapping. A cold
        cache costs one round trip on a spawn the operator is already waiting
        on; the alternative costs a migration.

        **Failures are barely cached at all** — see ``_cache_ttl``. Caching
        "Permission denied (publickey)" for fifteen minutes makes the fix
        invisible: docs/cockpit.md tells an operator to mount the key and
        retry, and the retry would answer from the cache until the agent
        restarted. A capability is a fact about a host and keeps; a failure to
        reach one is a fact about a moment.
        """
        cached = self._probes.get(host)
        if cached and not refresh and (time.time() - cached.probed_at) < self._cache_ttl(cached):
            return cached

        code, out, err = self._ssh_host(host, PROBE_SCRIPT)
        if code != 0:
            caps = HostCapabilities(probed_at=time.time(),
                                    error=(err.strip() or f"ssh exited {code}")[:300])
        else:
            caps = parse_probe(out)
            if not caps.arch:
                # The command ran but said nothing we understand — a restricted
                # shell, or a host whose login banner ate the output. Treat it
                # as unprobed rather than as "no capabilities", so the ladder
                # reports "could not ask" instead of silently landing on 3b.
                caps.error = "the capability probe returned no readable output"
        self._probes[host] = caps
        logger.info("cockpit probe %s: %s", host, caps.error or caps.summary())
        return caps

    def _cache_ttl(self, caps: HostCapabilities) -> int:
        """How long an answer is worth keeping.

        A successful probe describes the host and holds for the configured TTL.
        A failed one describes the network at one instant, and is kept only
        long enough to spare a single operation its second round trip (the
        endpoint probes, then the spawn or the janitor probes again). Anything
        longer and fixing the cause does not fix the symptom.
        """
        if caps.ok:
            return self._config.probe_cache_seconds
        return max(1, self._config.ssh_connect_timeout)

    def invalidate(self, host: str) -> None:
        self._probes.pop(host, None)

    # ---- spawn -------------------------------------------------------------

    def spawn(self, investigation_id: int, *, host: str, tier: str,
              ttl_seconds: int) -> Dict[str, Any]:
        """Create the session on ``host`` at ``tier`` and return its coordinates."""
        if not host:
            raise CockpitSpawnError(
                "a host cockpit needs a host, and none could be resolved from the "
                "investigation", 400)
        if host not in self._config.hosts:
            raise CockpitSpawnError(
                f"{host!r} is not in infrastructure.hosts, so there is no address or "
                f"credential to reach it with (known: "
                f"{', '.join(self.known_host_names()) or 'none configured'})", 400)
        if self._mint is None:
            raise CockpitSpawnError(
                "no token store: a cockpit cannot be spawned without a "
                "per-investigation session token", 503)

        name = session_name(investigation_id)
        # Before anything is created or minted: a session that cannot reach the
        # agent is a briefed session with no briefing, and it would fail *after*
        # the operator attached.
        host_agent_url(self._config)

        caps = self.probe(host)
        live = self._live_sessions(host, caps)
        # Dedupe first, cap second — an operator re-running their own command
        # must land back in their cockpit rather than be told the host is busy
        # with a session that is theirs.
        if name in live:
            return self._existing_session(host, investigation_id, live[name])
        if len(live) >= self._config.max_concurrent:
            raise CockpitSpawnError(
                f"cockpit concurrency cap reached on {host} "
                f"({self._config.max_concurrent} active: {', '.join(sorted(live))})", 429)

        token = self._mint(investigation_id, ttl_seconds, tier=tier, host=host)
        expires_at = int(time.time()) + int(ttl_seconds)
        try:
            if tier == TIER_CONTAINER:
                detail = self._spawn_container(host, investigation_id, name, caps,
                                               token, ttl_seconds, expires_at)
            else:
                detail = self._spawn_process(host, investigation_id, name, caps, token,
                                             ttl_seconds, expires_at, tier=tier)
        except Exception:
            # A credential whose session never started is the orphan CFOP-32
            # exists to prevent, and a cached probe that just produced an
            # impossible spawn is not worth keeping either.
            self.invalidate(host)
            self._revoke_quietly(token)
            raise

        detail.update({
            "status": "spawned",
            "tier": tier,
            "investigation_id": investigation_id,
            "session_name": name,
            "ttl_seconds": int(ttl_seconds),
            "expires_at": expires_at,
            "token_prefix": str(token.get("prefix") or ""),
        })
        logger.info("Spawned cockpit %s on %s (tier=%s, ttl=%ss)",
                    name, host, tier, ttl_seconds)
        return detail

    # -- tier 2 ---------------------------------------------------------------

    def _spawn_container(self, host, investigation_id, name, caps, token,
                         ttl_seconds, expires_at) -> Dict[str, Any]:
        """Detached container, then the operator attaches over their own ssh.

        Detached rather than the issue's ``docker run --rm``: spawn and attach
        are different connections, so a foreground container would be destroyed
        the moment this call returned.
        """
        runtime = caps.container_runtime or "docker"
        argv = [
            runtime, "run", "--detach", "--interactive", "--tty",
            "--name", name,
            "--label", f"{JOB_ROLE_LABEL}={JOB_ROLE_VALUE}",
            "--label", f"{COCKPIT_LABEL}={investigation_id}",
            "--label", f"{EXPIRES_LABEL}={expires_at}",
            # Docker has no activeDeadlineSeconds. The wrapper is the session's
            # own deadline and the janitor is the backstop for the case the
            # wrapper cannot cover — a daemon restart resurrecting the
            # container with its timer reset.
            "--stop-timeout", "5",
            # Read from *this ssh connection's* stdin, so the credential is
            # never in argv: argv lands in the host's process table, in the
            # agent's own subprocess logging, and in anyone's `ps`. It is still
            # visible to `docker inspect`, which is the honest degradation from
            # tier 1's Secret indirection and is documented as such.
            "--env-file", "/dev/stdin",
            # Docker has no activeDeadlineSeconds, so the deadline is a
            # wrapper around the image's own entrypoint.
            "--entrypoint", "timeout",
            self._config.image,
            str(int(ttl_seconds)), COCKPIT_ENTRYPOINT,
        ]
        # An exited namesake still owns the name, and `docker run` would 502 on
        # it — which is the ordinary path, not an edge case: attach, `exit`, run
        # the alert command again. `rm` without --force on purpose: a *running*
        # namesake was already returned by the dedupe above, so anything left
        # here is a corpse, and refusing to kill a live session is worth the
        # rarer loud failure if the two ever race.
        remote = (f"{shlex.quote(runtime)} rm {shlex.quote(name)} >/dev/null 2>&1; "
                  + " ".join(shlex.quote(a) for a in argv))
        code, out, err = self._ssh_host(host, remote,
                                        stdin=self._env_file(investigation_id, token,
                                                             tier=TIER_CONTAINER, host=host))
        if code != 0:
            raise CockpitSpawnError(
                f"{runtime} run failed on {host}: {(err.strip() or out.strip())[:400]}", 502)

        attach_argv = self._attach_argv(host, [runtime, "attach", name])
        return {
            "host": host,
            "runtime": runtime,
            "container_id": out.strip().splitlines()[-1][:64] if out.strip() else "",
            "attach_argv": attach_argv,
            "attach_command": " ".join(shlex.quote(a) for a in attach_argv),
            "placement": {"node": host, "note": f"{runtime} container on {host}"},
        }

    # -- tiers 3 / 3b ----------------------------------------------------------

    def _spawn_process(self, host, investigation_id, name, caps, token,
                       ttl_seconds, expires_at, *, tier) -> Dict[str, Any]:
        """Deliver the released cfassist, a 0600 credential, and a runner.

        **What the operator attaches to differs here, and the difference is the
        tier.** At tiers 1 and 2 a briefed session is already running and the
        attach joins it. There is no isolation left at tier 3, so there is
        nothing to run it *in*: the session starts when the operator's own ssh
        runs the delivered runner, and it is their connection that hosts it.
        What is lost is survival across a dropped connection — the reattachable
        variant needs a multiplexer on the host (``caps.tmux``) and belongs to
        the console drawer that actually requires it (CFOP-59).

        What does not change is the expiry: the credential and the binary are
        removed at the deadline whether anyone attached or not.
        """
        directory = f"/tmp/{name}"
        arch = caps.arch or "arm64"
        binary = self._cfassist_binary(arch)

        # umask before mkdir: the directory holds the session credential, and a
        # world-readable /tmp entry that is chmod-ed a moment later is a window.
        setup = (
            f"umask 077 && rm -rf {shlex.quote(directory)} && "
            f"mkdir -p {shlex.quote(directory)} && "
            f"cat > {shlex.quote(directory)}/cfassist && "
            f"chmod 700 {shlex.quote(directory)}/cfassist"
        )
        code, _out, err = self._ssh_host(host, setup, stdin=binary)
        if code != 0:
            raise CockpitSpawnError(
                f"could not deliver cfassist to {host}: {err.strip()[:400]}", 502)

        payload = (
            self._runner_script(investigation_id, directory, ttl_seconds, tier=tier)
            + "\n----\n"
            + self._env_file(investigation_id, token, tier=tier, host=host).decode()
        )
        # Runner and credential arrive as one stream split on a marker: a single
        # round trip, and the credential never reaches a command line on either
        # side. Written whole first because splitting it in the pipe would need
        # to read the same input twice.
        install = (
            f"umask 077 && cd {shlex.quote(directory)} && cat > payload && "
            "sed '/^----$/,$d' payload > run && chmod 700 run && "
            "sed '1,/^----$/d' payload > env && chmod 600 env && "
            "rm -f payload && "
            f"printf '%s' {shlex.quote(str(expires_at))} > expires"
        )
        code, _out, err = self._ssh_host(host, install, stdin=payload.encode())
        if code != 0:
            raise CockpitSpawnError(
                f"could not install the cockpit session on {host}: {err.strip()[:400]}", 502)

        note = self._arm_self_destruct(host, name, directory, caps, ttl_seconds, tier=tier)
        attach_argv = self._attach_argv(host, [f"{directory}/run"], tty=True)
        return {
            "host": host,
            "session_dir": directory,
            "attach_argv": attach_argv,
            "attach_command": " ".join(shlex.quote(a) for a in attach_argv),
            "placement": {"node": host, "note": note},
        }

    def _arm_self_destruct(self, host, name, directory, caps, ttl_seconds, *, tier) -> str:
        """Transient timer that removes the session whether or not it was used.

        Tier 3b has no such timer — that *is* tier 3b — and the janitor is what
        closes the leak. Arming is best-effort even at tier 3: a timer that
        could not be created downgrades the note rather than the session, since
        the janitor covers both cases and an operator mid-incident should not
        lose their cockpit to a systemd quirk.
        """
        if tier != TIER_HOST:
            return f"process on {host} — no transient unit; the janitor reaps it"
        user = caps.user_systemd
        prefix = ["systemd-run", "--user"] if user else ["sudo", "-n", "systemd-run"]
        argv = prefix + [
            f"--unit={name}-reap", f"--on-active={int(ttl_seconds)}s", "--collect",
            "/bin/rm", "-rf", directory,
        ]
        # Cancel before arming, and this is not tidiness. The unit name is
        # stable per investigation, so a previous session's timer is still
        # counting down on the ordinary re-run path (attach, exit, run the
        # alert command again). Leaving it would do two bad things at once:
        # `systemd-run` fails because the unit exists, and then the *old* timer
        # fires and deletes the *new* session out from under the operator.
        code, _out, err = self._ssh_host(
            host, cancel_reap_unit_command(name, user=user, sudo=not user) + "; "
            + " ".join(shlex.quote(a) for a in argv))
        if code != 0:
            logger.warning("cockpit %s: could not arm the self-destruct timer on %s: %s",
                           name, host, err.strip()[:200])
            return (f"process on {host} — transient timer refused "
                    f"({err.strip()[:80] or 'unknown error'}); the janitor reaps it")
        owner = "user" if caps.user_systemd else "system"
        return f"process on {host} — {owner} transient timer expires it in {int(ttl_seconds)}s"

    # ---- dedupe ------------------------------------------------------------

    def _live_sessions(self, host: str, caps: HostCapabilities) -> Dict[str, str]:
        """Cockpit sessions currently alive on ``host``: ``{name: kind}``.

        One listing answers both questions a spawn has to ask — "is mine
        already here" (dedupe) and "how many are here" (the cap). Asking them
        separately is how the two come to disagree, and the cap is the only
        thing bounding how many ``investigate`` tokens end up on a machine that
        has no cluster-side ceiling above it.

        Counts both kinds regardless of the tier being spawned: a box can carry
        a container cockpit and a process cockpit at once, and "how many
        cockpits are on this host" has one answer.
        """
        live: Dict[str, str] = {}

        runtime = caps.container_runtime
        if runtime:
            argv = [runtime, "ps", "--filter", f"label={JOB_ROLE_LABEL}={JOB_ROLE_VALUE}",
                    "--format", "{{.Names}}"]
            code, out, _err = self._ssh_host(host, " ".join(shlex.quote(a) for a in argv))
            if code == 0:
                for name in out.split():
                    if name.startswith(SESSION_PREFIX):
                        live[name] = TIER_CONTAINER

        code, out, _err = self._ssh_host(host, _SESSION_LISTING)
        if code == 0:
            now = time.time()
            for directory, expires in _parse_expiry_listing(out):
                name = os.path.basename(directory.rstrip("/"))
                # An expired directory belongs to the janitor, not to a live
                # session: counting it against the cap would lock an operator
                # out of a host until the next sweep.
                if name.startswith(SESSION_PREFIX) and (not expires or expires > now):
                    live.setdefault(name, TIER_HOST)
        return live

    def _existing_session(self, host: str, investigation_id: int, kind: str
                          ) -> Dict[str, Any]:
        """Report the cockpit already there, rather than starting a second one.

        Same rule as tier 1's dedupe, and the same reason: re-running the
        command the alert told you to run must land you back in your own
        session, not beside it and not against a busy-host error.
        """
        name = session_name(investigation_id)
        if kind == TIER_CONTAINER:
            runtime = self.probe(host).container_runtime or "docker"
            attach_argv = self._attach_argv(host, [runtime, "attach", name])
        else:
            attach_argv = self._attach_argv(host, [f"/tmp/{name}/run"], tty=True)
        return {
            "status": "existing",
            "tier": kind,
            "host": host,
            "investigation_id": investigation_id,
            "session_name": name,
            "attach_argv": attach_argv,
            "attach_command": " ".join(shlex.quote(a) for a in attach_argv),
            "placement": {"node": host, "note": "existing cockpit for this investigation"},
        }

    # ---- janitor ------------------------------------------------------------

    def reap(self, hosts: Optional[Iterable[str]] = None, *, now: Optional[float] = None
             ) -> List[Dict[str, str]]:
        """Remove cockpit artifacts that outlived their TTL, on every host.

        **Stateless, by convention.** It does not consult a session registry;
        it enumerates ``cfop-cockpit-*`` by name and label and reaps what has
        expired. That is deliberately stronger than tracking what this process
        spawned: an agent that restarted, or a previous agent instance, leaves
        sessions no registry of ours would remember — and those are exactly the
        orphans the tier-3b leak produces.

        Expiry is an integer written at spawn (a label on the container, a file
        in the session directory) rather than a parsed creation date: ``docker
        ps`` renders dates differently across versions, and a janitor that
        misreads one either spares an orphan or kills a live session.
        """
        now = time.time() if now is None else now
        reaped: List[Dict[str, str]] = []
        for host in (hosts if hosts is not None else self._config.hosts.keys()):
            try:
                reaped.extend(self._reap_host(host, now))
            except Exception as exc:  # noqa: BLE001
                # One unreachable host must not stop the sweep: the host that
                # is down is frequently the one with the orphan on it, and the
                # next cycle will get it.
                logger.warning("cockpit janitor: %s could not be swept: %s", host, exc)
        if reaped:
            logger.info("cockpit janitor reaped %d session(s): %s", len(reaped),
                        ", ".join(f"{r['host']}/{r['name']}" for r in reaped))
        return reaped

    def _reap_host(self, host: str, now: float) -> List[Dict[str, str]]:
        reaped: List[Dict[str, str]] = []
        caps = self.probe(host)
        if not caps.ok:
            return reaped

        runtime = caps.container_runtime
        if runtime:
            argv = [runtime, "ps", "--all", "--filter", f"label={JOB_ROLE_LABEL}={JOB_ROLE_VALUE}",
                    "--format", "{{.Names}} {{.Label \"" + EXPIRES_LABEL + "\"}}"]
            code, out, _err = self._ssh_host(host, " ".join(shlex.quote(a) for a in argv))
            if code == 0:
                for name, expires in _parse_expiry_listing(out):
                    if expires and expires > now:
                        continue
                    rm = [runtime, "rm", "--force", name]
                    self._ssh_host(host, " ".join(shlex.quote(a) for a in rm))
                    reaped.append({"host": host, "name": name, "kind": "container"})

        code, out, _err = self._ssh_host(host, _SESSION_LISTING)
        if code == 0:
            for directory, expires in _parse_expiry_listing(out):
                if expires and expires > now:
                    continue
                name = os.path.basename(directory.rstrip("/"))
                if not name.startswith(SESSION_PREFIX):
                    continue
                self._ssh_host(host, f"rm -rf {shlex.quote(directory)}")
                # A unit whose timer already fired is gone (--collect); one that
                # failed is not, and would block the next spawn of the same
                # name. Both flavours are tried — a session armed through sudo
                # leaves a *system* unit that `systemctl --user` cannot see, so
                # cancelling only the user one leaves exactly the orphan this
                # sweep exists to remove. A missing unit is not an error.
                self._ssh_host(host, cancel_reap_unit_command(name, user=True, sudo=True))
                reaped.append({"host": host, "name": name, "kind": "session"})
        return reaped

    # ---- plumbing -----------------------------------------------------------

    def _cfassist_binary(self, arch: str) -> bytes:
        """The released ``cfassist-linux-<arch>``, cached in this process.

        Fetched by the agent and pushed to the host rather than pulled by the
        host: an incident host may have no route out, and the one that has lost
        its network is exactly the one someone wants a cockpit on.
        """
        asset = f"cfassist-linux-{arch}"
        if arch not in ("amd64", "arm64", "arm"):
            raise CockpitSpawnError(
                f"no cfassist release asset for architecture {arch!r}", 409)
        if asset in self._binaries:
            return self._binaries[asset]
        version = self._config.cfassist_version
        url = f"{self._config.release_base}/cfassist-v{version}/{asset}"
        try:
            blob = self._fetch(url)
        except Exception as exc:  # noqa: BLE001
            raise CockpitSpawnError(
                f"could not fetch {asset} from cfassist-v{version} ({exc}); "
                # One literal, not two: docs/cockpit.md's troubleshooting table
                # is keyed on this text, and a contract test greps for it here.
                f"is the release tagged?", 502)
        if not blob:
            raise CockpitSpawnError(f"{url} returned an empty binary", 502)
        self._binaries[asset] = blob
        return blob

    def _env_file(self, investigation_id: int, token: Dict[str, Any],
                  *, tier: str = "", host: str = "") -> bytes:
        """The session's environment, as a docker ``--env-file`` / shell prelude.

        Same variable names the pod entrypoint reads, because the entrypoint is
        the same file: the tier changes the runtime, never the contract.
        """
        lines = [
            f"CFOP_INVESTIGATION_ID={investigation_id}",
            f"CFOP_AGENT_URL={host_agent_url(self._config)}",
            f"{TOKEN_ENV}={token.get('secret') or ''}",
            # Where the session ran, for its own write-back (CFOP-37).
            f"CFOP_COCKPIT_TIER={tier}",
            f"CFOP_COCKPIT_HOST={host}",
        ]
        if self._config.llm_url:
            lines.append(f"CFOP_COCKPIT_LLM_URL={self._config.llm_url}")
        if self._config.llm_model:
            lines.append(f"CFOP_COCKPIT_LLM_MODEL={self._config.llm_model}")
        return ("\n".join(lines) + "\n").encode()

    def _runner_script(self, investigation_id: int, directory: str, ttl_seconds: int,
                       *, tier: str) -> str:
        """What the operator's ssh executes. Reads the credential from a 0600
        file next to it — never from argv, and never from the ssh command line
        the operator's own shell history would keep."""
        wrapper = "timeout" if tier in (TIER_HOST, TIER_SSH) else ""
        return "\n".join([
            "#!/bin/sh",
            "# cfop cockpit session runner (CFOP-36). Generated at spawn; removed at TTL.",
            "set -eu",
            f"cd {shlex.quote(directory)}",
            "set -a; . ./env; set +a",
            # trap covers the clean exit; the transient timer (tier host) or the
            # janitor (tier ssh) covers everything else.
            #
            # NOT `exec` below, and this is the whole reason: exec replaces the
            # shell, and a replaced shell runs no traps — the credential and the
            # binary would then survive every ordinary exit, which is exactly
            # the thing "leaves nothing behind" promises they do not. One extra
            # shell process is the price of the cleanup actually happening.
            f"trap 'rm -rf {shlex.quote(directory)}' EXIT INT TERM",
            'echo "cockpit — investigation #{} — {}"'.format(
                investigation_id, "no isolation: this session runs directly on the host"),
            'echo "the session token dies with this session, or at its TTL."',
            (f"{wrapper} {int(ttl_seconds)} ./cfassist attach {investigation_id} "
             f'--agent-url "$CFOP_AGENT_URL" --no-session-token '
             f'${{CFOP_COCKPIT_LLM_URL:+--url "$CFOP_COCKPIT_LLM_URL"}} '
             f'${{CFOP_COCKPIT_LLM_MODEL:+--model "$CFOP_COCKPIT_LLM_MODEL"}} '
             "|| status=$?"),
            # `set -e` would take the exit before the trap could report it, and
            # a session that ends non-zero (timeout fires: 124) is a normal
            # outcome to pass back rather than a failure to swallow.
            "exit ${status:-0}",
        ]) + "\n"

    def _attach_argv(self, host: str, remote: Sequence[str], *, tty: bool = True
                     ) -> List[str]:
        """The operator-side attach, as argv.

        argv rather than a command string because the client executes what the
        agent returns: a string would have to go through a shell on the
        operator's machine, which turns a compromised or confused agent into
        arbitrary local execution. Nothing here needs a shell.
        """
        cfg = self._config
        host_cfg = cfg.hosts.get(host) or {}
        ssh_cfg = host_cfg.get("ssh") if isinstance(host_cfg.get("ssh"), dict) else {}
        argv = ["ssh"]
        if tty:
            argv.append("-t")
        user = str(ssh_cfg.get("user") or cfg.ssh_user).strip()
        address = str(host_cfg.get("address") or host).strip()
        port = ssh_cfg.get("port")
        if port:
            argv.extend(["-p", str(port)])
        argv.append(f"{user}@{address}")
        argv.extend(remote)
        return argv

    def _ssh_host(self, host: str, remote_command: str, stdin: Optional[bytes] = None
                  ) -> Tuple[int, str, str]:
        cfg = self._config
        if cfg.ssh_secret_dir and not self._identity_staged:
            # Once per process, and lazily: an install with no host inventory
            # never has a secret mounted, and should not log about one.
            self._identity_staged = True
            if prepare_ssh_identity(cfg.ssh_secret_dir):
                logger.info("cockpit: staged the ssh identity from %s", cfg.ssh_secret_dir)
            else:
                logger.warning("cockpit: ssh secret dir %s is missing or empty; "
                               "host tiers will fail to authenticate", cfg.ssh_secret_dir)
        host_cfg = cfg.hosts.get(host) or {}
        ssh_cfg = host_cfg.get("ssh") if isinstance(host_cfg.get("ssh"), dict) else {}
        user = str(ssh_cfg.get("user") or cfg.ssh_user).strip()
        address = str(host_cfg.get("address") or host).strip()
        key = str(ssh_cfg.get("key_path") or cfg.ssh_key_path).strip()
        argv = [
            "ssh",
            "-o", f"ConnectTimeout={cfg.ssh_connect_timeout}",
            # Never prompt: this runs in a pod with no terminal, and a
            # passphrase prompt there is a hung spawn rather than an error.
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]
        if key:
            argv.extend(["-i", os.path.expanduser(key)])
        port = ssh_cfg.get("port")
        if port:
            argv.extend(["-p", str(port)])
        argv.append(f"{user}@{address}")
        # `sh` with the script on stdin is not usable here — stdin carries the
        # payload — so the command goes as an argument and every interpolated
        # value in it is shlex-quoted at the call site.
        argv.append(remote_command)
        return self._ssh(argv, stdin)

    def _revoke_quietly(self, token: Dict[str, Any]) -> None:
        token_id = token.get("id")
        if self._revoke is None or token_id is None:
            return
        try:
            self._revoke(int(token_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not revoke cockpit token %s after a failed spawn: %s",
                           token.get("prefix"), exc)


# ---- module helpers ----------------------------------------------------------

#: Enumerates session directories and the expiry each one recorded. ``mtime`` is
#: the fallback for a directory whose ``expires`` never got written (a spawn
#: that died between mkdir and install): without it that directory would be
#: immortal, which is the leak the janitor exists to close.
_SESSION_LISTING = (
    "for d in /tmp/" + SESSION_PREFIX + "-*/; do "
    "[ -d \"$d\" ] || continue; "
    "e=$(cat \"$d/expires\" 2>/dev/null || echo ''); "
    "[ -n \"$e\" ] || e=$(( $(stat -c %Y \"$d\" 2>/dev/null || echo 0) + "
    + str(MAX_TTL_SECONDS) + " )); "
    "printf '%s %s\\n' \"${d%/}\" \"$e\"; done"
)


def cancel_reap_unit_command(name: str, *, user: bool = True, sudo: bool = False) -> str:
    """Stop and forget a session's self-destruct timer, if it has one.

    Stop *and* ``reset-failed``: a unit that failed is not collected, so it
    keeps the name and blocks the next spawn for the same investigation — which
    is the same investigation an hour later, i.e. the normal case.

    Every command is silenced and the whole thing ends in ``true``: a unit that
    was never created is the expected state on a first spawn, and a non-zero
    exit there would read as a failure to arm.
    """
    parts = []
    unit = shlex.quote(f"{name}-reap.timer")
    service = shlex.quote(f"{name}-reap.service")
    if user:
        parts += [f"systemctl --user stop {unit} {service} >/dev/null 2>&1",
                  f"systemctl --user reset-failed {unit} {service} >/dev/null 2>&1"]
    if sudo:
        parts += [f"sudo -n systemctl stop {unit} {service} >/dev/null 2>&1",
                  f"sudo -n systemctl reset-failed {unit} {service} >/dev/null 2>&1"]
    return "; ".join(parts + ["true"])


def session_name(investigation_id: int) -> str:
    """Stable per-investigation name — the dedupe, the attach and the janitor
    all address a session by it, so it must not carry a timestamp the way the
    tier-1 Job name does (a Job is addressed by the API, not by convention)."""
    return f"{SESSION_PREFIX}-{int(investigation_id)}"


def _parse_expiry_listing(output: str) -> List[Tuple[str, float]]:
    """``<name> <expires>`` lines → pairs. A blank or unparseable expiry reads
    as 0 (expired): the artifact exists, so something spawned it, and an
    unreadable deadline is not a reason to keep it forever."""
    rows: List[Tuple[str, float]] = []
    for line in (output or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if not name:
            continue
        try:
            expires = float(parts[1]) if len(parts) > 1 else 0.0
        except (TypeError, ValueError):
            expires = 0.0
        rows.append((name, expires))
    return rows


def prepare_ssh_identity(secret_dir: str, ssh_dir: Optional[str] = None) -> bool:
    """Stage a mounted SSH secret into ``~/.ssh`` with key-safe permissions.

    The same dance the executor does (``executor/nodeaction.py:prepare_ssh``)
    and for the same two reasons: a Kubernetes secret volume is root-owned and
    at best group-readable, which ssh refuses outright for a private key; and
    copying the whole directory means the key keeps its own filename, so ssh
    finds it as a default identity and nothing has to know whether the fleet
    uses ``id_rsa`` or ``id_ed25519``.

    Not shared code with the executor on purpose — that image is deliberately
    stdlib-only and imports nothing from here, so the duplication is the price
    of the executor staying portable.
    """
    source = pathlib.Path(secret_dir)
    if not source.is_dir():
        return False
    target_dir = pathlib.Path(ssh_dir) if ssh_dir else pathlib.Path.home() / ".ssh"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = False
    for entry in source.iterdir():
        # Secret volumes use ..data/ symlink indirection; skip the dot-dirs.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        target = target_dir / entry.name
        shutil.copyfile(entry, target)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — ssh refuses looser
        staged = True
    return staged


def _run_ssh(argv: Sequence[str], stdin: Optional[bytes]) -> Tuple[int, str, str]:
    """Run ssh. Bytes on stdin because tier 3 pushes a binary through it."""
    try:
        proc = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            timeout=180,
        )
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))
    except FileNotFoundError:
        return 127, "", "ssh not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "ssh timed out"


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cfoperator-cockpit"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - pinned https release URL
        return resp.read()
