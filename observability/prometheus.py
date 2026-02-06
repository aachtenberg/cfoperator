"""
Prometheus Metrics Backend Implementation
"""

import requests
from datetime import datetime
from typing import Dict, Any, Optional
from .base import MetricsBackend, AlertsBackend

class PrometheusMetrics(MetricsBackend):
    """Prometheus implementation of MetricsBackend."""

    def __init__(self, url: str = 'http://localhost:9090'):
        """
        Initialize Prometheus backend.

        Args:
            url: Prometheus server URL
        """
        self.url = url.rstrip('/')
        self.timeout = 30

    def query(self, query: str, time: Optional[datetime] = None) -> Dict[str, Any]:
        """Execute instant query."""
        params = {'query': query}
        if time:
            params['time'] = time.timestamp()

        resp = requests.get(
            f'{self.url}/api/v1/query',
            params=params,
            timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def query_range(self, query: str, start: datetime, end: datetime, step: str = '1m') -> Dict[str, Any]:
        """Execute range query."""
        params = {
            'query': query,
            'start': start.timestamp(),
            'end': end.timestamp(),
            'step': step
        }

        resp = requests.get(
            f'{self.url}/api/v1/query_range',
            params=params,
            timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

class AlertmanagerAlerts(AlertsBackend):
    """Alertmanager implementation of AlertsBackend."""

    def __init__(self, url: str = 'http://localhost:9093'):
        """
        Initialize Alertmanager backend.

        Args:
            url: Alertmanager server URL
        """
        self.url = url.rstrip('/')
        self.timeout = 10

    def get_firing_alerts(self) -> list:
        """Get currently firing alerts."""
        resp = requests.get(
            f'{self.url}/api/v2/alerts',
            params={'filter': 'state="active"'},
            timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def silence_alert(self, alert_id: str, duration: str = '1h', comment: str = '') -> bool:
        """Create a silence for an alert."""
        # Parse duration to seconds
        duration_map = {'h': 3600, 'm': 60, 's': 1}
        value = int(duration[:-1])
        unit = duration[-1]
        seconds = value * duration_map.get(unit, 3600)

        silence = {
            'matchers': [{'name': 'alertname', 'value': alert_id, 'isRegex': False}],
            'startsAt': datetime.now().isoformat(),
            'endsAt': (datetime.now() + timedelta(seconds=seconds)).isoformat(),
            'comment': comment or f'Silenced by CFOperator',
            'createdBy': 'cfoperator'
        }

        resp = requests.post(
            f'{self.url}/api/v2/silences',
            json=silence,
            timeout=self.timeout
        )
        return resp.status_code == 200
