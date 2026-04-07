"""Portable runtime bootstrap for minimal setup deployments."""

from __future__ import annotations

import os
from pathlib import Path

from .defaults import (
    HostContextProvider,
    JsonFileScheduler,
    OpenReasoningDecisionEngine,
    build_default_action_handlers,
)
from .engine import EventRuntime
from .plugin_manager import PluginManager
from .state.composite import CompositeStateSink
from .state.local_outbox import LocalOutboxStateSink
from .state.postgres import PostgresStateSink
from .state.replay import ReplayingStateSink


def build_portable_runtime() -> EventRuntime:
    """Build a runtime that runs with only Python stdlib dependencies."""
    base_dir = Path(os.getenv("CFOP_EVENT_RUNTIME_DIR", str(Path.home() / ".cfoperator" / "event-runtime")))
    outbox_dir = os.getenv("CFOP_EVENT_RUNTIME_OUTBOX_DIR", str(base_dir / "outbox"))
    schedule_dir = os.getenv("CFOP_EVENT_RUNTIME_SCHEDULE_DIR", str(base_dir / "scheduled"))
    replay_interval = int(os.getenv("CFOP_EVENT_RUNTIME_REPLAY_INTERVAL_SECONDS", "30"))
    pg_dsn = os.getenv("CFOP_EVENT_RUNTIME_PG_DSN", "")

    local_sink = LocalOutboxStateSink(directory=outbox_dir)
    if pg_dsn:
        sink = ReplayingStateSink(
            local_sink=local_sink,
            remote_sinks=[PostgresStateSink(dsn=pg_dsn)],
            replay_interval_seconds=replay_interval,
        )
    else:
        sink = CompositeStateSink([local_sink])

    plugins = PluginManager()
    plugins.register_state_sink(sink)
    plugins.register_decision_engine(OpenReasoningDecisionEngine())
    plugins.register_context_provider(HostContextProvider())
    plugins.register_scheduler(JsonFileScheduler(directory=schedule_dir))
    for handler in build_default_action_handlers().values():
        plugins.register_action_handler(handler)
    return EventRuntime(plugins)