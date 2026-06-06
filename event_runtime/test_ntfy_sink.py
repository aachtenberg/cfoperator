"""NtfyNotificationSink: configurable ntfy delivery + bootstrap wiring.

Verifies nothing about the target is hard-coded — url/topic/priority/tags/
title/token are all driven by constructor args (config) — and that the sink is
inert until configured.
"""
import urllib.error

from event_runtime import bootstrap
from event_runtime.notifications import NtfyNotificationSink


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, status=200):
    """Patch urlopen and return a dict that records the outbound request."""
    cap = {}

    def fake_urlopen(req, timeout=None):
        cap["url"] = req.full_url
        cap["headers"] = {k.lower(): v for k, v in req.header_items()}
        cap["body"] = req.data
        cap["timeout"] = timeout
        return _FakeResp(status)

    monkeypatch.setattr(
        "event_runtime.notifications.urllib.request.urlopen", fake_urlopen
    )
    return cap


def test_posts_to_topic_with_default_headers(monkeypatch):
    cap = _capture(monkeypatch)
    sink = NtfyNotificationSink(base_url="http://ntfy.example/", topic="/alerts/", timeout=7)
    ok = sink.notify(
        "x", severity="critical",
        details={"action": "notify", "alert_summary": "node down"},
    )
    assert ok is True
    assert cap["url"] == "http://ntfy.example/alerts"          # slashes normalized
    assert cap["timeout"] == 7
    assert cap["headers"]["x-priority"] == "5"                 # critical -> max
    assert cap["headers"]["x-tags"] == "rotating_light"
    assert cap["headers"]["x-title"] == NtfyNotificationSink.DEFAULT_TITLE
    assert b"node down" in cap["body"]


def test_priority_tags_title_default_are_configurable(monkeypatch):
    cap = _capture(monkeypatch)
    sink = NtfyNotificationSink(
        base_url="http://n", topic="t",
        priority_map={"warning": "2"}, tags_map={"warning": "lock"},
        title="Custom", default_priority="1",
    )
    sink.notify("x", severity="warning", details=None)
    assert cap["headers"]["x-priority"] == "2"
    assert cap["headers"]["x-tags"] == "lock"
    assert cap["headers"]["x-title"] == "Custom"
    # severity not in the map falls back to the configurable default_priority
    sink.notify("x", severity="info", details=None)
    assert cap["headers"]["x-priority"] == "1"
    assert "x-tags" not in cap["headers"]                      # no tag configured for info


def test_auth_token_is_optional(monkeypatch):
    cap = _capture(monkeypatch)
    NtfyNotificationSink(base_url="http://n", topic="t", token="tk_secret").notify("x")
    assert cap["headers"]["authorization"] == "Bearer tk_secret"

    cap2 = _capture(monkeypatch)
    NtfyNotificationSink(base_url="http://n", topic="t").notify("x")
    assert "authorization" not in cap2["headers"]


def test_inert_when_unconfigured(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        "event_runtime.notifications.urllib.request.urlopen",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    assert NtfyNotificationSink(base_url="", topic="").notify("x") is False
    assert NtfyNotificationSink(base_url="http://n", topic="").notify("x") is False
    assert NtfyNotificationSink(base_url="", topic="t").notify("x") is False
    assert calls["n"] == 0                                     # never hit the network


def test_returns_false_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("event_runtime.notifications.urllib.request.urlopen", boom)
    assert NtfyNotificationSink(base_url="http://n", topic="t").notify("x") is False


def test_bootstrap_builds_ntfy_from_env(monkeypatch):
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.setenv("NTFY_URL", "http://ntfy")
    monkeypatch.setenv("NTFY_TOPIC", "homelab")
    monkeypatch.setenv("NTFY_PRIORITY_MAP", '{"warning": "5"}')

    sinks = bootstrap._build_notification_sinks(config_path=None)
    ntfy = [s for s in sinks if isinstance(s, NtfyNotificationSink)]
    assert len(ntfy) == 1
    assert ntfy[0].url == "http://ntfy/homelab"
    assert ntfy[0].priority_map == {"warning": "5"}           # JSON env map parsed


def test_json_dict_env_helper_is_tolerant():
    assert bootstrap._json_dict_or_none(None) is None
    assert bootstrap._json_dict_or_none("") is None
    assert bootstrap._json_dict_or_none("not json") is None
    assert bootstrap._json_dict_or_none("[1,2]") is None       # not an object
    assert bootstrap._json_dict_or_none('{"a": "b"}') == {"a": "b"}
