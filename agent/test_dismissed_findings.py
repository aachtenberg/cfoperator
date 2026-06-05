"""#17: don't re-post findings the operator marked acknowledged/false_positive."""
from agent import CFOperator


class _KB:
    def __init__(self, keys): self._keys = keys
    def get_dismissed_finding_keys(self, days=30): return self._keys


class _ResKB:
    def __init__(self, keys): self._kb = _KB(keys)


def _op_posting(monkeypatch_env, dismissed):
    import os
    os.environ["CFOP_EVENT_RUNTIME_URL"] = "http://er.local:8080"
    op = CFOperator.__new__(CFOperator)
    op.kb = _ResKB(dismissed)
    return op


def test_dismissed_by_summary_skipped(monkeypatch):
    posted = []
    op = _op_posting(monkeypatch, {"svclb-traefik failing"})
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not post")))
    # the only finding is dismissed by summary -> no post attempted
    op._post_findings_to_event_runtime([{"finding": "svclb-traefik failing", "severity": "warning"}])
    # if we reach here without the AssertionError, it was skipped


def test_non_dismissed_is_posted(monkeypatch):
    sent = []
    op = _op_posting(monkeypatch, {"some other finding"})
    import urllib.request
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    def _fake_urlopen(req, timeout=5):
        sent.append(req); return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    op._post_findings_to_event_runtime([{"finding": "a real new finding", "severity": "warning"}])
    assert len(sent) == 1  # posted


def test_dismissed_by_id_skipped(monkeypatch):
    op = _op_posting(monkeypatch, {"abc12345"})
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not post")))
    op._post_findings_to_event_runtime([{"finding": "x", "id": "abc12345", "severity": "warning"}])


# --- #14 Tier 3: count-insensitive signature generalization -------------------

from knowledge_base import normalize_finding_signature


def test_signature_normalizes_counts():
    a = normalize_finding_signature("faster-whisper restarted 11 times (restartCount 11)")
    b = normalize_finding_signature("faster-whisper restarted 14 times (restartCount 14)")
    assert a == b
    # different resource -> different signature
    assert normalize_finding_signature("vaultwarden restarted 2 times") != \
           normalize_finding_signature("faster-whisper restarted 2 times")


def test_dismissed_signature_suppresses_count_variant(monkeypatch):
    sig = "sig::" + normalize_finding_signature("faster-whisper restarted 11 times (restartCount 11)")
    op = _op_posting(monkeypatch, {sig})
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not post")))
    # a NEW count variant of the dismissed finding -> suppressed by signature
    op._post_findings_to_event_runtime(
        [{"finding": "faster-whisper restarted 14 times (restartCount 14)", "severity": "warning"}])
