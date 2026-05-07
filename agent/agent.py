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
import yaml
import logging
import hashlib
import subprocess
from datetime import datetime
import queue
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

# Prometheus metrics
from prometheus_client import Counter, Gauge, Histogram, Info

# Import core components
from knowledge_base import ResilientKnowledgeBase
from llm_fallback import LLMFallbackManager as LLMFallback
from embedding_service import EmbeddingService

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
AGENT_INFO = Info('cfoperator_agent', 'CFOperator agent information')
AGENT_UPTIME = Gauge('cfoperator_uptime_seconds', 'Agent uptime in seconds')
MONITORED_HOSTS = Gauge('cfoperator_monitored_hosts', 'Number of monitored hosts')
RUNNING_CONTAINERS = Gauge('cfoperator_running_containers', 'Number of running containers across fleet')
ERROR_RATE = Counter('cfoperator_errors_total', 'Total errors')

# LLM Observability metrics
LLM_REQUESTS = Counter('cfoperator_llm_requests_total', 'Total LLM requests', ['provider', 'model', 'result'])
LLM_TOKENS = Counter('cfoperator_llm_tokens_total', 'Total tokens used', ['provider', 'model', 'type'])  # type: prompt/completion
LLM_LATENCY = Histogram('cfoperator_llm_latency_seconds', 'LLM request latency', ['provider', 'model'])
LLM_ERRORS = Counter('cfoperator_llm_errors_total', 'LLM errors by provider', ['provider', 'error_type'])
LLM_FALLBACKS = Counter('cfoperator_llm_fallbacks_total', 'LLM fallback chain activations', ['from_provider', 'to_provider'])
EMBEDDING_REQUESTS = Counter('cfoperator_embedding_requests_total', 'Embedding generation requests', ['result'])
EMBEDDING_CACHE_HITS = Counter('cfoperator_embedding_cache_hits_total', 'Embedding cache hits vs misses', ['result'])

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
        self.start_time = time.time()

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
        """Load configuration from YAML file."""
        self._load_env_file(config_path)
        if not os.path.exists(config_path):
            logger.warning(f"Config file {config_path} not found, using defaults")
            return self._default_config()

        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Expand environment variables
        config = self._expand_env_vars(config)
        return config

    def _load_env_file(self, config_path: str) -> None:
        """Load a colocated .env file so config.yaml placeholders resolve consistently."""
        config_dir = Path(config_path).expanduser().resolve().parent
        env_path = config_dir / ".env"
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)

    def _expand_env_vars(self, config: Any) -> Any:
        """Recursively expand ${VAR} references in config."""
        if isinstance(config, dict):
            return {k: self._expand_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._expand_env_vars(item) for item in config]
        elif isinstance(config, str) and config.startswith('${') and config.endswith('}'):
            var = config[2:-1]
            return os.getenv(var, '')
        return config

    def _default_config(self) -> Dict[str, Any]:
        """Return default configuration."""
        return {
            'observability': {
                'metrics': {'backend': 'prometheus', 'url': 'http://prometheus:9090'},
                'logs': {'backend': 'loki', 'url': 'http://loki:3100'},
                'containers': {'backend': 'docker', 'hosts': {'local': 'unix:///var/run/docker.sock'}},
                'alerts': {'backend': 'alertmanager', 'url': 'http://alertmanager:9093'},
                'notifications': [{'backend': 'slack', 'webhook_url': os.getenv('SLACK_WEBHOOK_URL', '')}]
            },
            'database': {
                'host': 'postgres',
                'port': 5432,
                'database': 'cfoperator',
                'user': 'cfoperator',
                'password': os.getenv('POSTGRES_PASSWORD', '')
            },
            'ooda': {
                'alert_check_interval': 10,
                'sweep_interval': 1800,
                'sweep': {
                    'metrics': True,
                    'logs': True,
                    'containers': True,
                    'baseline_drift': True,
                    'learning_consolidation': True
                }
            }
        }

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

        # Metrics backend
        metrics_config = obs_config.get('metrics', {})
        if metrics_config.get('backend') == 'prometheus':
            self.metrics = PrometheusMetrics(url=metrics_config.get('url'))
            logger.info(f"Initialized Prometheus metrics backend: {metrics_config.get('url')}")
        else:
            logger.warning(f"Unsupported metrics backend: {metrics_config.get('backend')}")
            self.metrics = None

        # Logs backend
        logs_config = obs_config.get('logs', {})
        if logs_config.get('backend') == 'loki':
            self.logs = LokiLogs(url=logs_config.get('url'))
            logger.info(f"Initialized Loki logs backend: {logs_config.get('url')}")
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
                ssh_user = container_config.get('ssh_user', 'aachten')
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
        if alerts_config.get('backend') == 'alertmanager':
            self.alerts = AlertmanagerAlerts(url=alerts_config.get('url'))
            logger.info(f"Initialized Alertmanager backend: {alerts_config.get('url')}")
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
        logger.info(f"Reactive: check alerts every {alert_interval}s")
        logger.info(f"Proactive: deep sweep every {sweep_interval}s ({sweep_interval//60} minutes)")
        logger.info("="*60)

        # Start web server in background thread
        if self.web_server:
            self.web_server.run_threaded()
            logger.info(f"Web UI available at http://0.0.0.0:{self.config.get('chat', {}).get('port', 8083)}")

        while True:
            try:
                # Update uptime metric
                AGENT_UPTIME.set(time.time() - self.start_time)
                OODA_CYCLES.inc()

                # MODE 1: Reactive - handle alerts immediately
                if self.alerts:
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
        Reactive mode: Handle a firing alert using OODA loop.

        Steps:
        1. OBSERVE: Gather context about the alert
        2. ORIENT: Search for similar past issues and learnings
        3. DECIDE: Triage (investigate, ignore, escalate)
        4. ACT: Run investigation with LLM + tools
        """
        logger.info(f"REACTIVE MODE: Handling alert: {alert.get('labels', {}).get('alertname', 'unknown')}")

        # OBSERVE
        context = self._observe_alert(alert)

        # ORIENT
        context = self._orient(context)

        # DECIDE
        decision = self._decide(context)
        logger.info(f"Triage decision: {decision}")

        if decision == 'investigate':
            # ACT
            self._act(context)

    def _observe_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """OBSERVE phase: Gather context about the alert."""
        return {
            'alert': alert,
            'timestamp': datetime.now(),
            'trigger': alert.get('annotations', {}).get('summary', 'Unknown alert')
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
        except Exception:
            pass

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

    def _decide(self, context: Dict[str, Any]) -> str:
        """
        DECIDE phase: Should we investigate?

        Uses LLM to triage with low temperature for consistency.

        Returns:
            'investigate', 'ignore', or 'escalate'
        """
        # TODO: Implement LLM triage
        # For now, always investigate
        return 'investigate'

    def _act(self, context: Dict[str, Any]):
        """
        ACT phase: Investigate and fix.

        - Create investigation record
        - Run LLM investigation loop with tools
        - Extract learnings from resolved investigations
        """
        trigger = context.get('trigger', 'Unknown trigger')
        logger.info(f"Starting investigation: {trigger[:100]}")

        # Create investigation record
        inv_id = self.kb.start_investigation(trigger=trigger)
        self.current_investigation = inv_id
        start_time = time.time()

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
            system_prompt = f"""You are CFOperator investigating an infrastructure alert.

