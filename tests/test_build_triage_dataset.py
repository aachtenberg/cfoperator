"""Unit tests for scripts/build_triage_dataset.py's labeling functions.

The dataset builder turns investigation history into SFT labels, so a bug
here trains the production triage model on wrong answers — silently, since
training "succeeds" either way. These tests pin the labeling rules the
module's docstrings promise (PR #144 review):

  - hindsight never overrides visible context (conflicts return None),
    with the ONE stated exception: escalate labels from outcome alone;
  - the notify rule requires a resolved precedent at >=0.85;
  - noise/non-triage/benchmark filters keep the wrong rows out entirely.
"""

from repo_paths import REPO_ROOT
import importlib.util
import os

_REPO_ROOT = str(REPO_ROOT)
_spec = importlib.util.spec_from_file_location(
    "build_triage_dataset",
    os.path.join(_REPO_ROOT, "scripts", "build_triage_dataset.py"),
)
btd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(btd)


def _resolved_hit(sim=0.9):
    return [{"id": 1, "trigger": "same alert, earlier", "outcome": "resolved",
             "similarity": sim}]


# ---- derive_label: rubric-from-visible-context ----------------------------


def test_noise_trigger_is_log_only():
    got = btd.derive_label("Pod smoke-test-runner-2xk4f is crash-looping",
                           "resolved", [])
    assert got[0] == "log_only"


def test_noise_trigger_that_turned_real_is_conflict():
    assert btd.derive_label("Pod tmp-migrate-1 is crash-looping",
                            "needs_action", []) is None


def test_hardware_watchdog_is_not_noise():
    """Lowercase 'watchdog' is a real hardware/systemd event in this homelab,
    not the Alertmanager Watchdog alert — must never be labeled log_only."""
    got = btd.derive_label(
        "Unclean reboot on headless-gpu, journal shows watchdog reset",
        "monitoring", [])
    assert got[0] != "log_only"


def test_escalate_outcome_labels_escalate_from_hindsight():
    # The stated exception: escalate is hindsight-only (see docstring).
    got = btd.derive_label("Node ubuntu-cm5-01 NotReady", "escalated", [])
    assert got[0] == "escalate"
    assert got[1] == "outcome-escalate"  # basis flags the hindsight source


def test_resolved_precedent_with_cheap_outcome_is_notify():
    got = btd.derive_label("Pod foo not ready for 30m", "resolved",
                           _resolved_hit(0.9))
    assert got[0] == "notify"


def test_resolved_precedent_below_threshold_is_not_notify():
    got = btd.derive_label("Pod foo not ready for 30m", "resolved",
                           _resolved_hit(0.7))
    assert got[0] == "investigate"


def test_resolved_precedent_but_needed_action_is_conflict():
    # Rubric says notify, reality says needs_action: either label trains a
    # lie, so the row must be dropped for human review, never labeled.
    assert btd.derive_label("river-history ingest failing station fetches",
                            "needs_action", _resolved_hit(0.9)) is None


def test_monitoring_precedent_is_investigate_not_notify():
    # Precedent PRESENCE is not precedent OUTCOME — the qwen3.8 shortcut.
    hits = [{"id": 2, "trigger": "same pattern", "outcome": "monitoring",
             "similarity": 0.95}]
    got = btd.derive_label("Pod loki-0 restarted", "monitoring", hits)
    assert got[0] == "investigate"


# ---- filters --------------------------------------------------------------


def test_non_triage_shapes_are_filtered():
    for trigger in ("monitoring_cycle: camera-dashboard errors",
                    "monitoring_cycle",
                    "Retry of #455",
                    "Context-driven re-investigation of #455",
                    "Investigate immich-kiosk timeout pattern: query Loki",
                    "Monitor Loki stability",
                    "verify remediation 12"):
        assert btd.NON_TRIAGE_RE.match(trigger), trigger


def test_alert_shaped_triggers_are_not_filtered():
    for trigger in ("Pod foo not ready for 30m",
                    "Node raspberrypi4 unreachable",
                    "Per-host backup failed on raspberrypi3"):
        assert not btd.NON_TRIAGE_RE.match(trigger), trigger


def test_benchmark_cases_are_excluded():
    import triage_eval
    case_tokens = [btd._tokens(c["summary"]) for c in triage_eval.CASES]
    # Every eval case summary must exclude itself.
    for c in triage_eval.CASES:
        assert btd.overlaps_benchmark(c["summary"], case_tokens), c["name"]
    assert not btd.overlaps_benchmark(
        "Quebec Vigilance feed frozen for 6 hours", case_tokens)


# ---- severity / labels reconstruction -------------------------------------


def test_severity_derived_only_when_unambiguous():
    sev, src = btd.derive_severity(
        "Node ubuntu-cm5-01 (control plane) has been NotReady for 8 minutes")
    assert (sev, src) == ("critical", "derived")
    sev, src = btd.derive_severity("Quebec Vigilance feed frozen")
    assert (sev, src) == ("unknown", "unknown")


def test_pod_labels_handle_both_trigger_forms():
    assert btd.derive_labels("Pod infrastructure/ntfy-7b794cd687 not ready") \
        == {"pod": "ntfy-7b794cd687", "namespace": "infrastructure"}
    got = btd.derive_labels("Pod foo-abc in namespace apps was OOMKilled")
    assert got["pod"] == "foo-abc"
    assert got["namespace"] == "apps"
