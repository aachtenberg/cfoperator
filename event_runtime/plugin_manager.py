"""Explicit plugin registry for the event-driven runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .plugins import (
    ActionHandler,
    AlertPolicy,
    AlertSource,
    ContextProvider,
    DecisionEngine,
    HostObservabilityProvider,
    NotificationSink,
    Scheduler,
    StateSink,
)


@dataclass(slots=True)
class PluginManager:
    """Register and expose runtime plugins by role."""

    alert_sources: List[AlertSource] = field(default_factory=list)
    alert_policies: List[AlertPolicy] = field(default_factory=list)
    host_observability_providers: List[HostObservabilityProvider] = field(default_factory=list)
    context_providers: List[ContextProvider] = field(default_factory=list)
    action_handlers: Dict[str, ActionHandler] = field(default_factory=dict)
    notification_sinks: List[NotificationSink] = field(default_factory=list)
    schedulers: List[Scheduler] = field(default_factory=list)
    decision_engine: Optional[DecisionEngine] = None
    state_sink: Optional[StateSink] = None

    def register_alert_source(self, plugin: AlertSource) -> None:
        self.alert_sources.append(plugin)

    def register_alert_policy(self, plugin: AlertPolicy) -> None:
        self.alert_policies.append(plugin)

    def register_host_observability_provider(self, plugin: HostObservabilityProvider) -> None:
        self.host_observability_providers.append(plugin)

    def register_context_provider(self, plugin: ContextProvider) -> None:
        self.context_providers.append(plugin)

    def register_action_handler(self, plugin: ActionHandler) -> None:
        self.action_handlers[plugin.action_name] = plugin

    def register_notification_sink(self, plugin: NotificationSink) -> None:
        self.notification_sinks.append(plugin)

    def register_scheduler(self, plugin: Scheduler) -> None:
        self.schedulers.append(plugin)

    def register_decision_engine(self, plugin: DecisionEngine) -> None:
        self.decision_engine = plugin

    def register_state_sink(self, plugin: StateSink) -> None:
        self.state_sink = plugin

    def _iter_unique_plugins(self, *groups: Iterable[object]) -> Iterable[object]:
        seen: set[int] = set()
        for group in groups:
            for plugin in group:
                plugin_id = id(plugin)
                if plugin_id in seen:
                    continue
                seen.add(plugin_id)
                yield plugin

    def start_all(self) -> None:
        for plugin in self._iter_unique_plugins(
            self.alert_sources,
            self.alert_policies,
            self.host_observability_providers,
            self.context_providers,
            self.action_handlers.values(),
            self.notification_sinks,
            self.schedulers,
        ):
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
        for plugin in self._iter_unique_plugins(
            self.schedulers,
            self.action_handlers.values(),
            self.notification_sinks,
            self.context_providers,
            self.host_observability_providers,
            self.alert_policies,
            self.alert_sources,
        ):
            plugin.stop()