Alert: {trigger}
Alert details: {json.dumps(alert_info, default=str)[:1000]}
{learnings_text}{similar_text}

Investigate this alert using the available tools. Check metrics, logs, and container/service status.
When done, provide a summary of findings and whether the issue is resolved, needs monitoring, or should be escalated."""

            # Get LLM provider (respects user's UI model selection)
            resolved = self._resolve_provider()
            if not resolved:
                logger.error("No LLM provider available for investigation")
                self.kb.update_investigation(
                    investigation_id=inv_id,
                    completed_at=datetime.now(),
                    findings={'error': 'No LLM provider available'},
                    outcome='failed',
                    duration_seconds=time.time() - start_time
                )
                INVESTIGATIONS.labels(outcome='failed').inc()
                return

            provider_type, url, model = resolved

            # Run LLM investigation with tools
            result = self._chat_with_tools(
                provider_type=provider_type,
                url=url,
                model=model,
                messages=[{'role': 'user', 'content': f'Investigate this alert: {trigger}'}],
                system_context=system_prompt
            )

            response_text = result.get('response', '')
            tool_calls_count = result.get('tool_calls', 0)
            duration = time.time() - start_time

            # Determine outcome from response
            response_lower = response_text.lower()
            if any(w in response_lower for w in ['resolved', 'fixed', 'no issue', 'healthy', 'normal']):
                outcome = 'resolved'
            elif any(w in response_lower for w in ['escalat', 'critical', 'urgent']):
                outcome = 'escalated'
            else:
                outcome = 'monitoring'

            findings = {
                'response': response_text[:5000],
                'tool_calls': tool_calls_count,
                'provider': f"{provider_type}/{model}"
            }

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
            except Exception:
                pass
            INVESTIGATIONS.labels(outcome='failed').inc()
        finally:
            self.current_investigation = None

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
            patterns = self.kb._kb.find_service_failure_patterns(days=30)
            if patterns:
                for p in patterns:
                    svc_a = p.get('service_a', '')
                    svc_b = p.get('service_b', '')
                    if svc_a and svc_b:
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
                except Exception:
                    pass
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
        except Exception:
            pass

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

        prompt = f"""Analyze this operational data from the last 24 hours and identify patterns, root causes, or concerns.

