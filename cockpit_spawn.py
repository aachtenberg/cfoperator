"""Ephemeral cockpit Jobs — tier 1 of the incident cockpit (CFOP-35).

`cfassist attach <id>` briefs a session wherever the operator already is.
`--spawn` puts that session *on the affected infrastructure*: a Job whose pod
carries the toolchain (kubectl, ssh, cfassist), a credential that dies with it,
a TTL, and nothing resident afterwards.

Three conventions are inherited rather than invented:

* **kubectl over a subprocess, not the python k8s client.** Both existing
  launchers — ``event_runtime/deep_investigation.py`` and
  ``agent/agent.py:_build_executor_manifest`` — shell out to
  ``kubectl create -f -`` and authenticate with the pod's own service account
  token. The runner is injectable so tests never shell out.
* **The Job's guards are the deep-investigation launcher's guards:** dedupe per
  investigation, then a concurrency cap. (No daily budget: a cockpit costs an
  operator's attention, which is self-limiting in a way an LLM Job is not.)
* **The pod runs as a different, weaker identity than the agent.** The Job's
  service account is ``cfoperator-cockpit``: read-only, no exec, no write, no
  secrets — the same posture as ``cfoperator-worker``.

The one thing that is *not* inherited: the session token never appears in the
manifest. Anyone who can read a Job can read its env, and a Job manifest is the
most widely-readable object in the namespace, so the token is written to a
short-lived Secret referenced by ``secretKeyRef``. The Secret carries an
ownerReference to the Job, so Kubernetes' own TTL machinery deletes both.
``test_cockpit_spawn.py`` asserts the secret value is absent from the manifest.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("cfoperator.cockpit")

# Shared with the other ephemeral workloads so one label selector finds every
# cfoperator-launched Job; the per-investigation label is what the CFOP-36
# janitor (and the dedupe below) hangs off.
JOB_ROLE_LABEL = "cfop.dev/role"
JOB_ROLE_VALUE = "cockpit"
COCKPIT_LABEL = "cfop-cockpit"
# Tier 1's name in the ladder (CFOP-36). Defined here rather than imported from
# cockpit_ladder so the dependency runs one way: the ladder builds on tier 1,
# not the other way round.
TIER_POD = "pod"

#: "the caller did not look this up", which is not the same as "the caller
#: looked and there is no such node" — the second is a decision worth
#: inheriting, the first is a lookup still to do.
_UNRESOLVED = object()

DEFAULT_IMAGE = "ghcr.io/aachtenberg/cfoperator-cockpit:main"
DEFAULT_SERVICE_ACCOUNT = "cfoperator-cockpit"
# Matches cfassist's own --session-ttl default: the pod's credential and the
# pod's deadline are the same session, so they expire together.
DEFAULT_TTL_SECONDS = 4 * 60 * 60
# A cockpit is a human sitting in a terminal. Past half a day it is not a
# session any more, it is a resident agent — which is the thing this design
# exists to avoid.
MAX_TTL_SECONDS = 12 * 60 * 60
DEFAULT_TTL_AFTER_FINISHED = 60 * 60
DEFAULT_MAX_CONCURRENT = 2

# The env var every cfassist client reads (ResolveEndpoint / mcp_server). The
# pod inherits the session credential through it, so children of the in-pod
# session get the dying token and never a standing one.
TOKEN_ENV = "CFOP_API_TOKEN"

_KubectlRunner = Callable[[Sequence[str], Optional[str]], Tuple[int, str, str]]
_TokenMinter = Callable[..., Dict[str, Any]]
_TokenRevoker = Callable[[int], None]


class CockpitSpawnError(RuntimeError):
    """A spawn that could not happen. ``status`` is the HTTP status to answer.

    Carrying the status here keeps the endpoint a thin translator: 'the node
    listing failed' is a 502 whether it is raised from placement or from the
    dedupe scan, and deciding that twice is how the two drift apart.
    """

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


@dataclass
class CockpitConfig:
    namespace: str = "apps"
    image: str = DEFAULT_IMAGE
    service_account: str = DEFAULT_SERVICE_ACCOUNT
    image_pull_secret: str = "ghcr-pull-secret"
    # In-cluster address the pod fetches its own briefing from. Not the
    # operator-facing URL: the pod resolves cluster DNS, not the LAN name.
    agent_url: str = "http://cfoperator.apps.svc.cluster.local:8083"
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    ttl_after_finished_seconds: int = DEFAULT_TTL_AFTER_FINISHED
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    # The in-pod session needs a model. Inherited from the agent's own llm
    # block by default — a cockpit that talks to a different model than the
    # investigation it is about would be a confusing thing to hand someone.
    llm_url: str = ""
    llm_model: str = ""


def build_cockpit_config(agent_config: Any = None) -> CockpitConfig:
    """Config from the agent's ``cockpit:`` block, env, then defaults.

    Env wins over the config file for the deployment-shaped values (namespace,
    image, agent URL) the same way the executor's do: those are set by the
    manifest that also sets the image tag, so keeping them together is what
    stops a config file from outliving a deploy.
    """
    cfg = agent_config if isinstance(agent_config, dict) else {}
    block = cfg.get("cockpit") if isinstance(cfg.get("cockpit"), dict) else {}

    # The agent's in-memory config, not a config.yaml: cfshared.config's
    # normalize_aliases folds the flat getting-started keys (llm.url,
    # llm.model) into llm.primary.* at load, so by the time anything here runs
    # the flat keys are gone. Reading them left CFOP_COCKPIT_LLM_URL empty,
    # which made the entrypoint drop --url and point the in-pod session at
    # cfassist's localhost default instead of the model the investigation
    # actually ran on. llm.primary is where every other consumer looks
    # (web_server's ollama URL, _triage_model, embeddings); the flat keys stay
    # as a fallback for a config dict that never went through the loader.
    llm_block = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    primary = llm_block.get("primary") if isinstance(llm_block.get("primary"), dict) else {}
    llm = {**llm_block, **primary}

    def _str(env: str, key: str, default: str) -> str:
        return str(os.getenv(env) or block.get(key) or default).strip()

    def _int(env: str, key: str, default: int) -> int:
        raw = os.getenv(env) or block.get(key) or default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return CockpitConfig(
        namespace=_str("CFOP_COCKPIT_NAMESPACE", "namespace", "apps"),
        image=_str("CFOP_COCKPIT_IMAGE", "image", DEFAULT_IMAGE),
        service_account=_str("CFOP_COCKPIT_SERVICE_ACCOUNT", "service_account",
                             DEFAULT_SERVICE_ACCOUNT),
        image_pull_secret=_str("CFOP_COCKPIT_IMAGE_PULL_SECRET", "image_pull_secret",
                               "ghcr-pull-secret"),
        agent_url=_str("CFOP_COCKPIT_AGENT_URL", "agent_url",
                       "http://cfoperator.apps.svc.cluster.local:8083"),
        ttl_seconds=_int("CFOP_COCKPIT_TTL_SECONDS", "ttl_seconds", DEFAULT_TTL_SECONDS),
        ttl_after_finished_seconds=_int("CFOP_COCKPIT_TTL_AFTER_FINISHED",
                                        "ttl_after_finished_seconds",
                                        DEFAULT_TTL_AFTER_FINISHED),
        max_concurrent=_int("CFOP_COCKPIT_MAX_CONCURRENT", "max_concurrent",
                            DEFAULT_MAX_CONCURRENT),
        llm_url=_str("CFOP_COCKPIT_LLM_URL", "llm_url", str(llm.get("url") or "")),
        llm_model=_str("CFOP_COCKPIT_LLM_MODEL", "llm_model", str(llm.get("model") or "")),
    )


def clamp_ttl(requested: Any, default: int = DEFAULT_TTL_SECONDS) -> int:
    """Bound a caller-supplied TTL. Junk reads as 'unspecified', not as zero —
    an activeDeadlineSeconds of 0 kills the pod before the operator attaches."""
    try:
        ttl = int(requested)
    except (TypeError, ValueError):
        return default
    if ttl <= 0:
        return default
    return min(ttl, MAX_TTL_SECONDS)


class CockpitSpawner:
    """Launches (and dedupes) the ephemeral cockpit Job for an investigation."""

    def __init__(
        self,
        config: CockpitConfig,
        *,
        kubectl_runner: Optional[_KubectlRunner] = None,
        token_minter: Optional[_TokenMinter] = None,
        token_revoker: Optional[_TokenRevoker] = None,
    ):
        self._config = config
        self._kubectl = kubectl_runner or _run_kubectl
        self._mint = token_minter
        self._revoke = token_revoker

    @property
    def config(self) -> CockpitConfig:
        return self._config

    # ---- the launch ---------------------------------------------------------

    def spawn(self, investigation_id: int, *, host: str = "",
              ttl_seconds: Optional[int] = None,
              node: Any = _UNRESOLVED) -> Dict[str, Any]:
        """Create the cockpit Job (and its token Secret) for an investigation.

        Returns the coordinates the caller needs to attach. Raises
        :class:`CockpitSpawnError` with an HTTP status for every refusal, so a
        deduped spawn and a failed one are never confused.

        ``node`` may be passed pre-resolved by a caller that already looked the
        host up — the ladder does, to decide tier 1 versus tiers 2/3 (CFOP-36).
        Looking it up twice would not only cost a second API call: the two
        answers could disagree about whether a host is in the cluster, and the
        session would land somewhere neither decision intended.
        """
        cfg = self._config
        ttl = clamp_ttl(ttl_seconds if ttl_seconds is not None else cfg.ttl_seconds,
                        cfg.ttl_seconds)

        active = self._active_jobs()

        # Dedupe first, cap second: an operator re-running the same command
        # must land back in their existing cockpit rather than be told the
        # cluster is busy with a Job that is theirs.
        for job in active:
            if job.get("investigation") == str(investigation_id):
                return {
                    "status": "existing",
                    "tier": TIER_POD,
                    "job_name": job.get("name", ""),
                    "namespace": cfg.namespace,
                    "investigation_id": investigation_id,
                    "pod_selector": f"{COCKPIT_LABEL}={investigation_id}",
                    "attach_argv": self.attach_argv(job.get("name", "")),
                    "attach_command": self.attach_command(job.get("name", "")),
                    # No new token and no new placement decision: this is the
                    # cockpit that already exists, reported as such rather than
                    # dressed up as a fresh one.
                    "placement": {"node": "", "note": "existing cockpit for this investigation"},
                }
        if len(active) >= cfg.max_concurrent:
            raise CockpitSpawnError(
                f"cockpit concurrency cap reached ({cfg.max_concurrent} active)", 429)

        node, placement_note = self._placement(host, node)

        if self._mint is None:
            raise CockpitSpawnError(
                "no token store: a cockpit cannot be spawned without a "
                "per-investigation session token", 503)
        # tier/host ride along into the audit row: "which runtime did this
        # session get, and on what" is the first question asked of a cockpit
        # after the fact, and the token mint is the one event every tier shares.
        token = self._mint(investigation_id, ttl, tier=TIER_POD, host=node or "")

        job_name = self._job_name(investigation_id)
        secret_name = f"{job_name}-token"
        manifest = self._build_cockpit_manifest(
            investigation_id,
            job_name=job_name,
            secret_name=secret_name,
            node=node,
            placement_note=placement_note,
            ttl_seconds=ttl,
        )

        # Job first, Secret second, so the Secret can carry an ownerReference to
        # a UID that exists: GC then deletes the credential with the Job and the
        # agent never needs `delete` on secrets. The cost is a few seconds of
        # CreateContainerConfigError if the kubelet is quicker than this call —
        # the kubelet retries, and the alternative (a standing delete grant on
        # every secret in the namespace) is a worse trade.
        code, out, stderr = self._kubectl(
            ["create", "-n", cfg.namespace, "-o", "json", "-f", "-"], json.dumps(manifest))
        if code != 0:
            self._revoke_quietly(token)
            raise CockpitSpawnError(f"kubectl create failed: {stderr.strip()[:500]}", 502)

        job_uid = ""
        try:
            job_uid = str((json.loads(out).get("metadata") or {}).get("uid") or "")
        except (ValueError, AttributeError):
            logger.warning("cockpit Job %s created but its UID was unreadable; the "
                           "token Secret will not be garbage-collected with it", job_name)

        secret = self._build_token_secret_manifest(
            secret_name, job_name=job_name, job_uid=job_uid, token=str(token.get("secret") or ""))
        code, _out, stderr = self._kubectl(
            ["create", "-n", cfg.namespace, "-f", "-"], json.dumps(secret))
        if code != 0:
            # A cockpit with no credential is a pod that will sit in
            # CreateContainerConfigError until its deadline: tear it down.
            self._kubectl(["delete", "job", job_name, "-n", cfg.namespace], None)
            self._revoke_quietly(token)
            raise CockpitSpawnError(
                f"kubectl create secret failed: {stderr.strip()[:500]}", 502)

        logger.info("Spawned cockpit Job %s for investigation %s (%s, ttl=%ss)",
                    job_name, investigation_id, placement_note, ttl)
        return {
            "status": "spawned",
            "tier": TIER_POD,
            "job_name": job_name,
            "namespace": cfg.namespace,
            "investigation_id": investigation_id,
            "pod_selector": f"{COCKPIT_LABEL}={investigation_id}",
            "attach_argv": self.attach_argv(job_name),
            "attach_command": self.attach_command(job_name),
            "ttl_seconds": ttl,
            "placement": {"node": node or "", "note": placement_note},
            "token_prefix": str(token.get("prefix") or ""),
        }

    def attach_argv(self, job_name: str) -> List[str]:
        """The operator-side attach, as argv. Deliberately *their* kubectl: no
        service identity in this system holds pods/attach or pods/exec, and an
        operator spawning a cockpit from a laptop has cluster credentials by
        definition. The agent-side PTY bridge the console drawer needs is
        CFOP-59's problem, and that is where the RBAC question gets decided.

        argv rather than only a string because the client *executes* this: a
        command string would have to go through a shell on the operator's
        machine. Every tier answers in the same shape (CFOP-36), so the client
        never has to know which runtime it is attaching to.
        """
        return ["kubectl", "attach", "-it", "-n", self._config.namespace,
                f"job/{job_name}"]

    def attach_command(self, job_name: str) -> str:
        """The same attach, rendered for a human to read or paste."""
        return " ".join(self.attach_argv(job_name))

    # ---- guards -------------------------------------------------------------

    def _active_jobs(self) -> List[Dict[str, str]]:
        """Cockpit Jobs still running. A listing failure is an error, never an
        empty list — 'I could not check' must not read as 'nothing is running',
        which would defeat both the dedupe and the cap."""
        cfg = self._config
        code, out, stderr = self._kubectl(
            ["get", "jobs", "-n", cfg.namespace,
             "-l", f"{JOB_ROLE_LABEL}={JOB_ROLE_VALUE}", "-o", "json"],
            None,
        )
        if code != 0:
            raise CockpitSpawnError(
                f"kubectl job listing failed: {stderr.strip()[:500] or code}", 502)
        try:
            items = json.loads(out).get("items", [])
        except (ValueError, AttributeError) as exc:
            raise CockpitSpawnError(
                f"unparseable kubectl output ({type(exc).__name__})", 502)

        active: List[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not (item.get("status") or {}).get("active"):
                continue
            meta = item.get("metadata") or {}
            labels = meta.get("labels") or {}
            active.append({
                "name": meta.get("name", ""),
                "investigation": str(labels.get(COCKPIT_LABEL, "")),
            })
        return active

    def get_node(self, host: str) -> Optional[Dict[str, Any]]:
        """The cluster node named ``host``, or None if there is no such node.

        Split out of ``_placement`` because the CFOP-36 ladder asks a different
        question of the same call — "is this a cluster node at all", which
        decides tier 1 versus tiers 2/3 — and two kubectl invocations that
        could disagree about whether a host is in the cluster would put a
        session in the wrong place.
        """
        host = (host or "").strip()
        if not host:
            return None
        # `--` before the name: host is ultimately alert-derived, and a value
        # starting with `-` would otherwise be parsed as a kubectl flag.
        code, out, stderr = self._kubectl(["get", "node", "-o", "json", "--", host], None)
        if code != 0:
            logger.info("cockpit: %s is not a cluster node (%s)", host, stderr.strip()[:200])
            return None
        try:
            node = json.loads(out)
        except (ValueError, AttributeError):
            logger.warning("cockpit: node %s returned unreadable JSON", host)
            return None
        return node if isinstance(node, dict) else None

    def _placement(self, host: str,
                   node: Any = _UNRESOLVED) -> Tuple[Optional[str], str]:
        """Resolve the nodeSelector for a host-level finding.

        The unschedulable case from the issue is the interesting one: the node
        the incident is about is frequently the cordoned one, and a cockpit
        that sits Pending forever is worse than one that runs next door. We
        tolerate nothing on purpose, so *any* NoSchedule/NoExecute taint —
        cordon, NotReady, disk pressure — means spawn adjacent and say so.
        """
        host = (host or "").strip()
        if not host:
            return None, "no affected node on the investigation — spawned anywhere"

        if node is _UNRESOLVED:
            node = self.get_node(host)
        if node is None:
            # Not a cluster node (a bare host, a VM, a typo): nothing to pin to.
            # Not an error — most investigations are not host-level, and the
            # ones that are on a *bare* host are the ladder's tiers 2/3.
            return None, f"{host} is not a cluster node — spawned anywhere"

        spec = node.get("spec") or {}
        if spec.get("unschedulable"):
            return None, f"spawned adjacent — node {host} is cordoned"
        blocking = [
            t.get("key", "?") for t in (spec.get("taints") or [])
            if isinstance(t, dict) and t.get("effect") in ("NoSchedule", "NoExecute")
        ]
        if blocking:
            return None, (f"spawned adjacent — node {host} carries {', '.join(blocking)} "
                          "and a cockpit tolerates nothing")
        return host, f"pinned to node {host}"

    # ---- manifests ----------------------------------------------------------

    def _job_name(self, investigation_id: int) -> str:
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        return f"cfop-cockpit-{investigation_id}-{stamp}"

    def _build_cockpit_manifest(
        self,
        investigation_id: int,
        *,
        job_name: str,
        secret_name: str,
        node: Optional[str],
        placement_note: str,
        ttl_seconds: int,
    ) -> Dict[str, Any]:
        cfg = self._config
        labels = {
            "app.kubernetes.io/managed-by": "cfoperator",
            JOB_ROLE_LABEL: JOB_ROLE_VALUE,
            COCKPIT_LABEL: str(investigation_id),
        }
        env = [
            {"name": "CFOP_INVESTIGATION_ID", "value": str(investigation_id)},
            {"name": "CFOP_AGENT_URL", "value": cfg.agent_url},
            {"name": "CFOP_COCKPIT_PLACEMENT", "value": placement_note},
            {"name": "CFOP_COCKPIT_LLM_URL", "value": cfg.llm_url},
            {"name": "CFOP_COCKPIT_LLM_MODEL", "value": cfg.llm_model},
            # The credential, by reference. Never {"value": <secret>}: a Job
            # manifest is readable by anything with `get jobs`, which includes
            # the cockpit's own read-only service account.
            {"name": TOKEN_ENV,
             "valueFrom": {"secretKeyRef": {"name": secret_name, "key": TOKEN_ENV}}},
        ]
        pod_spec: Dict[str, Any] = {
            "restartPolicy": "Never",
            "serviceAccountName": cfg.service_account,
            "imagePullSecrets": [{"name": cfg.image_pull_secret}],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
            "containers": [
                {
                    "name": "cockpit",
                    "image": cfg.image,
                    # Floating :main tag (no kustomize transformer reaches an
                    # env-carried image ref), so nodes must not keep a cached one.
                    "imagePullPolicy": "Always",
                    # An interactive Job: the pty exists from the start, and
                    # stdin stays open across detach/re-attach cycles.
                    # stdinOnce would end the session the first time the
                    # operator's laptop dropped its connection.
                    "stdin": True,
                    "stdinOnce": False,
                    "tty": True,
                    "env": env,
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                }
            ],
        }
        if node:
            pod_spec["nodeSelector"] = {"kubernetes.io/hostname": node}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": cfg.namespace, "labels": dict(labels)},
            "spec": {
                "backoffLimit": 0,  # a rerun is the operator typing the command again
                "ttlSecondsAfterFinished": cfg.ttl_after_finished_seconds,
                # The session TTL *is* the deadline: an orphaned cockpit (laptop
                # closed, VPN dropped) dies here without anyone noticing it.
                "activeDeadlineSeconds": ttl_seconds,
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": pod_spec,
                },
            },
        }

    def _build_token_secret_manifest(self, secret_name: str, *, job_name: str,
                                     job_uid: str, token: str) -> Dict[str, Any]:
        cfg = self._config
        meta: Dict[str, Any] = {
            "name": secret_name,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "cfoperator",
                JOB_ROLE_LABEL: JOB_ROLE_VALUE,
            },
        }
        if job_uid:
            # Owned by the Job: ttlSecondsAfterFinished deletes the Job, and
            # cascading GC takes the credential with it. blockOwnerDeletion
            # stays false so a stuck Secret can never wedge the Job's deletion.
            meta["ownerReferences"] = [{
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job_name,
                "uid": job_uid,
                "controller": False,
                "blockOwnerDeletion": False,
            }]
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": meta,
            "stringData": {TOKEN_ENV: token},
        }

    # ---- helpers ------------------------------------------------------------

    def _revoke_quietly(self, token: Dict[str, Any]) -> None:
        """Kill a token whose pod never existed. Best-effort: it expires anyway,
        and a revoke failure must not mask the spawn failure being reported."""
        token_id = token.get("id")
        if self._revoke is None or token_id is None:
            return
        try:
            self._revoke(int(token_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not revoke cockpit token %s after a failed spawn: %s",
                           token.get("prefix"), exc)


def _run_kubectl(args: Sequence[str], stdin: Optional[str]) -> Tuple[int, str, str]:
    """Run kubectl with the given args. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["kubectl", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "kubectl not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "kubectl timed out"
