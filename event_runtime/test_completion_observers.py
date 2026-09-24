"""CompletionObserver: every completed action, before notification policy (CFOP-212)."""

from __future__ import annotations

import pytest

from event_runtime.engine import EventRuntime
from event_runtime.models import ActionResult, Alert, AlertSeverity, Decision
from event_runtime.plugin_manager import PluginManager
from event_runtime.plugins import ActionHandler, CompletionObserver, DecisionEngine, NotificationSink, StateSink


class MemorySink(StateSink):
    name = "memory"

    def __init__(self):
        self.events = []

    def append(self, events):
        self.events.extend(events)

    def recent(self, limit=50):
        return list(reversed(self.events))[:limit]

    def health(self):
        return {"name": self.name}


class Recorder(CompletionObserver):
    name = "recorder"

    def __init__(self):
        self.seen, self.started, self.stopped = [], False, False

    def observe(self, alert, result):
        self.seen.append((alert, result))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class Broken(CompletionObserver):
    name = "broken"

    def observe(self, alert, result):
        raise RuntimeError("observer blew up")


class Pager(NotificationSink):
    name = "pager"

    def __init__(self):
        self.pages = []

    def notify(self, summary, *, severity="info", details=None):
        self.pages.append(summary)
        return True


class Always(DecisionEngine):
    name = "always"

    def decide(self, envelope):
        return Decision(action="investigate", confidence=1.0, reasoning="test")


class Handler(ActionHandler):
    name = "handler"
    action_name = "investigate"

    def __init__(self, result):
        self.result = result

    def execute(self, request):
        return self.result


def _alert(severity=AlertSeverity.WARNING):
    return Alert(source="dynatrace", severity=severity, summary="Dynatrace P-1: Slowdown", fingerprint="dynatrace:1")


def _runtime(*observers, handler_result=None):
    plugins = PluginManager()
    plugins.register_state_sink(MemorySink())
    plugins.register_decision_engine(Always())
    if handler_result is not None:
        plugins.register_action_handler(Handler(handler_result))
    pager = Pager()
    plugins.register_notification_sink(pager)
    for observer in observers:
        plugins.register_completion_observer(observer)
    return EventRuntime(plugins), pager


RESOLVED = ActionResult(action="investigate", success=True, message="Resolved: Dynatrace P-1: Slowdown",
                        details={"investigation_id": 7, "outcome": "resolved"})


def test_an_observer_sees_a_completion_the_digest_keeps_from_the_pager(monkeypatch):
    """The reason this role exists: a warning resolved outcome never pages, but is still a result."""
    monkeypatch.delenv("CFOP_DIGEST_LOW_SEVERITY", raising=False)     # default: digest on
    recorder = Recorder()
    runtime, pager = _runtime(recorder)
    runtime.record_external_action_completion(_alert(), RESOLVED)
    assert pager.pages == []                                           # held for the digest
    ((alert, result),) = recorder.seen
    assert alert.fingerprint == "dynatrace:1" and result.details["outcome"] == "resolved"


def test_an_in_process_completion_is_observed_too(monkeypatch):
    monkeypatch.setenv("CFOP_DIGEST_LOW_SEVERITY", "0")
    recorder = Recorder()
    runtime, _ = _runtime(recorder, handler_result=ActionResult(action="investigate", success=True, message="done"))
    runtime.handle_alert(_alert())
    ((_, result),) = recorder.seen
    assert result.message == "done"


def test_an_interim_quiet_result_is_not_a_completion():
    recorder = Recorder()
    queued = ActionResult(action="investigate", success=True, message="Investigation dispatched", quiet=True)
    runtime, _ = _runtime(recorder, handler_result=queued)
    runtime.handle_alert(_alert())
    assert recorder.seen == []


def test_a_failing_observer_changes_nothing_else(monkeypatch, caplog):
    monkeypatch.setenv("CFOP_DIGEST_LOW_SEVERITY", "0")
    recorder = Recorder()
    runtime, pager = _runtime(Broken(), recorder)
    with caplog.at_level("WARNING"):
        runtime.record_external_action_completion(_alert(AlertSeverity.CRITICAL), RESOLVED)
    assert "Completion observer broken failed" in caplog.text
    assert len(recorder.seen) == 1                 # the next observer still ran
    assert pager.pages                             # and the page still went out
    assert [e["event_type"] for e in runtime.recent_events()] == ["action_completed"]


def test_observers_are_listed_and_follow_the_plugin_lifecycle():
    recorder = Recorder()
    runtime, _ = _runtime(recorder)
    assert runtime.health()["completion_observers"] == ["recorder"]
    runtime.plugins.start_all()
    runtime.plugins.stop_all()
    assert recorder.started and recorder.stopped


def test_an_external_plugin_can_register_one(tmp_path, monkeypatch):
    from event_runtime.external_plugins import PluginContext, load_external_plugins

    (tmp_path / "cfop212_observer.py").write_text(
        "from event_runtime.plugins import CompletionObserver\n"
        "class O(CompletionObserver):\n"
        "    name = 'from-plugin'\n"
        "    def observe(self, alert, result):\n"
        "        pass\n"
        "def register(plugins, context):\n"
        "    plugins.register_completion_observer(O())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    plugins = PluginManager()
    load_external_plugins(plugins, PluginContext(), raw="cfop212_observer")
    assert [o.name for o in plugins.completion_observers] == ["from-plugin"]
