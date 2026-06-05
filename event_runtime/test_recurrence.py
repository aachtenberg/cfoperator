"""Tier-2d recurrence suppression: notify a recurring finding once per window,
let escalations through, keep the short cooldown for storms."""
import os
from event_runtime.dedupe import RecurrenceSuppressionPolicy
from event_runtime.defaults import build_default_alert_policies
from event_runtime.models import Alert


def _alert(summary="svclb-traefik failing", severity="warning"):
    return Alert.from_dict({"source": "cfoperator-sweep", "summary": summary,
                            "severity": severity, "namespace": "kube-system",
                            "resource_type": "pod", "resource_name": "svclb-traefik"})


def test_first_notifies_recurrence_suppressed(tmp_path):
    p = RecurrenceSuppressionPolicy(path=str(tmp_path / "r.json"), window_seconds=21600)
    allowed, _ = p.evaluate(_alert())
    assert allowed                      # first time: notify
    allowed2, reason = p.evaluate(_alert())
    assert not allowed2 and "recurring" in reason   # recurrence: suppressed


def test_escalation_passes_through(tmp_path):
    p = RecurrenceSuppressionPolicy(path=str(tmp_path / "r.json"), window_seconds=21600)
    assert p.evaluate(_alert(severity="warning"))[0]        # warning notifies
    assert not p.evaluate(_alert(severity="warning"))[0]    # warning recurrence suppressed
    # escalation to critical = different fingerprint = notifies
    assert p.evaluate(_alert(severity="critical"))[0]


def test_distinct_findings_independent(tmp_path):
    p = RecurrenceSuppressionPolicy(path=str(tmp_path / "r.json"), window_seconds=21600)
    assert p.evaluate(_alert(summary="finding A"))[0]
    assert p.evaluate(_alert(summary="finding B"))[0]   # different finding still notifies


def test_window_expiry_renotifies(tmp_path):
    p = RecurrenceSuppressionPolicy(path=str(tmp_path / "r.json"), window_seconds=-1)  # already expired
    assert p.evaluate(_alert())[0]
    assert p.evaluate(_alert())[0]   # expired immediately -> notifies again


def test_critical_uses_shorter_window(tmp_path):
    p = RecurrenceSuppressionPolicy(path=str(tmp_path / "r.json"),
                                    window_seconds=21600, critical_window_seconds=1800)
    # both recur-suppress within their windows; this just checks critical is tracked
    assert p.evaluate(_alert(severity="critical"))[0]
    assert not p.evaluate(_alert(severity="critical"))[0]


def test_build_registers_both_policies(tmp_path):
    os.environ.pop("CFOP_EVENT_RUNTIME_DEDUPE_COOLDOWN_SECONDS", None)
    os.environ.pop("CFOP_EVENT_RUNTIME_RECURRENCE_WINDOW_SECONDS", None)
    pols = build_default_alert_policies(base_dir=str(tmp_path))
    names = {p.name for p in pols}
    assert "file-backed-cooldown" in names
    assert "recurrence-suppression" in names


def test_build_recurrence_disabled_by_env(tmp_path):
    os.environ["CFOP_EVENT_RUNTIME_RECURRENCE_WINDOW_SECONDS"] = "0"
    try:
        pols = build_default_alert_policies(base_dir=str(tmp_path))
        assert "recurrence-suppression" not in {p.name for p in pols}
    finally:
        os.environ.pop("CFOP_EVENT_RUNTIME_RECURRENCE_WINDOW_SECONDS", None)