SWEEP FINDINGS (this cycle):
{json.dumps(sweep_findings[:10], default=str)[:1500]}

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
            elif provider_type == 'groq':
                api_key = os.getenv('GROQ_API_KEY', '')
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
                    'https://api.groq.com/openai/v1/chat/completions',
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
            for insight in insights[:3]:
                if not insight.get('title') or not insight.get('description'):
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
                        ]))
                        self._embed_learning(lid, search_text)
                except Exception as e:
                    logger.warning(f"Failed to store correlation insight: {e}")

            if stored:
                logger.info(f"Correlation analysis: {stored} insights stored as learnings")
                # Notify about correlation insights
                titles = [i.get('title', '') for i in insights[:3] if i.get('title')]
                summary = f"[Correlation] {stored} insight(s): " + "; ".join(titles)
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

        summary = "Infrastructure hosts:\n" + "\n".join(lines)

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

    def _sweep_with_llm(self, task: str, max_iterations: int = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase. The LLM gets the task description,
        infrastructure context, and access to all tools (prometheus_query,
        loki_query, docker_list, ssh_execute, etc).

        Returns list of findings: [{'severity': ..., 'finding': ...}]
        """
        if max_iterations is None:
            max_iterations = self._get_max_tool_iterations()

        # Check for sweep-specific backend/model override (DB settings)
        sweep_backend = self.kb.get_setting('sweep_backend', '')
        sweep_model = self.kb.get_setting('sweep_model', '')

        if sweep_backend:
            resolved = self._resolve_provider(backend=sweep_backend, model=sweep_model or None)
        else:
            resolved = self._resolve_provider()

        if not resolved:
            logger.warning("No LLM provider available for sweep — skipping")
            return []

        provider_type, url, model = resolved
        infra = self._get_infra_summary()

        system_prompt = f"""You are CFOperator performing a proactive infrastructure sweep.

{infra}

{task}

After investigating, respond with your findings as a JSON array:
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "exact tool output or data supporting this finding", "remediation": "suggested fix or action"}}]

The "evidence" field is REQUIRED — paste the specific metric value, log line, container name, or tool output that proves the finding. Do not make claims without evidence.

If everything looks healthy, return an empty array: []
Only return the JSON array, no other text."""

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
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
                f"{len(response_text)} chars | "
                f"tokens: {input_tokens}in/{output_tokens}out"
            )

            # Parse findings from response
            return self._parse_sweep_findings(response_text)

        except Exception as e:
            logger.error(f"Sweep LLM failed: {e}")
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
            "Never report a specific pod name as 'missing' — check the parent Deployment's ready replica count instead."
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
                                     max_iterations: int = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase on a specific Ollama instance.

        Like _sweep_with_llm() but takes explicit url/model from pool checkout
        instead of resolving via _resolve_provider().
        """
        if max_iterations is None:
            max_iterations = self._get_max_tool_iterations()

        provider_type = 'ollama'
        infra = self._get_infra_summary()

        system_prompt = f"""You are CFOperator performing a proactive infrastructure sweep.

{infra}

{task}

After investigating, respond with your findings as a JSON array:
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "exact tool output or data supporting this finding", "remediation": "suggested fix or action"}}]

The "evidence" field is REQUIRED — paste the specific metric value, log line, container name, or tool output that proves the finding. Do not make claims without evidence.

If everything looks healthy, return an empty array: []
Only return the JSON array, no other text."""

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
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model}@{url} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
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
                    ssh_user = host_info.get('ssh', {}).get('user', 'aachten')
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
    "applies_when": "Conditions when this learning applies",
    "services": ["service1"],
    "tags": ["tag1", "tag2"],
    "category": "resource"
  }}
]}}

