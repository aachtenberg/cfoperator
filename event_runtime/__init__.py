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
    ScheduledTask,
    SinkHealth,
)
from .plugin_manager import PluginManager

__all__ = [
    "ActionRequest",
    "ActionResult",
    "Alert",
    "AlertSeverity",
    "ContextEnvelope",
    "Decision",
    "DomainEvent",
    "EventRuntime",
    "PluginManager",
    "ScheduledTask",
    "SinkHealth",
]