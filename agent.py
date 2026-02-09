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
import sys
import time
import json
import yaml
import logging
import hashlib
from datetime import datetime
import queue
from typing import Dict, List, Any, Optional
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
    AlertmanagerAlerts,
    SlackNotifications
)

# Import web server
from web_server import WebServer

# Import tool registry
from tools import ToolRegistry

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

        # Initialize embeddings service for vector search
        self.embeddings = EmbeddingService(
            ollama_url=self.config.get('llm', {}).get('ollama_url', os.getenv('OLLAMA_URL', 'http://localhost:11434')),
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
        self.sweep_interval = self.config['ooda']['sweep_interval']
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
        if not os.path.exists(config_path):
            logger.warning(f"Config file {config_path} not found, using defaults")
            return self._default_config()

        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Expand environment variables
        config = self._expand_env_vars(config)
        return config

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

        # Container backend
        container_config = obs_config.get('containers', {})
        backend_type = container_config.get('backend')

        if backend_type == 'prometheus':
            # Use Prometheus for discovery, SSH for actions
            from observability.prometheus_containers import PrometheusContainers
            prometheus_url = metrics_config.get('url')  # Reuse Prometheus URL
            ssh_user = container_config.get('ssh_user', 'aachten')
            self.containers = PrometheusContainers(prometheus_url=prometheus_url, ssh_user=ssh_user)
            logger.info(f"Initialized Prometheus container backend (SSH user: {ssh_user})")
        elif backend_type == 'docker':
            self.containers = DockerContainers(hosts=container_config.get('hosts', {}))
            logger.info(f"Initialized Docker backend with {len(container_config.get('hosts', {}))} hosts")
        else:
            logger.warning(f"Unsupported container backend: {backend_type}")
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
            if notif_config.get('backend') == 'slack':
                notif = SlackNotifications(webhook_url=notif_config.get('webhook_url'))
                self.notifications.append(notif)
                logger.info("Initialized Slack notifications")

    def run(self):
        """
        Main OODA loop - dual mode operation.

        Runs continuously with:
        - Reactive: Check for alerts every 10 seconds
        - Proactive: Deep sweep every 30 minutes
        """
        logger.info("="*60)
        logger.info("Starting CFOperator OODA loop")
        logger.info(f"Reactive: check alerts every {self.config['ooda']['alert_check_interval']}s")
        logger.info(f"Proactive: deep sweep every {self.sweep_interval}s ({self.sweep_interval//60} minutes)")
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
                if time.time() - self.last_sweep > self.sweep_interval:
                    logger.info("="*60)
                    logger.info("PROACTIVE MODE: Starting deep system sweep")
                    logger.info("="*60)
                    SWEEPS.labels(mode='proactive').inc()
                    self._deep_system_sweep()
                    self.last_sweep = time.time()

                # MODE 3: Morning summary (TPS report style)
                self._check_morning_summary()

                time.sleep(self.config['ooda']['alert_check_interval'])

            except KeyboardInterrupt:
                logger.info("Shutting down CFOperator...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                ERROR_RATE.inc()
                LOG_MESSAGES.labels(level='ERROR', component='cfoperator').inc()
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

        findings = []
        sweep_config = self.config['ooda']['sweep']

        # 1. Metric sweep - look at trends over 24h
        if sweep_config.get('metrics') and self.metrics:
            logger.info("Sweeping metrics...")
            metric_findings = self._sweep_metrics()
            findings.extend(metric_findings)
            logger.info(f"Metric sweep found {len(metric_findings)} findings")

        # 2. Log sweep - scan for patterns across all services
        if sweep_config.get('logs') and self.logs:
            logger.info("Sweeping logs...")
            log_findings = self._sweep_logs()
            findings.extend(log_findings)
            logger.info(f"Log sweep found {len(log_findings)} findings")

        # 3. Container sweep - check health of all containers
        if sweep_config.get('containers') and self.containers:
            logger.info("Sweeping containers...")
            container_findings = self._sweep_containers()
            findings.extend(container_findings)
            logger.info(f"Container sweep found {len(container_findings)} findings")

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

        # 5b. Backfill embeddings for unindexed investigations
        try:
            if self.embeddings.is_available():
                result = self.embeddings.batch_index_investigations(
                    kb=self.kb._kb,
                    batch_size=10,
                    max_total=50
                )
                if result.get('success', 0) > 0:
                    logger.info(f"Embedding backfill: {result['success']} indexed, {result.get('remaining', 0)} remaining")
        except Exception as e:
            logger.debug(f"Embedding backfill skipped: {e}")

        # 6. Generate sweep report
        if findings:
            logger.info(f"Sweep found {len(findings)} total issues")
            report = self._generate_sweep_report(findings)

            # Post to chat/notifications if critical/warning
            if report['severity'] in ['warning', 'critical']:
                logger.warning(f"Critical/warning findings in sweep: {report['summary'][:200]}")
                self._notify_sweep_findings(report)

            # TODO: Store in database
            # self.kb.store_sweep_report(report)
        else:
            logger.info("Sweep complete - no findings")

    def _sweep_metrics(self) -> List[Dict[str, Any]]:
        """Query all key metrics and look for trends."""
        # TODO: Implement metric sweep with LLM analysis
        return []

    def _sweep_logs(self) -> List[Dict[str, Any]]:
        """Scan logs across all services looking for patterns."""
        # TODO: Implement log sweep with LLM pattern detection
        return []

    def _sweep_containers(self) -> List[Dict[str, Any]]:
        """Check all Docker containers systematically."""
        findings = []

        try:
            containers = self.containers.list_containers()
            logger.info(f"Found {len(containers)} containers across all hosts")

            # Update running containers metric
            running_count = sum(1 for c in containers if c.get('status') == 'running')
            RUNNING_CONTAINERS.set(running_count)

            for container in containers:
                # Check for unhealthy status
                if container.get('status') != 'running':
                    findings.append({
                        'severity': 'warning',
                        'finding': f"{container['name']} on {container['host']}: status={container['status']}"
                    })

        except Exception as e:
            logger.error(f"Error sweeping containers: {e}")
            ERROR_RATE.inc()

        return findings

    def _check_baseline_drift(self) -> List[Dict[str, Any]]:
        """Compare current state to expected baseline."""
        # TODO: Implement baseline drift detection
        return []

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

            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': 'You are a structured data extractor. Return ONLY valid JSON.'},
                        {'role': 'user', 'content': prompt}
                    ],
                    'stream': False,
                    'temperature': 0.3,
                    'format': 'json'
                }
                resp = req.post(f"{url}/api/chat", json=payload, timeout=60)
                data = resp.json()
                text = data.get('message', {}).get('content', '')
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

    def _generate_sweep_report(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate summary report from sweep findings."""
        max_severity = 'info'
        if any(f.get('severity') == 'critical' for f in findings):
            max_severity = 'critical'
        elif any(f.get('severity') == 'warning' for f in findings):
            max_severity = 'warning'

        summary = f"System sweep found {len(findings)} issues:\n"
        for f in findings:
            summary += f"- [{f.get('severity', 'info').upper()}] {f.get('finding', '')}\n"

        return {
            'timestamp': datetime.now(),
            'findings': findings,
            'summary': summary,
            'severity': max_severity
        }

    def _notify_sweep_findings(self, report: Dict[str, Any]):
        """Send notifications for sweep findings."""
        for notif in self.notifications:
            try:
                notif.send(report['summary'], severity=report['severity'])
            except Exception as e:
                logger.error(f"Error sending notification: {e}")

    def _get_max_tool_iterations(self) -> int:
        """Get max tool iterations: DB setting → config.yaml → default 10."""
        try:
            val = self.kb.get_setting('max_tool_iterations', '')
            if val:
                return max(1, min(50, int(val)))
        except Exception:
            pass
        return self.config.get('chat', {}).get('max_tool_iterations', 10)

    def _resolve_provider(self, backend: str = 'auto', model: str = None):
        """
        Resolve LLM provider from UI selection.

        Centralizes provider resolution so chat, skills, and OODA all stay in sync.

        Args:
            backend: 'auto', 'ollama', 'groq', 'gemini', 'anthropic'
            model: Explicit model override, or None to resolve from DB/config

        Returns:
            Tuple of (provider_type, url, model) or None if unavailable
        """
        if backend == 'auto':
            # Use fallback chain, but still respect DB model selection for ollama
            provider_info = self.llm.get_next_provider()
            if not provider_info:
                return None
            provider_type, url, resolved_model = provider_info
            # If fallback chain selected ollama, override model with user's DB selection
            if provider_type == 'ollama' and not model:
                primary = self.config.get('llm', {}).get('primary', {})
                db_model = self.kb.get_setting('ollama_selected_model', '')
                if db_model:
                    resolved_model = db_model
            return (provider_type, url, model or resolved_model)

        provider_type = backend
        llm_config = self.config.get('llm', {})

        if backend == 'ollama':
            primary = llm_config.get('primary', {})
            url = primary.get('url', os.getenv('OLLAMA_URL', ''))
            if not model:
                model = self.kb.get_setting('ollama_selected_model', '') or primary.get('model', '')
            return (provider_type, url, model)
        elif backend in ('groq', 'gemini', 'anthropic'):
            url = None
            if not model:
                for fb in llm_config.get('fallback', []):
                    if fb.get('provider') == backend:
                        model = fb.get('model', '')
                        break
            return (provider_type, url, model)
        else:
            return None

    def _chat_with_tools(self, provider_type: str, url: str, model: str,
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

        # Get tool schemas
        tools = self.tools.get_schemas()

        # Build initial messages with system context
        full_messages = [{'role': 'system', 'content': system_context}] + messages

        for iteration in range(max_iterations):
            try:
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
                    response = requests.post(
                        f"{url}/api/chat",
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    data = response.json()

                    # Extract tokens
                    if 'prompt_eval_count' in data:
                        total_input_tokens += data.get('prompt_eval_count', 0)
                    if 'eval_count' in data:
                        total_output_tokens += data.get('eval_count', 0)

                    # Check for tool calls
                    message = data.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Execute tool
                        tool_call = tool_calls[0]
                        tool_name = tool_call['function']['name']
                        tool_args = tool_call['function']['arguments']

                        # Notify UI about tool call
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

                        # Notify UI about tool result
                        if event_callback:
                            result_preview = json.dumps(result, default=str)[:500]
                            event_callback('tool_result', {
                                'tool': tool_name,
                                'result': result_preview,
                                'iteration': iteration + 1
                            })

                        # Track tool execution
                        TOOL_CALLS.labels(tool_name=tool_name, result='success').inc()

                        # Append assistant message with tool call
                        full_messages.append(message)

                        # Append tool result
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
                        'output_tokens': total_output_tokens
                    }

                else:
                    # For other providers (groq, gemini, anthropic) - simplified for now
                    # TODO: Implement full support for other providers
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
                    'output_tokens': total_output_tokens
                }

        # Hit max iterations
        return {
            'response': "Maximum tool iterations reached. Please simplify your request.",
            'tool_calls': tool_calls_count,
            'input_tokens': total_input_tokens,
            'output_tokens': total_output_tokens
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
                'tool_calls': tool_calls_count
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
                'tool_calls': tool_calls_count
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

        start_time = time.time()
        tool_calls_count = 0

        try:
            resolved = self._resolve_provider(backend, model)
            if not resolved:
                return {'response': f'LLM provider unavailable: {backend}', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            provider_type, url, model = resolved

            messages = list(history) + [{'role': 'user', 'content': message}]

            result = self._chat_with_tools(
                provider_type=provider_type, url=url, model=model,
                messages=messages, system_context=system_context,
                event_callback=event_callback
            )

            tool_calls_count = result.get('tool_calls', 0)
            latency = time.time() - start_time
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='success').inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)
            if result.get('input_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='input').inc(result['input_tokens'])
            if result.get('output_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='output').inc(result['output_tokens'])

            provider_key = f"{provider_type}/{url}/{model}" if url else f"{provider_type}/{model}"
            self.llm.record_success(provider_key)

            return {'response': result.get('response', ''), 'backend': provider_type, 'model': model, 'tool_calls': tool_calls_count}

        except Exception as e:
            latency = time.time() - start_time
            provider = provider_type if 'provider_type' in locals() else 'unknown'
            model_name = model if 'model' in locals() else 'unknown'
            LLM_REQUESTS.labels(provider=provider, model=model_name, result='error').inc()
            LLM_ERRORS.labels(provider=provider, error_type=type(e).__name__).inc()
            raise

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
            try:
                notif.send(summary['text'], severity='info')
            except Exception as e:
                logger.error(f"Error sending morning summary: {e}")

        # Store in DB
        # TODO: Store as sweep_report with type='morning_summary'

        logger.info("Morning summary sent")

    def _generate_morning_summary(self) -> Dict[str, Any]:
        """
        Generate TPS report style morning summary.

        Example output:
        ```
        ## Infrastructure Summary - 2024-01-15 07:30

        ### Overnight Activity (midnight - 7am)
        - 3 investigations resolved automatically
        - 1 alert fired and auto-resolved (immich-ml memory)
        - 12 container restarts across fleet (5 on Pi2, 4 on Pi3, 3 on Pi4)

        ### Patterns Detected
        - Pi2: telegraf restarts every ~2 hours (memory leak suspected)
        - Pi3: immich-ml OOM events correlate with backup jobs
        - Cross-host: redis connection spikes during postgres restarts

        ### Metric Trends (7-day)
        - Pi2 disk: 82% → 85% (+3% this week, +12% this month)
        - Pi3 memory: Steady 95% utilization (container limits may need adjustment)
        - Pi4 CPU: Spikes to 80% during backup windows (expected)

        ### Learnings Extracted
        - immich-ml: Increasing memory limit to 2GB prevents OOM during large imports
        - telegraf: Restart every 6h as workaround for memory leak (upstream issue filed)

        ### Recommendations
        1. Increase Pi2 disk soon (will hit 90% in ~3 weeks at current rate)
        2. Consider moving immich-ml to Pi4 (more memory available)
        3. Update telegraf to v1.29 when available (memory leak fix)

        ### System Health: GOOD ✓
        - All hosts responding
        - All critical services running
        - No pending investigations
        ```
        """
        from datetime import datetime as dt, timedelta

        # Query overnight activity (midnight to now)
        midnight = dt.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # TODO: Query investigations from last night
        # investigations = self.kb.get_investigations_since(midnight)

        # TODO: Query alerts from last night
        # alerts = self.kb.get_alerts_since(midnight)

        # TODO: Query container events from last night
        # container_events = self.kb.get_container_events_since(midnight)

        # TODO: Get metric trends (7-day comparison)
        # metric_trends = self._get_metric_trends(days=7)

        # TODO: Get newly extracted learnings
        # new_learnings = self.kb.get_learnings_since(midnight)

        # For now, generate placeholder summary
        summary_text = f"""## Infrastructure Summary - {dt.now().strftime("%Y-%m-%d %H:%M")}

### Overnight Activity (midnight - {dt.now().strftime("%H:%M")})
- Morning summary not yet fully implemented
- Placeholder report

### System Health: UNKNOWN
- Monitoring active
- Data collection in progress

### Next Steps
- Implement full morning summary generation
- Query DB for overnight events
- Analyze patterns with LLM
"""

        return {
            'text': summary_text,
            'timestamp': dt.now(),
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
