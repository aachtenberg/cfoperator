#!/usr/bin/env python3
"""
CFOperator - Continuous Feedback Operator
==========================================

Single central agent with dual-mode OODA loop:
- Reactive: Responds to alerts with LLM-driven investigations
- Proactive: Periodic deep sweeps to catch issues before they alert

Version: 1.0.8
"""

import os
import re
import sys
import time
import json
import uuid
import yaml
import logging
import hashlib
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
import queue
import threading
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

# Prometheus metrics
from prometheus_client import Counter, Gauge, Histogram, Info

# Import core components
from knowledge_base import ResilientKnowledgeBase, learning_has_trigger_condition, is_ephemeral_job_pod, normalize_finding_signature, normalize_remediation_fields, normalize_service_name, remediation_is_auto_eligible
from llm_fallback import LLMFallbackManager as LLMFallback
from embedding_service import EmbeddingService, vector_literal

# Import pluggable observability backends
from observability import (
    PrometheusMetrics,
    LokiLogs,
    DockerContainers,
    KubernetesContainers,
    CompositeContainerBackend,
    AlertmanagerAlerts,
    AlertmanagerNotifications,
    SlackNotifications,
    DiscordNotifications
)

# Import web server
from web_server import WebServer

# Import tool registry
from tools import ToolRegistry

# Import Ollama pool (for parallel sweeps)
from ollama_pool import OllamaPool
from remediation import RemediationProposer
from change_record_client import (
    ChangeRecordClientError,
    get_approval as change_record_approval,
    open_record as change_record_open,
)
from node_action_plan import (
    build_command_prompt as _na_build_command_prompt,
    normalize_plan as _na_normalize_plan,
    parse_command_plan as _na_parse_command_plan,
    validate_plan as _na_validate_plan,
)

# Config semantics shared with event_runtime — one loader, one default schema.
from cfshared import config as shared_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "component": "%(name)s", "msg": "%(message)s"}'
)
logger = logging.getLogger("cfoperator")

# Prometheus metrics
OODA_CYCLES = Counter('cfoperator_ooda_cycles_total', 'Total OODA cycles executed')
SWEEPS = Counter('cfoperator_sweeps_total', 'Total sweeps executed', ['mode'])  # reactive/proactive
TOOL_CALLS = Counter('cfoperator_tool_calls_total', 'Tool executions', ['tool_name', 'result'])
TOOLS_REGISTERED = Gauge('cfoperator_tools_registered', 'Number of registered tools')
INVESTIGATIONS = Counter('cfoperator_investigations_total', 'Total investigations', ['outcome'])
LOG_MESSAGES = Counter('log_messages_total', 'Log messages', ['level', 'component'])
INVESTIGATION_QUEUE_DEPTH = Gauge('cfoperator_investigation_queue_depth', 'Pending HTTP-triggered investigations')
INVESTIGATION_QUEUE_REJECTED = Counter('cfoperator_investigation_queue_rejected_total', 'HTTP investigations rejected because queue was full')
INVESTIGATION_POSTBACK = Counter('cfoperator_investigation_postback_total', 'Investigation completions posted back to event_runtime', ['status'])
REMEDIATION_QUEUE = Gauge('cfoperator_remediation_queue', 'Remediation queue rows by status', ['status'])
REMEDIATION_ENQUEUED = Counter('cfoperator_remediation_enqueued_total', 'Remediations enqueued', ['source', 'remediation_class', 'eligible'])
REMEDIATION_SPAWNED = Counter('cfoperator_remediation_executor_spawned_total', 'Executor Jobs spawned by the drainer', ['result'])
REMEDIATION_OUTCOME = Counter('cfoperator_remediation_outcome_total', 'Terminal remediation outcomes', ['outcome'])
REMEDIATION_REAPED = Counter('cfoperator_remediation_reaped_total', 'Remediations recovered from dead executor leases')

# Model floor for the node-action executor (the only path that runs shell on a
# host): used when remediation.executor.node_action.model is unset, so node-action
# never inherits a cost downgrade applied to the generic executor model.
_ANTHROPIC_DEFAULT_EXEC_MODEL = "claude-opus-4-8"

# The morning summary is authored by the cheap, unverified primary model, so a
# mutation-class rec from it is a HYPOTHESIS, not a diagnosis. These are routed
# through the investigation pipeline (capable model + real tools) instead of
# becoming a remediation directly, and the model's self-reported confidence is
# clamped so a confident hallucination can't look authoritative in the queue.
_SUMMARY_MUTATION_CLASSES = ('node-action', 'gitops-patch', 'k8s-action')
_SUMMARY_CONFIDENCE_CAP = 0.5

# Sweep/summary recs that say "check/verify/…" are evidence-gathering the agent
# can do itself — never park them as needs-human. Exclude physically-human work
# even when the text also contains a check/verify verb.
_INVESTIGATE_SHAPED = re.compile(
    r'\b(check|verify|confirm|investigate|monitor|look\s+into|examine)\b', re.I)
_HUMAN_ONLY_SHAPED = re.compile(
    r'\b(physically|hardware|power\s+supply|power\s+strip|sd\s+card|'
    r'replace|swap\s+it|wiring|console|hard-?cycle)\b', re.I)

# Triggers that describe a *recoverable* runtime condition — if the pod is
# healthy now, the thing the alert worried about has cleared. Used by the
# Tier-1 noise filter (early-exit + needs_action downgrade). See
# docs/noise-reduction.md.
#
# Kept as two classes because they need different flapping guards. The restart
# class leaves a trace in restartCount, so `recovered_restart_threshold` can
# tell a settled pod from a flapping one. The probe class leaves none — a
# readiness probe restarts nothing, so restartCount is structurally 0 however
# badly the probe is flapping. _PROBE_TRIGGER routes that class to its own
# guard (how long the pod has held Ready); see _recovered_and_healthy.
#
# The probe class names the three kubelet probe types explicitly rather than
# matching bare "probe"/"unhealthy". Those wider words reach findings that are
# not about a kubelet probe at all — "unhealthy upstream", "volume unhealthy",
# or a blackbox probe against an external URL — where the named pod being Ready
# says nothing about whether the reported problem is real, so silencing on pod
# health would be wrong. All six triggers from the incident match `readiness`.
_RESTART_CLASS = r"restart|terminat|exit\s*code|not\s*ready|notready|oom|crashloop|back-?off"
_PROBE_CLASS = r"readiness|liveness|startup\s*probe"
_RECOVERABLE_TRIGGER = re.compile(_RESTART_CLASS + "|" + _PROBE_CLASS, re.I)
_PROBE_TRIGGER = re.compile(_PROBE_CLASS, re.I)

# Workload names as they appear in free-form sweep prose ("plane-api",
# "faster-whisper"). Dashed identifiers only: bare words like "plane" or "pod"
# match half the cluster. Used by _resolve_pod_from_cluster.
_WORKLOAD_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


class _MetricsLogHandler(logging.Handler):
    """Logging handler that increments LOG_MESSAGES Prometheus counter."""
    def emit(self, record):
        try:
            level = record.levelname
            component = record.name or 'cfoperator'
            LOG_MESSAGES.labels(level=level, component=component).inc()
        except Exception:
            pass


logging.getLogger().addHandler(_MetricsLogHandler())


