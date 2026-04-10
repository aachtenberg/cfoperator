"""Modular event-driven runtime scaffold for CFOperator."""

from .engine import EventRuntime
from .models import (
    ActionRequest,
    ActionResult,
    Alert,
    AlertSeverity,
    ContextEnvelope,
    Decision,
    DomainEvent,
    HostObservation,
    HostTarget,
    ScheduledTask,
    SinkHealth,
)
from .plugin_manager import PluginManager
from .telemetry import render_metrics, telemetry_available
from .worker import BackgroundAlertWorker

__all__ = [
    "ActionRequest",
    "ActionResult",
    "Alert",
    "AlertSeverity",
    "ContextEnvelope",
    "Decision",
    "DomainEvent",
    "EventRuntime",
    "HostObservation",
    "HostTarget",
    "PluginManager",
    "render_metrics",
    "ScheduledTask",
    "SinkHealth",
    "telemetry_available",
    "BackgroundAlertWorker",
]