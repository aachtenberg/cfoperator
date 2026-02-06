"""
Observability Backends

Import and register available backends.
"""

from .base import (
    MetricsBackend,
    LogsBackend,
    ContainerBackend,
    AlertsBackend,
    NotificationBackend
)

# Import implementations
from .prometheus import PrometheusMetrics, AlertmanagerAlerts
from .loki import LokiLogs
from .docker import DockerContainers
from .slack import SlackNotifications

__all__ = [
    # Interfaces
    'MetricsBackend',
    'LogsBackend',
    'ContainerBackend',
    'AlertsBackend',
    'NotificationBackend',
    # Implementations
    'PrometheusMetrics',
    'LokiLogs',
    'DockerContainers',
    'AlertmanagerAlerts',
    'SlackNotifications',
]