def _llm_provider_tag(result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Format ``provider/model`` (or either alone) from an LLM result or meta dict.

    Accepts the chat/summary result shape (``backend`` + ``model``) and the
    sweep_meta shape (``provider`` + ``model``). Returns None when neither
    field is present so callers can omit the tag rather than invent "unknown".
    """
    if not isinstance(result, dict):
        return None
    backend = str(result.get('backend') or result.get('provider') or '').strip()
    model = str(result.get('model') or '').strip()
    if backend and model:
        return f"{backend}/{model}"
    return backend or model or None


def _append_llm_attribution(text: str, result: Dict[str, Any]) -> str:
    """Append a "_Generated by: backend/model_" footer to LLM-produced text.

    The fallback chain reports which provider actually served the call
    (the configured primary may have cold-started and been bypassed), so
    operators can correlate output quality with the served model. Both
    fields can be missing on the safe-default path — degrade gracefully
    instead of emitting a bare "Generated by: /" line.
    """
    attribution = _llm_provider_tag(result)
    if not attribution:
        return text
    return f"{text}\n\n_Generated by: {attribution}_"



AGENT_INFO = Info('cfoperator_agent', 'CFOperator agent information')
AGENT_UPTIME = Gauge('cfoperator_uptime_seconds', 'Agent uptime in seconds')
MONITORED_HOSTS = Gauge('cfoperator_monitored_hosts', 'Number of monitored hosts')
RUNNING_CONTAINERS = Gauge('cfoperator_running_containers', 'Number of running containers across fleet')
ERROR_RATE = Counter('cfoperator_errors_total', 'Total errors')

# LLM Observability metrics
LLM_REQUESTS = Counter('cfoperator_llm_requests_total', 'Total LLM requests', ['provider', 'model', 'result'])
LLM_TOKENS = Counter('cfoperator_llm_tokens_total', 'Total tokens used', ['provider', 'model', 'type'])  # type: prompt/completion
# Buckets span 1s..600s: LLM chat turns (incl. tool-calling iterations) routinely
# run tens of seconds and reasoning models reach several minutes. The Histogram
# default buckets top out at 10s, so every real request landed in +Inf and
# histogram_quantile() reported a flat 10.0 for every percentile.
LLM_LATENCY = Histogram(
    'cfoperator_llm_latency_seconds', 'LLM request latency', ['provider', 'model'],
    buckets=(1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 450, 600, float('inf')),
)
LLM_ERRORS = Counter('cfoperator_llm_errors_total', 'LLM errors by provider', ['provider', 'error_type'])
LLM_FALLBACKS = Counter('cfoperator_llm_fallbacks_total', 'LLM fallback chain activations', ['from_provider', 'to_provider'])
# Empty final responses from the tool loop (see _handle_empty_final). The
# `disposition` label keeps two very different signals apart:
#   nudged    - first empty of the turn. EMPTY_RESPONSE_NUDGE sent, one bonus
#               round granted; the benchmark recovered 19/19 this way. A
#               formatting quirk the loop absorbs.
#   exhausted - second empty. EmptyLLMResponseError raised and the provider
#               chain rotates. The model failing the task, at the cost of a
#               whole extra provider attempt.
# Collapsing them into one number cannot distinguish "gemma4 needs a second
# prompt sometimes" from "gemma4 cannot finish the job". Divide by
# cfoperator_llm_requests_total (incremented once per _chat_with_tools call,
# success and error alike) for the per-model rate.
LLM_EMPTY_FINALS = Counter('cfoperator_llm_empty_final_responses_total', 'Tool-loop turns that ended with an empty final message', ['provider', 'model', 'disposition'])
EMBEDDING_REQUESTS = Counter('cfoperator_embedding_requests_total', 'Embedding generation requests', ['result'])
EMBEDDING_CACHE_HITS = Counter('cfoperator_embedding_cache_hits_total', 'Embedding cache hits vs misses', ['result'])

# OpenAI-compatible cloud LLM providers. They share an identical request /
# response shape (chat/completions, OpenAI-style tool calling) and differ only
# in base URL and API-key env var, so one code path serves all of them.
OPENAI_COMPAT_PROVIDERS = {
    'groq': {
        'label': 'Groq',
        'base_url': 'https://api.groq.com/openai/v1',
        'key_env': 'GROQ_API_KEY',
    },
    'xai': {
        'label': 'xAI Grok',
        'base_url': 'https://api.x.ai/v1',
        'key_env': 'XAI_API_KEY',
    },
}

# Sent once when a model ends the tool loop with an empty message (no tool
# calls, no text). gemma4:26b does this on virtually every healthy-cluster
# investigation (benchmarks/empty_response_sim.py: 10/10 empty finals, and
# 19/19 recovered by this nudge); without it the empty response used to be
# stored verbatim and _extract_status('') silently defaulted to 'monitoring'.
EMPTY_RESPONSE_NUDGE = (
    "You have gathered enough data. Do NOT call any more tools. "
    "Respond NOW with a short summary of what you found, followed by your "
    "final answer in exactly the format the instructions above require."
)


class EmptyLLMResponseError(RuntimeError):
    """Model returned an empty final message even after the nudge retry.

    Must propagate out of _chat_with_tools_inner (never be swallowed into a
    synthetic response) so _chat_with_tools_with_fallback rotates to the
    next provider in the chain.
    """


@dataclass
class _ToolLoopStats:
    """Counters accumulated over one _chat_with_tools_inner tool loop.

    Shared by every provider branch so the loop's several exit points all
    report the same shape via ``result()``.
    """

    tool_calls: int = 0
    cached_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    learning_ids: List[str] = field(default_factory=list)

    def result(self, response: str) -> Dict[str, Any]:
        return {
            'response': response,
            'tool_calls': self.tool_calls,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'learning_ids': self.learning_ids,
            'cached_tool_hits': self.cached_hits,
        }


class CFOperator:
    """
    Continuous Feedback Operator

    Dual-mode OODA loop:
    1. Reactive: Handle firing alerts immediately
    2. Proactive: Deep system sweeps every 30 minutes
    """

    def __init__(self, config_path: str = "config.yaml"):
        logger.info("Initializing CFOperator...")

        # Load configuration
        self.config = self._load_config(config_path)

        # Initialize core components
        # Build database URL for ResilientKnowledgeBase
        db_url = f"postgresql://{self.config['database']['user']}:{self.config['database']['password']}@{self.config['database']['host']}:{self.config['database']['port']}/{self.config['database']['database']}"
        self.kb = ResilientKnowledgeBase(
            db_url=db_url,
            host_id='cfoperator'  # Single central agent
        )

        # Initialize database schema (creates tables if they don't exist)
        self.kb.initialize_schema()

        # Initialize LLM fallback chain
        self.llm = LLMFallback(
            db_session_factory=self.kb.session_scope,
            settings_getter=self._get_agent_settings
        )

        # LLM request timeout (generous default for cold model loads)
        self.llm_timeout = self.config.get('llm', {}).get('primary', {}).get('timeout', 180)

        # Initialize embeddings service for vector search
        embedding_config = self.config.get('llm', {}).get('embeddings', {})
        self.embeddings = EmbeddingService(
            ollama_url=embedding_config.get('url') or self.config.get('llm', {}).get('primary', {}).get('url') or os.getenv('OLLAMA_URL', 'http://localhost:11434'),
            model=embedding_config.get('model'),
            db_session_factory=self.kb.session_scope
        )

        # Initialize pluggable observability backends
        self._init_observability_backends()

        # Initialize tool registry
        self.tools = ToolRegistry(self)

        # Load skills from skills/ directory
        self.skills = self._load_skills()

        # OODA state
        self.current_investigation = None
        self.last_sweep = 0
        self.last_reap = 0    # remediation reaper tick
        self.last_drain = 0   # remediation drainer tick
        self.last_verify = 0   # remediation PR-reconcile tick
        self.last_metrics = 0  # remediation gauge refresh tick
        self.start_time = time.time()
        # Initialized to start_time so the first heartbeat fires after the
        # configured interval rather than immediately after the bootstrap
        # banner — avoids redundant chatter on the first cycle.
        self.last_heartbeat = self.start_time

        # HTTP-driven investigation queue (POST /v1/investigate).
        # Bounded; full queue rejects with 503 so event_runtime's worker retries.
        ooda_cfg = self.config.get('ooda', {})
        queue_size = max(1, int(ooda_cfg.get('investigation_queue_size', 32)))
        self._investigation_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=queue_size)
        self._investigation_worker_thread: Optional[threading.Thread] = None
        # Idempotent enqueue: retries from Slack bridges / MCP hosts carrying
        # the same idempotency_key (or alert_id) within the TTL are absorbed
        # instead of double-enqueued. In-memory by design — a restart clearing
        # the window only risks a duplicate investigation, never a lost one.
        self._enqueue_dedup_ttl = float(ooda_cfg.get('investigation_dedup_ttl_seconds', 3600))
        self._enqueue_dedup_keys: Dict[str, float] = {}
        self._enqueue_dedup_lock = threading.Lock()
        # Serializes investigations across the reactive poll (main thread)
        # and the HTTP worker thread. Without this, both paths could race
        # on self.current_investigation and other non-thread-safe state.
        self._investigation_lock = threading.Lock()
        # Reactive Alertmanager poll is preserved by default; PR C flips this to false.
        self._reactive_poll_enabled = bool(ooda_cfg.get('reactive_poll', True))

        # Initialize web server
        chat_config = self.config.get('chat', {})
        if chat_config.get('enabled', True):
            self.web_server = WebServer(
                operator=self,
                host='0.0.0.0',
                port=chat_config.get('port', 8083)
            )
        else:
            self.web_server = None

        # Initialize Ollama pool for parallel sweeps (if configured)
        pool_config = self.config.get('ollama_pool', {}).get('instances', [])
        if pool_config:
            self.ollama_pool = OllamaPool(pool_config, kb=self.kb)
            logger.info(f"Ollama pool initialized with {len(pool_config)} instances")
        else:
            self.ollama_pool = None

        # Update Prometheus metrics
        TOOLS_REGISTERED.set(len(self.tools.tools))
        MONITORED_HOSTS.set(len(self.config.get('infrastructure', {}).get('hosts', {})))
        AGENT_INFO.info({
            'version': '1.0.8',
            'host_id': 'cfoperator',
            'mode': 'dual_ooda'
        })

        logger.info("CFOperator initialized successfully")

    def reload_config(self) -> Dict[str, Any]:
        """Reload configuration from disk without restarting."""
        config_path = os.getenv('CONFIG_PATH', 'config.yaml')
        old_hosts = set(self.config.get('infrastructure', {}).get('hosts', {}).keys())
        self.config = self._load_config(config_path)
        new_hosts = set(self.config.get('infrastructure', {}).get('hosts', {}).keys())
        MONITORED_HOSTS.set(len(new_hosts))
        added = new_hosts - old_hosts
        removed = old_hosts - new_hosts
        logger.info(f"Config reloaded: {len(new_hosts)} hosts (added={added or 'none'}, removed={removed or 'none'})")
        return {'hosts': len(new_hosts), 'added': list(added), 'removed': list(removed)}

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration, merged over the shared default schema.

        Delegates to ``cfshared.config`` so the agent and the event runtime
        resolve the same file the same way. Before CFOP-26 a config file that
        existed at all bypassed ``_default_config()`` entirely, so every omitted
        setting fell through to whatever literal was written at its call site.
        """
        return shared_config.load_config(config_path)

    def _load_env_file(self, config_path: str) -> None:
        """Load a colocated .env file so config.yaml placeholders resolve consistently."""
        shared_config.load_env_file(config_path)

    def _expand_env_vars(self, config: Any) -> Any:
        """Recursively expand ${VAR} references in config."""
        return shared_config.expand_env_vars(config)

    def _default_config(self) -> Dict[str, Any]:
        """Return default configuration.

        Now the same schema every config is merged over, rather than a second,
        incomplete opinion that only applied when the file was missing (it had
        no ``llm`` section at all, so a fileless start had no model to call).
        """
        return shared_config.default_config()

    def _load_skills(self) -> Dict[str, Dict[str, Any]]:
        """
        Load skills from skills/ directory.

        Each skill is in its own subdirectory with a SKILL.md file containing:
        - YAML frontmatter (name, description)
        - Markdown instructions for the LLM

        Returns:
            Dict mapping skill name to {name, description, instructions}
        """
        skills = {}
        skills_dir = Path('skills')

        if not skills_dir.exists():
            logger.warning("Skills directory not found - skills disabled")
            return skills

        for skill_path in skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            skill_file = skill_path / 'SKILL.md'
            if not skill_file.exists():
                logger.warning(f"Skipping {skill_path.name} - no SKILL.md file")
                continue

            try:
                content = skill_file.read_text()

                # Parse YAML frontmatter
                if content.startswith('---'):
                    parts = content.split('---', 2)
                    if len(parts) >= 3:
                        frontmatter = yaml.safe_load(parts[1])
                        instructions = parts[2].strip()

                        skill_name = frontmatter.get('name')
                        if skill_name:
                            skills[skill_name] = {
                                'name': skill_name,
                                'description': frontmatter.get('description', ''),
                                'instructions': instructions
                            }
                            logger.info(f"Loaded skill: {skill_name}")
                        else:
                            logger.warning(f"Skipping {skill_path.name} - no 'name' in frontmatter")
                else:
                    logger.warning(f"Skipping {skill_path.name} - missing YAML frontmatter")
            except Exception as e:
                logger.error(f"Failed to load skill from {skill_path.name}: {e}")

        logger.info(f"Loaded {len(skills)} skills: {list(skills.keys())}")
        return skills

    def _init_observability_backends(self):
        """Initialize pluggable observability backends based on config."""
        obs_config = self.config.get('observability', {})

        # Metrics backend. An empty URL is the configured-off state, not an
        # error: since CFOP-26 every config is merged over a default schema, so
        # these sections always exist and it is the URL that says whether the
        # operator actually has one. Logs in particular are optional.
        metrics_config = obs_config.get('metrics', {})
        if metrics_config.get('backend') == 'prometheus' and metrics_config.get('url'):
            self.metrics = PrometheusMetrics(url=metrics_config.get('url'))
            logger.info(f"Initialized Prometheus metrics backend: {metrics_config.get('url')}")
        elif not metrics_config.get('url'):
            logger.info("Metrics backend disabled (no observability.metrics.url configured)")
            self.metrics = None
        else:
            logger.warning(f"Unsupported metrics backend: {metrics_config.get('backend')}")
            self.metrics = None

        # Logs backend
        logs_config = obs_config.get('logs', {})
        if logs_config.get('backend') == 'loki' and logs_config.get('url'):
            self.logs = LokiLogs(url=logs_config.get('url'))
            logger.info(f"Initialized Loki logs backend: {logs_config.get('url')}")
        elif not logs_config.get('url'):
            logger.info("Logs backend disabled (no observability.logs.url configured)")
            self.logs = None
        else:
            logger.warning(f"Unsupported logs backend: {logs_config.get('backend')}")
            self.logs = None

        # Container backend(s) — supports list (like notifications) or single dict
        container_configs = obs_config.get('containers', [])
        if isinstance(container_configs, dict):
            container_configs = [container_configs]  # backward compat
        self._container_configs = container_configs  # stash for drift check

        container_backends = []
        for container_config in container_configs:
            backend_type = container_config.get('backend')
            if backend_type == 'prometheus':
                from observability.prometheus_containers import PrometheusContainers
                prometheus_url = metrics_config.get('url')
                ssh_user = container_config.get('ssh_user', 'sre')
                backend = PrometheusContainers(prometheus_url=prometheus_url, ssh_user=ssh_user)
                container_backends.append(backend)
                logger.info(f"Initialized Prometheus container backend (SSH user: {ssh_user})")
            elif backend_type == 'docker':
                backend = DockerContainers(hosts=container_config.get('hosts', {}))
                container_backends.append(backend)
                logger.info(f"Initialized Docker backend with {len(container_config.get('hosts', {}))} hosts")
            elif backend_type == 'kubernetes':
                k8s_config = self.config.get('kubernetes', {})
                backend = KubernetesContainers(
                    kubeconfig=container_config.get('kubeconfig', k8s_config.get('kubeconfig')),
                    context=container_config.get('context', k8s_config.get('context'))
                )
                container_backends.append(backend)
                logger.info("Initialized Kubernetes container backend")
            else:
                if backend_type:
                    logger.warning(f"Unsupported container backend: {backend_type}")

        if container_backends:
            self.containers = CompositeContainerBackend(container_backends)
        else:
            self.containers = None

        # Alerts backend
        alerts_config = obs_config.get('alerts', {})
        if alerts_config.get('backend') == 'alertmanager' and alerts_config.get('url'):
            self.alerts = AlertmanagerAlerts(url=alerts_config.get('url'))
            logger.info(f"Initialized Alertmanager backend: {alerts_config.get('url')}")
        elif not alerts_config.get('url'):
            logger.info("Alerts backend disabled (no observability.alerts.url configured)")
            self.alerts = None
        else:
            logger.warning(f"Unsupported alerts backend: {alerts_config.get('backend')}")
            self.alerts = None

        # Notifications backend(s)
        self.notifications = []
        for notif_config in obs_config.get('notifications', []):
            webhook = notif_config.get('webhook_url', '')
            if notif_config.get('backend') == 'slack':
                if not webhook:
                    logger.info("Slack notifications skipped (no webhook URL)")
                    continue
                notif = SlackNotifications(webhook_url=webhook)
                self.notifications.append(notif)
                logger.info("Initialized Slack notifications")
            elif notif_config.get('backend') == 'discord':
                if not webhook:
                    logger.info("Discord notifications skipped (no webhook URL)")
                    continue
                notif = DiscordNotifications(webhook_url=webhook)
                self.notifications.append(notif)
                logger.info("Initialized Discord notifications")
            elif notif_config.get('backend') == 'alertmanager':
                notif = AlertmanagerNotifications(url=notif_config.get('url', alerts_config.get('url', '')))
                self.notifications.append(notif)
                logger.info("Initialized Alertmanager notifications")

    def run(self):
        """
        Main OODA loop - dual mode operation.

        Runs continuously with:
        - Reactive: Check for alerts every 10 seconds
        - Proactive: Deep sweep every 30 minutes
        """
        logger.info("="*60)
        logger.info("Starting CFOperator OODA loop")
        alert_interval = self._get_alert_check_interval()
        sweep_interval = self._get_sweep_interval()
        logger.info(f"Reactive poll: {'enabled' if self._reactive_poll_enabled else 'disabled'} (check alerts every {alert_interval}s)")
        logger.info(f"Proactive: deep sweep every {sweep_interval}s ({sweep_interval//60} minutes)")
        logger.info("="*60)

        # Start the HTTP investigation worker before the web server so the
        # POST /v1/investigate endpoint has something to drain into.
        self._start_investigation_worker()

        # Remediation reaper/drainer/verify in their own thread (see loop note).
        self._start_remediation_worker()

        # Start web server in background thread
        if self.web_server:
            self.web_server.run_threaded()
            logger.info(f"Web UI available at http://0.0.0.0:{self.config.get('chat', {}).get('port', 8083)}")

        while True:
            try:
                # Update uptime metric
                AGENT_UPTIME.set(time.time() - self.start_time)
                OODA_CYCLES.inc()

                # Heartbeat — proves the loop is alive between events.
                if time.time() - self.last_heartbeat >= self._get_heartbeat_interval():
                    logger.info(self._format_heartbeat())
                    self.last_heartbeat = time.time()

                # MODE 1: Reactive - handle alerts immediately
                if self._reactive_poll_enabled and self.alerts:
                    alerts = self._check_alerts()
                    if alerts:
                        logger.info(f"Alerts detected: {len(alerts)}")
                        SWEEPS.labels(mode='reactive').inc()
                        for alert in alerts:
                            self._handle_alert_reactive(alert)

                # MODE 2: Proactive - periodic deep sweep
                if time.time() - self.last_sweep > self._get_sweep_interval():
                    logger.info("="*60)
                    logger.info("PROACTIVE MODE: Starting deep system sweep")
                    logger.info("="*60)
                    SWEEPS.labels(mode='proactive').inc()
                    self._deep_system_sweep()
                    self.last_sweep = time.time()

                # MODE 3: Morning summary (TPS report style)
                self._check_morning_summary()

                # Remediation reaper/drainer/verify run in their own daemon thread
                # (_remediation_worker_loop) so a long proactive sweep can't starve
                # them — the OODA loop is single-threaded and a sweep blocks for
                # minutes. Metrics gauge refresh is cheap, so it stays inline.
                self._update_remediation_metrics()

                time.sleep(self._get_alert_check_interval())

            except KeyboardInterrupt:
                logger.info("Shutting down CFOperator...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                ERROR_RATE.inc()
                time.sleep(30)  # Back off on errors

    def _check_alerts(self) -> List[Dict[str, Any]]:
        """Check for firing alerts from Alertmanager."""
        try:
            return self.alerts.get_firing_alerts()
        except Exception as e:
            # Only log alert errors once per minute to avoid spam
            if not hasattr(self, '_last_alert_error') or time.time() - self._last_alert_error > 60:
                logger.warning(f"Alertmanager unavailable: {type(e).__name__} - reactive mode disabled")
                self._last_alert_error = time.time()
            return []

    def _handle_alert_reactive(self, alert: Dict[str, Any]):
        """
        Reactive mode: Handle a firing alert by running an investigation.

        The orient/decide/act split lives inside run_investigation. This path
        ignores the returned ActionResult — Slack notification is owned by the
        agent's own notifier today. When event_runtime drives investigations
        over HTTP, the result is posted back instead.
        """
        logger.info(f"REACTIVE MODE: Handling alert: {alert.get('labels', {}).get('alertname', 'unknown')}")
        try:
            self.run_investigation(alert)
        except Exception:
            logger.exception("Reactive investigation failed")

    def _observe_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """OBSERVE phase: Gather context about the alert.

        Accepts both event_runtime Alert dicts (top-level ``summary``) and
        raw Alertmanager payloads (``annotations.summary``). Without this
        fallback, HTTP-driven investigations ran with trigger='Unknown alert'.
        """
        trigger = (
            alert.get('summary')
            or alert.get('annotations', {}).get('summary')
            or 'Unknown alert'
        )
        return {
            'alert': alert,
            'timestamp': datetime.now(),
            'trigger': trigger,
        }

    def _orient(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ORIENT phase: Understand what's happening.

        - Search knowledge base for similar issues
        - Search learnings for known solutions
        - Get current baseline state
        """
        trigger = context.get('trigger', '')

        # Generate embedding once for both learning and investigation search
        query_embedding = None
        try:
            if self.embeddings.is_available():
                query_embedding = self.embeddings.generate_embedding(trigger)
        except Exception as e:
            logger.warning(f"Query embedding failed, falling back to FTS search: {e}")

        # Search for relevant learnings (hybrid if embedding available, FTS otherwise)
        try:
            if query_embedding:
                learnings = self.kb._kb.find_learnings_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3
                )
            else:
                learnings = self.kb.find_learnings(query=trigger, limit=3)
            if learnings:
                logger.info(f"Found {len(learnings)} relevant learnings for: {trigger[:60]}")
            context['known_learnings'] = learnings
        except Exception as e:
            logger.warning(f"Learning search failed: {e}")
            context['known_learnings'] = []

        # Search for similar past investigations using embeddings (semantic) + FTS
        try:
            if query_embedding:
                similar = self.kb._kb.find_similar_investigations_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3
                )
                if similar:
                    logger.info(f"Found {len(similar)} similar investigations via hybrid search")
                context['similar_investigations'] = similar
            else:
                context['similar_investigations'] = []
        except Exception as e:
            logger.warning(f"Similar investigation search failed: {e}")
            context['similar_investigations'] = []

        return context

    def run_investigation(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Run one investigation end-to-end for a single alert dict.

        Wraps observe + orient + act so it can be invoked from either the
        reactive Alertmanager poll loop or the HTTP /v1/investigate path.
        Held under ``_investigation_lock`` so the two paths can't race on
        shared state (``current_investigation``, KB session, embeddings).
        Returns an ActionResult-shaped dict (see event_runtime.models.ActionResult).
        """
        with self._investigation_lock:
            context = self._observe_alert(alert)
            context = self._orient(context)
            return self._act(context)

    def run_triage(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Classify an alert without running a full investigation.

        Called from event_runtime's HTTPTriageDecisionEngine before it
        decides whether to dispatch the alert to /v1/investigate. The LLM
        sees the alert plus the top 3 similar past investigations from the
        embeddings index, and returns one of four actions:

          - log_only:    known noise; record and move on
          - notify:      operator should see the alert but no LLM dive needed
          - investigate: novel pattern, do a full LLM investigation
          - escalate:    page-worthy, severity is high and pattern is bad

        Returns ``{"action": ..., "reason": ..., "confidence": ...}``. On
        any failure (LLM unreachable, parse error, etc.) returns
        ``action="investigate"`` so we err on the side of investigating
        rather than dropping an alert. Tight prompt + max_iterations=1 +
        cheap-model preference keeps this fast.
        """
        # Build a one-shot classification prompt. No tools — the LLM should
        # not actually investigate; it should decide whether to.
        trigger = (
            alert.get('summary')
            or alert.get('annotations', {}).get('summary')
            or 'Unknown alert'
        )
        severity = alert.get('severity', 'unknown')
        labels = alert.get('labels') or alert.get('details', {}).get('labels', {}) or {}

        # Resolution alerts ("finding X has cleared since last sweep") are
        # synthesized by the sweep, not externally observed — there is no
        # classification to make. Short-circuit to notify so Slack gets the
        # ":white_check_mark: Resolved: …" line without spending an LLM call.
        details = alert.get('details') or {}
        if isinstance(details, dict) and details.get('resolution'):
            return {
                'action': 'notify',
                'reason': 'finding cleared since previous sweep',
                'confidence': 1.0,
                'backend': None,
                'model': None,
            }

        similar_context = ""
        try:
            if self.embeddings.is_available():
                query_embedding = self.embeddings.generate_embedding(trigger)
                similar = self.kb._kb.find_similar_investigations_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3,
                )
                if similar:
                    lines = []
                    for inv in similar:
                        sim = inv.get('similarity') or inv.get('vector_similarity', 0)
                        lines.append(
                            f"- [{inv.get('outcome','?'):10}] "
                            f"{inv.get('trigger','')[:100]} (similarity: {sim:.2f})"
                        )
                    similar_context = "\n\nSimilar past investigations:\n" + "\n".join(lines)
        except Exception:
            pass  # Best-effort; missing context is not a triage blocker.

        system_prompt = """You are a triage classifier for infrastructure alerts.
Decide the cheapest correct response. Respond ONLY with a JSON object,
no other text:
{
  "action":     "log_only" | "notify" | "investigate" | "escalate",
  "reason":     "<one short sentence>",
  "confidence": <0.0 to 1.0>
}

Action rubric:
  log_only    Known noise. Test pods (smoke-test-*, tmp-*), Alertmanager
              Watchdog, intentionally-failing canaries.
  notify      Operator should see this, but a full LLM investigation is
              waste. Use when a similar past investigation resolved with
              little effort, when severity=info, or when the pattern is
              one the operator already understands (e.g. raspberrypi
              SD-card warning that's been known for weeks). Requires a
              clear precedent: do NOT use notify for pod failures
              (CrashLoop, OOMKilled, ImagePullBackOff, NotReady) unless
              a similar past investigation is listed in the alert
              context.
  investigate Novel pattern, no similar resolved precedent, or pattern
              that previous investigations classified as 'monitoring'.
              A pod failure with no similar past investigation listed is
              novel by definition — investigate it. Default if
              uncertain.
  escalate    Severity=critical AND impact is broad (NodeNotReady on a
              control plane, data-loss patterns, multiple correlated
              services down). Operator should page in.

Prefer notify and log_only when there is a clear precedent. Prefer
investigate when uncertain. Use escalate only for genuinely urgent."""

        user_msg = (
            f"Alert severity: {severity}\n"
            f"Alert summary: {trigger}\n"
            f"Labels: {json.dumps(labels, default=str)[:500]}"
            f"{similar_context}\n\n"
            "Classify."
        )

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_msg}],
                system_context=system_prompt,
                max_iterations=1,  # one-shot classification — no tool loop
            )
        except Exception as e:
            logger.warning(f"Triage LLM unavailable, defaulting to investigate: {e}")
            return {
                'action': 'investigate',
                'reason': f'triage LLM unavailable ({type(e).__name__})',
                'confidence': 0.0,
                'backend': None,
                'model': None,
            }

        # The fallback chain reports which provider actually served the call,
        # not just the configured primary — surface it so Slack can show
        # "triaged by groq/openai/gpt-oss-120b" when Ollama cold-started and
        # we fell over. Without this, operators can't tell which LLM
        # classified an alert (matters for cost attribution + debugging
        # disagreements between models).
        served_backend = result.get('backend')
        served_model = result.get('model')

        response_text = result.get('response', '').strip()
        # The LLM sometimes wraps JSON in fenced code blocks or prose; pull
        # the first JSON object out instead of relying on perfect output.
        decision = self._parse_triage_response(response_text)
        if decision is None:
            logger.warning(f"Triage LLM returned unparseable response, defaulting to investigate: {response_text[:200]}")
            return {
                'action': 'investigate',
                'reason': 'triage response unparseable',
                'confidence': 0.0,
                'backend': served_backend,
                'model': served_model,
            }
        decision['backend'] = served_backend
        decision['model'] = served_model
        return decision

    @staticmethod
    def _parse_triage_response(response_text: str) -> Optional[Dict[str, Any]]:
        """Extract a valid triage decision dict from raw LLM output.

        Returns None if no valid JSON with the required fields is found.
        Tolerates markdown code fences and trailing prose.
        """
        if not response_text:
            return None
        # Strip optional markdown code fence (```json ... ``` or ``` ... ```).
        text = response_text.strip()
        if text.startswith("```"):
            # Drop the first line (fence + optional language) and trailing fence.
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        # Find the first {...} JSON object in the text.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        action = payload.get("action")
        if action not in {"log_only", "notify", "investigate", "escalate"}:
            return None
        return {
            "action": action,
            "reason": str(payload.get("reason", ""))[:280],
            "confidence": float(payload.get("confidence", 0.5)),
        }

    def enqueue_investigation(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Non-blocking enqueue for an HTTP-triggered investigation.

        Raises queue.Full when the queue has no slot; caller should map that
        to HTTP 503 so the event_runtime worker retries with backoff. The
        rejection counter is incremented here so callers don't reach into
        module-level metrics.

        Alerts carrying idempotency_key (preferred) or alert_id are deduped
        within a TTL window: a repeat within the window returns
        status='deduped' without enqueuing.
        """
        dedup_key = alert.get('idempotency_key') or alert.get('alert_id')
        if dedup_key:
            now = time.time()
            with self._enqueue_dedup_lock:
                self._enqueue_dedup_keys = {
                    k: t for k, t in self._enqueue_dedup_keys.items()
                    if now - t < self._enqueue_dedup_ttl
                }
                if dedup_key in self._enqueue_dedup_keys:
                    return {
                        'status': 'deduped',
                        'queue_depth': self._investigation_queue.qsize(),
                        'alert_id': alert.get('alert_id'),
                    }
                self._enqueue_dedup_keys[str(dedup_key)] = now
        try:
            self._investigation_queue.put_nowait(alert)
        except queue.Full:
            INVESTIGATION_QUEUE_REJECTED.inc()
            # The rejected alert never entered the queue — drop its dedup
            # claim so the caller's retry isn't absorbed as 'deduped'.
            if dedup_key:
                with self._enqueue_dedup_lock:
                    self._enqueue_dedup_keys.pop(str(dedup_key), None)
            raise
        INVESTIGATION_QUEUE_DEPTH.set(self._investigation_queue.qsize())
        return {
            'status': 'queued',
            'queue_depth': self._investigation_queue.qsize(),
            'alert_id': alert.get('alert_id'),
        }

    def _start_investigation_worker(self) -> None:
        """Spawn the single background thread that drains the investigation queue."""
        if self._investigation_worker_thread and self._investigation_worker_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._investigation_worker_loop,
            daemon=True,
            name='cfoperator-investigation-worker',
        )
        thread.start()
        self._investigation_worker_thread = thread
        logger.info("Investigation worker thread started")

    def _investigation_worker_loop(self) -> None:
        """Drain the investigation queue. One request at a time — LLM throughput is the bottleneck."""
        while True:
            try:
                alert = self._investigation_queue.get()
            except Exception:
                logger.exception("Investigation queue read failed; worker exiting")
                return
            try:
                INVESTIGATION_QUEUE_DEPTH.set(self._investigation_queue.qsize())
                result = self.run_investigation(alert)
                self._post_action_result_to_event_runtime(alert, result)
            except Exception:
                logger.exception("HTTP-triggered investigation failed")
            finally:
                self._investigation_queue.task_done()

    def _post_action_result_to_event_runtime(self, alert: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Best-effort post-back of completed ActionResult to event_runtime.

        Sends ``{"alert": <alert>, "result": <ActionResult>}`` so the
        completion endpoint can fire its Slack notification with the
        original alert's severity and summary. No-op when
        CFOP_EVENT_RUNTIME_URL is unset or the completion endpoint is
        unavailable (it ships in a follow-up PR). Failures are logged at
        debug — durability lives in the agent's investigation row, not here.
        """
        url = os.getenv('CFOP_EVENT_RUNTIME_URL', '').strip()
        if not url:
            return
        alert_id = alert.get('alert_id')
        if not alert_id:
            return
        endpoint = f"{url.rstrip('/')}/v1/investigations/{alert_id}/complete"
        body = json.dumps({'alert': alert, 'result': result}, default=str).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        # Shared secret matches event_runtime's CFOP_COMPLETION_SHARED_SECRET.
        # Without the header, event_runtime returns 401 (when its secret is set)
        # so completion notifications can't be spoofed by other cluster pods.
        secret = os.getenv('CFOP_COMPLETION_SHARED_SECRET', '').strip()
        if secret:
            headers['X-CFOP-Token'] = secret
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError
        req = Request(endpoint, data=body, headers=headers, method='POST')
        try:
            with urlopen(req, timeout=5) as resp:
                status = 'ok' if 200 <= resp.status < 300 else f'http_{resp.status}'
        except HTTPError as exc:
            status = f'http_{exc.code}'
            logger.debug(f"Post-back to event_runtime returned {exc.code}: {endpoint}")
        except (URLError, TimeoutError, OSError) as exc:
            status = 'transport_error'
            logger.debug(f"Post-back to event_runtime failed ({type(exc).__name__}): {endpoint}")
        INVESTIGATION_POSTBACK.labels(status=status).inc()

    def _act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ACT phase: Investigate and fix.

        - Create investigation record
        - Run LLM investigation loop with tools
        - Extract learnings from resolved investigations

        Returns an ActionResult-shaped dict so callers (HTTP path, future
        event_runtime post-back) can surface the real outcome rather than
        a stub success message.
        """
        trigger = context.get('trigger', 'Unknown trigger')
        logger.info(f"Starting investigation: {trigger[:100]}")

        # Create investigation record
        inv_id = self.kb.start_investigation(trigger=trigger)
        self.current_investigation = inv_id
        start_time = time.time()
        outcome = 'failed'
        message = f"Investigation failed: {trigger[:200]}"
        details: Dict[str, Any] = {'investigation_id': inv_id, 'outcome': outcome}

        try:
            # Build investigation prompt with learnings and similar investigations context
            learnings_text = ""
            if context.get('known_learnings'):
                learnings_text = "\n\nRelevant past learnings:\n"
                for l in context['known_learnings']:
                    learnings_text += f"- [{l['learning_type']}] {l['title']}: {l['description'][:200]}\n"

            similar_text = ""
            if context.get('similar_investigations'):
                similar_text = "\n\nSimilar past investigations:\n"
                for inv in context['similar_investigations'][:3]:
                    sim_score = inv.get('similarity') or inv.get('vector_similarity', 0)
                    similar_text += f"- [{inv.get('outcome', '?')}] {inv.get('trigger', '')[:100]} (similarity: {sim_score})\n"

            alert_info = context.get('alert', {})

            # Tier-1 noise filter (1b): if the alert is about a recoverable
            # runtime condition and the pod is healthy now with only a few
            # restarts, don't spend a full investigation on it — record a
            # 'monitoring' result and return. Flapping (high restart count) and
            # still-broken pods fall through to a real investigation.
            # _recovered_and_healthy applies the probe class's own flapping
            # guard internally, since restart_thresh cannot see that class.
            noise_cfg = self._noise_config()
            noise_on = noise_cfg.get('enabled', True)
            restart_thresh = int(noise_cfg.get('recovered_restart_threshold', 3))
            if noise_on:
                pre_recovered, pre_note, pre_restarts = self._recovered_and_healthy(alert_info, trigger)
                if pre_recovered and pre_restarts <= restart_thresh:
                    return self._early_exit_monitoring(inv_id, trigger, start_time, pre_note)

            system_prompt = f"""You are CFOperator investigating an infrastructure alert.

Alert: {trigger}
Alert details: {json.dumps(alert_info, default=str)[:1000]}
{learnings_text}{similar_text}

Investigate this alert using the available tools. Check metrics, logs, and container/service status.
First give a short summary of what you found. Then end your response with exactly these two lines:

STATUS: <one of: resolved | needs_action | monitoring | escalate>
  - resolved: the resource is healthy RIGHT NOW — the problem is gone, or you fixed it during this investigation. Do NOT use resolved just because you identified a fix that someone still has to apply.
  - needs_action: you found the problem but it needs a change you could not make yourself; your RECOMMENDATION says what to do.
  - monitoring: transient or inconclusive; worth watching, no action yet.
  - escalate: urgent; a human should look now.
RECOMMENDATION: <the single most useful operator-facing next step — a concrete command or config change, or "No action needed" when the resource is genuinely healthy>"""

            # Run LLM investigation with tools, with provider fallback so a
            # transient Ollama timeout (e.g. GPU cold-start) doesn't abort
            # the investigation.
            try:
                result = self._chat_with_tools_with_fallback(
                    messages=[{'role': 'user', 'content': f'Investigate this alert: {trigger}'}],
                    system_context=system_prompt,
                )
            except RuntimeError as e:
                if "No LLM providers available" not in str(e):
                    raise
                logger.error("No LLM provider available for investigation")
                duration = time.time() - start_time
                self.kb.update_investigation(
                    investigation_id=inv_id,
                    completed_at=datetime.now(),
                    findings={'error': 'No LLM provider available'},
                    outcome='failed',
                    duration_seconds=duration
                )
                INVESTIGATIONS.labels(outcome='failed').inc()
                details.update({'duration_s': round(duration, 1), 'error': 'no_llm_provider'})
                return self._build_action_result(
                    success=False,
                    message=f"Investigation failed (no LLM provider): {trigger[:200]}",
                    details=details,
                )

            provider_type = result.get('backend', 'unknown')
            model = result.get('model', 'unknown')

            response_text = result.get('response', '')
            tool_calls_count = result.get('tool_calls', 0)
            duration = time.time() - start_time

            # Classify outcome from the model's explicit STATUS verdict rather
            # than keyword-sniffing the whole response. The old heuristic matched
            # 'resolved'/'healthy'/'normal' anywhere in the text, so any thorough
            # investigation ("CPU is normal", "can be resolved by...") was
            # mislabeled resolved — even for a pod still stuck Pending.
            outcome = self._extract_status(response_text)

            # B1: don't take "resolved" on faith — confirm against live cluster
            # state. If the alert pins to a pod that is still Pending/CrashLoop,
            # downgrade resolved -> needs_action so we never announce a fix that
            # didn't happen.
            outcome, verify_note = self._verify_investigation_outcome(outcome, alert_info, trigger)

            # Tier-1 noise filter (1a): if the investigation lands on needs_action
            # but the alerted runtime condition has recovered (pod healthy now,
            # few restarts) — including pods that recovered *during* the
            # investigation — downgrade to monitoring so it doesn't page red.
            if noise_on and outcome == 'needs_action':
                post_recovered, post_note, post_restarts = self._recovered_and_healthy(alert_info, trigger)
                if post_recovered and post_restarts <= restart_thresh:
                    outcome = 'monitoring'
                    verify_note = ((verify_note + '; ') if verify_note else '') + f"recovered — {post_note}"
                    logger.info(f"Noise filter: needs_action -> monitoring ({post_note})")

            # The investigation prompt asks the LLM to end with a
            # "RECOMMENDATION:" line; surface it as the operator-facing next
            # step so a direct /investigate carries actionable guidance (not
            # just a bare "Resolved"), matching what the sweep path already does.
            recommendation = self._extract_recommendation(response_text)

            # Phase-B remediation: for a confirmed needs_action, see if this is a
            # case we can propose a concrete fix for. Default off; dry-run
            # unless `remediation.open_prs` is set, in which case this can open
            # a real (human-merge-gated) PR. Never touches the running cluster
            # either way. Conservative by design — it mostly turns a vague
            # "needs_action" into either a candidate patch or a precise decline
            # reason (see remediation.py + the design doc).
            proposal = self._maybe_propose_remediation(outcome, alert_info, trigger)

            findings = {
                'response': response_text[:5000],
                'tool_calls': tool_calls_count,
                'provider': f"{provider_type}/{model}",
                'recommendation': recommendation,
            }
            if verify_note:
                findings['outcome_verification'] = verify_note
            if proposal is not None:
                findings['remediation_proposal'] = proposal.to_details()

            # Update investigation record
            self.kb.update_investigation(
                investigation_id=inv_id,
                completed_at=datetime.now(),
                findings=findings,
                outcome=outcome,
                duration_seconds=duration,
                tool_calls_count=tool_calls_count
            )
            INVESTIGATIONS.labels(outcome=outcome).inc()
            logger.info(f"Investigation #{inv_id} completed: {outcome} ({duration:.1f}s, {tool_calls_count} tool calls)")

            # Extract learnings from resolved investigations
            if outcome == 'resolved':
                self._extract_learnings(inv_id, trigger, findings)

            # Generate embedding for this investigation (async, non-blocking)
            self._embed_investigation(inv_id, trigger, findings, outcome)

            message = self._action_message(outcome, trigger, duration, tool_calls_count)
            details.update({
                'outcome': outcome,
                'duration_s': round(duration, 1),
                'tool_calls': tool_calls_count,
                'provider': f"{provider_type}/{model}",
                'findings_snippet': response_text[:500],
            })
            # event_runtime renders details['remediation'] as the
            # "Recommendation:" line on the completion notification.
            if recommendation:
                details['remediation'] = recommendation
            if proposal is not None:
                details['remediation_proposal'] = proposal.to_details()
            return self._build_action_result(
                success=outcome != 'failed',
                message=message,
                details=details,
            )

        except Exception as e:
            logger.error(f"Investigation #{inv_id} failed: {e}", exc_info=True)
            duration = time.time() - start_time
            try:
                self.kb.update_investigation(
                    investigation_id=inv_id,
                    completed_at=datetime.now(),
                    findings={'error': str(e)},
                    outcome='failed',
                    duration_seconds=duration
                )
            except Exception as persist_err:
                logger.warning(f"Could not persist failure record for investigation #{inv_id}: {persist_err}")
            INVESTIGATIONS.labels(outcome='failed').inc()
            details.update({
                'outcome': 'failed',
                'duration_s': round(duration, 1),
                'error': str(e)[:500],
            })
            return self._build_action_result(
                success=False,
                message=f"Investigation failed: {type(e).__name__}: {str(e)[:200]}",
                details=details,
            )
        finally:
            self.current_investigation = None

    @staticmethod
    def _extract_recommendation(response_text: str) -> str:
        """Pull the operator-facing next step out of an investigation response.

        The investigation prompt asks the LLM to end with a line prefixed
        ``RECOMMENDATION:``. We surface that as the notification's
        "Recommendation:" line. Uses the *last* occurrence so a passing
        mention earlier in the reasoning doesn't win over the final verdict.
        Returns "" when absent so callers can omit the field entirely.
        """
        if not response_text:
            return ""
        marker = 'recommendation:'
        idx = response_text.lower().rfind(marker)
        if idx == -1:
            return ""
        tail = response_text[idx + len(marker):].strip()
        # Stop at the first blank line so we capture just the recommendation
        # paragraph, then cap length for a one-line notification.
        return tail.split('\n\n')[0].strip()[:400]

    @staticmethod
    def _extract_status(response_text: str) -> str:
        """Classify the investigation outcome from the model's explicit verdict.

        The prompt requires a final ``STATUS:`` line with one of
        resolved | needs_action | monitoring | escalate. We parse that line
        instead of keyword-sniffing the whole response — the old heuristic
        marked anything mentioning "resolved"/"healthy"/"normal" as resolved,
        which falsely cleared issues that were still broken (e.g. a pod stuck
        Pending whose fix the model only *recommended*).

        Non-resolved tokens are checked first so a line like
        "needs_action — can be resolved by ..." classifies as needs_action,
        not resolved. Falls back to a conservative heuristic (never resolved
        on loose keywords) when the model omits the line.
        """
        text = response_text or ""
        idx = text.lower().rfind('status:')
        if idx != -1:
            line = text[idx + len('status:'):].split('\n', 1)[0].lower()
            if any(k in line for k in ('needs_action', 'needs-action', 'needs action', 'unresolved', 'action needed')):
                return 'needs_action'
            if any(k in line for k in ('escalate', 'escalated', 'urgent')):
                return 'escalated'
            if 'monitor' in line:
                return 'monitoring'
            if any(k in line for k in ('resolved', 'fixed', 'healthy', 'no action', 'no issue')):
                return 'resolved'
        # No usable STATUS line — be conservative. Escalation signals win;
        # otherwise default to monitoring. Never infer 'resolved' here.
        low = text.lower()
        if any(w in low for w in ('escalat', 'urgent')):
            return 'escalated'
        return 'monitoring'

    @staticmethod
    def _identify_pod(alert_info: Dict[str, Any], trigger: str) -> Optional[tuple]:
        """Best-effort (namespace, pod_name) from an alert, or None.

        Tries structured alert fields first, then known trigger shapes:
          - "Pod <ns>/<pod> not ready ..."   (alertmanager)
          - "<pod> on <ns>: status=..."       (sweep finding)
        Returns None when it can't confidently pin a single pod.
        """
        ai = alert_info or {}
        ns = ai.get('namespace') or ai.get('ns')
        name = ai.get('resource_name') or ai.get('pod') or ai.get('pod_name')
        rtype = str(ai.get('resource_type') or '').lower()
        if ns and name and rtype in ('', 'pod', 'pods'):
            return (str(ns), str(name))
        text = trigger or ai.get('summary') or ''
        m = re.search(r'\bPod\s+([a-z0-9-]+)/([a-z0-9][a-z0-9.-]*)', text, re.I)
        if m:
            return (m.group(1), m.group(2))
        m = re.search(r'\b([a-z0-9][a-z0-9.-]*-[a-z0-9]+)\s+on\s+([a-z0-9-]+)\s*:', text, re.I)
        if m:
            return (m.group(2), m.group(1))
        return None

    @staticmethod
    def _pod_is_healthy(status: Dict[str, Any]) -> bool:
        """True only if a pod is actually up right now (Running+Ready, or Succeeded)."""
        phase = status.get('phase')
        if phase == 'Succeeded':
            return True
        if phase != 'Running':
            return False
        for c in status.get('conditions', []):
            if c.get('type') == 'Ready':
                return c.get('status') == 'True'
        return False

    def _noise_config(self) -> Dict[str, Any]:
        """The ``ooda.noise`` settings block, defensively — the noise filter is
        also exercised on instances built without a config."""
        cfg = getattr(self, 'config', None)
        if not isinstance(cfg, dict):
            return {}
        return (cfg.get('ooda', {}) or {}).get('noise', {}) or {}

    def _resolve_pod_from_cluster(self, trigger: str) -> Optional[tuple]:
        """Live-state fallback for _identify_pod: pin (namespace, pod) by
        matching a workload name out of free-form prose against running pods.

        Sweep findings are LLM prose carrying no structured resource fields —
        the finding schema is severity/finding/evidence/remediation — so
        neither _identify_pod's structured branch nor either of its two
        trigger shapes can fire for them. Resolving against the cluster rather
        than adding a third regex keeps the match honest: a name that isn't
        running cannot be matched.

        Gives up whenever the answer is not unique — more than one workload
        named, or more than one pod behind the one workload. A missed filter
        costs one redundant investigation; a wrong pin silences the wrong
        alert.
        """
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        tokens = {t for t in _WORKLOAD_TOKEN.findall((trigger or "").lower()) if len(t) >= 4}
        if not tokens:
            return None
        try:
            res = k8s.get_pods(all_namespaces=True)
        except Exception:
            return None
        # An exact name beats a prefix, so "cert-manager" resolves to
        # cert-manager rather than dying ambiguous against -webhook and
        # -cainjector. Each pod is tested against *every* token before being
        # classified: breaking on the first match would let a shorter prefix
        # token beat an exact one purely on set iteration order.
        exact: Dict[tuple, list] = {}
        prefix: Dict[tuple, list] = {}
        for pod in res.get('pods', []):
            meta = pod.get('metadata') or {}
            name, namespace = meta.get('name'), meta.get('namespace')
            if not name or not namespace:
                continue
            workload = normalize_service_name(name)
            if any(token in (name, workload) for token in tokens):
                exact.setdefault((namespace, workload), []).append(name)
            elif any(workload.startswith(token + '-') for token in tokens):
                prefix.setdefault((namespace, workload), []).append(name)
        hits = exact or prefix
        if len(hits) != 1:
            return None
        (namespace, _workload), pods = next(iter(hits.items()))
        if len(pods) != 1:
            return None  # replicas — can't confidently pin a single pod
        return (namespace, pods[0])

    @staticmethod
    def _ready_stable_seconds(status: Dict[str, Any]) -> Optional[float]:
        """Seconds the pod has continuously held Ready=True, or None when the
        transition time is missing or unparseable.

        None means "can't tell", and callers treat that as not-stable — for a
        noise filter an unknown answer must not license silencing an alert.
        """
        for cond in status.get('conditions', []):
            if cond.get('type') != 'Ready' or cond.get('status') != 'True':
                continue
            raw = cond.get('lastTransitionTime')
            if not raw:
                return None
            try:
                when = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            except (TypeError, ValueError):
                return None
            now = datetime.now(timezone.utc) if when.tzinfo else datetime.now()
            return max(0.0, (now - when).total_seconds())
        return None

    def _recovered_and_healthy(self, alert_info: Dict[str, Any], trigger: str) -> tuple:
        """Tier-1 noise filter: is the alert about a recoverable runtime
        condition whose pod is healthy *right now*? Returns
        (recovered: bool, note: str|None, restart_count: int).

        Only fires for restart/termination/exit-code/not-ready/crashloop/oom
        and probe-failure triggers tied to an identifiable pod that is
        currently Running+Ready. A healthy pod with a *non-runtime* concern
        (mis-config, deprecation) won't match — it keeps its needs_action.
        """
        if not _RECOVERABLE_TRIGGER.search(trigger or ""):
            return (False, None, 0)
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return (False, None, 0)
        ident = self._identify_pod(alert_info, trigger) or self._resolve_pod_from_cluster(trigger)
        if not ident:
            return (False, None, 0)
        namespace, pod_name = ident
        try:
            status = k8s.get_pod_status(namespace, pod_name)
        except Exception:
            return (False, None, 0)
        if not status.get('success') or not self._pod_is_healthy(status):
            return (False, None, 0)
        # Probe-class triggers carry no restart signal, so the caller's restart
        # threshold cannot tell a settled pod from one whose readiness is
        # flapping. Ask how long it has held Ready instead: a flapping probe
        # transitions the condition, one failing below failureThreshold never
        # does.
        stable_note = ""
        if _PROBE_TRIGGER.search(trigger or ""):
            stable = self._ready_stable_seconds(status)
            min_stable = int(self._noise_config().get('recovered_ready_stable_seconds', 600))
            if stable is None or stable < min_stable:
                return (False, None, 0)
            stable_note = f"Ready {int(stable // 60)}m, "
        restarts = max((c.get('restartCount', 0) for c in status.get('containerStatuses', [])),
                       default=0)
        return (True,
                f"{namespace}/{pod_name} healthy now ({stable_note}{restarts} restart(s), recovered)",
                restarts)

    def _ephemeral_service_names(self) -> set:
        """Normalized service names of ephemeral Job/CronJob pods in the cluster.
        Used to keep their scheduled churn out of failure correlations (and to
        purge any that were persisted before the baseline filter landed)."""
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return set()
        try:
            res = k8s.get_pods(all_namespaces=True)
        except Exception:
            return set()
        names = set()
        for p in res.get('pods', []):
            pod_name = (p.get('metadata') or {}).get('name', '')
            if pod_name and is_ephemeral_job_pod(pod_name):
                names.add(normalize_service_name(pod_name))
        return names

    def _restart_finding_is_noise(self, finding_text: str, threshold: int) -> Optional[str]:
        """Reason if a 'container restarted N times' sweep finding is recovered
        noise — the pod is healthy now with <= threshold restarts. None otherwise.

        Mirrors the Tier-1 alert-path filter for the *sweep* path, which
        generates these findings independently (e.g. faster-whisper: healthy
        21h, restartCount 1, re-flagged every sweep)."""
        text = (finding_text or "").lower()
        if 'restart' not in text:
            return None
        cm = re.search(r"container ['\"]([a-z0-9._-]+)['\"]", text)
        nm = re.search(r"namespace ['\"]([a-z0-9-]+)['\"]", text)
        if not (cm and nm):
            return None
        name, ns = cm.group(1), nm.group(1)
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        try:
            res = k8s.get_pods(namespace=ns)
        except Exception:
            return None
        matched = [p for p in res.get('pods', [])
                   if (p.get('metadata') or {}).get('name', '').startswith(name)]
        if not matched:
            return None
        worst = 0
        for p in matched:
            st = p.get('status', {})
            if not self._pod_is_healthy({'phase': st.get('phase'),
                                         'conditions': st.get('conditions', [])}):
                return None  # something still unhealthy — keep the finding
            worst = max(worst, max((c.get('restartCount', 0)
                                    for c in st.get('containerStatuses', [])), default=0))
        if worst > threshold:
            return None  # flapping — keep the finding
        return (f"container '{name}' in {ns} is healthy now with <= {threshold} "
                f"restart(s) — recovered transient, not actionable")

    def _early_exit_monitoring(self, inv_id: int, trigger: str, start_time: float,
                               note: str) -> Dict[str, Any]:
        """Record a lightweight 'monitoring' result without running the LLM loop
        (Tier-1 1b). Used when the alerted condition has already recovered."""
        duration = time.time() - start_time
        rec = f"No action needed — {note}. Skipped deep investigation (noise filter)."
        findings = {'response': rec, 'tool_calls': 0, 'recommendation': rec, 'preflight_skip': True}
        try:
            self.kb.update_investigation(
                investigation_id=inv_id, completed_at=datetime.now(), findings=findings,
                outcome='monitoring', duration_seconds=duration, tool_calls_count=0)
        except Exception as e:
            logger.debug(f"early-exit record skipped: {e}")
        INVESTIGATIONS.labels(outcome='monitoring').inc()
        logger.info(f"Investigation #{inv_id} early-exit (noise filter): monitoring — {note}")
        return self._build_action_result(
            success=True,
            message=self._action_message('monitoring', trigger, duration, 0),
            details={'investigation_id': inv_id, 'outcome': 'monitoring',
                     'duration_s': round(duration, 1), 'tool_calls': 0,
                     'preflight_skip': True, 'remediation': rec},
        )

    def _maybe_propose_remediation(self, outcome: str, alert_info: Dict[str, Any],
                                   trigger: str):
        """Phase-B: for a confirmed needs_action pod, build a dry-run remediation
        proposal (patch candidate or precise decline). Returns a Proposal or None.

        Off unless ``remediation.enabled`` is set in config. Never opens a PR in
        this build — ``open_prs`` is plumbed through but the live path is a
        deferred TODO in remediation.py.
        """
        if outcome != 'needs_action':
            return None
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not rcfg.get('enabled'):
            return None
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        ident = self._identify_pod(alert_info, trigger)
        if not ident:
            return None
        namespace, pod_name = ident
        try:
            open_prs = bool(rcfg.get('open_prs'))
            proposer = RemediationProposer(
                k8s,
                repos=self.config.get('git', {}).get('repos', []),
                open_prs=open_prs,
                default_repo_name=rcfg.get('default_repo', 'homelab-infra'),
                github=self._github_write_client() if open_prs else None,
                max_open_prs=int(rcfg.get('max_open_prs', 3)),
            )
            workload = normalize_service_name(pod_name)
            proposal = proposer.propose_for(namespace, pod_name, workload=workload)
            if proposal is None:
                return None
            logger.info(
                f"Remediation proposal for {namespace}/{pod_name}: "
                f"{proposal.kind} ({proposal.fix_class or 'n/a'})"
            )
            # Live path: only patch proposals, only when open_prs is enabled.
            if proposal.is_patch and open_prs:
                proposal.pr_result = proposer.open_pr(proposal, namespace, workload)
                if proposal.pr_result:
                    logger.info(f"Remediation PR for {namespace}/{workload}: {proposal.pr_result}")
            return proposal
        except Exception as e:
            logger.debug(f"Remediation proposal skipped: {e}")
            return None

    _REMEDIATION_FLAGS = ('queue_feed', 'queue_drain', 'queue_reap', 'queue_verify')

    def _remediation_flag(self, name: str) -> bool:
        """Resolve a remediation flag: DB setting overrides config.yaml.

        A DB setting (set via the operator console) wins so flags can be toggled
        live without a redeploy/restart; falls back to the config block.

        The profile is a hard ceiling over both. ``load_config`` already zeroed
        the config side, but the DB override is read live and would otherwise be
        a way to escalate past the profile from the console — the same
        privilege-escalation shape ``ROLE_SCOPE_CEILING`` exists to close in
        auth/models.py.

        The profile is read straight off ``self.config`` rather than through a
        helper method: several tests drive this with a ``MagicMock`` operator
        carrying a real config dict, and a helper would be auto-mocked into an
        object that is neither a profile nor ``None``.
        """
        profile = self.config.get('profile') if isinstance(self.config, dict) else None
        if not shared_config.profile_allows(profile, shared_config.SCOPE_REMEDIATE):
            return False
        try:
            val = self.kb.get_setting('remediation_' + name, '')
            if val not in (None, ''):
                return str(val).strip().lower() in ('1', 'true', 'yes', 'on')
        except Exception as e:
            logger.debug(f"Could not read remediation flag '{name}' from DB, using config: {e}")
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        return bool(rcfg.get(name))

    def _start_remediation_worker(self) -> None:
        """Run the remediation reaper/drainer/verify in a daemon thread.

        Off the main OODA loop so a long proactive sweep (minutes, single-thread)
        can't starve the drain tick — same pattern as the HTTP investigation
        worker. Each task self-gates on its flag + interval.
        """
        threading.Thread(target=self._remediation_worker_loop, daemon=True,
                         name="remediation-worker").start()
        logger.info("Remediation worker thread started")

    def _remediation_worker_loop(self) -> None:
        while True:
            try:
                now = time.time()
                if now - self.last_reap > self._get_reap_interval():
                    self._reap_remediations(); self.last_reap = now
                if now - self.last_drain > self._get_drain_interval():
                    self._drain_remediation_queue(); self.last_drain = now
                if now - self.last_verify > self._get_verify_interval():
                    self._reconcile_remediation_prs(); self.last_verify = now
            except Exception:
                logger.exception("Remediation worker tick failed")
            time.sleep(10)

    def _reap_remediations(self) -> int:
        """Recover remediations whose executor lease expired (gated, safe).

        Off unless ``remediation.queue_reap`` is set. Harmless when the queue is
        empty, so it can be enabled independently of the drainer.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_reap'):
            return 0
        try:
            count = self.kb.requeue_stale_remediations()
            if count:
                REMEDIATION_REAPED.inc(count)
                logger.info(f"Reaped {count} stale remediation(s) back to the queue")
            return count
        except Exception as e:
            logger.error(f"Remediation reaper failed: {e}", exc_info=True)
            return 0

    def _drain_remediation_queue(self) -> int:
        """Claim auto-eligible remediations and spawn an executor Job per item.

        Off unless ``remediation.queue_drain`` is set. Bounded per tick so one
        cycle can't fan out the whole queue. A spawn failure fails the claim so
        the reaper/retry path recovers it rather than leaving it stuck claimed.
        Returns the number of executor Jobs spawned.

        When ``CFOP_EXEC_CHANGE_URL`` is set, node-action rows are gated on the
        changerecord microservice (open + named approval) before spawn — so an
        unapproved record never reaches ``run_ssh_plan``. Unset URL preserves
        prior console-escalation behavior byte-for-byte.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_drain'):
            return 0
        max_per_tick = max(1, int(rcfg.get('max_drain_per_tick', 3)))
        spawned = 0
        # Rows released mid-tick (awaiting approval / transient recorder errors)
        # go back to queued with the same priority — skip them for the rest of
        # this tick so they cannot starve later items via reclaim churn.
        skip_ids: set = set()
        for _ in range(max_per_tick):
            job_name = f"cfop-executor-{uuid.uuid4().hex[:10]}"
            try:
                work = self.kb.claim_next_remediation(job_name, exclude_ids=skip_ids)
            except Exception as e:
                logger.error(f"Remediation claim failed: {e}", exc_info=True)
                break
            if not work:
                break  # queue drained
            try:
                gated = self._prepare_node_action_change_record(work)
                if gated is None:
                    # Waiting on approval (or hard gate failure already released/
                    # failed the claim). Do not spawn; continue draining others.
                    skip_ids.add(work['id'])
                    continue
                work = gated
                self._spawn_remediation_executor(job_name, work)
                spawned += 1
                REMEDIATION_SPAWNED.labels(result='ok').inc()
                logger.info(
                    f"Spawned executor {job_name} for remediation #{work['id']} "
                    f"({work.get('remediation_class')}, risk={work.get('risk')})"
                )
            except Exception as e:
                # Don't leave the row stuck 'claimed' — fail it so retry/reaper recovers.
                REMEDIATION_SPAWNED.labels(result='failed').inc()
                logger.error(f"Executor spawn failed for remediation #{work['id']}: {e}")
                self.kb.fail_remediation(work['id'], f"executor spawn failed: {e}")
                skip_ids.add(work['id'])
        return spawned

    def _executor_config(self) -> Dict[str, Any]:
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        ec = rcfg.get('executor') if isinstance(rcfg.get('executor'), dict) else {}
        return ec

    def _change_record_url(self) -> str:
        """Base URL of the changerecord Service, or '' when unset (homelab default).

        Prefer process env (wired into the agent Deployment) over nested config
        so the gate is not Job-only.
        """
        env_url = (os.getenv('CFOP_EXEC_CHANGE_URL') or '').strip()
        if env_url:
            return env_url.rstrip('/')
        na = self._executor_config().get('node_action')
        na = na if isinstance(na, dict) else {}
        cr = na.get('change_record') if isinstance(na.get('change_record'), dict) else {}
        return str(cr.get('url') or '').strip().rstrip('/')

    def _complete_node_action_plan(self, prompt: str) -> str:
        """LLM completion for a node-action plan (same model floor as the Job)."""
        import requests as req
        ec = self._executor_config()
        llm = ec.get('llm') if isinstance(ec.get('llm'), dict) else {}
        na = ec.get('node_action') if isinstance(ec.get('node_action'), dict) else {}
        model = str(na.get('model') or llm.get('model') or _ANTHROPIC_DEFAULT_EXEC_MODEL)
        api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY required to plan node-action before open")
        payload = {
            'model': model,
            'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        resp = req.post(
            'https://api.anthropic.com/v1/messages',
            json=payload,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            },
            timeout=120,
        )
        resp.raise_for_status()
        return '\n'.join(
            b.get('text', '') for b in resp.json().get('content', [])
            if b.get('type') == 'text'
        )

    def _generate_node_action_plan(self, work: Dict[str, Any]) -> Dict[str, Any]:
        """Produce a validated {host, commands, explanation} plan for open()/spawn.

        Reuses any plan already persisted on the change_record / payload so a
        reclaim after release does not re-call the LLM.
        """
        result = work.get('result') if isinstance(work.get('result'), dict) else {}
        cr = result.get('change_record') if isinstance(result.get('change_record'), dict) else {}
        for candidate in (cr.get('plan'), work.get('approved_plan'),
                          (work.get('payload') or {}).get('plan') if isinstance(work.get('payload'), dict) else None):
            if isinstance(candidate, dict) and candidate.get('commands'):
                plan = _na_normalize_plan(candidate)
                ok, reason = _na_validate_plan(plan['commands'])
                if ok:
                    return plan
                raise RuntimeError(f"persisted plan failed safety gate: {reason}")
        reply = self._complete_node_action_plan(_na_build_command_prompt(work))
        parsed = _na_parse_command_plan(reply)
        if not parsed:
            raise RuntimeError("model produced no parseable command plan")
        plan = _na_normalize_plan(parsed)
        ok, reason = _na_validate_plan(plan['commands'])
        if not ok:
            raise RuntimeError(f"command plan failed safety gate: {reason}")
        return plan

    def _prepare_node_action_change_record(self, work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Gate node-actions on changerecord approval before executor spawn.

        Generates the concrete command plan *before* open() so the record PR
        a human merges is exactly what the executor will run. Returns the
        (possibly enriched) work order when spawn may proceed, or None when
        the claim was released/failed and spawn must be skipped.
        Non-node-action work and unset URL are pass-through (no behavior change).
        """
        if (work.get('remediation_class') or '') != 'node-action':
            return work
        base_url = self._change_record_url()
        if not base_url:
            return work

        result = work.get('result') if isinstance(work.get('result'), dict) else {}
        cr = dict(result.get('change_record')) if isinstance(result.get('change_record'), dict) else {}
        ref = str(cr.get('ref') or '').strip()
        payload = work.get('payload') if isinstance(work.get('payload'), dict) else {}
        target = payload.get('target') if isinstance(payload.get('target'), dict) else {}
        na = self._executor_config().get('node_action')
        na = na if isinstance(na, dict) else {}
        ec = self._executor_config()
        image = ec.get('image', os.getenv('CFOP_EXECUTOR_IMAGE',
                                         'ghcr.io/aachtenberg/cfoperator-executor:main'))
        flag_snapshot = {
            "node_action.enabled": bool(na.get('enabled')),
            "queue_drain": bool(self._remediation_flag('queue_drain')),
            "change_record.url": base_url,
        }

        try:
            plan = self._generate_node_action_plan(work)
            cr = {**cr, "plan": plan}
            host = (plan.get('host') or str(target.get('host') or na.get('host') or '')).strip()
            if not ref:
                opened = change_record_open(base_url, {
                    "remediation_id": work.get('id'),
                    "investigation_id": work.get('investigation_id'),
                    "host": host,
                    "commands": list(plan.get('commands') or []),
                    "justification": str(payload.get('recommendation') or ''),
                    "image": str(image),
                    "flag_snapshot": flag_snapshot,
                    "risk": str(work.get('risk') or ''),
                    "confidence": work.get('confidence'),
                })
                ref = str(opened['ref'])
                cr = {"ref": ref, "url": opened.get('url'), "plan": plan}
                logger.info(
                    "Opened change record for remediation #%s (ref=%s, %d cmd(s))",
                    work['id'], ref[:24], len(plan.get('commands') or []),
                )

            approval = change_record_approval(base_url, ref)
            if approval is None:
                # Persist ref+plan, release claim — next drain tick reclaims and re-polls.
                # Never spawn: unapproved records must not reach run_ssh_plan.
                self.kb.release_remediation_claim(
                    work['id'],
                    result={"change_record": cr},
                    last_error="awaiting change-record approval",
                )
                return None
        except ChangeRecordClientError as e:
            logger.error("Change-record gate failed for remediation #%s: %s", work['id'], e)
            # Only closed-without-merge (HTTP 409) burns an attempt; transport/5xx
            # release and retry next tick so a recorder blip cannot needs-human.
            if e.status == 409:
                self.kb.fail_remediation(work['id'], f"change record gate: {e}")
            else:
                self.kb.release_remediation_claim(
                    work['id'],
                    result={"change_record": cr} if cr else None,
                    last_error=f"change record transient: {e}",
                )
            return None
        except Exception as e:  # noqa: BLE001
            # Plan generation / validation failures are hard — burn an attempt.
            logger.error("Change-record plan failed for remediation #%s: %s", work['id'], e)
            self.kb.fail_remediation(work['id'], f"change record plan: {e}")
            return None

        # Approved — stamp ref + approval + approved_plan into the Job work order.
        enriched = dict(work)
        enriched['change_record_ref'] = ref
        enriched['change_record_url'] = cr.get('url')
        enriched['change_record_approval'] = approval
        enriched['approved_plan'] = plan
        try:
            self.kb.update_remediation_status(
                work['id'], 'claimed',
                result={"change_record": {**cr, "approval": approval, "plan": plan}},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("could not persist change-record approval: %s", e)
        return enriched

    def _build_executor_manifest(self, job_name: str, work_order: Dict[str, Any]) -> Dict[str, Any]:
        """Build the cfoperator-executor Job manifest for a claimed remediation.

        GitOps classes are read-only toward the cluster (their only mutation is a
        GitHub PR). A node-action additionally SSHes to a host to run a guarded
        command plan, so for that class only we mount the SSH key and flip the
        opt-in env. LLM backend is fully env-driven so the model is swappable per
        the portable-executor design.
        """
        ec = self._executor_config()
        namespace = ec.get('namespace', os.getenv('CFOP_EXECUTOR_NAMESPACE', 'apps'))
        image = ec.get('image', os.getenv('CFOP_EXECUTOR_IMAGE', 'ghcr.io/aachtenberg/cfoperator-executor:main'))
        sa = ec.get('service_account', 'cfoperator-executor')
        secrets_name = ec.get('secrets_name', 'cfoperator-secrets')
        pull_secret = ec.get('image_pull_secret', 'ghcr-pull-secret')
        completion_base = ec.get('completion_base_url', os.getenv(
            'CFOP_EXECUTOR_COMPLETION_BASE_URL',
            'http://cfoperator.apps.svc.cluster.local:8083/v1/remediations'))
        # Per-item repo (work-order payload) wins over the config default, so a
        # cfoperator-deploy fix targets that repo while cluster fixes go to homelab-infra.
        payload_repo = (work_order.get('payload') or {}).get('repo')
        git_repo = payload_repo or ec.get('git_repo', os.getenv('CFOP_GIT_REPO', 'aachtenberg/homelab-infra'))
        git_base = ec.get('git_base', 'main')
        llm = ec.get('llm') if isinstance(ec.get('llm'), dict) else {}
        ttl = int(ec.get('ttl_seconds_after_finished', 3600))
        deadline = int(ec.get('active_deadline_seconds', 900))

        completion_url = f"{completion_base.rstrip('/')}/{work_order['id']}/complete"
        env = [
            {"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "ANTHROPIC_API_KEY", "optional": True}}},
            {"name": "GITHUB_TOKEN", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "GITHUB_TOKEN"}}},
            {"name": "CFOP_COMPLETION_TOKEN", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "CFOP_COMPLETION_SHARED_SECRET", "optional": True}}},
            {"name": "CFOP_COMPLETION_URL", "value": completion_url},
            {"name": "CFOP_REMEDIATION_JSON", "value": json.dumps(work_order, default=str)},
            {"name": "CFOP_GIT_REPO", "value": str(git_repo)},
            {"name": "CFOP_GIT_BASE", "value": str(git_base)},
            {"name": "CFOP_EXEC_LLM_BACKEND", "value": str(llm.get('backend', 'anthropic'))},
            {"name": "CFOP_EXEC_LLM_MODEL", "value": str(llm.get('model', ''))},
            {"name": "CFOP_EXEC_LLM_BASE_URL", "value": str(llm.get('base_url', ''))},
        ]
        # node-action only: opt in + mount the SSH key so the executor can run a
        # guarded command plan on a host. GitOps classes stay PR-only (no mount).
        volumes: List[Dict[str, Any]] = []
        volume_mounts: List[Dict[str, Any]] = []
        na = ec.get('node_action') if isinstance(ec.get('node_action'), dict) else {}
        if (work_order.get('remediation_class') or '') == 'node-action' and na.get('enabled'):
            # Reuse the forensics keypair the deep-investigation worker already
            # uses to SSH into hosts. Mount it at a staging dir (group-readable);
            # the executor copies it into ~/.ssh at 0600 (ssh refuses looser).
            ssh_secret = na.get('ssh_secret', 'cfop-forensics-ssh')
            env += [
                {"name": "CFOP_NODE_ACTION_ENABLED", "value": "true"},
                {"name": "CFOP_NODE_ACTION_HOST", "value": str(na.get('host', ''))},
                {"name": "CFOP_SSH_USER", "value": str(na.get('ssh_user', 'sre'))},
                {"name": "CFOP_SSH_SECRET_DIR", "value": "/ssh-secret"},
            ]
            # Change-record close URL for the executor (agent already gated on
            # approval before spawn). Unset → executor skips close entirely.
            cr = na.get('change_record') if isinstance(na.get('change_record'), dict) else {}
            change_url = (
                (os.getenv('CFOP_EXEC_CHANGE_URL') or '').strip()
                or str(cr.get('url') or '').strip()
            ).rstrip('/')
            if change_url:
                env.append({"name": "CFOP_EXEC_CHANGE_URL", "value": change_url})
                # Shared secret for /close (optional; matches changerecord Deployment).
                env.append({
                    "name": "CFOP_CHANGERECORD_SHARED_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": secrets_name,
                            "key": "CFOP_CHANGERECORD_SHARED_SECRET",
                            "optional": True,
                        }
                    },
                })
            # Node-action is the only path that runs shell on a host, so it pins
            # its own model floor: a cost downgrade of the generic executor model
            # must never silently drop the model deciding what to run on hosts.
            na_model = str(na.get('model', '') or _ANTHROPIC_DEFAULT_EXEC_MODEL)
            for e in env:
                if e.get("name") == "CFOP_EXEC_LLM_MODEL":
                    e["value"] = na_model
                    break
            volumes.append({"name": "ssh", "secret": {
                "secretName": ssh_secret, "defaultMode": 0o440}})
            volume_mounts.append({"name": "ssh", "mountPath": "/ssh-secret", "readOnly": True})
        labels = {
            "app.kubernetes.io/managed-by": "cfoperator",
            "cfop.dev/role": "remediation-executor",
        }
        container = {
            "name": "executor",
            "image": image,
            "imagePullPolicy": "Always",
            "env": env,
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        }
        if volume_mounts:
            container["volumeMounts"] = volume_mounts
        pod_spec = {
            "restartPolicy": "Never",
            "serviceAccountName": sa,
            "imagePullSecrets": [{"name": pull_secret}],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
            "containers": [container],
        }
        if volumes:
            pod_spec["volumes"] = volumes
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": namespace, "labels": dict(labels)},
            "spec": {
                "backoffLimit": 0,  # an LLM rerun is a drainer decision, not a retry policy
                "ttlSecondsAfterFinished": ttl,
                "activeDeadlineSeconds": deadline,
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": pod_spec,
                },
            },
        }

    def _kubectl_create(self, manifest: Dict[str, Any]) -> str:
        proc = subprocess.run(
            ["kubectl", "create", "-n", manifest["metadata"]["namespace"], "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"kubectl create failed: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    def _spawn_remediation_executor(self, job_name: str, work_order: Dict[str, Any]) -> None:
        """Spawn the cfoperator-executor Job for a claimed remediation."""
        manifest = self._build_executor_manifest(job_name, work_order)
        self._kubectl_create(manifest)

    @staticmethod
    def _parse_pr_url(url: str) -> Optional[tuple]:
        """Parse https://github.com/<owner>/<repo>/pull/<n> -> ('owner/repo', n)."""
        m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url or "")
        return (m.group(1), int(m.group(2))) if m else None

    def _reconcile_remediation_prs(self) -> int:
        """Advance 'pr-open' remediations by their PR state.

        Off unless ``remediation.queue_verify`` is set. Merged -> resolved (then
        re-investigate to confirm the signal cleared); closed-without-merge ->
        rejected. Returns the number of rows advanced.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_verify'):
            return 0
        try:
            rows = self.kb.list_remediations_by_status('pr-open')
        except Exception as e:
            logger.error(f"Remediation PR reconcile list failed: {e}", exc_info=True)
            return 0
        gh = self._github_write_client() if rows else None
        if gh is None:
            return 0
        advanced = 0
        for row in rows:
            ref = self._parse_pr_url(row.get('pr_url') or '')
            if not ref:
                continue
            repo, number = ref
            resp = gh.request("GET", f"/repos/{repo}/pulls/{number}")
            if not resp.get('success'):
                continue
            data = resp.get('data') or {}
            if data.get('merged'):
                self.kb.update_remediation_status(row['id'], 'resolved', result={'pr_merged': True})
                REMEDIATION_OUTCOME.labels(outcome='resolved').inc()
                self._verify_remediation(row)
                advanced += 1
            elif data.get('state') == 'closed':
                self.kb.update_remediation_status(row['id'], 'rejected',
                                                  last_error='PR closed without merge')
                REMEDIATION_OUTCOME.labels(outcome='rejected').inc()
                advanced += 1
        if advanced:
            logger.info(f"Reconciled {advanced} remediation PR(s)")
        return advanced

    _REMEDIATION_STATUSES = ('queued', 'claimed', 'executing', 'pr-open', 'verifying',
                             'resolved', 'failed', 'needs-human', 'rejected')

    def _update_remediation_metrics(self) -> None:
        """Refresh the cfoperator_remediation_queue gauge (throttled to ~30s).

        Reports every status (0 when empty) so the Grafana panel has stable
        series. Independent of the feed/drain flags.
        """
        if time.time() - self.last_metrics < 30:
            return
        self.last_metrics = time.time()
        try:
            counts = self.kb.count_remediations_by_status()
        except Exception as e:
            logger.debug(f"remediation metrics refresh skipped: {e}")
            return
        for status in self._REMEDIATION_STATUSES:
            REMEDIATION_QUEUE.labels(status=status).set(counts.get(status, 0))

    def _verify_remediation(self, row: Dict[str, Any]) -> None:
        """Best-effort post-merge verification: re-investigate the original signal.

        Enqueues a fresh investigation so the KB (and a human) sees whether the
        merge actually cleared the condition. Non-fatal.
        """
        try:
            rec = str((row.get('payload') or {}).get('recommendation') or 'remediation')
            self.enqueue_investigation({
                'summary': f"verify remediation #{row['id']}: {rec[:120]}",
                'source': 'remediation-verify',
            })
        except Exception as e:
            logger.debug(f"Remediation verify enqueue skipped: {e}")

    def _github_write_client(self):
        """Build a GitHub API client for opening remediation PRs, or None.

        Reuses event_runtime's self-contained GitHubApiClient. Token from
        GITHUB_TOKEN (same as the git context provider). Returns None when no
        token is set so the proposer falls back to dry-run.
        """
        token = os.getenv('GITHUB_TOKEN', '').strip()
        if not token:
            logger.warning("remediation.open_prs is on but GITHUB_TOKEN is unset; staying dry-run")
            return None
        try:
            from event_runtime.github_client import GitHubApiClient
            return GitHubApiClient(token=token)
        except Exception as e:
            logger.warning(f"Could not init GitHub client for remediation: {e}")
            return None

    def store_deep_investigation(self, alert: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest a deep-investigation worker's report into the knowledge base.

        Mirrors the storage tail of ``_act`` (start/update investigation +
        embedding) so the report surfaces in future triage's
        similar-investigation lookups. If the report carries a proposed diff
        and ``remediation.deep_open_prs`` is enabled, route it through the
        existing PR gates — never a parallel path.
        """
        details = result.get('details') if isinstance(result.get('details'), dict) else {}
        summary = str(alert.get('summary') or '(no summary)')
        trigger = f"[deep] {summary}"
        # The worker reports outcome "escalated" (the engine ledger's exact
        # match string); the KB convention from STATUS parsing is "escalate".
        outcome = str(details.get('outcome') or 'needs_action')
        kb_outcome = 'escalate' if outcome == 'escalated' else outcome

        inv_id = self.kb.start_investigation(trigger=trigger)
        provider = f"anthropic/{details.get('model') or 'unknown'}"
        findings = {
            'response': str(details.get('report') or '')[:5000],
            'recommendation': str(details.get('recommendation') or ''),
            'provider': provider,
            'deep': True,
            'host': details.get('host'),
        }
        self.kb.update_investigation(
            investigation_id=inv_id,
            completed_at=datetime.now(),
            findings=findings,
            outcome=kb_outcome,
            duration_seconds=float(details.get('duration_s') or 0.0),
        )
        self._embed_investigation(inv_id, trigger, findings, kb_outcome)
        logger.info(f"Deep investigation #{inv_id} stored: {kb_outcome} (host={details.get('host')})")

        # Feed the remediation queue from the structured hints the worker
        # emitted (remediation_class/risk/confidence). The queue's auto-gate
        # decides drainable vs needs-human; the drainer/executor take it from
        # there. Distinct from the legacy inline deep_open_prs path below.
        # Stamp the reporting LLM onto the queue payload (same string as findings).
        queue_details = dict(details)
        queue_details['provider'] = provider
        rid = self._maybe_queue_remediation(inv_id, queue_details)

        pr_result = None
        diff_text = str(details.get('proposed_diff') or '')
        if diff_text:
            pr_result = self._maybe_open_pr_from_deep_diff(alert, details, diff_text)
            if pr_result:
                logger.info(f"Deep-investigation PR for {details.get('host')}: {pr_result}")
                # Persist the attempt on the remediation row so declines survive
                # log rotation and show in the console (CFOP-22 D).
                if rid:
                    attempt = {k: pr_result[k] for k in ('status', 'detail', 'path', 'branch')
                               if k in pr_result and pr_result[k] is not None}
                    pr_url = pr_result.get('html_url') if pr_result.get('status') == 'opened' else None
                    try:
                        self.kb.merge_remediation_payload(rid, {'pr_attempt': attempt}, pr_url=pr_url)
                    except Exception as e:
                        logger.warning(f"Could not stamp pr_attempt on remediation #{rid}: {e}")

        out: Dict[str, Any] = {'investigation_id': inv_id, 'outcome': kb_outcome}
        if pr_result:
            out['pr_result'] = pr_result
        if rid:
            out['remediation_id'] = rid
        return out

    def _count_enqueued(self, source: str, rclass: str, risk: str, confidence) -> None:
        """Bump the enqueue counter, labelled by source/class and auto-eligibility."""
        nc, nr = normalize_remediation_fields(rclass, risk)
        elig = remediation_is_auto_eligible(nc, nr, confidence)
        REMEDIATION_ENQUEUED.labels(source=source, remediation_class=nc, eligible=str(elig).lower()).inc()

    def _maybe_queue_remediation(self, investigation_id: int, details: Dict[str, Any]) -> Optional[int]:
        """Enqueue a remediation from an investigation's structured hints.

        Off unless ``remediation.queue_feed`` is set. No-op when the investigator
        didn't classify the recommendation (no ``remediation_class``).
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return None
        rclass = details.get('remediation_class')
        if not rclass:
            return None
        risk = str(details.get('risk') or 'high')
        confidence = details.get('confidence')
        payload = {
            'recommendation': str(details.get('recommendation') or ''),
            'rendered_context': str(details.get('report') or '')[:5000],
            'proposed_diff': str(details.get('proposed_diff') or ''),
            'target': {'host': details.get('host')},
        }
        # Reporting LLM (provider/model) — same format as investigation findings.
        provider = details.get('provider')
        if provider:
            payload['provider'] = str(provider)
        try:
            rid = self.kb.queue_remediation(
                remediation_class=str(rclass),
                payload=payload,
                investigation_id=investigation_id,
                host_id=str(details.get('host') or 'default'),
                risk=risk,
                confidence=confidence,
            )
            if rid:
                self._count_enqueued('deep-investigation', str(rclass), risk, confidence)
            return rid
        except Exception as e:
            logger.error(f"Failed to queue remediation from investigation #{investigation_id}: {e}",
                         exc_info=True)
            return None

    _SEVERITY_RISK = {'critical': 'high', 'warning': 'med', 'info': 'low'}

    @staticmethod
    def _recommendation_is_investigate_shaped(text: str) -> bool:
        """True when the next step is evidence-gathering the agent can do itself.

        Matches the morning-summary prompt's investigate vocabulary
        (check/verify/confirm/investigate/monitor). Human-only cues
        (physically, hardware, power supply, …) stay on the manual queue even
        when a check/verify verb is also present.
        """
        if not text or _HUMAN_ONLY_SHAPED.search(text):
            return False
        return bool(_INVESTIGATE_SHAPED.search(text))

    def _feed_remediations_from_sweeps(self, reports: List[Dict[str, Any]]) -> int:
        """Feed overnight sweep findings into investigation or the remediation queue.

        Off unless ``remediation.queue_feed`` is set. Investigate-shaped
        recommendations (check/verify/monitor/…) are dispatched as autonomous
        investigations — the agent gathers evidence itself rather than parking
        them as needs-human. Only genuinely human-shaped recs enqueue as
        ``manual``. Deduped by finding id for the manual path.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return 0
        handled = 0
        dispatched = 0
        enq = 0
        for rep in reports or []:
            for f in (rep.get('findings') or []):
                rec = str(f.get('remediation') or '').strip()
                if not rec or rec.lower().startswith('no action') or rec.lower() in ('none', 'n/a', 'nothing'):
                    continue
                key = f"sweep-{f.get('id') or rec[:80]}"
                risk = self._SEVERITY_RISK.get(str(f.get('severity') or 'info'), 'high')
                finding = str(f.get('finding') or '').strip()
                title = finding or rec[:80]
                if self._recommendation_is_investigate_shaped(rec):
                    try:
                        self.enqueue_investigation({
                            'summary': f"{title}: {rec}"[:300],
                            'source': 'sweep-investigate',
                            'host': f.get('resource_name') or f.get('namespace') or '',
                        })
                        dispatched += 1
                        handled += 1
                    except Exception as e:
                        logger.warning(f"could not dispatch sweep investigation for '{title}': {e}")
                    continue
                try:
                    payload = {
                        'recommendation': rec,
                        'finding': f.get('finding'),
                        'evidence': f.get('evidence'),
                        'resource': {'type': f.get('resource_type'),
                                     'name': f.get('resource_name'),
                                     'namespace': f.get('namespace')},
                        'source': 'morning-summary/sweep',
                        'dedupe_key': key,
                    }
                    provider = _llm_provider_tag(rep.get('sweep_meta') or {})
                    if provider:
                        payload['provider'] = provider
                    rid = self.kb.queue_remediation(
                        remediation_class='manual',
                        payload=payload,
                        host_id=str(f.get('resource_name') or f.get('namespace') or 'default')[:64],
                        risk=risk,
                        confidence=None,
                        dedupe_key=key,
                    )
                    if rid:
                        enq += 1
                        handled += 1
                        self._count_enqueued('morning-summary/sweep', 'manual', risk, None)
                except Exception as e:
                    logger.error(f"sweep->remediation enqueue failed: {e}", exc_info=True)
        if handled:
            logger.info(f"Fed {handled} sweep finding(s): "
                        f"{dispatched} investigation(s), {enq} manual remediation(s)")
        return handled

    def feed_remediations_from_recent_sweeps(self, limit: int = 10) -> int:
        """On-demand: enqueue remediations from the most recent sweep reports."""
        return self._feed_remediations_from_sweeps(self.kb.get_recent_sweep_reports(limit=limit))

    @staticmethod
    def _parse_summary_recommendations(summary_text: str) -> List[Dict[str, Any]]:
        """Extract recs from the summary's ```json {"recommendations":[...]} block."""
        m = re.search(r"```json\s*(\{.*?\})\s*```", summary_text or "", re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return []
        recs = data.get('recommendations') if isinstance(data, dict) else None
        return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []

    @staticmethod
    def _strip_summary_recommendations_block(summary_text: str) -> str:
        """Remove the machine-readable ```json recommendations block from the
        summary so it never reaches operator-facing channels (Slack/ntfy).

        The block is emitted by the LLM purely to feed the remediation queue
        (see _feed_remediations_from_summary); humans see the prose table above
        it. Strip only after the queue has consumed it.
        """
        if not summary_text:
            return summary_text
        stripped = re.sub(r"\n*```json\s*\{.*?\}\s*```\n*", "\n", summary_text,
                          flags=re.DOTALL)
        return stripped.rstrip() + "\n" if stripped != summary_text else summary_text

    def _feed_remediations_from_summary(self, summary_text: str,
                                        overnight_reports: Optional[List[Dict[str, Any]]] = None,
                                        provider: Optional[str] = None) -> int:
        """Feed the queue from the summary's structured recommendations block.

        Captures the operator-facing 'Issues & Recommendations' (LLM synthesis),
        which raw sweep findings don't contain. Falls back to structured sweep
        findings when the LLM emits no usable block. Gated by queue_feed; the
        per-item remediation_class/risk/confidence drive the auto-execute gate,
        so a low-risk gitops-patch can become auto-eligible. Deduped by title.

        ``provider`` is the summary LLM tag (``backend/model``) stamped onto
        queued rows as ``payload.provider`` — not applied on the sweep fallback,
        which has its own ``sweep_meta``.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return 0
        recs = self._parse_summary_recommendations(summary_text)
        if not recs:
            return self._feed_remediations_from_sweeps(overnight_reports or [])
        enq = 0
        dispatched = 0  # investigate-class findings sent to the investigation pipeline
        for r in recs:
            rec = str(r.get('recommendation') or '').strip()
            if not rec or rec.lower().startswith('no action') or rec.lower() in ('none', 'n/a', 'nothing'):
                continue
            title = str(r.get('title') or rec[:80]).strip()
            key = "summary-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
            rclass = str(r.get('remediation_class') or 'manual')
            risk = str(r.get('risk') or 'med')
            conf = r.get('confidence') if isinstance(r.get('confidence'), (int, float)) else None
            # Clamp the cheap model's self-reported confidence: a confident
            # hallucination must not surface as a high-confidence queue row.
            if conf is not None:
                conf = min(conf, _SUMMARY_CONFIDENCE_CAP)
            # 'investigate' findings are evidence-gathering the agent does itself.
            # A mutation-class rec from the summary is an UNVERIFIED hypothesis
            # (cheap model, no enforced grounding), so route it the same way:
            # the deep tier verifies it and only a grounded finding becomes a
            # remediation — never a high-confidence host action straight from a
            # summary hunch. Mislabelled 'manual' with investigate-shaped text
            # (check/verify/monitor/…) is also dispatched — the cheap model
            # often defaults to manual for "check CoreDNS" style items.
            # Only genuinely human-only manuals still queue.
            route_investigate = (
                rclass == 'investigate'
                or rclass in _SUMMARY_MUTATION_CLASSES
                or (rclass == 'manual' and self._recommendation_is_investigate_shaped(rec))
            )
            if route_investigate:
                try:
                    # Preserve mutation-class proposals for the investigator;
                    # investigate / mislabelled-manual get no suffix.
                    suffix = (f" [proposed: {rclass}]"
                              if rclass in _SUMMARY_MUTATION_CLASSES else '')
                    self.enqueue_investigation({'summary': f"{title}: {rec}{suffix}"[:300],
                                                'source': 'summary-investigate',
                                                'host': r.get('host')})
                    dispatched += 1
                except Exception as e:
                    logger.warning(f"could not dispatch investigation for '{title}': {e}")
                continue
            try:
                payload = {'recommendation': rec, 'title': title,
                           'target': {'host': r.get('host')},
                           'repo': (str(r.get('repo') or '').strip() or None),
                           'source': 'morning-summary', 'dedupe_key': key}
                if provider:
                    payload['provider'] = provider
                rid = self.kb.queue_remediation(
                    remediation_class=rclass,
                    payload=payload,
                    host_id=str(r.get('host') or 'default')[:64],
                    risk=risk,
                    confidence=conf,
                    dedupe_key=key,
                )
                if rid:
                    enq += 1
                    self._count_enqueued('morning-summary', rclass, risk, conf)
            except Exception as e:
                logger.error(f"summary->remediation enqueue failed: {e}", exc_info=True)
        if enq or dispatched:
            logger.info(f"Morning summary: queued {enq} remediation(s), "
                        f"dispatched {dispatched} investigation(s)")
        return enq

    def _maybe_open_pr_from_deep_diff(self, alert: Dict[str, Any], details: Dict[str, Any],
                                      diff_text: str) -> Optional[Dict[str, Any]]:
        """Route a deep-investigation diff through the remediation PR gates.

        Off unless ``remediation.deep_open_prs`` is set — until then the diff
        only travels inside the report/notification (dry-run, like Phase B
        before open_prs went live).
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not rcfg.get('deep_open_prs'):
            return None
        try:
            proposer = RemediationProposer(
                getattr(self.tools, 'k8s_tools', None),
                repos=self.config.get('git', {}).get('repos', []),
                open_prs=True,
                default_repo_name=rcfg.get('default_repo', 'homelab-infra'),
                github=self._github_write_client(),
                max_open_prs=int(rcfg.get('max_open_prs', 3)),
            )
            host = str(details.get('host') or 'unknown')
            alertname = str((alert.get('details') or {}).get('alertname') or 'finding')
            title = f"cfoperator deep-investigation fix: {alertname} on {host}"
            body = (
                f"Proposed by a deep-investigation run for alert: {alert.get('summary')}\n\n"
                f"Recommendation: {details.get('recommendation') or '(see report)'}\n\n"
                "Generated from host forensics; review before merging.\n"
                f"Report excerpt:\n\n{str(details.get('report') or '')[:2000]}"
            )
            return proposer.open_pr_from_diff(
                diff_text=diff_text, title=title, body=body,
                dedupe_key=f"{host}-{alertname}",
            )
        except Exception as e:
            logger.warning(f"Deep-investigation PR path failed: {e}")
            return {'status': 'error', 'detail': str(e)[:200]}

    def _verify_investigation_outcome(self, outcome: str, alert_info: Dict[str, Any],
                                      trigger: str) -> tuple:
        """Deterministically check a 'resolved' verdict against live cluster state.

        The investigation LLM can claim 'resolved' while the resource is still
        broken (it only *recommended* a fix). When the alert pins to a specific
        k8s pod, re-query its real status; if it isn't actually healthy,
        downgrade 'resolved' -> 'needs_action'. Conservative: only downgrades,
        and no-ops when the resource can't be identified, the pod is gone, or
        K8sTools is unavailable. Returns (outcome, note_or_None).
        """
        if outcome != 'resolved':
            return outcome, None
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return outcome, None
        ident = self._identify_pod(alert_info, trigger)
        if not ident:
            return outcome, None
        namespace, pod_name = ident
        try:
            status = k8s.get_pod_status(namespace, pod_name)
        except Exception as e:
            logger.debug(f"Outcome verify skipped (status query failed): {e}")
            return outcome, None
        # Pod not found could mean it was replaced/cleaned up — don't assume broken.
        if not status.get('success') or self._pod_is_healthy(status):
            return outcome, None
        note = f"claimed resolved but {namespace}/{pod_name} is {status.get('phase', 'unknown')}"
        logger.info(f"Outcome verify downgraded resolved -> needs_action: {note}")
        return 'needs_action', note

    @staticmethod
    def _action_message(outcome: str, trigger: str, duration: float, tool_calls: int) -> str:
        """One-line ActionResult.message summarising an investigation outcome."""
        verb = {
            'resolved': 'Resolved',
            'needs_action': 'Action needed',
            'escalated': 'Escalated',
            'monitoring': 'Monitoring',
            'failed': 'Investigation failed',
        }.get(outcome, outcome.title())
        return f"{verb}: {trigger[:160]} ({duration:.1f}s, {tool_calls} tool calls)"

    def _build_action_result(self, *, success: bool, message: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a result dict matching event_runtime.models.ActionResult.to_dict()."""
        return {
            'action': 'investigate',
            'success': bool(success),
            'message': str(message),
            'details': dict(details),
            'executed_at': datetime.now(timezone.utc).isoformat(),
        }

    def _deep_system_sweep(self):
        """
        Proactive mode: Comprehensive system analysis.

        Every N minutes, systematically:
        1. Query ALL metrics and look for trends
        2. Scan ALL logs for patterns
        3. Check ALL containers/services
        4. Compare to baselines
        5. Search for slow degradation
        6. Consolidate learnings
        7. Generate summary report
        """
        logger.info("Starting deep system sweep")
        sweep_start = time.time()

        findings = []
        sweep_config = self.config['ooda']['sweep']

        # Parallel sweep: if pool has 2+ instances, fan out LLM phases concurrently
        if self.ollama_pool and self.ollama_pool.available_count() >= 2:
            logger.info(f"Using parallel sweep ({self.ollama_pool.available_count()} instances available)")
            try:
                from sweep_graph import run_parallel_sweep
                parallel_findings = run_parallel_sweep(self, self.ollama_pool, sweep_config)
                findings.extend(parallel_findings)
            except Exception as e:
                logger.error(f"Parallel sweep failed, falling back to sequential: {e}")
                findings.extend(self._sequential_sweep(sweep_config))
        else:
            if self.ollama_pool:
                logger.info("Pool has <2 available instances, using sequential sweep")
            findings.extend(self._sequential_sweep(sweep_config))

        # 4. Baseline drift detection
        if sweep_config.get('baseline_drift'):
            logger.info("Checking baseline drift...")
            drift_findings = self._check_baseline_drift()
            findings.extend(drift_findings)
            logger.info(f"Baseline drift check found {len(drift_findings)} findings")

        # 5. Learning consolidation - merge similar learnings
        if sweep_config.get('learning_consolidation'):
            logger.info("Consolidating learnings...")
            self._consolidate_learnings()

        # 5b. Backfill embeddings for unindexed investigations and learnings
        try:
            if self.embeddings.is_available():
                result = self.embeddings.batch_index_investigations(
                    kb=self.kb._kb,
                    batch_size=10,
                    max_total=50
                )
                if result.get('success', 0) > 0:
                    logger.info(f"Embedding backfill (investigations): {result['success']} indexed, {result.get('remaining', 0)} remaining")

                lr = self.embeddings.batch_index_learnings(
                    kb=self.kb._kb,
                    batch_size=10,
                    max_total=50
                )
                if lr.get('success', 0) > 0:
                    logger.info(f"Embedding backfill (learnings): {lr['success']} indexed, {lr.get('remaining', 0)} remaining")
        except Exception as e:
            logger.debug(f"Embedding backfill skipped: {e}")

        # 6. Deduplicate findings across phases
        findings = self._dedup_findings(findings)

        # 6b. LLM judge — filter hallucinated/unsupported findings
        findings = self._verify_findings(findings)

        # 6c. Post findings to event runtime (if configured)
        if findings:
            try:
                self._post_findings_to_event_runtime(findings)
            except Exception as e:
                logger.debug(f"Could not post findings to event runtime: {e}")

        # 6d. Emit resolutions for findings that cleared since last sweep
        # so Slack/Discord see explicit "Resolved: …" notifications instead
        # of silently dropping previously-fired alerts.
        try:
            resolved = self._get_resolved_findings(findings)
            if resolved:
                logger.info(f"Sweep: {len(resolved)} finding(s) resolved since last sweep")
                self._post_resolutions_to_event_runtime(resolved)
        except Exception as e:
            logger.debug(f"Could not emit resolutions: {e}")

        # 7. Generate sweep report
        if findings:
            logger.info(f"Sweep found {len(findings)} total issues")
            report = self._generate_sweep_report(findings)

            # Only notify if findings changed since last sweep
            if report['severity'] in ['warning', 'critical']:
                new_findings = self._get_new_findings(report['findings'])
                if new_findings:
                    logger.warning(f"New findings in sweep ({len(new_findings)} new): {report['summary'][:200]}")
                    # Build a notification-only report with just the new stuff
                    notif_report = self._generate_sweep_report(new_findings)
                    notif_report['summary'] = f"[{len(new_findings)} new of {len(findings)} total] " + notif_report['summary']
                    self._notify_sweep_findings(notif_report)
                else:
                    logger.info(f"Sweep found {len(findings)} issues (all known from previous sweep, skipping notification)")

            # Add timing and mode info to sweep_meta
            sweep_duration = time.time() - sweep_start
            sweep_mode = 'parallel' if (self.ollama_pool and self.ollama_pool.available_count() >= 0) else 'sequential'
            if report.get('sweep_meta'):
                report['sweep_meta']['duration_seconds'] = round(sweep_duration, 1)
                report['sweep_meta']['mode'] = sweep_mode

            # Always store the full report in DB
            try:
                self.kb.store_sweep_report(
                    severity=report['severity'],
                    findings=report['findings'],
                    summary=report['summary'],
                    sweep_meta=report.get('sweep_meta')
                )
            except Exception as e:
                logger.warning(f"Could not store sweep report (DB down?): {e}")
        else:
            logger.info("Sweep complete - no findings")

        # 7b. Capture metric snapshot for correlation baseline
        try:
            snapshot_metrics = self._capture_metric_snapshot()
            if snapshot_metrics:
                self.kb._kb.record_metric_snapshot(
                    metrics=snapshot_metrics,
                    snapshot_type='sweep'
                )
        except Exception as e:
            logger.debug(f"Metric snapshot skipped: {e}")

        # 8. Correlation analysis — detect patterns AND have LLM analyze them
        logger.info("Starting correlation analysis...")
        try:
            # Keep ephemeral Job/CronJob services out of correlations: clean any
            # previously-persisted false rows (recorded before the baseline
            # filter), and guard against recording new ones.
            ephemeral = self._ephemeral_service_names()
            if ephemeral:
                purged = self.kb._kb.purge_correlations_for_services(ephemeral)
                if purged:
                    logger.info(f"Purged {purged} false correlation(s) for ephemeral job services")
            patterns = self.kb._kb.find_service_failure_patterns(days=30)
            if patterns:
                for p in patterns:
                    svc_a = p.get('service_a', '')
                    svc_b = p.get('service_b', '')
                    if svc_a and svc_b and svc_a not in ephemeral and svc_b not in ephemeral:
                        ctype = p.get('correlation_type', 'co_failure')
                        self.kb._kb.record_service_correlation(
                            service_a=svc_a,
                            service_b=svc_b,
                            correlation_type=ctype,
                            time_delta_seconds=p.get('avg_time_delta_seconds'),
                            details={'co_failure_count': p.get('co_failure_count', 0)}
                        )
                logger.info(f"Correlation analysis: {len(patterns)} service failure patterns found")

            # Persist event correlations (investigation<->drift, investigation<->investigation)
            correlated = self.kb._kb.find_correlated_events(window_seconds=300, hours=168)
            persisted = 0
            for ce in correlated:
                try:
                    self.kb._kb.record_event_correlation(
                        event_a_type=ce['event_a']['type'],
                        event_a_id=ce['event_a']['id'],
                        event_b_type=ce['event_b']['type'],
                        event_b_id=ce['event_b']['id'],
                        time_delta_seconds=ce['time_delta_seconds'],
                        root_cause_candidate='event_a' if ce['time_delta_seconds'] > 0 else 'event_b',
                        analysis_notes=f"{ce['event_a'].get('trigger', '')} <-> {ce['event_b'].get('trigger', ce['event_b'].get('drift_type', ''))}"
                    )
                    persisted += 1
                except Exception as e:
                    logger.debug(f"Could not persist event correlation: {e}")
            if persisted:
                logger.info(f"Correlation analysis: persisted {persisted} event correlations")

            # LLM analysis of operational data + correlations
            self._analyze_correlations(findings, patterns or [])
        except Exception as e:
            logger.warning(f"Correlation analysis failed: {e}", exc_info=True)

    def _analyze_correlations(self, sweep_findings: list, failure_patterns: list):
        """Have the LLM analyze operational data and correlations to produce insights."""
        import requests as req

        # Gather operational context
        try:
            ops = self.kb.get_operational_summary(hours=24)
        except Exception:
            ops = {}

        correlated_events = []
        learned_correlations = []
        try:
            correlated_events = self.kb._kb.find_correlated_events(hours=168)[:10]
            learned_correlations = self.kb._kb.get_service_correlations(min_count=2)
        except Exception as e:
            logger.debug(f"Could not load correlations for analysis: {e}")

        # Skip if there's nothing interesting to analyze
        has_data = (
            sweep_findings
            or failure_patterns
            or correlated_events
            or ops.get('investigations', {}).get('total', 0) > 0
        )
        if not has_data:
            logger.info("Correlation analysis: no data to analyze, skipping")
            return

        resolved = self._resolve_provider()
        if not resolved:
            logger.info("Correlation analysis: no LLM provider available, skipping")
            return

        provider_type, url, model = resolved
        logger.info(f"Correlation analysis: sending to {provider_type}/{model} (findings={len(sweep_findings)}, patterns={len(failure_patterns)}, correlated={len(correlated_events)})")

        # Only feed actionable findings into the learning pipeline. Info-severity
        # findings are typically healthy-state restatements ("X is running fine")
        # and turning them into "patterns" just pollutes the knowledge base.
        actionable_findings = [
            f for f in sweep_findings
            if isinstance(f, dict) and str(f.get('severity', '')).lower() in ('warning', 'critical')
        ]

        prompt = f"""Analyze this operational data from the last 24 hours and identify patterns, root causes, or concerns.

SWEEP FINDINGS (this cycle):
{json.dumps(actionable_findings[:10], default=str)[:1500]}

OPERATIONAL SUMMARY:
- Sweeps: {ops.get('sweeps', {}).get('total', 0)} total, avg {ops.get('sweeps', {}).get('avg_findings', 0)} findings/sweep
- Severity breakdown: {json.dumps(ops.get('sweeps', {}).get('by_severity', {}))}
- Investigations: {ops.get('investigations', {}).get('total', 0)} total, outcomes: {json.dumps(ops.get('investigations', {}).get('by_outcome', {}))}
- Learnings extracted: {ops.get('learnings', {}).get('total', 0)}

SERVICE FAILURE PATTERNS (7-day window):
{json.dumps(failure_patterns[:5], default=str)[:800]}

CORRELATED EVENTS (same time window):
{json.dumps(correlated_events[:5], default=str)[:800]}

KNOWN SERVICE CORRELATIONS:
{json.dumps(learned_correlations[:5], default=str)[:500]}

Return ONLY valid JSON:
{{"insights": [
  {{
    "learning_type": "pattern",
    "title": "Brief title (max 100 chars)",
    "description": "What pattern was detected and what it means",
    "applies_when": "The concrete, observable condition under which this learning is relevant (e.g. 'pod X is OOMKilled', 'service Y and Z fail within 5 min of each other'). REQUIRED — an insight with no trigger condition is useless.",
    "services": ["service1"],
    "category": "resource"
  }}
]}}

learning_type must be one of: solution, pattern, root_cause, antipattern, insight
category must be one of: resource, network, config, dependency

Focus on:
- Services that fail together (dependency chains)
- Recurring issues across multiple sweeps
- Escalation patterns (info → warning → critical over time)
- Issues that investigations failed to resolve

Do NOT emit an insight for a healthy/normal state, a one-off transient blip, or a
restatement of a single finding. Only genuine cross-event patterns worth remembering.
Every insight MUST have a non-empty, specific `applies_when`. Omit any insight you
cannot give a real trigger condition for.
Return empty array if nothing notable: {{"insights": []}}"""

        messages = [
            {'role': 'system', 'content': 'You are an SRE analyst. Analyze operational data for patterns. Return ONLY valid JSON.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.3,
                    'format': 'json'
                }
                resp = req.post(f"{url}/api/chat", json=payload, timeout=self.llm_timeout)
                text = resp.json().get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                if not api_key:
                    return
                payload = {
                    'model': model,
                    'messages': messages,
                    'temperature': 0.3,
                    'max_tokens': 2048,
                    'response_format': {'type': 'json_object'}
                }
                resp = req.post(
                    endpoint,
                    json=payload,
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                    timeout=60
                )
                text = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                if not api_key:
                    return
                payload = {
                    'model': model,
                    'max_tokens': 2048,
                    'system': 'You are an SRE analyst. Analyze operational data for patterns. Return ONLY valid JSON.',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                }
                resp = req.post(
                    'https://api.anthropic.com/v1/messages',
                    json=payload,
                    headers={'Content-Type': 'application/json', 'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                    timeout=60
                )
                text = '\n'.join(
                    b.get('text', '') for b in resp.json().get('content', [])
                    if b.get('type') == 'text'
                )
            else:
                return

            result = json.loads(text)
            insights = result.get('insights', [])

            stored = 0
            skipped = 0
            for insight in insights[:3]:
                if not insight.get('title') or not insight.get('description'):
                    continue
                # Drop insights with no concrete trigger condition — they can
                # never be retrieved on relevance, so they are pure noise.
                if not learning_has_trigger_condition(insight):
                    skipped += 1
                    logger.info(f"Skipping correlation insight without applies_when: {insight.get('title','')[:60]}")
                    continue
                insight.setdefault('learning_type', 'insight')
                insight.setdefault('tags', ['correlation', 'automated'])
                valid_types = {'pattern', 'solution', 'root_cause', 'antipattern', 'insight'}
                if insight['learning_type'] not in valid_types:
                    logger.warning(f"Invalid learning_type '{insight['learning_type']}', defaulting to 'insight'")
                    insight['learning_type'] = 'insight'
                try:
                    lid = self.kb.store_learning(insight)
                    stored += 1
                    if lid and lid > 0:
                        search_text = ' '.join(filter(None, [
                            insight.get('title', ''),
                            insight.get('description', ''),
                            str(insight.get('applies_when', '')),
                        ]))
                        self._embed_learning(lid, search_text)
                except Exception as e:
                    logger.warning(f"Failed to store correlation insight: {e}")
            if skipped:
                logger.info(f"Correlation analysis: skipped {skipped} insight(s) lacking a trigger condition")

            # Tier-2 noise routing: correlation insights are informational, not
            # actionable-now. By default they're stored as learnings (and rolled
            # into the morning summary) rather than paged real-time. Set
            # notifications.realtime_correlation_insights=true to page them.
            realtime_insights = bool(
                (self.config.get('notifications', {}) or {}).get('realtime_correlation_insights', False)
            ) if isinstance(self.config, dict) else False
            if stored and not realtime_insights:
                logger.info(f"Correlation analysis: {stored} insight(s) stored as learnings; "
                            f"real-time notification suppressed (digest)")
            elif stored:
                logger.info(f"Correlation analysis: {stored} insights stored as learnings")
                # Notify about correlation insights
                titles = [i.get('title', '') for i in insights[:3] if i.get('title')]
                summary = f"[Correlation] {stored} insight(s): " + "; ".join(titles)
                # Attribute which LLM produced these insights so operators can
                # tell whether they came from the cheap local model or a paid
                # fallback (cost attribution + debugging when models disagree).
                summary = f"{summary}\n_Generated by: {provider_type}/{model}_"
                for notif in self.notifications:
                    success = False
                    error_msg = None
                    try:
                        notif.send(summary, severity='info')
                        success = True
                    except Exception as e:
                        error_msg = str(e)
                        logger.warning(f"Correlation notification failed: {e}")
                    try:
                        channel_type = getattr(notif, 'channel_type', 'slack')
                        self.kb._kb.record_notification_history(
                            channel_id=0,
                            channel_type=channel_type,
                            severity='info',
                            title=summary[:200],
                            message=summary,
                            success=success,
                            context={'insights_count': stored},
                            error_message=error_msg
                        )
                    except Exception as e:
                        logger.debug(f"Could not record notification history: {e}")
            else:
                logger.info(f"Correlation analysis: LLM returned {len(insights)} insights (0 stored)")

        except json.JSONDecodeError as e:
            logger.warning(f"Correlation analysis LLM response not valid JSON: {e}")
        except Exception as e:
            logger.warning(f"Correlation analysis LLM call failed: {e}")

    def _get_infra_summary(self) -> str:
        """Build a concise summary of the infrastructure from config for LLM context."""
        hosts = self.config.get('infrastructure', {}).get('hosts', {})
        lines = []
        for name, info in hosts.items():
            addr = info.get('address', '?')
            role = info.get('role', '?')
            services = [s.get('name', '?') for s in info.get('services', [])]
            lines.append(f"  {name} ({addr}, {role}): {', '.join(services)}")

        if lines:
            summary = "Infrastructure hosts:\n" + "\n".join(lines)
        else:
            # No static inventory is the normal case for a fresh install: the
            # fleet is discovered from the cluster and from Prometheus instead.
            # Saying so beats emitting an empty "Infrastructure hosts:" header,
            # which reads to the model as "there are no hosts".
            summary = (
                "Infrastructure hosts: not statically configured — discover them "
                "from the live cluster and from Prometheus targets."
            )

        # Append active container runtimes
        if self.containers and hasattr(self.containers, 'runtime_names'):
            runtimes = ', '.join(self.containers.runtime_names)
            summary += f"\nContainer runtimes: {runtimes}."
            if 'kubernetes' in runtimes:
                summary += " Use k8s_* tools for pods/deployments and k8s_get_events for recent BackOff/readiness failures."
                k8s_summary = self._get_k8s_observation_summary()
                if k8s_summary:
                    summary += f"\n{k8s_summary}"

        return summary

    def _get_k8s_observation_summary(self) -> str:
        """Summarize recent Kubernetes signals so recovered failures remain visible to sweeps."""
        if not getattr(self.tools, 'k8s_tools', None):
            return ""

        lines = []

        try:
            ns_result = self.tools.k8s_tools.get_namespaces()
            if ns_result.get('success') and ns_result.get('namespaces'):
                namespace_names = [n.get('name') for n in ns_result['namespaces'] if n.get('name')]
                if namespace_names:
                    lines.append(f"Kubernetes namespaces: {', '.join(namespace_names)}")
        except Exception as e:
            logger.debug(f"Could not summarize Kubernetes namespaces: {e}")

        try:
            events_result = self.tools.k8s_tools.get_events(all_namespaces=True)
            if events_result.get('success') and events_result.get('events'):
                warning_events = [e for e in events_result['events'] if e.get('type') == 'Warning']
                if warning_events:
                    lines.append(
                        "Recent Kubernetes warning events (important: a pod can be Running now but still have recent BackOff/Unhealthy history):"
                    )
                    for event in warning_events[-8:]:
                        obj = event.get('object', 'unknown')
                        reason = event.get('reason', 'unknown')
                        message = str(event.get('message', '')).replace('\n', ' ').strip()
                        if len(message) > 180:
                            message = message[:177] + '...'
                        lines.append(f"  - {obj}: {reason} — {message}")
        except Exception as e:
            logger.debug(f"Could not summarize Kubernetes events: {e}")

        return "\n".join(lines)

    def _build_sweep_system_prompt(self, task: str, skill_name: Optional[str] = None) -> str:
        """Shared system prompt for sweep phases. Rules here apply to every phase.

        The 'no positive observations' rule is load-bearing — without it, models
        emit "[INFO] All nodes Ready" / "No errors in container X" lines as
        findings, which produces notification noise even when nothing is wrong.

        When `skill_name` names a loaded skill, that skill's procedure is
        injected as the step-by-step playbook for the phase. Sweep phases left
        to improvise re-list cluster state dozens of times and over-fetch logs;
        an explicit ordered procedure keeps the investigation bounded.
        """
        infra = self._get_infra_summary()

        procedure = ""
        skill = (self.skills or {}).get(skill_name) if skill_name else None
        if skill:
            procedure = f"""

PROCEDURE — follow these steps in order, and do not repeat a step whose data you already have:
{skill['instructions']}
"""
        elif skill_name:
            logger.warning(f"Sweep requested skill '{skill_name}' but it is not loaded")

        return f"""You are CFOperator performing a proactive infrastructure sweep.

{infra}

{task}
{procedure}

A "finding" is a problem that requires attention or action. Healthy state, "no errors found", "all nodes Ready", "no warnings in container X", and similar status statements are NOT findings — they are the expected default. If a sweep phase finds nothing wrong, the correct response is the empty array [].

Severity rules:
- "critical": active outage, data loss risk, security breach, or imminent failure.
- "warning": real degradation, recoverable failure, or risk that warrants action soon.
- "info": ONLY for genuine actionable observations the operator should know about (e.g. a deprecated config still in use, an unusual but non-failing pattern). Do NOT emit "info" for healthy state, absence of errors, or "everything looks fine" reports.

After investigating, respond with your findings as a JSON array:
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "exact tool output or data supporting this finding", "remediation": "suggested fix or action"}}]

The "evidence" field is REQUIRED — paste the specific metric value, log line, container name, or tool output that proves the finding. Do not make claims without evidence. If your evidence is "no problems detected" or "queries returned no errors", do NOT emit a finding — return [] for that phase instead.

If everything looks healthy, return an empty array: []
Only return the JSON array, no other text."""

    def _sweep_with_llm(self, task: str, max_iterations: int = None,
                        skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase. The LLM gets the task description,
        infrastructure context, and access to all tools (prometheus_query,
        loki_query, docker_list, ssh_execute, etc).

        `skill_name` optionally injects a loaded skill's procedure as the
        ordered playbook for the phase (see _build_sweep_system_prompt).

        Returns list of findings: [{'severity': ..., 'finding': ...}]
        """
        if max_iterations is None:
            max_iterations = self._get_sweep_max_iterations()

        # Check for sweep-specific backend/model override (DB settings)
        sweep_backend = self.kb.get_setting('sweep_backend', '')
        sweep_model = self.kb.get_setting('sweep_model', '')

        system_prompt = self._build_sweep_system_prompt(task, skill_name=skill_name)

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': task}],
                system_context=system_prompt,
                backend=sweep_backend or 'auto',
                model=sweep_model or None,
                max_iterations=max_iterations,
            )

            provider_type = result.get('backend', 'unknown')
            model = result.get('model', 'unknown')
            response_text = result.get('response', '')
            tool_calls = result.get('tool_calls', 0)
            input_tokens = result.get('input_tokens', 0)
            output_tokens = result.get('output_tokens', 0)
            cached_hits = result.get('cached_tool_hits', 0)
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
                f"{cached_hits} cached | "
                f"{len(response_text)} chars | "
                f"tokens: {input_tokens}in/{output_tokens}out"
            )

            # Parse findings from response
            return self._parse_sweep_findings(response_text)

        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                logger.warning("No LLM provider available for sweep — skipping")
                return []
            logger.error(f"Sweep LLM failed: {e}")
            ERROR_RATE.inc()
            return []
        except Exception as e:
            # All providers in the fallback chain exhausted with this exception.
            logger.error(f"Sweep LLM failed (all providers exhausted): {e}")
            ERROR_RATE.inc()
            return []

    # Patterns that indicate the LLM is reporting its own tool failures, not real
    # infrastructure issues.  Case-insensitive substring match on finding text.
    _SELF_REFERENTIAL_PATTERNS = [
        'unable to query',
        'could not query',
        'failed to query',
        'syntax error',
        'query syntax is invalid',
        'no logs could be retrieved',
        'loki query parser is failing',
        'literal not terminated',
        'could not retrieve logs',
        'unable to retrieve logs',
        'query failed due to',
        'logql query error',
        'errors prevent log analysis',
        'prevent log retrieval',
        'invalid logql',
        'logql queries',
        'log aggregation fail',
        'monitoring system is compromised',
        'monitoring tools',
        'query configuration',
    ]

    def _is_self_referential(self, finding_text: str) -> bool:
        """Return True if a finding is about the agent's own tool failures."""
        lower = finding_text.lower()
        return any(p in lower for p in self._SELF_REFERENTIAL_PATTERNS)

    def _parse_sweep_findings(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse LLM response into structured findings."""
        # Try to extract JSON array from the response
        text = response_text.strip()

        # Find JSON array in the response (may be wrapped in markdown code blocks)
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            try:
                findings = json.loads(json_match.group())
                if isinstance(findings, list):
                    # Validate each finding has required keys
                    valid = []
                    # Patterns that indicate tool errors, not infrastructure issues
                    tool_error_patterns = [
                        'not found', 'failed with',
                        'returned empty', 'no such', 'could not find',
                    ]
                    for f in findings:
                        if isinstance(f, dict) and 'finding' in f:
                            finding_text = str(f['finding'])
                            evidence_text = str(f.get('evidence', ''))
                            if self._is_self_referential(finding_text):
                                logger.info(f"Filtered self-referential finding: {finding_text[:120]}")
                                continue
                            # Filter findings with no evidence — likely hallucinated
                            if not evidence_text.strip():
                                logger.info(f"Filtered no-evidence finding: {finding_text[:120]}")
                                continue
                            # Filter findings that are tool/query errors, not real issues
                            finding_lower = finding_text.lower()
                            if any(p in finding_lower for p in tool_error_patterns):
                                logger.info(f"Filtered tool-error finding: {finding_text[:120]}")
                                continue
                            parsed = {
                                'severity': f.get('severity', 'info'),
                                'finding': finding_text
                            }
                            if evidence_text.strip():
                                parsed['evidence'] = evidence_text
                            if f.get('remediation'):
                                parsed['remediation'] = str(f['remediation'])
                            valid.append(parsed)
                    return valid
            except json.JSONDecodeError:
                pass

        # If JSON parsing failed but response has content, treat it as a single info finding
        # Filter out iteration-limit messages and self-referential tool failures
        if text and text != '[]' and 'Maximum tool iterations' not in text:
            if not self._is_self_referential(text):
                return [{'severity': 'info', 'finding': text[:500]}]

        return []

    def _sweep_metrics(self) -> List[Dict[str, Any]]:
        """Sweep metrics across the infrastructure using LLM analysis."""
        logger.info("Starting LLM-driven metric sweep")
        return self._sweep_with_llm(
            "Check the health of all infrastructure hosts and services by examining metrics. "
            "Look at resource usage, scrape targets, container health, and anything that looks off."
        )

    def _sweep_logs(self) -> List[Dict[str, Any]]:
        """Sweep logs across all services using LLM pattern detection."""
        logger.info("Starting LLM-driven log sweep")
        return self._sweep_with_llm(
            "Check recent logs across infrastructure services for errors, warnings, or concerning patterns. "
            "Use loki_query with correct LogQL syntax. "
            "CORRECT examples: "
            '(1) {namespace="apps"} |= "error"  '
            '(2) {namespace=~"apps|monitoring"} |~ "error|warning"  '
            '(3) {pod=~"cfoperator.*"} |= "error"  '
            '(4) {namespace="monitoring", container="prometheus"} |= "error".  '
            "Use =~ for multi-value matching. NEVER use || or -- between {} selectors. "
            "Each loki_query call must contain exactly ONE stream selector {}."
        )

    def _sweep_containers(self) -> List[Dict[str, Any]]:
        """Check all containers/pods across configured backends + LLM review."""
        findings = []
        containers = []

        # Determine active runtime names for LLM context
        runtime_label = "all configured backends"
        if hasattr(self.containers, 'runtime_names'):
            runtime_label = ', '.join(self.containers.runtime_names)

        # Direct container status check (fast, no LLM needed)
        try:
            containers = self.containers.list_containers()
            logger.info(f"Found {len(containers)} containers/pods across {runtime_label}")

            running_count = sum(1 for c in containers if c.get('status') == 'running')
            RUNNING_CONTAINERS.set(running_count)

            for container in containers:
                if container.get('status') != 'running':
                    findings.append({
                        'severity': 'warning',
                        'finding': f"{container['name']} on {container['host']}: status={container['status']}"
                    })

        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            ERROR_RATE.inc()

        # LLM review of container health
        container_summary = ""
        if containers:
            container_summary = f"\n\nCurrently running {running_count} of {len(containers)} containers/pods."
            stopped = [c for c in containers if c.get('status') != 'running']
            if stopped:
                container_summary += f"\nStopped/unhealthy: {', '.join(c['name'] for c in stopped)}"

        k8s_context = self._get_k8s_observation_summary()
        if k8s_context:
            container_summary += f"\n\n{k8s_context}"

        llm_findings = self._sweep_with_llm(
            f"Review workload health across the fleet (backends: {runtime_label}).{container_summary} "
            "Use k8s tools (k8s_get_pods, k8s_get_all_unhealthy, k8s_get_events) for Kubernetes workloads across apps, monitoring, data, iot, ai, infrastructure, and kube-system, "
            "loki_query for workload logs, prometheus_query for resource metrics, and ssh_list_services for bare-metal hosts. "
            "Do not rely only on current pod phase: recovered failures may appear only in recent Kubernetes warning events or Loki logs. "
            "Check for BackOff, Unhealthy/readiness failures, CrashLoopBackOff, and other issues. "
            "IMPORTANT: High restart counts alone are NOT findings if the pod is currently healthy and the last restart was hours/days ago. "
            "Only report restarts as issues if they are RECENT (last 2 hours) or ONGOING. Stale restart counts from past node reboots are normal. "
            "IMPORTANT: Identify workloads by their Deployment/StatefulSet/DaemonSet name, NOT by specific pod names. "
            "Pod names include random suffixes (e.g., -7b5b6c8d9f-xyz12) that change on every rollout. "
            "Never report a specific pod name as 'missing' — check the parent Deployment's ready replica count instead.",
            skill_name='k3s-cluster-health',
        )
        findings.extend(llm_findings)

        return findings

    def _sequential_sweep(self, sweep_config: dict) -> List[Dict[str, Any]]:
        """Run sweep phases sequentially (fallback when pool unavailable)."""
        from ollama_pool import SWEEP_DURATION
        start = time.time()
        findings = []

        if sweep_config.get('metrics') and self.metrics:
            logger.info("Sweeping metrics...")
            metric_findings = self._sweep_metrics()
            findings.extend(metric_findings)
            logger.info(f"Metric sweep found {len(metric_findings)} findings")

        if sweep_config.get('logs') and self.logs:
            logger.info("Sweeping logs...")
            log_findings = self._sweep_logs()
            findings.extend(log_findings)
            logger.info(f"Log sweep found {len(log_findings)} findings")

        if sweep_config.get('containers') and self.containers:
            logger.info("Sweeping containers...")
            container_findings = self._sweep_containers()
            findings.extend(container_findings)
            logger.info(f"Container sweep found {len(container_findings)} findings")

        SWEEP_DURATION.labels(mode='sequential').observe(time.time() - start)
        return findings

    def _sweep_with_llm_on_instance(self, task: str, url: str, model: str,
                                     max_iterations: int = None,
                                     skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase on a specific Ollama instance.

        Like _sweep_with_llm() but takes explicit url/model from pool checkout
        instead of resolving via _resolve_provider().
        """
        if max_iterations is None:
            max_iterations = self._get_sweep_max_iterations()

        provider_type = 'ollama'
        system_prompt = self._build_sweep_system_prompt(task, skill_name=skill_name)

        try:
            result = self._chat_with_tools(
                provider_type=provider_type,
                url=url,
                model=model,
                messages=[{'role': 'user', 'content': task}],
                system_context=system_prompt,
                max_iterations=max_iterations
            )

            response_text = result.get('response', '')
            tool_calls = result.get('tool_calls', 0)
            input_tokens = result.get('input_tokens', 0)
            output_tokens = result.get('output_tokens', 0)
            cached_hits = result.get('cached_tool_hits', 0)
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model}@{url} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
                f"{cached_hits} cached | "
                f"{len(response_text)} chars | "
                f"tokens: {input_tokens}in/{output_tokens}out"
            )

            return self._parse_sweep_findings(response_text)

        except Exception as e:
            logger.error(f"Sweep LLM failed on {url}/{model}: {e}")
            ERROR_RATE.inc()
            return []

    def _check_baseline_drift(self) -> List[Dict[str, Any]]:
        """Compare expected infrastructure state to reality."""
        findings = []

        try:
            # Get expected services from config
            hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
            expected_services = {}
            for host_name, host_info in hosts_config.items():
                for svc in host_info.get('services', []):
                    container = svc.get('container')
                    if container:
                        expected_services.setdefault(host_name, []).append({
                            'name': svc['name'],
                            'container': container
                        })

            # Get actually-running containers
            actual_containers = {}
            if self.containers:
                try:
                    for c in self.containers.list_containers():
                        host = c.get('host', 'unknown')
                        actual_containers.setdefault(host, set()).add(c['name'])
                except Exception as e:
                    logger.warning(f"Failed to list containers for drift check: {e}")

            # Compare expected vs actual
            has_docker_backend = any(
                c.get('backend') in ('docker', 'prometheus')
                for c in self._container_configs
            )
            for host_name, services in expected_services.items():
                host_info = hosts_config.get(host_name, {})
                host_addr = host_info.get('address', '')

                # Match Prometheus engine_host to config host by exact name or IP
                actual_names = set()
                for actual_host, containers in actual_containers.items():
                    if (actual_host == host_name or
                            actual_host == host_addr or
                            actual_host.split('.')[0] == host_name):
                        actual_names.update(containers)

                # If no data for this host, try SSH docker ps (only if a Docker-type backend is configured)
                if not actual_names and host_addr and has_docker_backend:
                    ssh_user = host_info.get('ssh', {}).get('user', 'sre')
                    try:
                        result = subprocess.run(
                            ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                             '-o', 'ConnectTimeout=5', f'{ssh_user}@{host_addr}',
                             'docker', 'ps', '--format', '{{.Names}}'],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            actual_names = {name.strip() for name in result.stdout.strip().split('\n') if name.strip()}
                            logger.debug(f"Drift check: SSH fallback for {host_name} found {len(actual_names)} containers")
                    except Exception as e:
                        logger.debug(f"Drift check: SSH fallback failed for {host_name}: {e}")

                for svc in services:
                    container_name = svc['container']
                    if actual_names and container_name not in actual_names:
                        findings.append({
                            'severity': 'warning',
                            'finding': f"Expected service '{svc['name']}' (container: {container_name}) not found running on {host_name}"
                        })

            # Bootstrap/update baselines
            self._update_baselines(actual_containers)

        except Exception as e:
            logger.error(f"Error checking baseline drift: {e}")
            ERROR_RATE.inc()

        return findings

    def _update_baselines(self, actual_containers: Dict[str, set]):
        """Update stored baselines with current state."""
        # Ephemeral Job/CronJob pods churn by schedule — strip them so they
        # never enter the baseline or register as container_change drift (which
        # would surface as false "stopped"/co-failure findings).
        actual_containers = {
            host: {c for c in containers if not is_ephemeral_job_pod(c)}
            for host, containers in (actual_containers or {}).items()
        }
        try:
            stored = self.kb.get_baseline()

            if not stored:
                # No baselines yet — bootstrap from current state
                for host, containers in actual_containers.items():
                    self.kb.update_baseline(
                        service_name=f"host:{host}",
                        expected_state='running',
                        baseline_metrics={
                            'container_count': len(containers),
                            'containers': sorted(containers)
                        }
                    )
                if actual_containers:
                    logger.info(f"Bootstrapped baselines for {len(actual_containers)} hosts")
            else:
                # Compare to stored baselines and record drift
                for host, containers in actual_containers.items():
                    key = f"host:{host}"
                    baseline = stored.get(key, {})
                    if baseline:
                        old_containers = set(baseline.get('baseline_metrics', {}).get('containers', []))
                        new_containers = set(containers)
                        added = new_containers - old_containers
                        removed = old_containers - new_containers

                        if added or removed:
                            desc_parts = []
                            if added:
                                desc_parts.append(f"new: {', '.join(sorted(added))}")
                            if removed:
                                desc_parts.append(f"gone: {', '.join(sorted(removed))}")

                            self.kb.record_drift_event(
                                drift_type='container_change',
                                description=f"{host}: {'; '.join(desc_parts)}",
                                drift_details={
                                    'host': host,
                                    'added': sorted(added),
                                    'removed': sorted(removed),
                                    'current_count': len(containers)
                                }
                            )
                            # Update baseline to current state
                            self.kb.update_baseline(
                                service_name=key,
                                expected_state='running',
                                baseline_metrics={
                                    'container_count': len(containers),
                                    'containers': sorted(containers)
                                }
                            )
                            logger.info(f"Drift detected on {host}: {'; '.join(desc_parts)}")

        except Exception as e:
            logger.warning(f"Baseline update failed: {e}")

    def _consolidate_learnings(self):
        """Periodically consolidate similar learnings by deprecating duplicates."""
        try:
            learnings = self.kb.find_learnings(limit=100)
            if len(learnings) < 10:
                return  # Not enough to consolidate
            logger.info(f"Consolidating {len(learnings)} learnings...")
            # Group by title similarity — deprecate exact title duplicates
            seen_titles = {}
            deprecated_count = 0
            for l in learnings:
                title_key = l['title'].lower().strip()
                if title_key in seen_titles:
                    self.kb._kb.deprecate_learning(l['id'])  # No resilient wrapper needed
                    deprecated_count += 1
                else:
                    seen_titles[title_key] = l['id']
            if deprecated_count:
                logger.info(f"Deprecated {deprecated_count} duplicate learnings")
        except Exception as e:
            logger.warning(f"Learning consolidation failed: {e}")

    def _extract_learnings(self, inv_id: int, trigger: str, findings: Dict[str, Any]):
        """Extract structured learnings from a resolved investigation using LLM."""
        import requests as req

        try:
            resolved = self._resolve_provider()
            if not resolved:
                logger.warning("No LLM provider available for learning extraction")
                return

            provider_type, url, model = resolved

            prompt = f"""Analyze this resolved infrastructure investigation and extract 1-3 reusable learnings.

Investigation trigger: {trigger}
Findings: {json.dumps(findings, default=str)[:2000]}

Return ONLY valid JSON in this exact format:
{{"learnings": [
  {{
    "learning_type": "solution",
    "title": "Brief title (max 100 chars)",
    "description": "What was learned and how it was resolved",
    "applies_when": "The concrete, observable condition that should make a future investigation recall this (e.g. 'pod faster-whisper is OOMKilled', 'cfoperator restart alert with no matching k8s event'). REQUIRED.",
    "services": ["service1"],
    "tags": ["tag1", "tag2"],
    "category": "resource"
  }}
]}}

learning_type must be one of: solution, pattern, root_cause, antipattern, insight
category must be one of: resource, network, config, dependency
Keep learnings specific and actionable. Only extract a learning if there is genuine,
reusable insight — a root cause, a fix, or a non-obvious gotcha. Do NOT extract a
learning that just restates "X was healthy" or describes a one-off transient blip.
Every learning MUST have a non-empty, specific `applies_when`; omit any you cannot
write a real trigger condition for. Return {{"learnings": []}} if nothing qualifies."""

            messages = [
                {'role': 'system', 'content': 'You are a structured data extractor. Return ONLY valid JSON.'},
                {'role': 'user', 'content': prompt}
            ]

            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.3,
                    'format': 'json'
                }
                resp = req.post(f"{url}/api/chat", json=payload, timeout=self.llm_timeout)
                data = resp.json()
                text = data.get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                if not api_key:
                    key_env = OPENAI_COMPAT_PROVIDERS[provider_type]['key_env']
                    logger.warning(f"{key_env} not set for learning extraction")
                    return
                payload = {
                    'model': model,
                    'messages': messages,
                    'temperature': 0.3,
                    'max_tokens': 2048,
                    'response_format': {'type': 'json_object'}
                }
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
                resp = req.post(
                    endpoint,
                    json=payload, headers=headers, timeout=60
                )
                data = resp.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                if not api_key:
                    logger.warning("ANTHROPIC_API_KEY not set for learning extraction")
                    return
                payload = {
                    'model': model,
                    'max_tokens': 2048,
                    'system': 'You are a structured data extractor. Return ONLY valid JSON.',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                }
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01'
                }
                resp = req.post(
                    'https://api.anthropic.com/v1/messages',
                    json=payload, headers=headers, timeout=60
                )
                data = resp.json()
                text = '\n'.join(
                    b.get('text', '') for b in data.get('content', [])
                    if b.get('type') == 'text'
                )
            else:
                logger.warning(f"Learning extraction not implemented for {provider_type}")
                return

            # Parse JSON response
            result = json.loads(text)
            learnings = result.get('learnings', [])

            stored = 0
            skipped = 0
            for learning_data in learnings[:3]:  # Cap at 3
                learning_data['investigation_id'] = inv_id
                if not learning_data.get('learning_type') or not learning_data.get('title'):
                    continue
                if not learning_has_trigger_condition(learning_data):
                    skipped += 1
                    logger.info(f"Skipping extracted learning without applies_when: {learning_data.get('title','')[:60]}")
                    continue
                try:
                    lid = self.kb.store_learning(learning_data)
                    stored += 1
                    logger.info(f"Learning extracted: [{learning_data['learning_type']}] {learning_data['title'][:60]}")
                    # Generate embedding for the learning
                    if lid and lid > 0:
                        search_text = ' '.join(filter(None, [
                            learning_data.get('title', ''),
                            learning_data.get('description', ''),
                            learning_data.get('applies_when', ''),
                        ]))
                        self._embed_learning(lid, search_text)
                except Exception as e:
                    logger.warning(f"Failed to store learning: {e}")

            if stored:
                logger.info(f"Extracted {stored} learnings from investigation #{inv_id}")

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse learning extraction response: {e}")
        except Exception as e:
            logger.warning(f"Learning extraction failed for investigation #{inv_id}: {e}")

    def _embed_investigation(self, inv_id: int, trigger: str, findings: Dict[str, Any], outcome: str):
        """Generate and store embedding for a completed investigation."""
        try:
            if not self.embeddings.is_available():
                return

            investigation_data = {
                'trigger': trigger,
                'findings': findings,
                'outcome': outcome
            }
            embedding_text = self.embeddings.create_investigation_text(investigation_data)
            if not embedding_text or len(embedding_text) < 10:
                return

            embedding = self.embeddings.generate_embedding(embedding_text)
            if not embedding:
                return

            self.kb._kb.store_investigation_embedding(
                investigation_id=inv_id,
                embedding=embedding,
                embedding_model=self.embeddings.model,
                embedding_text=embedding_text
            )
            logger.info(f"Embedding stored for investigation #{inv_id}")
            EMBEDDING_REQUESTS.labels(result='success').inc()
        except Exception as e:
            logger.warning(f"Embedding generation failed for investigation #{inv_id}: {e}")
            EMBEDDING_REQUESTS.labels(result='error').inc()

    def _embed_learning(self, learning_id: int, search_text: str):
        """Generate and store embedding for a learning."""
        try:
            if not self.embeddings.is_available():
                return

            embedding = self.embeddings.generate_embedding(search_text)
            if not embedding:
                return

            from sqlalchemy import text as sql_text
            embedding_str = vector_literal(embedding)
            with self.kb._kb.session_scope() as session:
                session.execute(sql_text("""
                    UPDATE investigation_learnings
                    SET embedding_hash = :hash
                    WHERE id = :lid
                """), {'hash': hashlib.md5(search_text.encode()).hexdigest(), 'lid': learning_id})
                # Store in embedding cache for retrieval during search
                session.execute(sql_text("""
                    INSERT INTO learning_embeddings (learning_id, embedding, embedding_model, embedding_text)
                    VALUES (:lid, :embedding, :model, :text)
                    ON CONFLICT (learning_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_text = EXCLUDED.embedding_text
                """), {
                    'lid': learning_id,
                    'embedding': embedding_str,
                    'model': self.embeddings.model,
                    'text': search_text
                })
                session.commit()
            logger.info(f"Embedding stored for learning #{learning_id}")
        except Exception as e:
            logger.debug(f"Learning embedding failed for #{learning_id}: {e}")

    @staticmethod
    def _finding_key(finding: Dict[str, Any]) -> str:
        """Produce a stable key for dedup by stripping variable parts (numbers, timestamps)."""
        import re
        text = finding.get('finding', '')
        # Strip numbers (counts, ports, timestamps change across sweeps)
        text = re.sub(r'\d+', '#', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip().lower()
        # Take first 120 chars — enough to identify the issue
        return text[:120]

    def _dedup_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate findings across sweep phases.

        When multiple phases report the same issue, keep the one with highest severity.
        """
        severity_rank = {'critical': 3, 'warning': 2, 'info': 1}
        seen = {}  # key -> finding
        for f in findings:
            key = self._finding_key(f)
            existing = seen.get(key)
            if not existing or severity_rank.get(f.get('severity', 'info'), 0) > severity_rank.get(existing.get('severity', 'info'), 0):
                seen[key] = f
        deduped = list(seen.values())
        if len(deduped) < len(findings):
            logger.info(f"Deduplicated {len(findings)} findings to {len(deduped)}")
        return deduped

    # Tokens that look like real workload identifiers but are too generic to
    # match safely against pod/deployment names. Kept narrow on purpose — only
    # words the sweep prompts themselves use as scaffolding, not domain nouns.
    _GROUND_TRUTH_STOPWORDS = frozenset({
        'active', 'apparent', 'apps', 'cluster', 'config', 'configuration',
        'container', 'containers', 'control', 'critical', 'data', 'default',
        'degraded', 'deploy', 'deployed', 'deployment', 'docker', 'evidence',
        'expectations', 'expected', 'failed', 'failing', 'feature', 'finding',
        'found', 'health', 'healthy', 'history', 'image', 'images',
        'infrastructure', 'ingress', 'ingresses', 'install', 'installed',
        'instance', 'issue', 'issues', 'kubelet', 'logs', 'master', 'masters',
        'memory', 'metric', 'metrics', 'missing', 'monitoring', 'name',
        'namespace', 'namespaces', 'network', 'node', 'nodes', 'normal',
        'operator', 'overall', 'plane', 'pod', 'pods', 'pressure', 'primary',
        'production', 'project', 'prometheus', 'ready', 'related', 'remediation',
        'report', 'restart', 'restarts', 'running', 'scrape', 'service',
        'services', 'severity', 'should', 'stability', 'stable', 'status',
        'storage', 'system', 'systems', 'target', 'targets', 'unhealthy',
        'unstable', 'verify', 'warning', 'workload', 'workloads',
    })

    _MISSING_KEYWORDS = (
        'not installed', 'not running', 'not present', 'not deployed',
        'no active', 'no such', 'does not have', "doesn't have",
        'is missing', 'are missing', 'not found',
    )

    _NODE_HEALTH_KEYWORDS = (
        'kubelet', 'service issue', 'service is not', 'service not',
        'unhealthy', 'unstable', 'pressure', 'degraded', 'stability',
        'not running', 'not ready', 'notready', 'ready condition',
        'status=false', 'status="false"', 'condition false', 'down',
    )

    # Pattern 3: metrics sweep reads an empty prometheus_query result and
    # concludes the workload is not being scraped, even though the pod/service
    # exists and is almost certainly a Prometheus target.
    _SCRAPE_TARGET_KEYWORDS = (
        'not scraping', 'not being scraped', 'no scrape target', 'missing scrape target',
        'no metrics for', 'not reporting metrics', 'no active scrape', 'no targets for',
    )

    # Pattern 4: sweep claims a node is absent/unregistered when it is present
    # in kubectl get nodes (metrics sweep may read a stale kube_node_info
    # series as evidence that the node no longer exists).
    _NODE_ABSENT_KEYWORDS = (
        'not in cluster', 'not joined', 'missing from cluster', 'not part of cluster',
        'node missing', 'node not present', 'node not found', 'not registered',
    )

    # Pattern 5: containers sweep uses k8s_get_ingresses (added 2026-04-30)
    # and reports a service as unexposed when the tool returns empty for a
    # name-mismatch query, even though a matching ingress exists.
    _EXPOSURE_KEYWORDS = (
        'not exposed', 'has no ingress', 'no ingress for', 'not publicly accessible',
        'no external access', 'not reachable externally', 'no ingress rule',
        'not accessible externally',
    )

    def _ground_truth_snapshot(self) -> Optional[Dict[str, Any]]:
        """Pull a single cluster snapshot used to disprove obvious false positives.

        Returns None if K8sTools isn't wired up (tests, partial bootstrap),
        which makes the suppressor a no-op.
        """
        k8s = getattr(getattr(self, 'tools', None), 'k8s_tools', None)
        if not k8s:
            return None

        snapshot: Dict[str, Any] = {'nodes': {}, 'workloads': set(), 'ingresses': set()}

        try:
            nodes_result = k8s.get_nodes()
            if nodes_result.get('success'):
                for n in nodes_result.get('nodes', []):
                    name = n.get('name')
                    if name:
                        snapshot['nodes'][name.lower()] = n
        except Exception as e:
            logger.debug(f"Ground truth: could not load nodes: {e}")

        try:
            # Single broad lookup covering everything a sweep might claim is "missing".
            result = k8s._run_kubectl(
                ['get',
                 'pods,deployments,daemonsets,statefulsets,cronjobs,jobs,services,ingresses',
                 '-A', '-o', 'name'],
                timeout=15,
            )
            if result.get('success'):
                for line in result.get('stdout', '').splitlines():
                    # Lines look like "pod/river-history-ingest-29625252-8ltx8"
                    if '/' in line:
                        kind, resource_name = line.split('/', 1)
                        resource_name = resource_name.strip().lower()
                        if resource_name:
                            snapshot['workloads'].add(resource_name)
                            if kind.strip().lower() == 'ingress':
                                snapshot['ingresses'].add(resource_name)
        except Exception as e:
            logger.debug(f"Ground truth: could not load workloads: {e}")

        return snapshot

    def _match_workload_in_text(self, text: str,
                                snapshot: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Find a cluster workload named in ``text``.

        Returns the matching (token, workload_name), or None. Tokens are the
        dashed and ≥4-char words of the text minus the stopword list, so a
        claim naming a real pod/deployment can be disproved by the snapshot.
        """
        tokens: set = set()
        tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
        tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
        tokens -= self._GROUND_TRUTH_STOPWORDS

        for token in tokens:
            if len(token) < 4:
                continue
            for workload in snapshot['workloads']:
                if token == workload or token in workload.split('-'):
                    return token, workload
        return None

    def _ground_truth_suppress(self,
                               finding: Dict[str, Any],
                               snapshot: Dict[str, Any]) -> Optional[str]:
        """Return a reason string if the cluster snapshot disproves the finding."""
        if not snapshot:
            return None

        text = (str(finding.get('finding', '')) + ' '
                + str(finding.get('evidence', ''))).lower()
        if not text.strip():
            return None

        # Pattern 1: claim asserts a node-level health/kubelet problem, but the
        # node is actually Ready with no pressure. k3s embeds the kubelet, so a
        # missing kubelet.service is expected and not a real finding.
        for node_name, node in snapshot['nodes'].items():
            if node_name in text and any(k in text for k in self._NODE_HEALTH_KEYWORDS):
                ready = node.get('ready') == 'True'
                mem_ok = node.get('memoryPressure') in ('False', 'Unknown', None)
                disk_ok = node.get('diskPressure') in ('False', 'Unknown', None)
                if ready and mem_ok and disk_ok:
                    return (
                        f"node {node_name} reports Ready=True with no pressure "
                        f"(kubelet {node.get('kubeletVersion','?')}); "
                        f"k3s embeds the kubelet so a standalone kubelet.service is expected to be absent"
                    )

        # Pattern 2: claim asserts a workload is missing, but a matching pod /
        # deployment / cronjob / service / ingress exists in the cluster.
        if any(k in text for k in self._MISSING_KEYWORDS):
            matched = self._match_workload_in_text(text, snapshot)
            if matched:
                token, workload = matched
                return (
                    f"workload matching '{token}' exists in cluster "
                    f"({workload})"
                )

        # Pattern 3: claim asserts a workload is not being scraped by Prometheus
        # or has no metrics, but the named pod/service exists. The metrics sweep
        # commonly reads an empty prometheus_query result as "target absent"
        # rather than "series has no recent data".
        if any(k in text for k in self._SCRAPE_TARGET_KEYWORDS):
            matched = self._match_workload_in_text(text, snapshot)
            if matched:
                token, workload = matched
                return (
                    f"workload matching '{token}' exists in cluster "
                    f"({workload}); an empty prometheus_query result does not confirm the target is absent"
                )

        # Pattern 4: claim asserts a node is absent from / not registered in
        # the cluster, but the node appears in the snapshot. The metrics sweep
        # may misread a stale kube_node_info series as "node missing".
        if any(k in text for k in self._NODE_ABSENT_KEYWORDS):
            for node_name in snapshot['nodes']:
                if node_name in text:
                    return (
                        f"node '{node_name}' is present in the cluster "
                        f"(confirmed in kubectl get nodes snapshot)"
                    )

        # Pattern 5: claim asserts a service has no ingress / is not externally
        # accessible, but a matching Ingress resource exists. Triggered by
        # k8s_get_ingresses returning empty on a name-mismatch query, causing
        # the sweep to conclude the service is unexposed. Only fires on ingress
        # name matches (not pods/services) to avoid over-suppression.
        if any(k in text for k in self._EXPOSURE_KEYWORDS):
            tokens = set()
            tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
            tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
            tokens -= self._GROUND_TRUTH_STOPWORDS

            for token in tokens:
                if len(token) < 4:
                    continue
                for ingress_name in snapshot.get('ingresses', set()):
                    if token == ingress_name or token in ingress_name.split('-'):
                        return (
                            f"ingress matching '{token}' exists in cluster "
                            f"({ingress_name}); service exposure claim is likely a false positive"
                        )

        # Pattern 6: a "container restarted N times" finding where the pod is
        # healthy now with few restarts is recovered noise, not a real finding.
        # (The sweep-path analogue of the Tier-1 alert filter.)
        noise_cfg = (self.config.get('ooda', {}) or {}).get('noise', {}) if isinstance(self.config, dict) else {}
        if noise_cfg.get('enabled', True):
            reason = self._restart_finding_is_noise(text, int(noise_cfg.get('recovered_restart_threshold', 3)))
            if reason:
                return reason

        return None

    def _verify_single_finding(self,
                               finding: Dict[str, Any],
                               max_iterations: int) -> Optional[Dict[str, Any]]:
        """Actively try to disprove a finding before allowing it to be emitted."""
        infra = self._get_infra_summary()
        system_prompt = f"""You are a strict verification agent for infrastructure monitoring findings.

{infra}

Your job is to try to DISPROVE a drafted finding before it is emitted.

Verification procedure:
1. Read the drafted finding and its current evidence.
2. Identify the strongest counter-hypothesis that would make the finding false.
3. Use the available tools to test that counter-hypothesis before deciding. You MUST make at least one tool call before your final answer.
4. Keep the finding only if the fresh tool results still support it.

Rules:
- Prefer direct disproof queries over repeating the original evidence.
- Verify exact Kubernetes namespace, pod, service, ingress, deployment, and container names before trusting a claim.
- For missing exposure or routing claims, inspect Services and Ingresses in the relevant namespace before keeping the finding.
- For log-absence or missing-container claims, resolve the real pod/container identity first, then inspect logs or pod status.
- If the fresh query disproves the claim, if names do not match, if support is ambiguous, or if you cannot verify confidently, return [].
- Never report tool/query failures as findings.

Return ONLY a JSON array:
[]
or
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "fresh evidence from verification", "remediation": "suggested fix or action"}}]

Only return the JSON array, no other text."""

        user_msg = (
            "Actively verify this drafted finding before it can be emitted. "
            "Try to falsify it with fresh tool queries, then return [] if it does not survive verification.\n\n"
            f"Draft finding JSON:\n{json.dumps(finding, default=str)}"
        )

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_msg}],
                system_context=system_prompt,
                max_iterations=max_iterations,
            )
        except Exception as e:
            logger.warning(f"Verification skipped (LLM unavailable): {e}")
            return finding  # don't filter on a failed verification step

        tool_calls = result.get('tool_calls', 0)
        if tool_calls <= 0:
            logger.info(f"Verification dropped finding with no fresh checks: {finding.get('finding', '')[:150]}")
            return None

        verified = self._parse_sweep_findings(result.get('response', ''))
        if not verified:
            return None

        verified_finding = verified[0]
        if not verified_finding.get('remediation') and finding.get('remediation'):
            verified_finding['remediation'] = finding['remediation']
        return verified_finding

    def _verify_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Active verification pass to filter hallucinated or unsupported findings.

        Re-checks each finding individually with tool access and asks the model
        to actively look for the strongest disconfirming signal before keeping it.
        Graceful degradation: returns original findings if verification fails.
        """
        if not findings:
            return findings

        # Stage 1: deterministic ground-truth suppressor. Cheap, cluster-state
        # based, and catches the common LLM hallucinations (k3s embeds-kubelet,
        # CronJob workloads claimed missing). Skipped silently when K8sTools
        # isn't available (e.g. tests).
        snapshot = self._ground_truth_snapshot()
        if snapshot:
            survivors = []
            for f in findings:
                reason = self._ground_truth_suppress(f, snapshot)
                if reason:
                    logger.info(
                        f"Ground-truth suppressed: {str(f.get('finding',''))[:140]} — {reason}"
                    )
                    continue
                survivors.append(f)
            suppressed = len(findings) - len(survivors)
            if suppressed:
                logger.info(
                    f"Ground-truth filter: {len(findings)} → {len(survivors)} ({suppressed} suppressed)"
                )
            findings = survivors
            if not findings:
                return findings

        max_iterations = max(2, min(4, self._get_max_tool_iterations()))

        try:
            verified = []
            for finding in findings:
                verified_finding = self._verify_single_finding(
                    finding=finding,
                    max_iterations=max_iterations,
                )
                if verified_finding:
                    verified.append(verified_finding)

            removed = len(findings) - len(verified)
            logger.info(f"Finding verification: {len(findings)} → {len(verified)} ({removed} filtered)")

            if removed > 0:
                # Log which findings were filtered
                verified_texts = {v['finding'] for v in verified}
                for f in findings:
                    if f['finding'] not in verified_texts:
                        logger.info(f"Judge filtered: {f['finding'][:150]}")

            return verified

        except Exception as e:
            logger.warning(f"Finding verification failed, returning unfiltered: {e}")
            return findings

    def _get_new_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return only findings that weren't in the previous sweep report."""
        try:
            prev_reports = self.kb.get_recent_sweep_reports(limit=1)
            if not prev_reports:
                return findings  # First sweep — everything is new
            prev_keys = {self._finding_key(f) for f in prev_reports[0].get('findings', [])}
            new = [f for f in findings if self._finding_key(f) not in prev_keys]
            return new
        except Exception as e:
            logger.debug(f"Could not check previous sweep for dedup: {e}")
            return findings  # On error, notify for everything

    def _get_resolved_findings(self, current_findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return findings present in the previous sweep that are now gone.

        Used to emit "Resolved: …" notifications so operators see clear
        outcomes, not just the firing edge. Returns empty on first sweep
        (no baseline to diff against) or when the previous-sweep lookup
        fails — both cases are safer than emitting bogus resolutions.
        """
        try:
            prev_reports = self.kb.get_recent_sweep_reports(limit=1)
            if not prev_reports:
                return []
            prev_findings = prev_reports[0].get('findings', []) or []
            current_keys = {self._finding_key(f) for f in current_findings}
            return [f for f in prev_findings if self._finding_key(f) not in current_keys]
        except Exception as e:
            logger.debug(f"Could not compute resolved findings: {e}")
            return []

    def _capture_metric_snapshot(self) -> Optional[Dict[str, Any]]:
        """Capture key cluster metrics for correlation baseline."""
        snapshot = {}
        try:
            if self.metrics:
                # Node resource usage
                cpu_result = self.metrics.query('100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle",job="node"}[5m])) * 100)')
                if cpu_result:
                    snapshot['node_cpu_percent'] = {r['metric'].get('instance', '?'): round(float(r['value'][1]), 1) for r in cpu_result}

                mem_result = self.metrics.query('(1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"}) * 100')
                if mem_result:
                    snapshot['node_memory_percent'] = {r['metric'].get('instance', '?'): round(float(r['value'][1]), 1) for r in mem_result}

                # Pod counts by phase
                phase_result = self.metrics.query('sum by (phase) (kube_pod_status_phase)')
                if phase_result:
                    snapshot['pod_phases'] = {r['metric'].get('phase', '?'): int(float(r['value'][1])) for r in phase_result}

                # Container restart total
                restart_result = self.metrics.query('sum(increase(kube_pod_container_status_restarts_total[30m]))')
                if restart_result:
                    snapshot['restarts_30m'] = round(float(restart_result[0]['value'][1]), 1)

        except Exception as e:
            logger.debug(f"Metric snapshot partial failure: {e}")

        return snapshot if snapshot else None

    def _generate_sweep_report(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate summary report from sweep findings."""
        max_severity = 'info'
        if any(f.get('severity') == 'critical' for f in findings):
            max_severity = 'critical'
        elif any(f.get('severity') == 'warning' for f in findings):
            max_severity = 'warning'

        summary = f"System sweep found {len(findings)} issues:\n"
        for f in findings:
            summary += f"- [{f.get('severity', 'info').upper()}] {f.get('finding', '')}"
            if f.get('remediation'):
                summary += f"\n  -> {f['remediation']}"
            summary += "\n"

        sweep_backend = self.kb.get_setting('sweep_backend', '')
        sweep_model = self.kb.get_setting('sweep_model', '')

        return {
            'timestamp': datetime.now(),
            'findings': findings,
            'summary': summary,
            'severity': max_severity,
            'sweep_meta': {
                'sweep_backend': sweep_backend or 'default',
                'sweep_model': sweep_model or 'default',
            }
        }

    def _post_findings_to_event_runtime(self, findings: List[Dict[str, Any]]) -> None:
        """Post sweep findings as alerts to the event runtime if configured."""
        url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if not url:
            return
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        endpoint = f"{url.rstrip('/')}/alert?mode=async"
        # Honor operator dismissals: don't re-post findings marked
        # acknowledged/false_positive — they'd otherwise recur every sweep.
        try:
            dismissed = self.kb._kb.get_dismissed_finding_keys()
        except Exception:
            dismissed = set()
        for finding in findings:
            summary_text = str(finding.get("finding") or finding.get("summary") or "").strip()
            fid = finding.get("id") or hashlib.md5(
                (finding.get("finding", "") + finding.get("sweep_phase", "")).encode()
            ).hexdigest()[:8]
            sig = 'sig::' + normalize_finding_signature(summary_text)
            if dismissed and (fid in dismissed or sig in dismissed
                              or (summary_text and summary_text in dismissed)):
                logger.info(f"Skipping dismissed finding (acknowledged/false_positive/known-noise): {summary_text[:80]}")
                continue
            severity = str(finding.get("severity") or "info").lower()
            if severity not in ("info", "warning", "critical"):
                severity = "warning"
            payload = {
                "source": "cfoperator-sweep",
                "severity": severity,
                "summary": str(finding.get("finding") or finding.get("summary") or "sweep finding"),
                "namespace": finding.get("namespace"),
                "resource_type": finding.get("resource_type"),
                "resource_name": finding.get("resource_name") or finding.get("resource"),
                "details": {
                    "category": finding.get("category"),
                    "remediation": finding.get("remediation"),
                    "evidence": finding.get("evidence"),
                    "sweep_source": finding.get("source"),
                },
            }
            body = json.dumps(payload, default=str).encode("utf-8")
            try:
                req = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=5) as resp:
                    resp.read()
            except (URLError, TimeoutError, OSError) as exc:
                logger.debug(f"Failed to post finding to event runtime: {exc}")
                return  # Stop trying on first failure

    def _post_resolutions_to_event_runtime(self, resolved: List[Dict[str, Any]]) -> None:
        """Post 'finding cleared' notifications to the event runtime.

        These ride the same /alert path as live findings but are tagged
        with ``details.resolution=True`` so:
          - run_triage short-circuits to action=notify (no LLM spend),
          - the Slack formatter renders ":white_check_mark: Resolved: …"
            instead of the "[severity]" prefix.
        Severity is forced to info — a resolution is by definition not
        a firing alert.
        """
        url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if not url or not resolved:
            return
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        endpoint = f"{url.rstrip('/')}/alert?mode=async"
        for finding in resolved:
            payload = {
                "source": "cfoperator-sweep",
                "severity": "info",
                "summary": str(finding.get("finding") or finding.get("summary") or "sweep finding"),
                "namespace": finding.get("namespace"),
                "resource_type": finding.get("resource_type"),
                "resource_name": finding.get("resource_name") or finding.get("resource"),
                "details": {
                    "resolution": True,
                    "requested_action": "notify",
                    "category": finding.get("category"),
                    "sweep_source": finding.get("source"),
                },
            }
            body = json.dumps(payload, default=str).encode("utf-8")
            try:
                req = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=5) as resp:
                    resp.read()
            except (URLError, TimeoutError, OSError) as exc:
                logger.debug(f"Failed to post resolution to event runtime: {exc}")
                return

    def _notify_sweep_findings(self, report: Dict[str, Any]):
        """Send notifications for sweep findings and record in history.

        When CFOP_EVENT_RUNTIME_URL is set, the event runtime is the sole
        owner of Slack/Discord for sweep findings: each finding is already
        forwarded to /alert by _post_findings_to_event_runtime and triaged
        individually, so emitting a roll-up here would produce duplicate
        (and lower-fidelity) Slack messages. We still record one
        notification_history row so audit/UI counters reflect that the
        sweep produced operator-visible output.
        """
        event_runtime_url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if event_runtime_url:
            try:
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type='event-runtime',
                    severity=report['severity'],
                    title=report['summary'][:200],
                    message=report['summary'],
                    success=True,
                    context={
                        'findings_count': len(report.get('findings', [])),
                        'delegated_to': 'event_runtime',
                    },
                    error_message=None,
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")
            return

        for notif in self.notifications:
            success = False
            error_msg = None
            try:
                notif.send(report['summary'], severity=report['severity'])
                success = True
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending notification: {e}")

            # Record in notification_history
            try:
                channel_type = getattr(notif, 'channel_type', 'slack')
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type=channel_type,
                    severity=report['severity'],
                    title=report['summary'][:200],
                    message=report['summary'],
                    success=success,
                    context={'findings_count': len(report.get('findings', []))},
                    error_message=error_msg
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")

    def _get_alert_check_interval(self) -> int:
        """Get alert check interval: DB setting → config.yaml → default 10."""
        try:
            val = self.kb.get_setting('alert_check_interval', '')
            if val:
                return max(5, min(300, int(val)))
        except Exception as e:
            logger.debug(f"Invalid alert_check_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('alert_check_interval', 10)

    def _get_sweep_interval(self) -> int:
        """Get sweep interval: DB setting → config.yaml → default 1800."""
        try:
            val = self.kb.get_setting('sweep_interval', '')
            if val:
                return max(60, min(86400, int(val)))
        except Exception as e:
            logger.debug(f"Invalid sweep_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('sweep_interval', 1800)

    def _get_reap_interval(self) -> int:
        """Remediation reaper interval: DB setting → config.yaml → default 300."""
        try:
            val = self.kb.get_setting('remediation_reap_interval', '')
            if val:
                return max(60, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_reap_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_reap_interval_seconds', 300)

    def _get_drain_interval(self) -> int:
        """Remediation drainer interval: DB setting → config.yaml → default 60."""
        try:
            val = self.kb.get_setting('remediation_drain_interval', '')
            if val:
                return max(10, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_drain_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_drain_interval_seconds', 60)

    def _get_verify_interval(self) -> int:
        """Remediation PR-reconcile interval: DB setting → config.yaml → default 300."""
        try:
            val = self.kb.get_setting('remediation_verify_interval', '')
            if val:
                return max(30, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_verify_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_verify_interval_seconds', 300)

    def _format_heartbeat(self) -> str:
        """Build a one-line OODA heartbeat summary for periodic log emission.

        Single grep target ("OODA heartbeat:") plus the key fields an operator
        wants when checking "is the agent alive between events": uptime, the
        HTTP investigation queue depth (rising → worker saturated), minutes
        since last proactive sweep, and whether the reactive Alertmanager
        poll is on (true means the agent is independent of event_runtime).
        """
        uptime_min = (time.time() - self.start_time) / 60.0
        sweep_age = time.time() - self.last_sweep if self.last_sweep else None
        sweep_label = f"{sweep_age/60:.0f}m ago" if sweep_age is not None else "never"
        queue_depth = self._investigation_queue.qsize() if getattr(self, "_investigation_queue", None) else 0
        return (
            f"OODA heartbeat: uptime={uptime_min:.0f}m "
            f"queue_depth={queue_depth} "
            f"last_sweep={sweep_label} "
            f"reactive_poll={'on' if self._reactive_poll_enabled else 'off'}"
        )

    def _get_heartbeat_interval(self) -> int:
        """Get OODA-loop heartbeat interval: DB setting → config.yaml → default 300 (5 min).

        Heartbeats prove the loop is alive when nothing else is logging — with
        ``reactive_poll: false`` the agent can otherwise go silent for the
        full sweep interval (30 min by default) between HTTP investigations.
        """
        try:
            val = self.kb.get_setting('heartbeat_interval', '')
            if val:
                return max(30, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid heartbeat_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('heartbeat_interval_seconds', 300)

    # Slash shortcut expansions — map short commands to natural language prompts
    _SLASH_SHORTCUTS = {
        '/sweeps': 'Show me the recent sweep reports with findings summaries.',
        '/stats': 'Give me the operational summary for the last {0} hours.',
        '/investigations': 'List recent investigations with their triggers and outcomes.',
        '/correlations': 'Show me correlated events and service failure patterns.',
    }

    def _expand_slash_shortcut(self, message: str) -> str:
        """Expand slash shortcuts into natural language prompts.
        Returns the original message if not a shortcut."""
        if not message.startswith('/'):
            return message
        parts = message.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''
        template = self._SLASH_SHORTCUTS.get(cmd)
        if template:
            if '{0}' in template and args:
                return template.format(args)
            elif '{0}' in template:
                return template.format('24')
            return template
        return message

    def _get_max_tool_iterations(self) -> int:
        """Get max tool iterations: DB setting → config.yaml → default 10."""
        try:
            val = self.kb.get_setting('max_tool_iterations', '')
            if val:
                return max(1, min(50, int(val)))
        except Exception as e:
            logger.debug(f"Invalid max_tool_iterations setting, using default: {e}")
        return self.config.get('chat', {}).get('max_tool_iterations', 10)

    def _get_sweep_max_iterations(self) -> int:
        """Iteration cap for sweep phases.

        Sweep phases are bounded data-gathering tasks, not open-ended chat — a
        handful of tool calls is enough to inspect metrics/logs/containers. The
        global `max_tool_iterations` (used for interactive chat) was letting
        sweep phases loop up to 50 times, re-ingesting tool output each turn and
        blowing up token cost. Keep this small and independent.
        """
        try:
            val = self.config.get('ooda', {}).get('sweep', {}).get('max_iterations')
            if val:
                return max(2, min(20, int(val)))
        except Exception as e:
            logger.debug(f"Invalid ooda.sweep.max_iterations config, using default: {e}")
        return 12

    def _max_tool_result_chars(self) -> int:
        """Per-tool-result size cap (chars) before it is appended to context.

        Untrimmed tool output (kubectl dumps, Loki log floods) is re-sent on
        every subsequent iteration, so a single fat result inflates every later
        turn. Cap each result; the model still sees the head plus a marker.
        """
        try:
            val = self.config.get('chat', {}).get('max_tool_result_chars')
            if val:
                return max(500, int(val))
        except Exception as e:
            logger.debug(f"Invalid chat.max_tool_result_chars config, using default: {e}")
        return 6000

    @staticmethod
    def _serialize_tool_result(result: Any, max_chars: int) -> str:
        """JSON-serialize a tool result, truncating to max_chars.

        Truncation keeps the head (most tools put the salient summary first) and
        appends an explicit marker so the model knows output was clipped rather
        than treating a cut-off payload as the whole picture.
        """
        text = json.dumps(result, default=str)
        if len(text) <= max_chars:
            return text
        omitted = len(text) - max_chars
        return text[:max_chars] + f'\n...[truncated {omitted} chars of tool output]'

    @staticmethod
    def _handle_empty_final(empty_nudge_sent: bool, iteration_budget: int,
                            max_iterations: int, full_messages: list,
                            provider_type: str, model: str):
        """The model ended the tool loop with an empty message — no tool
        calls, no text (gemma4:26b does this on nearly every healthy-cluster
        investigation; see benchmarks/empty_response_sim.py).

        First occurrence: append EMPTY_RESPONSE_NUDGE and grant one bonus
        round (the nudge recovered a well-formed answer 19/19 times in the
        benchmark). Second occurrence: raise EmptyLLMResponseError so the
        provider fallback chain rotates — never return '' to the caller,
        where _extract_status('') silently classifies it as 'monitoring'
        (investigations #1880/#1884/#1885/#1889).

        Both branches increment LLM_EMPTY_FINALS, labelled by provider/model,
        because until now the only trace an empty final left was the warning
        below — there was no way to answer "does gemma4 need two attempts on
        40% of investigations, or 4%?". This is the single chokepoint every
        provider branch funnels through, so counting here (rather than at the
        three call sites) is what keeps a provider from quietly stopping
        counting.

        Returns the updated (empty_nudge_sent, iteration_budget).
        """
        if empty_nudge_sent:
            LLM_EMPTY_FINALS.labels(provider=provider_type, model=model,
                                    disposition='exhausted').inc()
            raise EmptyLLMResponseError(
                f"{provider_type}/{model} returned an empty final response "
                f"even after the nudge retry")
        LLM_EMPTY_FINALS.labels(provider=provider_type, model=model,
                                disposition='nudged').inc()
        logger.warning(
            f"[CHAT] empty final response from {provider_type}/{model} — "
            f"nudging once for an answer")
        full_messages.append({'role': 'user', 'content': EMPTY_RESPONSE_NUDGE})
        return True, min(iteration_budget + 1, max_iterations + 1)

    # Read-only inspection tools whose result is stable enough to memoize for
    # the lifetime of one _chat_with_tools call. A repeated identical call
    # returns a short stub instead of re-running the tool and re-dumping the
    # payload — this is what stops sweep phases from re-listing pods/deployments
    # dozens of times. Mutating tools (ssh_execute) are deliberately excluded.
    _MEMOIZABLE_TOOLS = frozenset({
        'k8s_get_pods', 'k8s_get_nodes', 'k8s_get_deployments', 'k8s_get_events',
        'k8s_get_all_unhealthy', 'k8s_get_ingresses', 'k8s_get_services',
        'k8s_get_pod_status', 'k8s_get_pod_logs', 'loki_query', 'prometheus_query',
        'ssh_list_services', 'ping_host',
    })

    def _cached_tool_exec(self, tool_name: str, tool_args: dict, cache: dict,
                          max_chars: int):
        """Execute a tool, memoizing read-only inspection tools within one session.

        On a repeated identical (tool, args) call the tool is NOT re-run — a
        short stub is returned telling the model the result is unchanged and to
        reuse the earlier output. Caps the redundant re-fetching that bloats
        sweep phases. Returns (content_str, result_obj, was_cached).
        """
        key = None
        if tool_name in self._MEMOIZABLE_TOOLS:
            try:
                key = tool_name + '|' + json.dumps(tool_args, sort_keys=True, default=str)
            except Exception:
                key = None
        if key is not None and key in cache:
            prior = cache[key]
            stub = json.dumps({
                'cached': True,
                'note': (f"Identical {tool_name} call already made in this session — "
                         f"result is unchanged. Reuse the earlier {tool_name} output "
                         f"above instead of re-fetching."),
            })
            return stub, prior, True
        result = self.tools.execute(tool_name, tool_args)
        if key is not None:
            cache[key] = result
        return self._serialize_tool_result(result, max_chars), result, False

    @staticmethod
    def _parse_tool_arguments(raw_args) -> dict:
        """Normalize an LLM tool-call ``arguments`` field into a dict.

        Providers disagree: Ollama may send a dict or a JSON string, Groq
        always sends a string (sometimes empty).
        """
        if isinstance(raw_args, str):
            return json.loads(raw_args) if raw_args.strip() else {}
        return raw_args if raw_args else {}

    def _dispatch_tool_call(self, tool_name: str, tool_args: dict, *,
                            stats: '_ToolLoopStats', tool_cache: dict,
                            max_result_chars: int, iteration: int,
                            max_iterations: int, event_callback=None) -> str:
        """Execute one model-requested tool call and return its message content.

        Shared by every provider branch of the tool loop: streams the
        tool_call/tool_result events, memoizes read-only tools, tracks the
        loop counters plus consulted learning ids, and records the metric.
        """
        if event_callback:
            event_callback('tool_call', {
                'tool': tool_name,
                'args': tool_args,
                'iteration': iteration + 1,
                'max': max_iterations
            })

        content, result, was_cached = self._cached_tool_exec(
            tool_name, tool_args, tool_cache, max_result_chars)
        stats.tool_calls += 1
        if was_cached:
            stats.cached_hits += 1
            logger.info(f"Tool result reused from cache: {tool_name}")
        else:
            logger.info(f"Executing tool: {tool_name}")

        if tool_name == 'find_learnings' and isinstance(result, list):
            stats.learning_ids.extend(r.get('id') for r in result if isinstance(r, dict) and r.get('id'))

        if event_callback:
            event_callback('tool_result', {
                'tool': tool_name,
                'result': json.dumps(result, default=str)[:500],
                'iteration': iteration + 1
            })

        TOOL_CALLS.labels(tool_name=tool_name, result='success').inc()
        return content

    @staticmethod
    def _to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert the internal (Ollama-shaped) message list to Anthropic format.

        Drops system messages (Anthropic takes ``system`` as a top-level
        param) and rewrites tool results as ``tool_result`` content blocks in
        a user message, preferring the stored ``tool_results`` array so
        parallel tool calls survive the round trip.
        """
        converted = []
        for m in messages:
            if m.get('role') == 'system':
                continue
            if m.get('role') == 'tool':
                if m.get('tool_results'):
                    converted.append({'role': 'user', 'content': m['tool_results']})
                else:
                    converted.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': m.get('tool_use_id', 'tool_0'),
                            'content': m.get('content', '')
                        }]
                    })
            elif m.get('role') == 'assistant' and isinstance(m.get('content'), list):
                converted.append(m)
            else:
                converted.append({
                    'role': m.get('role', 'user'),
                    'content': m.get('content', '')
                })
        return converted

    @staticmethod
    def _openai_compat_request_config(provider_type: str):
        """Resolve (api_key, chat_completions_url) for an OpenAI-compatible provider.

        Returns (None, None) for an unknown provider. The api_key may be '' if
        the provider's key env var is unset — callers check and raise.
        """
        cfg = OPENAI_COMPAT_PROVIDERS.get(provider_type)
        if not cfg:
            return None, None
        return os.getenv(cfg['key_env'], ''), cfg['base_url'].rstrip('/') + '/chat/completions'

    def _get_provider_chain(self, backend: str = 'auto', model: str = None) -> List[Tuple[str, str, str]]:
        """
        Get ordered list of providers to try for fallback.

        Returns providers in order: user-selected first, then fallbacks.
        Respects allow_paid_escalation setting for cloud providers.

        Args:
            backend: 'auto', 'ollama', 'groq', 'anthropic'
            model: Optional model override

        Returns:
            List of (provider_type, url, model) tuples to try in order
        """
        providers = []

        # First, add the selected/resolved provider
        primary = self._resolve_provider(backend, model)
        if primary:
            providers.append(primary)

        # Check if fallback is allowed
        allow_fallback = self.kb.get_setting('allow_paid_escalation', 'true')
        if allow_fallback == 'false':
            return providers

        # Define fallback order: ollama -> groq -> xai -> anthropic
        fallback_order = ['ollama', 'groq', 'xai', 'anthropic']

        # Add other providers as fallbacks (skip the primary)
        primary_type = primary[0] if primary else None
        for fb_type in fallback_order:
            if fb_type == primary_type:
                continue  # Skip primary - already added

            fb_provider = self._resolve_provider(fb_type, None)
            if fb_provider and fb_provider not in providers:
                # Verify the provider has required config (API keys, etc.)
                if fb_type in OPENAI_COMPAT_PROVIDERS and not os.getenv(
                        OPENAI_COMPAT_PROVIDERS[fb_type]['key_env']):
                    continue
                if fb_type == 'anthropic' and not os.getenv('ANTHROPIC_API_KEY'):
                    continue
                providers.append(fb_provider)

        return providers

    def _resolve_provider(self, backend: str = 'auto', model: str = None):
        """
        Resolve LLM provider from UI selection.

        Centralizes provider resolution so chat, skills, and OODA all stay in sync.

        Resolution order for 'auto' mode:
        1. DB `selected_backend` (UI provider selection - ollama/groq/anthropic)
        2. Fallback chain if no DB preference set

        For each provider, model resolution order:
        1. Explicit `model` param (caller override)
        2. DB `{provider}_selected_model` (UI model selection)
        3. Config fallback

        Args:
            backend: 'auto', 'ollama', 'groq', 'anthropic'
            model: Explicit model override, or None to resolve from DB/config

        Returns:
            Tuple of (provider_type, url, model) or None if unavailable
        """
        # For 'auto', check if user has selected a preferred backend in UI
        if backend == 'auto':
            db_backend = self.kb.get_setting('selected_backend', '')
            if db_backend and db_backend in ('ollama', 'groq', 'anthropic', 'xai'):
                backend = db_backend
                logger.info(f"[PROVIDER] Using UI-selected backend: {backend}")
            else:
                # No UI preference - use fallback chain
                provider_info = self.llm.get_next_provider()
                if not provider_info:
                    return None
                provider_type, url, resolved_model = provider_info
                source = 'fallback-chain'
                # If fallback chain selected ollama, override model with user's DB selection
                if provider_type == 'ollama' and not model:
                    db_model = self.kb.get_setting('ollama_selected_model', '')
                    if db_model:
                        resolved_model = db_model
                        source = 'db:ollama_selected_model'
                if model:
                    source = 'explicit-override'
                final = (provider_type, url, model or resolved_model)
                logger.debug(f"Resolved provider: {final[0]}/{final[2]} (source={source})")
                return final

        provider_type = backend
        llm_config = self.config.get('llm', {})

        if backend == 'ollama':
            primary = llm_config.get('primary', {})
            url = primary.get('url', os.getenv('OLLAMA_URL', ''))
            if not model:
                db_model = self.kb.get_setting('ollama_selected_model', '')
                config_model = primary.get('model', '')
                model = db_model or config_model
                source = 'db:ollama_selected_model' if db_model else 'config:llm.primary.model'
            else:
                source = 'explicit-override'
            logger.debug(f"[PROVIDER] Resolved ollama: {model} (source={source})")
            return (provider_type, url, model)
        elif backend in ('groq', 'anthropic', 'xai'):
            url = None
            if not model:
                # Check DB for user's model selection, fall back to config
                db_model = self.kb.get_setting(f'{backend}_selected_model', '')
                if db_model:
                    model = db_model
                else:
                    for fb in llm_config.get('fallback', []):
                        if fb.get('provider') == backend:
                            model = fb.get('model', '')
                            break
            logger.debug(f"Resolved provider: {provider_type}/{model}")
            return (provider_type, url, model)
        else:
            return None

    def _chat_with_tools_with_fallback(
        self,
        messages: List[Dict[str, str]],
        system_context: str = '',
        backend: str = 'auto',
        model: str = None,
        max_iterations: int = None,
        event_callback=None,
    ) -> Dict[str, Any]:
        """Run _chat_with_tools across the configured provider fallback chain.

        On ANY exception from one provider, record the failure for cooldown,
        log it, and try the next provider in the chain. The chain is
        primary (user-selected) → other locals → paid escalation, gated by
        the existing ``allow_paid_escalation`` setting.

        Use this for OODA-internal paths (sweep, investigation, morning
        summary, learning extraction, etc.) where a transient Ollama
        timeout (e.g. cold-start after GPU unload) should not fail the
        whole operation. The chat handler also uses this so all paths
        share the same fallback semantics.

        Returns the same shape as _chat_with_tools, plus:
          - ``backend``: provider type that actually succeeded
          - ``model``: model that actually succeeded
          - ``fallback_used``: True iff a non-primary provider was used

        Raises the last provider's exception if all providers in the chain
        fail, or ``RuntimeError`` if the chain is empty.
        """
        provider_chain = self._get_provider_chain(backend, model)
        if not provider_chain:
            raise RuntimeError("No LLM providers available")

        last_error = None
        prev_provider = None
        for idx, (provider_type, url, model_name) in enumerate(provider_chain):
            try:
                if idx > 0 and event_callback and prev_provider:
                    event_callback('fallback', {
                        'from': prev_provider,
                        'to': f"{provider_type}/{model_name}",
                        'reason': str(last_error)[:100] if last_error else 'unknown',
                    })
                logger.info(
                    f"[FALLBACK] Trying provider {idx+1}/{len(provider_chain)}: "
                    f"{provider_type}/{model_name}"
                )
                result = self._chat_with_tools(
                    provider_type=provider_type, url=url, model=model_name,
                    messages=messages, system_context=system_context,
                    max_iterations=max_iterations, event_callback=event_callback,
                )
                provider_key = (
                    f"{provider_type}/{url}/{model_name}" if url
                    else f"{provider_type}/{model_name}"
                )
                self.llm.record_success(provider_key)
                result['backend'] = provider_type
                result['model'] = model_name
                result['fallback_used'] = idx > 0
                return result
            except Exception as e:
                last_error = e
                prev_provider = f"{provider_type}/{model_name}"
                provider_key = (
                    f"{provider_type}/{url}/{model_name}" if url
                    else f"{provider_type}/{model_name}"
                )
                logger.warning(
                    f"[FALLBACK] Provider {provider_type}/{model_name} failed: "
                    f"{type(e).__name__}: {e}"
                )
                self.llm.record_failure(provider_key, self.llm.classify_error(e))
                continue

        logger.error(
            f"[FALLBACK] All {len(provider_chain)} providers failed. "
            f"Last error: {last_error}"
        )
        raise last_error or RuntimeError("All LLM providers exhausted")

    def _chat_with_tools(self, provider_type: str, url: str, model: str,
                         messages: List[Dict[str, str]], system_context: str,
                         max_iterations: int = None, event_callback=None) -> Dict[str, Any]:
        """
        Execute LLM chat with tool calling support.

        Wraps _chat_with_tools_inner with Prometheus metrics tracking.
        """
        start = time.time()
        try:
            result = self._chat_with_tools_inner(
                provider_type, url, model, messages, system_context,
                max_iterations, event_callback
            )
            latency = time.time() - start
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='success').inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)
            if result.get('input_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='input').inc(result['input_tokens'])
            if result.get('output_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='output').inc(result['output_tokens'])
            return result
        except Exception as e:
            latency = time.time() - start
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='error').inc()
            LLM_ERRORS.labels(provider=provider_type, error_type=type(e).__name__).inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)
            raise

    def _chat_with_tools_inner(self, provider_type: str, url: str, model: str,
                         messages: List[Dict[str, str]], system_context: str,
                         max_iterations: int = None, event_callback=None) -> Dict[str, Any]:
        """
        Execute LLM chat with tool calling support.

        Args:
            provider_type: 'ollama', 'groq', 'gemini', 'anthropic', etc.
            url: API endpoint URL
            model: Model name
            messages: Chat history
            system_context: System prompt
            max_iterations: Max tool call iterations

        Returns:
            {
                'response': '...',
                'tool_calls': 2,
                'input_tokens': 1234,
                'output_tokens': 567
            }
        """
        import requests

        if max_iterations is None:
            max_iterations = self._get_max_tool_iterations()

        stats = _ToolLoopStats()
        tool_cache = {}  # memoizes read-only tool results for this session
        dispatch_kwargs = {
            'stats': stats,
            'tool_cache': tool_cache,
            'max_result_chars': self._max_tool_result_chars(),
            'max_iterations': max_iterations,
            'event_callback': event_callback,
        }
        final_answer_forced = False  # final-iteration nudge sent only once
        empty_nudge_sent = False  # empty-final-response nudge sent only once

        # Get tool schemas
        tools = self.tools.get_schemas()

        # Build initial messages with system context
        full_messages = [{'role': 'system', 'content': system_context}] + messages

        # while (not for/range) so the empty-response nudge can grant one
        # bonus round past max_iterations — the empty final typically happens
        # ON the last iteration, where a `continue` would otherwise just fall
        # out of the loop.
        iteration = -1
        iteration_budget = max_iterations
        while iteration + 1 < iteration_budget:
            iteration += 1
            try:
                logger.debug(f"[CHAT] iteration {iteration+1}/{max_iterations}, messages count: {len(full_messages)}")
                # Force a final answer on the last iteration instead of falling
                # off the end of the loop empty-handed. Ollama/Anthropic: simply
                # withhold tools — they then return text cleanly. OpenAI-compatible
                # providers (groq, xai) hard-error ("tool_use_failed") if a
                # reasoning model emits a tool call while tools are absent — so
                # for those, keep tools available and nudge with a message.
                is_final_iteration = (iteration == iteration_budget - 1)
                is_openai_compat = provider_type in OPENAI_COMPAT_PROVIDERS
                offered_tools = [] if (is_final_iteration and not is_openai_compat) else tools
                if is_final_iteration and not final_answer_forced:
                    final_answer_forced = True
                    if is_openai_compat:
                        full_messages.append({
                            'role': 'user',
                            'content': ("FINAL STEP — you have gathered enough data. Do NOT "
                                        "call any more tools. Respond now with your findings "
                                        "as the JSON array described above."),
                        })
                    logger.info("[CHAT] final iteration — forcing an answer")
                # Build payload for Ollama (OpenAI-compatible format)
                if provider_type == 'ollama':
                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'stream': False,
                        'temperature': 0.7
                    }
                    if offered_tools:
                        payload['tools'] = offered_tools
                    headers = {'Content-Type': 'application/json'}
                    logger.debug(f"[CHAT] POST to {url}/api/chat, roles={[m.get('role') for m in full_messages]}")
                    response = requests.post(
                        f"{url}/api/chat",
                        json=payload,
                        headers=headers,
                        timeout=self.llm_timeout
                    )
                    # Convert 4xx/5xx into an exception so the fallback chain
                    # picks it up. Without this, Ollama's `{"error": "model
                    # not found"}` 404 body parses cleanly as JSON, data.get(
                    # "message", {}) is empty, and we silently return an
                    # empty response — bypassing fallback entirely.
                    response.raise_for_status()
                    data = response.json()
                    logger.debug(f"[CHAT] LLM status={response.status_code}, tool_calls={bool(data.get('message', {}).get('tool_calls'))}, content_len={len(data.get('message', {}).get('content', ''))}")

                    # Extract tokens
                    stats.input_tokens += data.get('prompt_eval_count', 0)
                    stats.output_tokens += data.get('eval_count', 0)

                    # Check for tool calls
                    message = data.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        # Execute ALL tool calls (not just the first)
                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            tool_args = self._parse_tool_arguments(
                                tool_call['function'].get('arguments', {}))

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            # Append each tool result (size-capped, memoized)
                            full_messages.append({
                                'role': 'tool',
                                'content': content
                            })

                        # Continue loop for next iteration
                        continue

                    # No tool calls, extract text response
                    text = message.get('content', '')
                    if not (text or '').strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                elif provider_type in OPENAI_COMPAT_PROVIDERS:
                    # OpenAI-compatible cloud provider (Groq, xAI Grok) with tool use
                    api_key, endpoint = self._openai_compat_request_config(provider_type)
                    key_env = OPENAI_COMPAT_PROVIDERS[provider_type]['key_env']
                    if not api_key:
                        raise ValueError(f"{key_env} not set")

                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'temperature': 0.7,
                        'max_tokens': 4096
                    }
                    if offered_tools:
                        payload['tools'] = offered_tools
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {api_key}'
                    }

                    response = requests.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    response.raise_for_status()
                    data = response.json()

                    if data.get('error'):
                        label = OPENAI_COMPAT_PROVIDERS[provider_type]['label']
                        raise ValueError(f"{label} API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    stats.input_tokens += usage.get('prompt_tokens', 0)
                    stats.output_tokens += usage.get('completion_tokens', 0)

                    # Check for tool calls
                    choice = data.get('choices', [{}])[0]
                    message = choice.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            tool_args = self._parse_tool_arguments(
                                tool_call['function'].get('arguments', '{}'))
                            tool_call_id = tool_call.get('id', f'call_{iteration}')

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            # Append each tool result as a separate message (size-capped, memoized)
                            full_messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call_id,
                                'content': content
                            })

                        continue

                    # No tool calls — return text response
                    text = message.get('content', '')
                    if not (text or '').strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                elif provider_type == 'anthropic':
                    # Anthropic Messages API with tool use
                    api_key = os.getenv('ANTHROPIC_API_KEY', '')
                    if not api_key:
                        raise ValueError("ANTHROPIC_API_KEY not set")

                    # Convert OpenAI tool schemas to Anthropic format
                    anthropic_tools = []
                    for t in tools:
                        func = t.get('function', {})
                        anthropic_tools.append({
                            'name': func['name'],
                            'description': func.get('description', ''),
                            'input_schema': func.get('parameters', {'type': 'object', 'properties': {}})
                        })

                    # Anthropic uses system as a top-level param, not a message
                    converted_messages = self._to_anthropic_messages(full_messages)

                    payload = {
                        'model': model,
                        'max_tokens': 4096,
                        'system': system_context,
                        'messages': converted_messages,
                        'temperature': 0.7
                    }
                    if offered_tools:
                        payload['tools'] = anthropic_tools
                    headers = {
                        'Content-Type': 'application/json',
                        'x-api-key': api_key,
                        'anthropic-version': '2023-06-01'
                    }

                    response = requests.post(
                        'https://api.anthropic.com/v1/messages',
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    response.raise_for_status()
                    data = response.json()

                    if data.get('error'):
                        raise ValueError(f"Anthropic API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    stats.input_tokens += usage.get('input_tokens', 0)
                    stats.output_tokens += usage.get('output_tokens', 0)

                    # Check for tool use in content blocks
                    # Anthropic can return multiple tool_use blocks in parallel
                    content_blocks = data.get('content', [])
                    tool_use_blocks = [b for b in content_blocks if b.get('type') == 'tool_use']
                    text_parts = [b.get('text', '') for b in content_blocks if b.get('type') == 'text']

                    if tool_use_blocks:
                        # Execute ALL tool calls and collect results
                        tool_results = []
                        for tool_block in tool_use_blocks:
                            tool_name = tool_block['name']
                            tool_args = tool_block.get('input', {})
                            tool_use_id = tool_block.get('id', f'tool_{iteration}')

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            tool_results.append({
                                'type': 'tool_result',
                                'tool_use_id': tool_use_id,
                                'content': content
                            })

                        # Append assistant message with all tool uses
                        full_messages.append({
                            'role': 'assistant',
                            'content': content_blocks
                        })
                        # Append all tool results in a single user message
                        full_messages.append({
                            'role': 'tool',
                            'tool_use_id': tool_results[0]['tool_use_id'],
                            'tool_results': tool_results,
                            'content': json.dumps([tr['content'] for tr in tool_results])
                        })
                        continue

                    # No tool calls — return text response
                    text = '\n'.join(text_parts)
                    if not text.strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                else:
                    raise NotImplementedError(f"Provider {provider_type} not yet implemented for chat")

            except EmptyLLMResponseError:
                # Must reach _chat_with_tools_with_fallback so the next
                # provider gets a shot — a synthetic error-text response
                # would be stored as findings and misread as a verdict.
                raise
            except Exception as e:
                logger.error(f"Chat iteration {iteration} failed: {e}", exc_info=True)
                if iteration == 0:
                    # First failure, raise immediately
                    raise
                # Subsequent failure during tool loop, return what we have
                return stats.result(f"Error during tool execution: {str(e)}")

        # Hit max iterations — do one final no-tools call to get a summary.
        # Extract tool results from conversation to provide as context.
        logger.info(f"Hit iteration limit ({max_iterations}), attempting summary call")
        try:
            # Collect tool results from the conversation for context
            tool_summaries = []
            for msg in full_messages:
                if msg.get('role') == 'tool':
                    content = msg.get('content', '')
                    # Truncate long tool results
                    if len(content) > 500:
                        content = content[:500] + '...'
                    tool_summaries.append(content)

            tool_context = "\n---\n".join(tool_summaries[-6:])  # Last 6 tool results

            summary_messages = [
                {'role': 'system', 'content': system_context},
                {'role': 'user', 'content': (
                    f'You investigated the infrastructure using {stats.tool_calls} tool calls. '
                    f'Here are the key results from your tool calls:\n\n{tool_context}\n\n'
                    f'Based on these results, provide your findings as a JSON array:\n'
                    f'[{{"severity": "info|warning|critical", "finding": "description", '
                    f'"remediation": "suggested fix"}}]\n'
                    f'If everything looks healthy, return: []\n'
                    f'Only return the JSON array, no other text.'
                )}
            ]

            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': summary_messages,
                    'stream': False,
                    'temperature': 0.7
                }
                response = requests.post(
                    f"{url}/api/chat",
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=self.llm_timeout
                )
                response.raise_for_status()
                data = response.json()
                stats.input_tokens += data.get('prompt_eval_count', 0)
                stats.output_tokens += data.get('eval_count', 0)
                summary_text = data.get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                payload = {
                    'model': model,
                    'messages': summary_messages,
                    'temperature': 0.7,
                    'max_tokens': 4096
                }
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()
                usage = data.get('usage', {})
                stats.input_tokens += usage.get('prompt_tokens', 0)
                stats.output_tokens += usage.get('completion_tokens', 0)
                summary_text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                converted = self._to_anthropic_messages(full_messages)
                payload = {
                    'model': model, 'max_tokens': 4096,
                    'system': system_context,
                    'messages': converted, 'temperature': 0.7
                }
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01'
                }
                response = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, timeout=120)
                response.raise_for_status()
                data = response.json()
                usage = data.get('usage', {})
                stats.input_tokens += usage.get('input_tokens', 0)
                stats.output_tokens += usage.get('output_tokens', 0)
                summary_text = '\n'.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
            else:
                summary_text = ''

            if summary_text.strip():
                logger.info(f"Got {len(summary_text)} char summary after hitting iteration limit")
                return stats.result(summary_text)
            else:
                logger.warning("Summary call returned empty response after iteration limit")
        except Exception as e:
            logger.warning(f"Failed to get summary after iteration limit: {e}", exc_info=True)

        # Fallback if summary call also failed
        return stats.result(
            "Maximum tool iterations reached. Please simplify your request.")

    def _build_chat_system_context(self, mention_skills: bool = False) -> str:
        """Build the chat system prompt: infra state, capabilities, recent learnings.

        Shared by the buffered and streaming chat paths. ``mention_skills``
        adds the slash-command bullet, which only the buffered path (the one
        that routes ``/skill`` messages itself) advertises.
        """
        hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
        host_list = ', '.join(f"{name} ({info.get('address', '?')}, {info.get('role', 'unknown')})"
                              for name, info in hosts_config.items())
        skills_line = "- Execute skills when requested (e.g., /investigate-container)\n" if mention_skills else ""

        # Capability list is derived from the live tool registry so it cannot
        # drift behind newly registered tools (CFOP-22 C). One line per schema.
        tool_lines = []
        try:
            for schema in (self.tools.get_schemas() if self.tools else []):
                fn = (schema.get('function') or {}) if isinstance(schema, dict) else {}
                name = fn.get('name') or ''
                desc = str(fn.get('description') or '').strip()
                if not name:
                    continue
                # Keep the prompt compact: first sentence / ~100 chars of desc.
                short = desc.split('. ')[0].strip()
                if len(short) > 100:
                    short = short[:97] + '...'
                tool_lines.append(f"- {name}: {short}" if short else f"- {name}")
        except Exception as e:
            logger.debug(f"Could not enumerate tool schemas for chat prompt: {e}")
        tools_block = '\n'.join(tool_lines) if tool_lines else '- (tool registry unavailable)'

        system_context = f"""You are CFOperator, an autonomous infrastructure monitoring agent.

Current System State:
- Active investigation: {self.current_investigation is not None}
- Last sweep: {int(time.time() - self.last_sweep)}s ago
- Monitoring {len(hosts_config)} hosts: {host_list}

You have access to these tools (use them — do not claim you lack a capability that appears here):
{tools_block}

Important: Some services run as systemd units (e.g., ollama on ollama-gpu), not containers.
Use ssh_list_services to see BOTH containers and systemd services on a host.

Your role:
- Answer infrastructure-specific questions
- Investigate issues using available tools
{skills_line}- ALWAYS use store_learning to save solutions when you or the user resolves an issue
- Use find_learnings to check for known solutions before investigating
- NOT general system administration (user has Claude Code CLI for that)

Be concise and infrastructure-focused.
"""

        # Surface recent verified learnings so LLM knows what's available
        try:
            recent_learnings = self.kb.find_learnings(limit=5, verified_only=False)
            if recent_learnings:
                system_context += "\n\nRecent learnings from past investigations:\n"
                for l in recent_learnings[:3]:
                    rate = f" ({l.get('success_rate', 0):.0%} success)" if l.get('times_applied', 0) > 0 else ""
                    system_context += f"- [{l.get('learning_type', '?')}] {l.get('title', '?')}{rate}\n"
                system_context += "Use find_learnings tool for more details on any of these.\n"
        except Exception:
            pass  # Don't break chat if KB is down

        return system_context

    def _prepare_skill_invocation(self, message: str):
        """Resolve a ``/skill args`` message into its LLM inputs.

        Returns ``(system_context, user_message, skill_name)``, or
        ``(None, None, skill_name)`` when the skill is unknown — callers turn
        that into the "Unknown skill" response.
        """
        parts = message.split(maxsplit=1)
        skill_name = parts[0][1:]  # Remove leading /
        skill_args = parts[1] if len(parts) > 1 else ''

        if skill_name not in self.skills:
            return None, None, skill_name

        skill = self.skills[skill_name]
        system_context = f"""You are CFOperator executing the "{skill['name']}" skill.

SKILL DESCRIPTION:
{skill['description']}

SKILL INSTRUCTIONS:
{skill['instructions']}

USER REQUEST:
{message}

IMPORTANT:
- Follow the skill instructions exactly as written
- Use the tools in the suggested sequence
- Provide structured output as described in the skill
- Be thorough but concise
"""
        user_message = f"Execute {skill_name} for: {skill_args}" if skill_args else f"Execute {skill_name}"
        logger.info(f"Executing skill: {skill_name} with args: {skill_args}")
        return system_context, user_message, skill_name

    def _unknown_skill_response(self, skill_name: str) -> Dict[str, Any]:
        available = ', '.join(self.skills.keys())
        return {
            'response': f"Unknown skill: {skill_name}\n\nAvailable skills: {available}",
            'backend': 'N/A',
            'model': 'N/A',
            'tool_calls': 0
        }

    def handle_chat_message(self, message: str, history: List[Dict[str, str]], backend: str = 'auto', model: str = None) -> Dict[str, Any]:
        """
        Handle chat message from user (via web UI).

        This is for infrastructure-specific questions like:
        - "Why did immich restart?"
        - "Show me Pi2 container status"
        - "What's using memory on Pi3?"
        - "/investigate-container immich-ml"

        NOT for general system administration (that's Claude Code CLI).

        Args:
            message: User's message
            history: Chat history
            backend: LLM backend to use (auto, ollama, groq, gemini, anthropic)
            model: Specific model to use (overrides default for the backend)

        Returns:
            {
                'response': '...',
                'backend': 'ollama',
                'model': 'qwen3:14b',
                'tool_calls': 2
            }
        """
        logger.info(f"Handling chat message: {message[:100]}")

        system_context = self._build_chat_system_context(mention_skills=True)

        # Expand shortcut slash commands into natural language prompts
        message = self._expand_slash_shortcut(message)

        # Check for skill/command invocation
        if message.startswith('/'):
            return self._execute_skill(message, backend=backend, model=model)

        # Check for explicit summary request (must be the primary intent, not just containing the word)
        msg_lower = message.lower().strip()
        if msg_lower in ('summary', 'report', 'status', 'tps report', 'morning summary', 'give me a summary', 'show summary'):
            summary = self._generate_morning_summary()
            return {
                'response': summary['text'],
                'backend': 'N/A',
                'model': 'N/A',
                'tool_calls': 0
            }

        # Call LLM with tools + metrics tracking
        start_time = time.time()
        tool_calls_count = 0

        try:
            # Build messages
            messages = list(history) + [{'role': 'user', 'content': message}]

            result = self._chat_with_tools_with_fallback(
                messages=messages,
                system_context=system_context,
                backend=backend,
                model=model,
            )

            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
                'learning_ids': result.get('learning_ids', []),
            }

        except Exception as e:
            # Track failed LLM request
            latency = time.time() - start_time
            provider = provider_type if 'provider_type' in locals() else 'unknown'
            model_name = model if 'model' in locals() else 'unknown'

            LLM_REQUESTS.labels(provider=provider, model=model_name, result='error').inc()
            LLM_ERRORS.labels(provider=provider, error_type=type(e).__name__).inc()
            LLM_LATENCY.labels(provider=provider, model=model_name).observe(latency)

            # Record failure in fallback manager
            if 'provider_key' in locals():
                error_type = self.llm.classify_error(e)
                self.llm.record_failure(provider_key, error_type)

            logger.error(f"Chat failed: {e}", exc_info=True)

            return {
                'response': f"Error processing request: {str(e)}",
                'backend': provider,
                'model': model_name,
                'tool_calls': tool_calls_count,
                'learning_ids': []
            }

    def handle_chat_message_stream(self, message: str, history: List[Dict[str, str]], backend: str = 'auto', model: str = None):
        """
        Streaming version of handle_chat_message. Yields SSE event dicts.

        Events yielded:
            {'event': 'tool_call', 'data': {'tool': ..., 'args': ..., 'iteration': ..., 'max': ...}}
            {'event': 'tool_result', 'data': {'tool': ..., 'result': ..., 'iteration': ...}}
            {'event': 'done', 'data': {'response': ..., 'backend': ..., 'model': ..., 'tool_calls': ...}}
            {'event': 'error', 'data': {'error': ...}}
        """
        event_queue = queue.Queue()

        def event_callback(event_type, data):
            event_queue.put({'event': event_type, 'data': data})

        def run_chat():
            try:
                # Expand shortcut slash commands
                nonlocal message
                message = self._expand_slash_shortcut(message)

                # Check for skill/command invocation
                if message.startswith('/'):
                    result = self._execute_skill_stream(message, backend=backend, model=model, event_callback=event_callback)
                elif message.lower().strip() in ('summary', 'report', 'status', 'tps report', 'morning summary', 'give me a summary', 'show summary'):
                    summary = self._generate_morning_summary()
                    result = {'response': summary['text'], 'backend': 'N/A', 'model': 'N/A', 'tool_calls': 0}
                else:
                    result = self._handle_chat_with_stream(message, history, backend, model, event_callback)
                event_queue.put({'event': 'done', 'data': result})
            except Exception as e:
                logger.error(f"Stream chat failed: {e}", exc_info=True)
                event_queue.put({'event': 'error', 'data': {'error': str(e)}})

        # Run the chat in a background thread
        import threading
        thread = threading.Thread(target=run_chat, daemon=True)
        thread.start()

        # Yield events as they arrive
        while True:
            try:
                evt = event_queue.get(timeout=180)
                yield evt
                if evt['event'] in ('done', 'error'):
                    break
            except queue.Empty:
                yield {'event': 'error', 'data': {'error': 'Timeout waiting for response'}}
                break

    def _handle_chat_with_stream(self, message, history, backend, model, event_callback):
        """Internal: runs handle_chat_message logic but passes event_callback to _chat_with_tools."""
        system_context = self._build_chat_system_context()

        messages = list(history) + [{'role': 'user', 'content': message}]

        try:
            result = self._chat_with_tools_with_fallback(
                messages=messages,
                system_context=system_context,
                backend=backend,
                model=model,
                event_callback=event_callback,
            )
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {'response': 'No LLM providers available', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            raise

        return {
            'response': result.get('response', ''),
            'backend': result.get('backend', 'unknown'),
            'model': result.get('model', 'unknown'),
            'tool_calls': result.get('tool_calls', 0),
            'learning_ids': result.get('learning_ids', []),
            'fallback_used': result.get('fallback_used', False),
        }

    def _execute_skill_stream(self, message: str, backend: str = 'auto', model: str = None, event_callback=None) -> Dict[str, Any]:
        """Execute a skill with streaming events."""
        system_context, user_message, skill_name = self._prepare_skill_invocation(message)
        if system_context is None:
            return self._unknown_skill_response(skill_name)

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                backend=backend,
                model=model,
                event_callback=event_callback,
            )
            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
            }
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {'response': f'LLM provider unavailable: {backend}', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            logger.error(f"Skill execution (stream) failed (all providers exhausted): {e}", exc_info=True)
            return {'response': f"Skill execution failed: {str(e)}", 'backend': 'error', 'model': 'N/A', 'tool_calls': 0}
        except Exception as e:
            logger.error(f"Skill execution (stream) failed: {e}", exc_info=True)
            return {'response': f"Skill execution failed: {str(e)}", 'backend': 'error', 'model': 'N/A', 'tool_calls': 0}

    def _execute_skill(self, message: str, backend: str = 'auto', model: str = None) -> Dict[str, Any]:
        """
        Execute a skill command (e.g., /investigate-container immich-ml).

        Skills are structured LLM prompts with:
        - Clear instructions for what to do
        - Tool calling sequence
        - Expected output format

        The skill instructions are injected into the system context,
        and the LLM executes the skill using available tools.
        """
        system_context, user_message, skill_name = self._prepare_skill_invocation(message)
        if system_context is None:
            return self._unknown_skill_response(skill_name)

        # Execute with LLM + tools
        start_time = time.time()

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                backend=backend,
                model=model,
            )
            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
            }
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {
                    'response': f'LLM provider unavailable: {backend}',
                    'backend': 'none',
                    'model': 'none',
                    'tool_calls': 0
                }
            logger.error(f"Skill execution failed (all providers exhausted): {e}", exc_info=True)
            return {
                'response': f"Skill execution failed: {str(e)}",
                'backend': 'error',
                'model': 'N/A',
                'tool_calls': 0
            }
        except Exception as e:
            logger.error(f"Skill execution failed: {e}", exc_info=True)
            return {
                'response': f"Skill execution failed: {str(e)}",
                'backend': 'error',
                'model': 'N/A',
                'tool_calls': 0
            }

    def answer_question(self, question_id: int, answer: str):
        """
        User answered a pending question.

        This unblocks an investigation that was waiting for input.
        """
        logger.info(f"Received answer for question {question_id}: {answer[:100]}")

        # TODO: Store answer in DB
        # TODO: Signal waiting investigation to continue

        # For now, just log
        logger.info(f"Answer handling not yet fully implemented")

    def _check_morning_summary(self):
        """
        Generate morning summary (TPS report style).

        Checks if it's morning (e.g., 7-9 AM) and we haven't sent today's report yet.
        If yes, generates summary of overnight events and patterns.

        Summary includes:
        - Investigations resolved overnight
        - Alerts that fired (and auto-resolved)
        - Container restarts
        - Notable metric trends
        - Log patterns detected
        - Learnings extracted
        - Recommendations for the day

        Sent to:
        - Chat UI (broadcast to any connected clients)
        - Slack (if configured)
        - Stored in DB as sweep_report type
        """
        from datetime import datetime as dt

        # Check if morning summary is enabled
        summary_config = self.config.get('ooda', {}).get('morning_summary', {})
        if not summary_config.get('enabled', True):
            return

        # Check if it's morning time
        now = dt.now()
        summary_hour_start = summary_config.get('hour_start', 7)
        summary_hour_end = summary_config.get('hour_end', 9)

        if not (summary_hour_start <= now.hour < summary_hour_end):
            return

        # Check if we already sent today's summary
        last_summary_date = getattr(self, 'last_summary_date', None)
        if last_summary_date == now.date():
            return

        logger.info("="*60)
        logger.info("MORNING SUMMARY: Generating overnight report")
        logger.info("="*60)

        # Generate the summary
        summary = self._generate_morning_summary()

        # Mark as sent
        self.last_summary_date = now.date()

        # Broadcast to UI
        if self.web_server:
            self.web_server.broadcast({
                'type': 'morning_summary',
                'summary': summary['text'],
                'timestamp': now.isoformat()
            })

        # Send to Slack
        for notif in self.notifications:
            success = False
            error_msg = None
            try:
                notif.send(summary['text'], severity='info')
                success = True
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending morning summary: {e}")
            try:
                channel_type = getattr(notif, 'channel_type', 'slack')
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type=channel_type,
                    severity='info',
                    title='Morning Summary',
                    message=summary['text'][:2000],
                    success=success,
                    error_message=error_msg
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")

        # Store in DB as a sweep report
        try:
            self.kb.store_sweep_report(
                severity=summary.get('severity', 'info'),
                findings=[{'severity': 'info', 'finding': summary['text'][:500]}],
                summary=f"Morning summary - {now.strftime('%Y-%m-%d')}",
                # full_text: the cfop://digest/morning MCP resource serves the
                # complete summary; findings stay truncated for the console.
                sweep_meta={'type': 'morning_summary', 'full_text': summary['text']}
            )
        except Exception as e:
            logger.warning(f"Could not store morning summary in DB: {e}")

        logger.info("Morning summary sent")

    def _generate_morning_summary(self) -> Dict[str, Any]:
        """
        Generate morning summary by gathering overnight data from DB
        and having the LLM synthesize it with live infrastructure checks.
        """
        from datetime import datetime as dt, timedelta

        midnight = dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        now = dt.now()

        # Gather overnight data from DB
        context_parts = []

        # 1. Sweep reports since midnight
        overnight_reports = []
        try:
            reports = self.kb.get_recent_sweep_reports(limit=10)
            overnight_reports = [r for r in reports
                                if r.get('swept_at', '') >= midnight.isoformat()]
            if overnight_reports:
                context_parts.append(f"## Overnight Sweep Reports ({len(overnight_reports)} sweeps)")
                for r in overnight_reports:
                    context_parts.append(
                        f"- {r['swept_at']}: {r['severity'].upper()} - "
                        f"{r['finding_count']} findings: {r.get('summary', '')[:200]}"
                    )
                    for f in (r.get('findings') or [])[:5]:
                        sev = f.get('severity', 'info')
                        finding = f.get('finding', '')[:150]
                        remediation = f.get('remediation', '')
                        context_parts.append(f"  [{sev}] {finding}")
                        if remediation:
                            context_parts.append(f"    -> {remediation[:150]}")
            else:
                context_parts.append("## No sweep reports since midnight")
        except Exception as e:
            context_parts.append(f"## Sweep reports unavailable: {e}")

        # 2. Investigations since midnight
        try:
            investigations = self.kb.get_recent_investigations(limit=20)
            overnight_inv = [i for i in investigations
                            if i.get('started_at', '') >= midnight.isoformat()]
            if overnight_inv:
                context_parts.append(f"\n## Overnight Investigations ({len(overnight_inv)})")
                for inv in overnight_inv:
                    outcome = inv.get('outcome', 'unknown')
                    trigger = inv.get('trigger', '')[:100]
                    duration = inv.get('duration_seconds', 0) or 0
                    tools = inv.get('tool_calls_count', 0) or 0
                    context_parts.append(
                        f"- [{outcome}] {trigger} ({duration}s, {tools} tool calls)"
                    )
            else:
                context_parts.append("\n## No investigations since midnight")
        except Exception as e:
            context_parts.append(f"\n## Investigations unavailable: {e}")

        # 3. New learnings since midnight
        try:
            learnings = self.kb.get_learnings_since(midnight, limit=20)
            if learnings:
                context_parts.append(f"\n## New Learnings ({len(learnings)})")
                for l in learnings:
                    context_parts.append(f"- {l.get('title', 'untitled')}: {l.get('description', '')[:150]}")
            else:
                context_parts.append("\n## No new learnings since midnight")
        except Exception as e:
            context_parts.append(f"\n## Learnings unavailable: {e}")

        overnight_data = "\n".join(context_parts)
        infra = self._get_infra_summary()

        # Ask LLM to synthesize + do live checks
        task = (
            f"Generate a concise morning infrastructure summary for "
            f"{now.strftime('%Y-%m-%d %H:%M')}.\n\n"
            f"{infra}\n\n"
            f"Here is overnight activity data from the database:\n{overnight_data}\n\n"
            f"Do a quick live check: ping each host, check key metrics (CPU, memory, disk), "
            f"and verify critical services are running. Then produce a summary covering:\n"
            f"1. Overnight activity highlights\n"
            f"2. Current system health status\n"
            f"3. Any issues or recommendations\n\n"
            f"Be concise and practical. Use markdown formatting.\n\n"
            f"AFTER the markdown, append the actionable items as EXACTLY one fenced "
            f"json block (use [] if none) so they can be tracked/remediated:\n"
            f"```json\n"
            f'{{"recommendations": [{{"title": "short label", '
            f'"recommendation": "the concrete next step", "host": "affected host or empty", '
            f'"remediation_class": "gitops-patch|k8s-action|node-action|investigate|manual", '
            f'"risk": "low|med|high", "confidence": 0.0, '
            f'"repo": "owning GitOps repo slug or empty"}}]}}\n'
            f"```\n"
            f"Classify remediation_class honestly:\n"
            f"- investigate: the next step is to GATHER EVIDENCE you can collect "
            f"yourself — check pod/job logs, query metrics/Loki, confirm an endpoint "
            f"responds, look for a pattern. PREFER THIS over manual for anything "
            f"'check/verify/confirm/investigate/monitor'; the agent will investigate "
            f"autonomously rather than ask a human.\n"
            f"- gitops-patch: a single manifest change in a GitOps repo (set repo: "
            f"aachtenberg/homelab-infra for cluster apps, aachtenberg/cfoperator-deploy "
            f"for cfoperator/event-runtime itself).\n"
            f"- k8s-action: a reversible in-cluster verb (rollout restart, delete pod).\n"
            f"- node-action: a host change over ssh/ansible (DNS, files, systemd).\n"
            f"- manual: genuinely needs a human's hands or judgement (hardware, wiring, "
            f"a risky decision) — NOT something you could investigate first.\n"
            f"Be conservative with risk."
        )

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': task}],
                system_context=(
                    f"You are CFOperator generating a morning infrastructure summary. "
                    f"You have tools to check live infrastructure. Be concise and actionable.\n\n"
                    f"{infra}"
                ),
                max_iterations=15,
            )
            summary_text = result.get('response', '')
            if summary_text and 'Maximum tool iterations' not in summary_text:
                # Attribute which LLM produced this so operators can correlate
                # summary quality with the served model (the fallback chain may
                # have rolled over to a different provider than the configured
                # primary; without this line nobody can tell after the fact).
                summary_text = _append_llm_attribution(summary_text, result)
                # Feed the queue from the summary's structured recommendations
                # (captures the prose 'Issues & Recommendations' the operator
                # sees); falls back to raw sweep findings if no block emitted.
                self._feed_remediations_from_summary(
                    summary_text, overnight_reports,
                    provider=_llm_provider_tag(result),
                )
                # Strip the machine-readable JSON block now that the queue has
                # consumed it — operators only need the prose table above it,
                # not the raw recommendations block leaking into Slack/ntfy.
                summary_text = self._strip_summary_recommendations_block(summary_text)
                return {
                    'text': summary_text,
                    'timestamp': now,
                    'severity': 'info'
                }
        except RuntimeError as e:
            if "No LLM providers available" not in str(e):
                logger.error(f"LLM morning summary failed (all providers exhausted): {e}")
        except Exception as e:
            logger.error(f"LLM morning summary failed: {e}")

        # Fallback: return the raw data if LLM is unavailable
        summary_text = (
            f"## Infrastructure Summary - {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{overnight_data}\n\n"
            f"*LLM unavailable — raw data shown above*\n\n"
            f"_Generated by: fallback (no LLM available)_"
        )

        return {
            'text': summary_text,
            'timestamp': now,
            'severity': 'info'
        }

    def _get_agent_settings(self) -> Dict[str, Any]:
        """
        Get agent settings relevant to LLM fallback.

        Returns dict with:
        - enable_local_ollama: Whether to use local Ollama instances
        - llm_fallback_chain: List of Ollama provider keys in priority order
        - paid_llm_escalation: Single paid provider key
        - allow_paid_escalation: Boolean flag to enable/disable paid LLM usage
        """
        settings = {}

        # Get enable_local_ollama flag (default: True)
        enable_local = self.kb.get_setting("enable_local_ollama", "true")
        settings["enable_local_ollama"] = enable_local.lower() == "true" if isinstance(enable_local, str) else enable_local

        # Get fallback chain (newline-separated string or JSON array)
        chain_raw = self.kb.get_setting("llm_fallback_chain", "")
        if chain_raw:
            try:
                # Try JSON array first
                settings["llm_fallback_chain"] = json.loads(chain_raw)
            except json.JSONDecodeError:
                # Treat as newline-separated
                settings["llm_fallback_chain"] = [line.strip() for line in chain_raw.split('\n') if line.strip()]
        else:
            settings["llm_fallback_chain"] = []

        # Get paid LLM escalation provider
        settings["paid_llm_escalation"] = self.kb.get_setting("paid_llm_escalation", "")

        # Get allow paid flag (default: False for safety)
        allow_paid = self.kb.get_setting("allow_paid_escalation", "false")
        settings["allow_paid_escalation"] = allow_paid.lower() == "true" if isinstance(allow_paid, str) else allow_paid

        # Get Ollama instances configuration (used by fallback manager to get URLs)
        ollama_instances = self.kb.get_setting("ollama_instances", "{}")
        try:
            settings["ollama_instances"] = json.loads(ollama_instances)
        except json.JSONDecodeError:
            settings["ollama_instances"] = {}

        return settings

def main():
    """Main entry point."""
    logger.info("="*60)
    logger.info("CFOperator - Continuous Feedback Operator")
    logger.info("Version: 1.0.8")
    logger.info("="*60)

    # Load a .env file if present, so API keys (XAI_API_KEY, GROQ_API_KEY, ...)
    # can live in .env. override=False — real environment variables (e.g. k8s
    # secrets injected into the pod) take precedence; .env only fills the gaps.
    # CFOP_NO_DOTENV opts the whole process out (see cfshared.config.
    # load_env_file). Honoured here too: this is a second, independent reader,
    # and a switch that means "do not read .env" while one code path still
    # does is worse than no switch at all.
    if os.getenv("CFOP_NO_DOTENV", "").strip():
        logger.debug("CFOP_NO_DOTENV set — not reading .env")
    else:
        try:
            from dotenv import load_dotenv
            if load_dotenv(override=False):
                logger.info("Loaded environment from .env")
        except ImportError:
            logger.debug("python-dotenv not installed — skipping .env load")

    config_path = os.getenv('CONFIG_PATH', 'config.yaml')
    operator = CFOperator(config_path=config_path)
    operator.run()

if __name__ == '__main__':
    main()
