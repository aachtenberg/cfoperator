#!/usr/bin/env python3
"""
CFOperator - Continuous Feedback Operator
==========================================

Single central agent with dual-mode OODA loop:
- Reactive: Responds to alerts with LLM-driven investigations
- Proactive: Periodic deep sweeps to catch issues before they alert

Version: 1.0.0
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

# Import proven components from SRE Sentinel
from knowledge_base import ResilientKnowledgeBase
from llm_fallback import LLMFallback
from embedding_service import EmbeddingService

# Import pluggable observability backends
from observability import (
    PrometheusMetrics,
    LokiLogs,
    DockerContainers,
    AlertmanagerAlerts,
    SlackNotifications
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "component": "%(name)s", "msg": "%(message)s"}'
)
logger = logging.getLogger("cfoperator")

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
        self.kb = ResilientKnowledgeBase(
            host=self.config['database']['host'],
            port=self.config['database']['port'],
            database=self.config['database']['database'],
            user=self.config['database']['user'],
            password=self.config['database']['password']
        )

        self.llm = LLMFallback(kb=self.kb)
        self.embeddings = EmbeddingService(kb=self.kb)

        # Initialize pluggable observability backends
        self._init_observability_backends()

        # TODO: Load tools from tools/ directory
        # self.tools = ToolRegistry()

        # TODO: Load skills from skills/ directory
        # self.skills = load_skills()

        # OODA state
        self.current_investigation = None
        self.last_sweep = 0
        self.sweep_interval = self.config['ooda']['sweep_interval']

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

        while True:
            try:
                # MODE 1: Reactive - handle alerts immediately
                if self.alerts:
                    alerts = self._check_alerts()
                    if alerts:
                        logger.info(f"Alerts detected: {len(alerts)}")
                        for alert in alerts:
                            self._handle_alert_reactive(alert)

                # MODE 2: Proactive - periodic deep sweep
                if time.time() - self.last_sweep > self.sweep_interval:
                    logger.info("="*60)
                    logger.info("PROACTIVE MODE: Starting deep system sweep")
                    logger.info("="*60)
                    self._deep_system_sweep()
                    self.last_sweep = time.time()

                # TODO: Handle chat messages (if any pending)
                # self._process_chat_queue()

                # TODO: Check for answered questions
                # self._check_question_answers()

                time.sleep(self.config['ooda']['alert_check_interval'])

            except KeyboardInterrupt:
                logger.info("Shutting down CFOperator...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(30)  # Back off on errors

    def _check_alerts(self) -> List[Dict[str, Any]]:
        """Check for firing alerts from Alertmanager."""
        try:
            return self.alerts.get_firing_alerts()
        except Exception as e:
            logger.error(f"Error checking alerts: {e}")
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

            for container in containers:
                # Check for unhealthy status
                if container.get('status') != 'running':
                    findings.append({
                        'severity': 'warning',
                        'finding': f"{container['name']} on {container['host']}: status={container['status']}"
                    })

        except Exception as e:
            logger.error(f"Error sweeping containers: {e}")

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
