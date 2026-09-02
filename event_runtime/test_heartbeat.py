"""Outbound liveness heartbeat + last-poll timestamp (CFOP-152).

The thing under test is not "does a request go out" -- it is *when* the runtime
is allowed to claim it is alive. HOMELAB-15 documents the failure this exists to
avoid: an alert keyed on a self-reported liveness flag that a crashed process
never updates, so absence reads as health. These tests pin the two properties
that keep that from recurring:

  - a poll cycle that RAISED must not advance either signal
  - unconfigured must stay genuinely off, through the real config loader
"""

from __future__ import annotations

import threading
import time

import pytest

from event_runtime.heartbeat import HeartbeatPusher, build_heartbeat_pusher
from event_runtime.server import _start_poll_loop


# --------------------------------------------------------------- pusher

def test_unconfigured_is_inert():
    p = build_heartbeat_pusher({})
    assert not p.enabled
    assert p.beat() is False
    assert p.due() is False


@pytest.mark.parametrize("config", [None, {}, {"event_runtime": {}},
                                    {"event_runtime": {"heartbeat": {}}},
                                    {"event_runtime": {"heartbeat": {"url": ""}}},
                                    {"event_runtime": "nonsense"}])
def test_absent_url_never_enables(config):
    """`url` is the on switch, so anything short of a real one must stay off."""
    assert build_heartbeat_pusher(config).enabled is False


def test_schema_supplies_no_heartbeat_url():
    """Guards the CFOP-154 regression at its source: a merged default would
    switch this on for every installation and point it at nothing."""
    from cfshared.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["event_runtime"]["heartbeat"] == {}


def test_config_flows_through_the_real_loader(tmp_path):
    """Hand-built dicts skip deep_merge, which is where CFOP-154 hid."""
    import yaml
    from cfshared.config import load_config
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"event_runtime": {"heartbeat": {
        "url": "https://hc.example/ping/abc", "method": "post",
        "min_interval_seconds": 60}}}))
    pusher = build_heartbeat_pusher(load_config(str(p)))
    assert pusher.enabled and pusher.method == "POST"
    assert pusher.min_interval_seconds == 60


def test_unknown_method_degrades_to_get_rather_than_disabling():
    """A typo in a verb must not silently cost liveness reporting."""
    assert HeartbeatPusher("https://x.example/p", method="PUSH").method == "GET"


def test_beat_never_raises_on_a_dead_endpoint():
    """A heartbeat that can break the poll loop is worse than none: the damage
    would look exactly like the outage it exists to report."""
    p = HeartbeatPusher("http://127.0.0.1:1/nope", timeout_seconds=1)
    assert p.beat() is False


def test_min_interval_throttles_between_sends():
    p = HeartbeatPusher("https://x.example/p", min_interval_seconds=60)
    assert p.due(now=1000.0) is True
    p._last_sent = 1000.0
    assert p.due(now=1030.0) is False
    assert p.due(now=1060.0) is True


# --------------------------------------------------------------- poll loop

class _Src:
    def __init__(self, boom=False):
        self.boom = boom
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("source is wedged")
        return []


class _Plugins:
    def __init__(self, sources):
        self.alert_sources = sources


class _Runtime:
    def __init__(self, sources):
        self.plugins = _Plugins(sources)

    def handle_alert(self, alert):  # pragma: no cover - no alerts emitted here
        return {}


class _Recorder(HeartbeatPusher):
    def __init__(self):
        super().__init__("https://x.example/p")
        self.beats = 0

    def beat(self, now=None):
        self.beats += 1
        return True


def _run_one_cycle(source, heartbeat):
    stop = threading.Event()
    thread = _start_poll_loop(_Runtime([source]), None, 0.01, stop, heartbeat)
    assert thread is not None
    deadline = time.time() + 3
    while source.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2)
    return source.calls


def test_clean_cycle_beats():
    hb = _Recorder()
    assert _run_one_cycle(_Src(), hb) >= 1
    assert hb.beats >= 1


def test_failing_cycle_does_not_beat():
    """The whole point. The loop swallows the exception and keeps running --
    correct for resilience, and precisely why 'still running' is not liveness.
    A wedged source must read as stale, not as healthy."""
    hb = _Recorder()
    assert _run_one_cycle(_Src(boom=True), hb) >= 1
    assert hb.beats == 0


def test_no_sources_means_no_loop():
    """No poll loop, so nothing should claim the runtime is polling."""
    assert _start_poll_loop(_Runtime([]), None, 0.01, threading.Event(), _Recorder()) is None