learning_type must be one of: solution, pattern, root_cause, antipattern, insight
category must be one of: resource, network, config, dependency
Keep learnings specific and actionable. Only extract learnings if there's genuine insight."""

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
            elif provider_type == 'groq':
                api_key = os.getenv('GROQ_API_KEY', '')
                if not api_key:
                    logger.warning("GROQ_API_KEY not set for learning extraction")
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
                    'https://api.groq.com/openai/v1/chat/completions',
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
            for learning_data in learnings[:3]:  # Cap at 3
                learning_data['investigation_id'] = inv_id
                if not learning_data.get('learning_type') or not learning_data.get('title'):
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
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
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
        'not running', 'not ready', 'down',
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
            tokens: set[str] = set()
            tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
            tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
            tokens -= self._GROUND_TRUTH_STOPWORDS

            for token in tokens:
                if len(token) < 4:
                    continue
                for workload in snapshot['workloads']:
                    if token == workload or token in workload.split('-'):
                        return (
                            f"workload matching '{token}' exists in cluster "
                            f"({workload})"
                        )

        # Pattern 3: claim asserts a workload is not being scraped by Prometheus
        # or has no metrics, but the named pod/service exists. The metrics sweep
        # commonly reads an empty prometheus_query result as "target absent"
        # rather than "series has no recent data".
        if any(k in text for k in self._SCRAPE_TARGET_KEYWORDS):
            tokens = set()
            tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
            tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
            tokens -= self._GROUND_TRUTH_STOPWORDS

            for token in tokens:
                if len(token) < 4:
                    continue
                for workload in snapshot['workloads']:
                    if token == workload or token in workload.split('-'):
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

        return None

    def _verify_single_finding(self,
                               finding: Dict[str, Any],
                               provider_type: str,
                               url: str,
                               model: str,
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

        result = self._chat_with_tools(
            provider_type=provider_type,
            url=url,
            model=model,
            messages=[{'role': 'user', 'content': user_msg}],
            system_context=system_prompt,
            max_iterations=max_iterations
        )

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

        resolved = self._resolve_provider()
        if not resolved:
            logger.warning("No LLM provider for finding verification — skipping")
            return findings

        provider_type, url, model = resolved
        max_iterations = max(2, min(4, self._get_max_tool_iterations()))

        try:
            verified = []
            for finding in findings:
                verified_finding = self._verify_single_finding(
                    finding=finding,
                    provider_type=provider_type,
                    url=url,
                    model=model,
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
        for finding in findings:
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

    def _notify_sweep_findings(self, report: Dict[str, Any]):
        """Send notifications for sweep findings and record in history."""
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
        except Exception:
            pass
        return self.config.get('ooda', {}).get('alert_check_interval', 10)

    def _get_sweep_interval(self) -> int:
        """Get sweep interval: DB setting → config.yaml → default 1800."""
        try:
            val = self.kb.get_setting('sweep_interval', '')
            if val:
                return max(60, min(86400, int(val)))
        except Exception:
            pass
        return self.config.get('ooda', {}).get('sweep_interval', 1800)

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
        except Exception:
            pass
        return self.config.get('chat', {}).get('max_tool_iterations', 10)

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

        # Define fallback order: ollama -> groq -> anthropic
        fallback_order = ['ollama', 'groq', 'anthropic']

        # Add other providers as fallbacks (skip the primary)
        primary_type = primary[0] if primary else None
        for fb_type in fallback_order:
            if fb_type == primary_type:
                continue  # Skip primary - already added

            fb_provider = self._resolve_provider(fb_type, None)
            if fb_provider and fb_provider not in providers:
                # Verify the provider has required config (API keys, etc.)
                if fb_type == 'groq' and not os.getenv('GROQ_API_KEY'):
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
            if db_backend and db_backend in ('ollama', 'groq', 'anthropic'):
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
        elif backend in ('groq', 'anthropic'):
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

        tool_calls_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        learnings_used = []  # Track learning IDs consulted during this conversation

        # Get tool schemas
        tools = self.tools.get_schemas()

        # Build initial messages with system context
        full_messages = [{'role': 'system', 'content': system_context}] + messages

        for iteration in range(max_iterations):
            try:
                logger.debug(f"[CHAT] iteration {iteration+1}/{max_iterations}, messages count: {len(full_messages)}")
                # Build payload for Ollama (OpenAI-compatible format)
                if provider_type == 'ollama':
                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'stream': False,
                        'tools': tools,
                        'temperature': 0.7
                    }
                    headers = {'Content-Type': 'application/json'}
                    logger.debug(f"[CHAT] POST to {url}/api/chat, roles={[m.get('role') for m in full_messages]}")
                    response = requests.post(
                        f"{url}/api/chat",
                        json=payload,
                        headers=headers,
                        timeout=self.llm_timeout
                    )
                    data = response.json()
                    logger.debug(f"[CHAT] LLM status={response.status_code}, tool_calls={bool(data.get('message', {}).get('tool_calls'))}, content_len={len(data.get('message', {}).get('content', ''))}")

                    # Extract tokens
                    if 'prompt_eval_count' in data:
                        total_input_tokens += data.get('prompt_eval_count', 0)
                    if 'eval_count' in data:
                        total_output_tokens += data.get('eval_count', 0)

                    # Check for tool calls
                    message = data.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        # Execute ALL tool calls (not just the first)
                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            raw_args = tool_call['function'].get('arguments', {})
                            # Ollama may return arguments as JSON string or dict
                            if isinstance(raw_args, str):
                                tool_args = json.loads(raw_args) if raw_args.strip() else {}
                            else:
                                tool_args = raw_args if raw_args else {}

                            if event_callback:
                                event_callback('tool_call', {
                                    'tool': tool_name,
                                    'args': tool_args,
                                    'iteration': iteration + 1,
                                    'max': max_iterations
                                })

                            logger.info(f"Executing tool: {tool_name}")
                            result = self.tools.execute(tool_name, tool_args)
                            tool_calls_count += 1

                            if tool_name == 'find_learnings' and isinstance(result, list):
                                learnings_used.extend(r.get('id') for r in result if isinstance(r, dict) and r.get('id'))

                            if event_callback:
                                result_preview = json.dumps(result, default=str)[:500]
                                event_callback('tool_result', {
                                    'tool': tool_name,
                                    'result': result_preview,
                                    'iteration': iteration + 1
                                })

                            TOOL_CALLS.labels(tool_name=tool_name, result='success').inc()

                            # Append each tool result
                            full_messages.append({
                                'role': 'tool',
                                'content': json.dumps(result)
                            })

                        # Continue loop for next iteration
                        continue

                    # No tool calls, extract text response
                    text = message.get('content', '')
                    return {
                        'response': text,
                        'tool_calls': tool_calls_count,
                        'input_tokens': total_input_tokens,
                        'output_tokens': total_output_tokens,
                        'learning_ids': learnings_used
                    }

                elif provider_type == 'groq':
                    # Groq API (OpenAI-compatible) with tool use
                    api_key = os.getenv('GROQ_API_KEY', '')
                    if not api_key:
                        raise ValueError("GROQ_API_KEY not set")

                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'tools': tools,
                        'temperature': 0.7,
                        'max_tokens': 4096
                    }
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {api_key}'
                    }

                    response = requests.post(
                        'https://api.groq.com/openai/v1/chat/completions',
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    data = response.json()

                    if data.get('error'):
                        raise ValueError(f"Groq API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    total_input_tokens += usage.get('prompt_tokens', 0)
                    total_output_tokens += usage.get('completion_tokens', 0)

                    # Check for tool calls
                    choice = data.get('choices', [{}])[0]
                    message = choice.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            # Groq returns arguments as JSON string (handle empty strings safely)
                            raw_args = tool_call['function'].get('arguments', '{}')
                            if isinstance(raw_args, str):
                                tool_args = json.loads(raw_args) if raw_args.strip() else {}
                            else:
                                tool_args = raw_args if raw_args else {}
                            tool_call_id = tool_call.get('id', f'call_{iteration}')

                            if event_callback:
                                event_callback('tool_call', {
                                    'tool': tool_name,
                                    'args': tool_args,
                                    'iteration': iteration + 1,
                                    'max': max_iterations
                                })

                            logger.info(f"Executing tool: {tool_name}")
                            result = self.tools.execute(tool_name, tool_args)
                            tool_calls_count += 1

                            if tool_name == 'find_learnings' and isinstance(result, list):
                                learnings_used.extend(r.get('id') for r in result if isinstance(r, dict) and r.get('id'))

                            if event_callback:
                                result_preview = json.dumps(result, default=str)[:500]
                                event_callback('tool_result', {
                                    'tool': tool_name,
                                    'result': result_preview,
                                    'iteration': iteration + 1
                                })

                            TOOL_CALLS.labels(tool_name=tool_name, result='success').inc()

                            # Append each tool result as a separate message
                            full_messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call_id,
                                'content': json.dumps(result, default=str)
                            })

                        continue

                    # No tool calls — return text response
                    text = message.get('content', '')
                    return {
                        'response': text,
                        'tool_calls': tool_calls_count,
                        'input_tokens': total_input_tokens,
                        'output_tokens': total_output_tokens,
                        'learning_ids': learnings_used
                    }

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
                    anthropic_messages = [m for m in full_messages if m.get('role') != 'system']

                    # Convert tool results from Ollama format to Anthropic format
                    converted_messages = []
                    for m in anthropic_messages:
                        if m.get('role') == 'tool':
                            # Use stored tool_results array if present (parallel tool calls)
                            if m.get('tool_results'):
                                converted_messages.append({
                                    'role': 'user',
                                    'content': m['tool_results']
                                })
                            else:
                                converted_messages.append({
                                    'role': 'user',
                                    'content': [{
                                        'type': 'tool_result',
                                        'tool_use_id': m.get('tool_use_id', 'tool_0'),
                                        'content': m.get('content', '')
                                    }]
                                })
                        elif m.get('role') == 'assistant' and isinstance(m.get('content'), list):
                            converted_messages.append(m)
                        else:
                            converted_messages.append({
                                'role': m.get('role', 'user'),
                                'content': m.get('content', '')
                            })

                    payload = {
                        'model': model,
                        'max_tokens': 4096,
                        'system': system_context,
                        'messages': converted_messages,
                        'tools': anthropic_tools,
                        'temperature': 0.7
                    }
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
                    data = response.json()

                    if data.get('error'):
                        raise ValueError(f"Anthropic API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    total_input_tokens += usage.get('input_tokens', 0)
                    total_output_tokens += usage.get('output_tokens', 0)

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

                            if event_callback:
                                event_callback('tool_call', {
                                    'tool': tool_name,
                                    'args': tool_args,
                                    'iteration': iteration + 1,
                                    'max': max_iterations
                                })

                            logger.info(f"Executing tool: {tool_name}")
                            result = self.tools.execute(tool_name, tool_args)
                            tool_calls_count += 1

                            if tool_name == 'find_learnings' and isinstance(result, list):
                                learnings_used.extend(r.get('id') for r in result if isinstance(r, dict) and r.get('id'))

                            if event_callback:
                                result_preview = json.dumps(result, default=str)[:500]
                                event_callback('tool_result', {
                                    'tool': tool_name,
                                    'result': result_preview,
                                    'iteration': iteration + 1
                                })

                            TOOL_CALLS.labels(tool_name=tool_name, result='success').inc()
                            tool_results.append({
                                'type': 'tool_result',
                                'tool_use_id': tool_use_id,
                                'content': json.dumps(result, default=str)
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
                    return {
                        'response': text,
                        'tool_calls': tool_calls_count,
                        'input_tokens': total_input_tokens,
                        'output_tokens': total_output_tokens,
                        'learning_ids': learnings_used
                    }

                else:
                    raise NotImplementedError(f"Provider {provider_type} not yet implemented for chat")

            except Exception as e:
                logger.error(f"Chat iteration {iteration} failed: {e}", exc_info=True)
                if iteration == 0:
                    # First failure, raise immediately
                    raise
                # Subsequent failure during tool loop, return what we have
                return {
                    'response': f"Error during tool execution: {str(e)}",
                    'tool_calls': tool_calls_count,
                    'input_tokens': total_input_tokens,
                    'output_tokens': total_output_tokens,
                    'learning_ids': learnings_used
                }

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
                    f'You investigated the infrastructure using {tool_calls_count} tool calls. '
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
                data = response.json()
                total_input_tokens += data.get('prompt_eval_count', 0)
                total_output_tokens += data.get('eval_count', 0)
                summary_text = data.get('message', {}).get('content', '')
            elif provider_type == 'groq':
                api_key = os.getenv('GROQ_API_KEY', '')
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
                    'https://api.groq.com/openai/v1/chat/completions',
                    json=payload,
                    headers=headers,
                    timeout=120
                )
                data = response.json()
                usage = data.get('usage', {})
                total_input_tokens += usage.get('prompt_tokens', 0)
                total_output_tokens += usage.get('completion_tokens', 0)
                summary_text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                anthropic_messages = [m for m in full_messages if m.get('role') != 'system']
                converted = []
                for m in anthropic_messages:
                    if m.get('role') == 'tool':
                        if m.get('tool_results'):
                            converted.append({'role': 'user', 'content': m['tool_results']})
                        else:
                            converted.append({
                                'role': 'user',
                                'content': [{'type': 'tool_result', 'tool_use_id': m.get('tool_use_id', 'tool_0'), 'content': m.get('content', '')}]
                            })
                    elif m.get('role') == 'assistant' and isinstance(m.get('content'), list):
                        converted.append(m)
                    else:
                        converted.append({'role': m.get('role', 'user'), 'content': m.get('content', '')})
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
                data = response.json()
                usage = data.get('usage', {})
                total_input_tokens += usage.get('input_tokens', 0)
                total_output_tokens += usage.get('output_tokens', 0)
                summary_text = '\n'.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
            else:
                summary_text = ''

            if summary_text.strip():
                logger.info(f"Got {len(summary_text)} char summary after hitting iteration limit")
                return {
                    'response': summary_text,
                    'tool_calls': tool_calls_count,
                    'input_tokens': total_input_tokens,
                    'output_tokens': total_output_tokens,
                    'learning_ids': learnings_used
                }
            else:
                logger.warning("Summary call returned empty response after iteration limit")
        except Exception as e:
            logger.warning(f"Failed to get summary after iteration limit: {e}", exc_info=True)

        # Fallback if summary call also failed
        return {
            'response': "Maximum tool iterations reached. Please simplify your request.",
            'tool_calls': tool_calls_count,
            'input_tokens': total_input_tokens,
            'output_tokens': total_output_tokens,
            'learning_ids': learnings_used
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

        # Build host list dynamically from config
        hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
        host_list = ', '.join(f"{name} ({info.get('address', '?')}, {info.get('role', 'unknown')})"
                              for name, info in hosts_config.items())

        # Build system context with current infrastructure state
        system_context = f"""You are CFOperator, an autonomous infrastructure monitoring agent.

Current System State:
- Active investigation: {self.current_investigation is not None}
- Last sweep: {int(time.time() - self.last_sweep)}s ago
- Monitoring {len(hosts_config)} hosts: {host_list}

You have access to:
- Prometheus metrics (all hosts)
- Loki logs (all hosts)
- Docker containers AND systemd services (all hosts via SSH — not everything is a container!)
- Knowledge base: store_learning (save solutions/insights) and find_learnings (search past learnings)
- Web search: web_search (look up docs, error messages, CVEs via SearXNG)

Important: Some services run as systemd units (e.g., ollama on ollama-gpu), not containers.
Use ssh_list_services to see BOTH containers and systemd services on a host.

Your role:
- Answer infrastructure-specific questions
- Investigate issues using available tools
- Execute skills when requested (e.g., /investigate-container)
- ALWAYS use store_learning to save solutions when you or the user resolves an issue
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
            resolved = self._resolve_provider(backend, model)
            if not resolved:
                return {
                    'response': f'LLM provider unavailable: {backend}',
                    'backend': 'none',
                    'model': 'none',
                    'tool_calls': 0
                }
            provider_type, url, model = resolved

            # Build messages
            messages = []
            for msg in history:
                messages.append(msg)
            messages.append({'role': 'user', 'content': message})

            # Call LLM with tools
            result = self._chat_with_tools(
                provider_type=provider_type,
                url=url,
                model=model,
                messages=messages,
                system_context=system_context
            )

            tool_calls_count = result.get('tool_calls', 0)
            response_text = result.get('response', '')

            # Track successful LLM request
            latency = time.time() - start_time
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='success').inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)

            # Track tokens if available
            if result.get('input_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='input').inc(result['input_tokens'])
            if result.get('output_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='output').inc(result['output_tokens'])

            # Record success in fallback manager
            provider_key = f"{provider_type}/{url}/{model}" if url else f"{provider_type}/{model}"
            self.llm.record_success(provider_key)

            return {
                'response': response_text,
                'backend': provider_type,
                'model': model,
                'tool_calls': tool_calls_count,
                'learning_ids': result.get('learning_ids', [])
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
        hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
        host_list = ', '.join(f"{name} ({info.get('address', '?')}, {info.get('role', 'unknown')})"
                              for name, info in hosts_config.items())

        system_context = f"""You are CFOperator, an autonomous infrastructure monitoring agent.

Current System State:
- Active investigation: {self.current_investigation is not None}
- Last sweep: {int(time.time() - self.last_sweep)}s ago
- Monitoring {len(hosts_config)} hosts: {host_list}

You have access to:
- Prometheus metrics (all hosts)
- Loki logs (all hosts)
- Docker containers AND systemd services (all hosts via SSH — not everything is a container!)
- Knowledge base: store_learning (save solutions/insights) and find_learnings (search past learnings)
- Web search: web_search (look up docs, error messages, CVEs via SearXNG)

Important: Some services run as systemd units (e.g., ollama on ollama-gpu), not containers.
Use ssh_list_services to see BOTH containers and systemd services on a host.

Your role:
- Answer infrastructure-specific questions
- Investigate issues using available tools
- ALWAYS use store_learning to save solutions when you or the user resolves an issue
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

        start_time = time.time()
        tool_calls_count = 0

        # Get provider chain for fallback
        provider_chain = self._get_provider_chain(backend, model)
        if not provider_chain:
            return {'response': 'No LLM providers available', 'backend': 'none', 'model': 'none', 'tool_calls': 0}

        messages = list(history) + [{'role': 'user', 'content': message}]
        last_error = None
        prev_provider = None

        for idx, (provider_type, url, model_name) in enumerate(provider_chain):
            try:
                # Notify UI if falling back to a different provider
                if idx > 0 and event_callback and prev_provider:
                    event_callback('fallback', {
                        'from': prev_provider,
                        'to': f"{provider_type}/{model_name}",
                        'reason': str(last_error)[:100] if last_error else 'unknown'
                    })

                logger.info(f"[FALLBACK] Trying provider {idx+1}/{len(provider_chain)}: {provider_type}/{model_name}")

                result = self._chat_with_tools(
                    provider_type=provider_type, url=url, model=model_name,
                    messages=messages, system_context=system_context,
                    event_callback=event_callback
                )

                # Success!
                tool_calls_count = result.get('tool_calls', 0)
                latency = time.time() - start_time
                LLM_REQUESTS.labels(provider=provider_type, model=model_name, result='success').inc()
                LLM_LATENCY.labels(provider=provider_type, model=model_name).observe(latency)
                if result.get('input_tokens'):
                    LLM_TOKENS.labels(provider=provider_type, model=model_name, type='input').inc(result['input_tokens'])
                if result.get('output_tokens'):
                    LLM_TOKENS.labels(provider=provider_type, model=model_name, type='output').inc(result['output_tokens'])

                provider_key = f"{provider_type}/{url}/{model_name}" if url else f"{provider_type}/{model_name}"
                self.llm.record_success(provider_key)

                return {
                    'response': result.get('response', ''),
                    'backend': provider_type,
                    'model': model_name,
                    'tool_calls': tool_calls_count,
                    'learning_ids': result.get('learning_ids', []),
                    'fallback_used': idx > 0  # Indicate if fallback was used
                }

            except Exception as e:
                last_error = e
                prev_provider = f"{provider_type}/{model_name}"
                provider_key = f"{provider_type}/{url}/{model_name}" if url else f"{provider_type}/{model_name}"

                logger.warning(f"[FALLBACK] Provider {provider_type}/{model_name} failed: {e}")
                self.llm.record_failure(provider_key, str(e))
                LLM_REQUESTS.labels(provider=provider_type, model=model_name, result='error').inc()
                LLM_ERRORS.labels(provider=provider_type, error_type=type(e).__name__).inc()
                continue  # Try next provider

        # All providers failed
        logger.error(f"[FALLBACK] All {len(provider_chain)} providers failed. Last error: {last_error}")
        raise last_error or RuntimeError("All LLM providers exhausted")

    def _execute_skill_stream(self, message: str, backend: str = 'auto', model: str = None, event_callback=None) -> Dict[str, Any]:
        """Execute a skill with streaming events."""
        parts = message.split(maxsplit=1)
        skill_name = parts[0][1:]
        skill_args = parts[1] if len(parts) > 1 else ''

        if skill_name not in self.skills:
            available = ', '.join(self.skills.keys())
            return {'response': f"Unknown skill: {skill_name}\n\nAvailable skills: {available}", 'backend': 'N/A', 'model': 'N/A', 'tool_calls': 0}

        skill = self.skills[skill_name]
        logger.info(f"Executing skill (stream): {skill_name} with args: {skill_args}")

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
        start_time = time.time()

        try:
            resolved = self._resolve_provider(backend, model)
            if not resolved:
                return {'response': f'LLM provider unavailable: {backend}', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            provider_type, url, model = resolved

            result = self._chat_with_tools(
                provider_type=provider_type, url=url, model=model,
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                max_iterations=None,
                event_callback=event_callback
            )

            duration = time.time() - start_time
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(duration)
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='success').inc()
            LLM_TOKENS.labels(provider=provider_type, model=model, type='prompt').inc(result.get('input_tokens', 0))
            LLM_TOKENS.labels(provider=provider_type, model=model, type='completion').inc(result.get('output_tokens', 0))

            return {'response': result.get('response', ''), 'backend': provider_type, 'model': model, 'tool_calls': result.get('tool_calls', 0)}

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
        parts = message.split(maxsplit=1)
        skill_name = parts[0][1:]  # Remove leading /
        skill_args = parts[1] if len(parts) > 1 else ''

        # Check if skill exists
        if skill_name not in self.skills:
            available = ', '.join(self.skills.keys())
            return {
                'response': f"Unknown skill: {skill_name}\n\nAvailable skills: {available}",
                'backend': 'N/A',
                'model': 'N/A',
                'tool_calls': 0
            }

        skill = self.skills[skill_name]
        logger.info(f"Executing skill: {skill_name} with args: {skill_args}")

        # Build system context with skill instructions
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

        # Build user message
        if skill_args:
            user_message = f"Execute {skill_name} for: {skill_args}"
        else:
            user_message = f"Execute {skill_name}"

        # Execute with LLM + tools
        start_time = time.time()

        try:
            resolved = self._resolve_provider(backend, model)
            if not resolved:
                return {
                    'response': f'LLM provider unavailable: {backend}',
                    'backend': 'none',
                    'model': 'none',
                    'tool_calls': 0
                }
            provider_type, url, model = resolved

            # Call LLM with tools
            result = self._chat_with_tools(
                provider_type=provider_type,
                url=url,
                model=model,
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                max_iterations=None  # Uses DB/config setting
            )

            # Track metrics
            duration = time.time() - start_time
            LLM_LATENCY.labels(
                provider=provider_type,
                model=model
            ).observe(duration)

            LLM_REQUESTS.labels(
                provider=provider_type,
                model=model,
                result='success'
            ).inc()

            LLM_TOKENS.labels(
                provider=provider_type,
                model=model,
                type='prompt'
            ).inc(result.get('input_tokens', 0))

            LLM_TOKENS.labels(
                provider=provider_type,
                model=model,
                type='completion'
            ).inc(result.get('output_tokens', 0))

            return {
                'response': result.get('response', ''),
                'backend': provider_type,
                'model': model,
                'tool_calls': result.get('tool_calls', 0)
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
                sweep_meta={'type': 'morning_summary'}
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
            f"Be concise and practical. Use markdown formatting."
        )

        resolved = self._resolve_provider()
        if resolved:
            provider_type, url, model = resolved
            try:
                result = self._chat_with_tools(
                    provider_type=provider_type,
                    url=url,
                    model=model,
                    messages=[{'role': 'user', 'content': task}],
                    system_context=(
                        f"You are CFOperator generating a morning infrastructure summary. "
                        f"You have tools to check live infrastructure. Be concise and actionable.\n\n"
                        f"{infra}"
                    ),
                    max_iterations=15
                )
                summary_text = result.get('response', '')
                if summary_text and 'Maximum tool iterations' not in summary_text:
                    return {
                        'text': summary_text,
                        'timestamp': now,
                        'severity': 'info'
                    }
            except Exception as e:
                logger.error(f"LLM morning summary failed: {e}")

        # Fallback: return the raw data if LLM is unavailable
        summary_text = (
            f"## Infrastructure Summary - {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{overnight_data}\n\n"
            f"*LLM unavailable — raw data shown above*"
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

    config_path = os.getenv('CONFIG_PATH', 'config.yaml')
    operator = CFOperator(config_path=config_path)
    operator.run()

if __name__ == '__main__':
    main()
