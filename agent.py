#!/usr/bin/env python3
"""
CFOperator - Continuous Feedback Operator
==========================================

Single central agent with dual-mode OODA loop:
- Reactive: Responds to alerts with LLM-driven investigations
- Proactive: Periodic deep sweeps to catch issues before they alert

Version: 1.0.4
"""

import os
import sys
import time
import json
import yaml
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

# Prometheus metrics
from prometheus_client import Counter, Gauge, Histogram, Info

# Import proven components from SRE Sentinel
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

        # Initialize core components (from SRE Sentinel)
        # Build database URL for ResilientKnowledgeBase
        db_url = f"postgresql://{self.config['database']['user']}:{self.config['database']['password']}@{self.config['database']['host']}:{self.config['database']['port']}/{self.config['database']['database']}"
        self.kb = ResilientKnowledgeBase(
            db_url=db_url,
            host_id='cfoperator'  # Single central agent
        )

        # Initialize database schema (creates tables if they don't exist)
        self.kb.initialize_schema()

        # Initialize LLM fallback chain (reuses SRE Sentinel's proven architecture)
        self.llm = LLMFallback(
            db_session_factory=self.kb.session_scope,
            settings_getter=self._get_agent_settings
        )

        # Initialize embeddings service for vector search
        self.embeddings = EmbeddingService(
            ollama_url=self.config.get('llm', {}).get('ollama_url', os.getenv('OLLAMA_URL', 'http://192.168.0.198:11434')),
            db_session_factory=self.kb.session_scope
        )

        # Initialize pluggable observability backends
        self._init_observability_backends()

        # Initialize tool registry
        self.tools = ToolRegistry(self)

        # TODO: Load skills from skills/ directory
        # self.skills = load_skills()

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
            'version': '1.0.4',
            'host_id': 'cfoperator',
            'mode': 'dual_ooda'
        })

        logger.info("CFOperator initialized successfully")

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
        if container_config.get('backend') == 'docker':
            self.containers = DockerContainers(hosts=container_config.get('hosts', {}))
            logger.info(f"Initialized Docker backend with {len(container_config.get('hosts', {}))} hosts")
        else:
            logger.warning(f"Unsupported container backend: {container_config.get('backend')}")
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

        # TODO: Search for similar past investigations
        # similar = self.kb.search_similar_investigations(query=trigger, limit=5)

        # TODO: Search for relevant learnings
        # learnings = self.kb.search_learnings(query=trigger, limit=3)

        # context['similar_investigations'] = similar
        # context['known_learnings'] = learnings
        context['similar_investigations'] = []
        context['known_learnings'] = []

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
        - Extract learnings
        """
        logger.info("Starting investigation...")

        # TODO: Create investigation record
        # inv_id = self.kb.create_investigation(trigger=context['trigger'])
        # self.current_investigation = inv_id

        try:
            # TODO: Run LLM investigation cycle with tools
            # self._run_investigation_cycle(context)

            # TODO: Extract learnings from resolved investigation
            # self._extract_learnings(inv_id)

            logger.info("Investigation not yet implemented - placeholder")

        except Exception as e:
            logger.error(f"Investigation failed: {e}", exc_info=True)
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
        """Periodically consolidate similar learnings."""
        # TODO: Use LLM to find and merge duplicate learnings
        pass

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

    def _chat_with_tools(self, provider_type: str, url: str, model: str,
                         messages: List[Dict[str, str]], system_context: str,
                         max_iterations: int = 10) -> Dict[str, Any]:
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

                        logger.info(f"Executing tool: {tool_name}")
                        result = self.tools.execute(tool_name, tool_args)
                        tool_calls_count += 1

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

    def handle_chat_message(self, message: str, history: List[Dict[str, str]], backend: str = 'auto') -> Dict[str, Any]:
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

        Returns:
            {
                'response': '...',
                'backend': 'ollama',
                'model': 'qwen3:14b',
                'tool_calls': 2
            }
        """
        logger.info(f"Handling chat message: {message[:100]}")

        # Build system context with current infrastructure state
        system_context = f"""You are CFOperator, an autonomous infrastructure monitoring agent.

Current System State:
- Active investigation: {self.current_investigation is not None}
- Last sweep: {int(time.time() - self.last_sweep)}s ago
- Monitoring: 4 Raspberry Pi hosts (raspberrypi, raspberrypi2, raspberrypi3, raspberrypi4)

You have access to:
- Prometheus metrics (all hosts)
- Loki logs (all hosts)
- Docker containers (all hosts via remote API)
- Investigation history and learnings (vector DB)

Your role:
- Answer infrastructure-specific questions
- Investigate issues using available tools
- Execute skills when requested (e.g., /investigate-container)
- NOT general system administration (user has Claude Code CLI for that)

Be concise and infrastructure-focused.
"""

        # Check for skill/command invocation
        if message.startswith('/'):
            return self._execute_skill(message)

        # Check for summary request (special command)
        if any(keyword in message.lower() for keyword in ['summary', 'report', 'status', 'tps']):
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
            # Get next available provider
            provider_info = self.llm.get_next_provider()
            if not provider_info:
                return {
                    'response': 'All LLM providers are currently unavailable. Please try again later.',
                    'backend': 'none',
                    'model': 'none',
                    'tool_calls': 0
                }

            provider_type, url, model = provider_info

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

    def _execute_skill(self, message: str) -> Dict[str, Any]:
        """Execute a skill command (e.g., /investigate-container immich-ml)."""
        # TODO: Implement skill execution
        parts = message.split(maxsplit=1)
        skill_name = parts[0][1:]  # Remove leading /
        args = parts[1] if len(parts) > 1 else ''

        return {
            'response': f"Skill execution not yet implemented: {skill_name}",
            'backend': 'N/A',
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
    logger.info("Version: 1.0.0")
    logger.info("="*60)

    config_path = os.getenv('CONFIG_PATH', 'config.yaml')
    operator = CFOperator(config_path=config_path)
    operator.run()

if __name__ == '__main__':
    main()
