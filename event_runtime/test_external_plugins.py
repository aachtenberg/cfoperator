"""CFOP_EVENT_RUNTIME_PLUGINS: loading plugins the core does not name (CFOP-208)."""

from __future__ import annotations

import itertools
import textwrap

import pytest

from event_runtime.escalation import EscalationLedger
from event_runtime.external_plugins import (
    PLUGINS_ENV,
    PluginContext,
    PluginLoadError,
    load_external_plugins,
    parse_plugin_specs,
)
from event_runtime.plugin_manager import PluginManager

_names = itertools.count()


@pytest.fixture
def make_module(tmp_path, monkeypatch):
    """Write a throwaway importable module and return its (unique) name.

    Unique per call because importlib caches by name: a second test reusing a
    name would silently get the first test's module.
    """
    monkeypatch.syspath_prepend(str(tmp_path))

    def _make(source: str) -> str:
        name = f"cfop208_plugin_{next(_names)}"
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
        return name

    return _make


# A source that emits one alert on its first poll, then nothing.
_SOURCE_PLUGIN = """
    from event_runtime.models import Alert, AlertSeverity
    from event_runtime.plugins import AlertSource

    class OneShotSource(AlertSource):
        name = "cfop208-one-shot"

        def __init__(self):
            self.fired = False

        def poll(self):
            if self.fired:
                return []
            self.fired = True
            return [Alert(source=self.name, severity=AlertSeverity.WARNING,
                          summary="cfop-208 plugin alert", fingerprint="cfop208-fp")]

    SEEN = {}

    def register(plugins, context):
        SEEN["context"] = context
        plugins.register_alert_source(OneShotSource())
"""


def test_unset_or_blank_changes_nothing():
    plugins = PluginManager()
    assert load_external_plugins(plugins, PluginContext(), raw="") == []
    assert load_external_plugins(plugins, PluginContext(), raw=" , ,") == []
    assert plugins.alert_sources == [] and plugins.action_handlers == {}


def test_specs_default_the_callable_and_load_a_repeat_once():
    assert parse_plugin_specs("a.b, c.d:setup ,a.b:register") == [
        ("a.b", "register"),
        ("c.d", "setup"),
    ]


@pytest.mark.parametrize("raw", ["a.b:", ":setup", "a.b : "])
def test_a_malformed_entry_is_refused(raw):
    with pytest.raises(PluginLoadError, match="malformed entry"):
        parse_plugin_specs(raw)


def test_a_missing_module_stops_startup():
    with pytest.raises(PluginLoadError, match="cannot import cfop208_no_such_module:register"):
        load_external_plugins(PluginManager(), PluginContext(), raw="cfop208_no_such_module")


def test_a_missing_callable_stops_startup(make_module):
    name = make_module("VALUE = 1\n")
    with pytest.raises(PluginLoadError, match="has no callable 'register'"):
        load_external_plugins(PluginManager(), PluginContext(), raw=name)


def test_a_non_callable_attribute_stops_startup(make_module):
    name = make_module("register = 'not a function'\n")
    with pytest.raises(PluginLoadError, match="has no callable 'register'"):
        load_external_plugins(PluginManager(), PluginContext(), raw=name)


def test_a_register_that_raises_stops_startup_and_keeps_the_cause(make_module):
    name = make_module("""
        def register(plugins, context):
            raise ValueError("DT_PLATFORM_TOKEN is not set")
    """)
    with pytest.raises(PluginLoadError, match="failed while registering: DT_PLATFORM_TOKEN is not set") as info:
        load_external_plugins(PluginManager(), PluginContext(), raw=name)
    assert isinstance(info.value.__cause__, ValueError)


def test_a_named_callable_gets_the_manager_and_context(make_module):
    name = make_module("""
        CALLS = []

        def setup(plugins, context):
            CALLS.append((plugins, context))
    """)
    plugins, context = PluginManager(), PluginContext(config={"k": "v"})
    assert load_external_plugins(plugins, context, raw=f"{name}:setup") == [f"{name}:setup"]
    module = __import__(name)
    assert module.CALLS == [(plugins, context)]


def test_a_plugin_source_reaches_the_runtime_end_to_end(make_module, monkeypatch, tmp_path):
    """Through the real bootstrap: the plugin's alert is polled and handled."""
    from event_runtime.bootstrap import build_portable_runtime

    name = make_module(_SOURCE_PLUGIN)
    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_ALERTMANAGER_URL", raising=False)
    monkeypatch.setenv(PLUGINS_ENV, name)

    runtime = build_portable_runtime()

    assert "cfop208-one-shot" in runtime.health()["sources"]
    results = runtime.poll_sources()
    assert len(results) == 1
    received = runtime.recent_events(limit=50, event_type="alert_received")
    assert [e["payload"]["alert"]["summary"] for e in received] == ["cfop-208 plugin alert"]

    # The context is the runtime's own: same ledger object, and the merged
    # config rather than an empty placeholder.
    context = __import__(name).SEEN["context"]
    assert isinstance(context.escalation_ledger, EscalationLedger)
    assert context.escalation_ledger is runtime._escalation_ledger
    assert "observability" in context.config


def test_a_broken_plugin_stops_the_real_bootstrap(monkeypatch, tmp_path):
    from event_runtime.bootstrap import build_portable_runtime

    monkeypatch.setenv("CFOP_EVENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("CFOP_EVENT_RUNTIME_PG_DSN", raising=False)
    monkeypatch.delenv("CFOP_AGENT_URL", raising=False)
    monkeypatch.setenv(PLUGINS_ENV, "cfop208_no_such_module")
    with pytest.raises(PluginLoadError):
        build_portable_runtime()
