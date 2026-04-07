"""Explicit plugin registry for the event-driven runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .plugins import ActionHandler, AlertSource, ContextProvider, DecisionEngine, Scheduler, StateSink


@dataclass(slots=True)
class PluginManager:
    """Register and expose runtime plugins by role."""

    alert_sources: List[AlertSource] = field(default_factory=list)
    context_providers: List[ContextProvider] = field(default_factory=list)
    action_handlers: Dict[str, ActionHandler] = field(default_factory=dict)
    schedulers: List[Scheduler] = field(default_factory=list)
    decision_engine: Optional[DecisionEngine] = None
    state_sink: Optional[StateSink] = None

    def register_alert_source(self, plugin: AlertSource) -> None:
        self.alert_sources.append(plugin)

    def register_context_provider(self, plugin: ContextProvider) -> None:
        self.context_providers.append(plugin)

    def register_action_handler(self, plugin: ActionHandler) -> None:
        self.action_handlers[plugin.action_name] = plugin

    def register_scheduler(self, plugin: Scheduler) -> None:
        self.schedulers.append(plugin)

    def register_decision_engine(self, plugin: DecisionEngine) -> None:
        self.decision_engine = plugin

    def register_state_sink(self, plugin: StateSink) -> None:
        self.state_sink = plugin

    def start_all(self) -> None:
        for plugin in self.alert_sources:
            plugin.start()
        for plugin in self.context_providers:
            plugin.start()
        for plugin in self.action_handlers.values():
            plugin.start()
        for plugin in self.schedulers:
            plugin.start()
        if self.decision_engine:
            self.decision_engine.start()
        if self.state_sink:
            self.state_sink.start()

    def stop_all(self) -> None:
        if self.state_sink:
            self.state_sink.stop()
        if self.decision_engine:
            self.decision_engine.stop()
        for plugin in self.schedulers:
            plugin.stop()
        for plugin in self.action_handlers.values():
            plugin.stop()
        for plugin in self.context_providers:
            plugin.stop()
        for plugin in self.alert_sources:
            plugin.stop()