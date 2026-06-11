"""Deep-investigation tier: ephemeral k8s Jobs running a headless agent.

This is the escalation tier above the agent's HTTP investigate loop. When
triage escalates a host-shaped alert (or investigates one with low
confidence), the ``EscalationRoutingDecisionEngine`` wrapper rewrites the
decision to ``deep_investigate`` and the ``DeepInvestigationActionHandler``
launches a Kubernetes Job running the cfoperator-worker image (headless
Claude Code + ssh + read-only kubectl). The Job SSHes into the affected host,
performs forensics (journalctl, dmesg, disk health), and posts its report
back to ``POST /v1/investigations/{alert_id}/complete`` — the same completion
protocol the agent uses, so notifications, audit events, and the escalation
ledger all work unchanged.

The handler launches quietly; the completion post-back fires the loud
notification (matching the HTTP investigate UX). Guards, in order:

  1. fingerprint dedupe — an active Job for the same alert fingerprint
     means we skip quietly;
  2. concurrency cap — at the limit we defer LOUDLY (the operator is paged
     rather than the alert silently dropping);
  3. daily launch budget — same loud deferral, persisted across restarts in
     a small JSON state file.

Everything here is wired from ``bootstrap`` only when
``event_runtime.deep_investigation.enabled`` (or
``CFOP_DEEP_INVESTIGATION_ENABLED``) is true; disabled deployments are
byte-identical to before this module existed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .models import ActionRequest, ActionResult, Alert, ContextEnvelope, Decision, utc_now
from .plugins import ActionHandler, DecisionEngine

logger = logging.getLogger(__name__)

DEEP_INVESTIGATE_ACTION = "deep_investigate"

JOB_ROLE_LABEL = "cfop.dev/role"
JOB_ROLE_VALUE = "deep-investigation"
JOB_FINGERPRINT_LABEL = "cfop.dev/fingerprint"

# Job env vars carrying JSON payloads are capped so a sweep-enriched alert
# can't blow past the k8s env-size limits and reject the pod.
_ENV_JSON_MAX_BYTES = 16 * 1024

# kubectl exit/parse problems shouldn't take the engine down; they surface
# as loud ActionResults instead.
_KubectlRunner = Callable[[Sequence[str], Optional[str]], Tuple[int, str, str]]


@dataclass(slots=True)
class DeepInvestigationConfig:
    """Knobs for the deep-investigation tier (config.yaml + CFOP_DEEP_* env)."""

    enabled: bool = False
    image: str = "ghcr.io/aachtenberg/cfoperator-worker:main"
    namespace: str = "apps"
    service_account: str = "cfoperator-worker"
    secrets_name: str = "cfoperator-secrets"
    image_pull_secret: str = "ghcr-pull-secret"
    ssh_secret_name: str = "cfop-forensics-ssh"
    ssh_user: str = "aachten"
    completion_base_url: str = ""
    agent_url: str = ""
    max_concurrent: int = 2
    daily_budget: int = 10
    ttl_seconds_after_finished: int = 21600
    active_deadline_seconds: int = 900
    claude_timeout_seconds: int = 600
    default_template: str = "host-forensics"
    default_model: str = "claude-opus-4-8"
    budget_state_path: str = ""
    # Routing knobs (consumed by EscalationRoutingDecisionEngine)
    confidence_threshold: float = 0.4
    route_escalate: bool = True
    route_low_confidence_investigate: bool = True
    escalate_fallback_action: str = "notify"


# slots=True turns class attributes into member descriptors, so defaults must
# be read off an instance, not the class.
_DEFAULTS = DeepInvestigationConfig()


def build_deep_investigation_config(event_runtime_cfg: dict) -> DeepInvestigationConfig:
    """Resolve config from the ``event_runtime.deep_investigation`` block.

    Env vars win over YAML, YAML over defaults — same precedence as the
    scheduler config loader in bootstrap. The caller passes the already
    expanded ``event_runtime`` config block so this module never reads the
    YAML file itself (keeps the bootstrap-import direction one-way).
    """
    raw = event_runtime_cfg.get("deep_investigation") if isinstance(event_runtime_cfg, dict) else None
    cfg = raw if isinstance(raw, dict) else {}
    routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}

    def _str(env: str, key: str, default: str, source: dict = cfg) -> str:
        return str(os.getenv(env, "").strip() or source.get(key) or default)

    def _int(env: str, key: str, default: int, source: dict = cfg) -> int:
        raw_value = os.getenv(env, "").strip() or source.get(key)
        try:
            return int(raw_value) if raw_value is not None else default
        except (TypeError, ValueError):
            return default

    def _float(env: str, key: str, default: float, source: dict = routing) -> float:
        raw_value = os.getenv(env, "").strip() or source.get(key)
        try:
            return float(raw_value) if raw_value is not None else default
        except (TypeError, ValueError):
            return default

    def _flag(env: str, key: str, default: bool, source: dict = cfg) -> bool:
        raw_value = os.getenv(env)
        if raw_value is not None:
            return raw_value.strip().lower() in {"1", "true", "yes", "on"}
        value = source.get(key)
        return default if value is None else bool(value)

    return DeepInvestigationConfig(
        enabled=_flag("CFOP_DEEP_INVESTIGATION_ENABLED", "enabled", False),
        image=_str("CFOP_DEEP_WORKER_IMAGE", "image", _DEFAULTS.image),
        namespace=_str("CFOP_DEEP_JOB_NAMESPACE", "namespace", _DEFAULTS.namespace),
        service_account=_str("CFOP_DEEP_SERVICE_ACCOUNT", "service_account", _DEFAULTS.service_account),
        secrets_name=_str("CFOP_DEEP_SECRETS_NAME", "secrets_name", _DEFAULTS.secrets_name),
        image_pull_secret=_str("CFOP_DEEP_IMAGE_PULL_SECRET", "image_pull_secret", _DEFAULTS.image_pull_secret),
        ssh_secret_name=_str("CFOP_DEEP_SSH_SECRET_NAME", "ssh_secret_name", _DEFAULTS.ssh_secret_name),
        ssh_user=_str("CFOP_DEEP_SSH_USER", "ssh_user", _DEFAULTS.ssh_user),
        completion_base_url=_str("CFOP_DEEP_COMPLETION_BASE_URL", "completion_base_url", ""),
        agent_url=_str("CFOP_DEEP_AGENT_URL", "agent_url", os.getenv("CFOP_AGENT_URL", "").strip()),
        max_concurrent=_int("CFOP_DEEP_MAX_CONCURRENT", "max_concurrent", _DEFAULTS.max_concurrent),
        daily_budget=_int("CFOP_DEEP_DAILY_BUDGET", "daily_budget", _DEFAULTS.daily_budget),
        ttl_seconds_after_finished=_int(
            "CFOP_DEEP_TTL_SECONDS", "ttl_seconds_after_finished", _DEFAULTS.ttl_seconds_after_finished
        ),
        active_deadline_seconds=_int(
            "CFOP_DEEP_ACTIVE_DEADLINE_SECONDS", "active_deadline_seconds", _DEFAULTS.active_deadline_seconds
        ),
        claude_timeout_seconds=_int(
            "CFOP_DEEP_CLAUDE_TIMEOUT", "claude_timeout_seconds", _DEFAULTS.claude_timeout_seconds
        ),
        default_template=_str("CFOP_DEEP_TEMPLATE", "default_template", _DEFAULTS.default_template),
        default_model=_str("CFOP_DEEP_MODEL", "default_model", _DEFAULTS.default_model),
        budget_state_path=_str("CFOP_DEEP_BUDGET_STATE_PATH", "budget_state_path", ""),
        confidence_threshold=_float("CFOP_DEEP_CONFIDENCE_THRESHOLD", "confidence_threshold", _DEFAULTS.confidence_threshold),
        route_escalate=_flag("CFOP_DEEP_ROUTE_ESCALATE", "route_escalate", True, source=routing),
        route_low_confidence_investigate=_flag(
            "CFOP_DEEP_ROUTE_LOW_CONFIDENCE", "route_low_confidence_investigate", True, source=routing
        ),
        escalate_fallback_action=_str(
            "CFOP_DEEP_ESCALATE_FALLBACK", "escalate_fallback_action", _DEFAULTS.escalate_fallback_action, source=routing
        ),
    )


def _is_host_shaped(alert: Alert) -> bool:
    """Conservative predicate for "this alert is about a host, not a workload".

    Mirrors the Alertmanager normalization in sources.py: ``resource_type``
    is set to node/instance only when there is no pod label, so checking it
    covers the labeled cases; ``details.host`` covers synthetic alerts (e.g.
    future boot-forensics posts) that set the hint directly.
    """
    if alert.resource_type in ("node", "instance"):
        return True
    labels = alert.details.get("labels") if isinstance(alert.details.get("labels"), dict) else {}
    if labels.get("pod"):
        return False
    return bool(alert.details.get("host") or labels.get("node"))


def _target_host(alert: Alert) -> str:
    """Best-effort hostname for the forensics target."""
    labels = alert.details.get("labels") if isinstance(alert.details.get("labels"), dict) else {}
    host = (
        alert.details.get("host")
        or labels.get("node")
        or labels.get("instance")
        or (alert.resource_name if alert.resource_type in ("node", "instance") else None)
    )
    # Instance labels are often host:port — strip the port for ssh.
    return str(host).split(":")[0] if host else ""


class EscalationRoutingDecisionEngine(DecisionEngine):
    """Confidence-threshold tier routing, wrapped around the triage engine.

    Host-shaped ``escalate`` verdicts — and host-shaped ``investigate``
    verdicts the triage LLM was unsure about (0 < confidence < threshold;
    exactly 0.0 means the triage *call failed*, which shouldn't burn deep
    budget) — are rewritten to ``deep_investigate`` with the prior tier's
    findings attached in ``params["deep_context"]``.

    Non-host ``escalate`` verdicts are aliased to a configurable fallback
    action (default ``notify``). Before this module, ``escalate`` had no
    registered handler at all and failed as ``action_missing``.
    """

    name = "escalation-routing"

    def __init__(
        self,
        inner: DecisionEngine,
        *,
        confidence_threshold: float = 0.4,
        route_escalate: bool = True,
        route_low_confidence_investigate: bool = True,
        escalate_fallback_action: str = "notify",
    ):
        self._inner = inner
        self._threshold = float(confidence_threshold)
        self._route_escalate = bool(route_escalate)
        self._route_low_conf = bool(route_low_confidence_investigate)
        self._escalate_fallback = str(escalate_fallback_action or "notify")

    @property
    def inner(self) -> DecisionEngine:
        return self._inner

    def start(self) -> None:
        self._inner.start()

    def stop(self) -> None:
        self._inner.stop()

    def decide(self, envelope: ContextEnvelope) -> Decision:
        decision = self._inner.decide(envelope)

        if not _is_host_shaped(envelope.alert):
            if decision.action == "escalate":
                # No escalate handler exists for workload alerts; alias to the
                # fallback so the verdict pages someone instead of failing.
                params = dict(decision.params)
                params["routed_by"] = self.name
                params["original_action"] = "escalate"
                return replace(decision, action=self._escalate_fallback, params=params)
            return decision

        reroute = (decision.action == "escalate" and self._route_escalate) or (
            decision.action == "investigate"
            and self._route_low_conf
            and 0.0 < decision.confidence < self._threshold
        )
        if not reroute:
            return decision

        params = dict(decision.params)
        params["routed_by"] = self.name
        params["deep_context"] = {
            "original_action": decision.action,
            "original_confidence": decision.confidence,
            "triage_reasoning": decision.reasoning,
            "triage_backend": params.get("triage_backend"),
            "triage_model": params.get("triage_model"),
        }
        logger.info(
            "Routing alert %s to deep investigation (action=%s confidence=%.2f)",
            envelope.alert.alert_id, decision.action, decision.confidence,
        )
        return replace(
            decision,
            action=DEEP_INVESTIGATE_ACTION,
            reasoning=f"rerouted from {decision.action}: {decision.reasoning}",
            params=params,
        )


class DeepInvestigationActionHandler(ActionHandler):
    """Launch an ephemeral forensics Job for a host-shaped alert.

    Job creation goes through ``kubectl create -f -`` (subprocess, matching
    the codebase convention — no python k8s client dependency); the in-pod
    service account token authenticates it. ``kubectl_runner`` is injectable
    so tests never shell out.
    """

    name = "deep-investigation-action"
    action_name = DEEP_INVESTIGATE_ACTION

    def __init__(self, config: DeepInvestigationConfig, *, kubectl_runner: _KubectlRunner | None = None):
        self._config = config
        self._kubectl = kubectl_runner or _run_kubectl
        if config.budget_state_path:
            self._budget_path = Path(config.budget_state_path)
        else:
            base_dir = Path(os.getenv("CFOP_EVENT_RUNTIME_DIR", str(Path.home() / ".cfoperator" / "event-runtime")))
            self._budget_path = base_dir / "deep-investigation" / "budget.json"
        self._budget_lock = threading.Lock()

    def execute(self, request: ActionRequest) -> ActionResult:
        alert = request.alert
        host = _target_host(alert)
        if not host:
            return self._loud_failure(alert, "no target host resolvable from alert", outcome="failed")

        fp12 = alert.effective_fingerprint()[:12]

        active, err = self._active_jobs()
        if err is not None:
            return self._loud_failure(alert, f"kubectl job listing failed: {err}", outcome="failed")
        if any(job.get("fingerprint") == fp12 for job in active):
            return ActionResult(
                action=self.action_name,
                success=True,
                message=f"Deep investigation already running for: {alert.summary}",
                details={"outcome": "deduped", "fingerprint": fp12},
                quiet=True,
            )
        if len(active) >= self._config.max_concurrent:
            return self._loud_deferral(alert, f"concurrency cap reached ({self._config.max_concurrent} active)")
        if not self._budget_take():
            return self._loud_deferral(alert, f"daily budget exhausted ({self._config.daily_budget}/day)")

        template = str(request.decision.params.get("deep_template") or self._config.default_template)
        model = str(request.decision.params.get("deep_model") or self._config.default_model)
        manifest = self._build_job_manifest(alert, request.decision, host=host, fp12=fp12, template=template, model=model)
        job_name = manifest["metadata"]["name"]

        code, _out, stderr = self._kubectl(
            ["create", "-n", self._config.namespace, "-f", "-"], json.dumps(manifest)
        )
        if code != 0:
            self._budget_release()
            return self._loud_failure(alert, f"kubectl create failed: {stderr.strip()[:500]}", outcome="failed")

        logger.info("Launched deep-investigation Job %s for alert %s (host=%s)", job_name, alert.alert_id, host)
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Deep investigation Job {job_name} launched for: {alert.summary}",
            details={
                "job_name": job_name,
                "fingerprint": fp12,
                "host": host,
                "template": template,
                "model": model,
            },
            quiet=True,  # The worker's completion post-back fires the loud notification.
        )

    # ---- guards -----------------------------------------------------------

    def _active_jobs(self) -> Tuple[list[dict], Optional[str]]:
        """List active deep-investigation Jobs. Returns (jobs, error)."""
        code, out, stderr = self._kubectl(
            [
                "get", "jobs",
                "-n", self._config.namespace,
                "-l", f"{JOB_ROLE_LABEL}={JOB_ROLE_VALUE}",
                "-o", "json",
            ],
            None,
        )
        if code != 0:
            return [], stderr.strip()[:500] or f"kubectl exited {code}"
        try:
            items = json.loads(out).get("items", [])
        except (ValueError, AttributeError) as exc:
            return [], f"unparseable kubectl output ({type(exc).__name__})"
        active = []
        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status") or {}
            if not status.get("active"):
                continue
            labels = (item.get("metadata") or {}).get("labels") or {}
            active.append({
                "name": (item.get("metadata") or {}).get("name", ""),
                "fingerprint": labels.get(JOB_FINGERPRINT_LABEL, ""),
            })
        return active, None

    def _budget_take(self) -> bool:
        """Atomically consume one launch from today's budget. False = exhausted."""
        today = utc_now().date().isoformat()
        with self._budget_lock:
            state = self._load_budget()
            if state.get("date") != today:
                state = {"date": today, "count": 0}
            if int(state.get("count") or 0) >= self._config.daily_budget:
                return False
            state["count"] = int(state.get("count") or 0) + 1
            self._persist_budget(state)
        return True

    def _budget_release(self) -> None:
        """Refund a launch that failed before the Job actually existed."""
        with self._budget_lock:
            state = self._load_budget()
            if int(state.get("count") or 0) > 0:
                state["count"] = int(state["count"]) - 1
                self._persist_budget(state)

    def _load_budget(self) -> Dict[str, Any]:
        try:
            with open(self._budget_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Failed to load deep-investigation budget from %s: %s", self._budget_path, exc)
            return {}

    def _persist_budget(self, state: Dict[str, Any]) -> None:
        self._budget_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._budget_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())

    # ---- results ------------------------------------------------------------

    def _loud_deferral(self, alert: Alert, reason: str) -> ActionResult:
        """Deferred-but-not-lost: page the operator instead of silently dropping."""
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Deep investigation deferred ({reason}) for: {alert.summary}",
            details={"outcome": "needs_action", "deferred": True, "reason": reason},
        )

    def _loud_failure(self, alert: Alert, reason: str, *, outcome: str) -> ActionResult:
        return ActionResult(
            action=self.action_name,
            success=False,
            message=f"Deep investigation launch failed ({reason}) for: {alert.summary}",
            details={"outcome": outcome, "reason": reason},
        )

    # ---- Job manifest -------------------------------------------------------

    def _build_job_manifest(
        self, alert: Alert, decision: Decision, *, host: str, fp12: str, template: str, model: str
    ) -> dict:
        cfg = self._config
        stamp = utc_now().strftime("%H%M%S")
        job_name = f"cfop-deep-{fp12}-{stamp}"
        labels = {
            "app.kubernetes.io/managed-by": "cfoperator",
            JOB_ROLE_LABEL: JOB_ROLE_VALUE,
            JOB_FINGERPRINT_LABEL: fp12,
        }
        completion_url = f"{cfg.completion_base_url.rstrip('/')}/v1/investigations/{alert.alert_id}/complete"
        deep_context = decision.params.get("deep_context")
        env = [
            {"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"name": cfg.secrets_name, "key": "ANTHROPIC_API_KEY"}}},
            {"name": "CFOP_COMPLETION_TOKEN", "valueFrom": {"secretKeyRef": {"name": cfg.secrets_name, "key": "CFOP_COMPLETION_SHARED_SECRET", "optional": True}}},
            {"name": "CFOP_COMPLETION_URL", "value": completion_url},
            {"name": "CFOP_AGENT_URL", "value": cfg.agent_url},
            {"name": "CFOP_ALERT_JSON", "value": _bounded_json(alert.to_dict())},
            {"name": "CFOP_DEEP_CONTEXT_JSON", "value": _bounded_json(deep_context if isinstance(deep_context, dict) else {})},
            {"name": "CFOP_TEMPLATE", "value": template},
            {"name": "CFOP_TARGET_HOST", "value": host},
            {"name": "CFOP_SSH_USER", "value": cfg.ssh_user},
            {"name": "CLAUDE_MODEL", "value": model},
            {"name": "CFOP_CLAUDE_TIMEOUT", "value": str(cfg.claude_timeout_seconds)},
        ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": cfg.namespace, "labels": dict(labels)},
            "spec": {
                "backoffLimit": 0,  # an LLM rerun is a human decision, not a retry policy
                "ttlSecondsAfterFinished": cfg.ttl_seconds_after_finished,
                "activeDeadlineSeconds": cfg.active_deadline_seconds,
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": cfg.service_account,
                        "imagePullSecrets": [{"name": cfg.image_pull_secret}],
                        # fsGroup makes the secret volume group-readable by the
                        # worker uid; the entrypoint copies keys into ~/.ssh
                        # with 0600 (ssh refuses group-readable private keys).
                        "securityContext": {
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                        },
                        "containers": [
                            {
                                "name": "worker",
                                "image": cfg.image,
                                "env": env,
                                "volumeMounts": [
                                    {"name": "ssh", "mountPath": "/ssh-secret", "readOnly": True}
                                ],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "256Mi"},
                                    "limits": {"cpu": "1", "memory": "1Gi"},
                                },
                            }
                        ],
                        "volumes": [
                            {"name": "ssh", "secret": {"secretName": cfg.ssh_secret_name, "defaultMode": 0o440}}
                        ],
                    },
                },
            },
        }


def _bounded_json(payload: Dict[str, Any]) -> str:
    """Serialize a dict for a Job env var, trimming oversize content.

    Alert details can carry large sweep/annotation blobs; rather than
    truncating mid-JSON (which would hand the worker an unparseable string),
    drop the heavyweight keys until the payload fits.
    """
    text = json.dumps(payload, default=str, separators=(",", ":"))
    if len(text.encode("utf-8")) <= _ENV_JSON_MAX_BYTES:
        return text

    slim = dict(payload)
    details = slim.get("details")
    if isinstance(details, dict):
        # Keep routing-relevant keys; drop the rest (annotations, sweeps, ...).
        keep = {k: details[k] for k in ("host", "alertname", "labels") if k in details}
        keep["_truncated"] = True
        slim["details"] = keep
    text = json.dumps(slim, default=str, separators=(",", ":"))
    if len(text.encode("utf-8")) <= _ENV_JSON_MAX_BYTES:
        return text

    # Last resort: clamp every long string value at the top level.
    clamped = {
        key: (value[:1024] if isinstance(value, str) else value)
        for key, value in slim.items()
    }
    clamped["_truncated"] = True
    return json.dumps(clamped, default=str, separators=(",", ":"))


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
