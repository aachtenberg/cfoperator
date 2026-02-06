"""
Observability Backend Interfaces

Define abstract interfaces for pluggable observability backends.
Users can implement these for Prometheus, VictoriaMetrics, Datadog, etc.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime

class MetricsBackend(ABC):
    """Abstract interface for metrics backends (Prometheus, VictoriaMetrics, etc.)"""

    @abstractmethod
    def query(self, query: str, time: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Execute a metric query.

        Args:
            query: Backend-specific query string
            time: Optional point-in-time for query

        Returns:
            Dict with 'data' key containing results
        """
        pass

    @abstractmethod
    def query_range(self, query: str, start: datetime, end: datetime, step: str = '1m') -> Dict[str, Any]:
        """
        Execute a range query over time period.

        Args:
            query: Backend-specific query string
            start: Start time
            end: End time
            step: Resolution (e.g., '1m', '5m')

        Returns:
            Dict with 'data' key containing time series
        """
        pass

class LogsBackend(ABC):
    """Abstract interface for logs backends (Loki, Elasticsearch, etc.)"""

    @abstractmethod
    def query(self, query: str, since: str = '1h', limit: int = 100) -> List[Dict[str, Any]]:
        """
        Query logs.

        Args:
            query: Backend-specific query (LogQL, Lucene, etc.)
            since: Time range (e.g., '1h', '24h')
            limit: Max results

        Returns:
            List of log entries
        """
        pass

    @abstractmethod
    def query_range(self, query: str, start: datetime, end: datetime, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Query logs over specific time range.

        Args:
            query: Backend-specific query
            start: Start time
            end: End time
            limit: Max results

        Returns:
            List of log entries with timestamps
        """
        pass

class ContainerBackend(ABC):
    """Abstract interface for container runtimes (Docker, Podman, Kubernetes, etc.)"""

    @abstractmethod
    def list_containers(self, host: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all containers.

        Args:
            host: Optional host filter (for remote execution)

        Returns:
            List of container dicts with name, status, image, etc.
        """
        pass

    @abstractmethod
    def inspect(self, container: str, host: Optional[str] = None) -> Dict[str, Any]:
        """
        Get detailed container information.

        Args:
            container: Container name or ID
            host: Optional host (for remote execution)

        Returns:
            Dict with container details
        """
        pass

    @abstractmethod
    def get_logs(self, container: str, tail: int = 100, since: Optional[str] = None, host: Optional[str] = None) -> str:
        """
        Get container logs.

        Args:
            container: Container name or ID
            tail: Number of lines
            since: Time filter (e.g., '1h')
            host: Optional host

        Returns:
            Log output as string
        """
        pass

    @abstractmethod
    def restart(self, container: str, host: Optional[str] = None) -> bool:
        """
        Restart a container.

        Args:
            container: Container name or ID
            host: Optional host

        Returns:
            True if successful
        """
        pass

class AlertsBackend(ABC):
    """Abstract interface for alerting systems (Alertmanager, PagerDuty, etc.)"""

    @abstractmethod
    def get_firing_alerts(self) -> List[Dict[str, Any]]:
        """
        Get currently firing alerts.

        Returns:
            List of alert dicts with name, severity, labels, annotations
        """
        pass

    @abstractmethod
    def silence_alert(self, alert_id: str, duration: str = '1h', comment: str = '') -> bool:
        """
        Silence an alert.

        Args:
            alert_id: Alert identifier
            duration: How long to silence
            comment: Reason for silence

        Returns:
            True if successful
        """
        pass

class NotificationBackend(ABC):
    """Abstract interface for notifications (Slack, Discord, Email, etc.)"""

    @abstractmethod
    def send(self, message: str, severity: str = 'info') -> bool:
        """
        Send a notification.

        Args:
            message: Message text
            severity: 'info', 'warning', 'critical'

        Returns:
            True if sent successfully
        """
        pass
