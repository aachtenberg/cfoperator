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
from .prometheus import PrometheusMetrics
from .loki import LokiLogs
from .docker import DockerContainers
from .alertmanager import AlertmanagerAlerts
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
