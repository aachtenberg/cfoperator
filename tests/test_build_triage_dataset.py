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
import re
import pytest

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


# ── CFOP-153: reasons must describe the alert, not the action ────────────────
#
# v1 emitted one fixed string per action, so `reason` and `confidence` were
# renames of `action` and the fine-tune learned that four-way lookup. These
# pin the properties that stop it recurring, and they are the same properties
# docs/triage-eval-v2-plan.md Tier 3 grades on the model's output.

V1_CANNED_REASONS = frozenset({
    "no resolved precedent for this pattern",
    "similar past investigation resolved with little effort",
    "critical with broad impact, operator should page in",
    "known noise pattern (test pod or watchdog heartbeat)",
})


def _derive(trigger, outcome, similar_past=()):
    labels = btd.derive_labels(trigger)
    return btd.derive_label(trigger, outcome, list(similar_past), labels), labels


_ALERTS = [
    ("Pod promtail-2xvvb is dropping log entries", "resolved",
     [{"outcome": "resolved", "similarity": 0.94, "trigger": "promtail on raspberrypi2 failed to push"}]),
    ("Pod promtail-9zzzz is dropping log entries", "monitoring",
     [{"outcome": "resolved", "similarity": 0.87, "trigger": "promtail push failure on pi2"}]),
    ("Pod loki-0 restarting repeatedly", "monitoring",
     [{"outcome": "monitoring", "similarity": 0.81, "trigger": "loki ingester memory pressure"}]),
    ("Pod camera-api-5f in namespace apps exited 255", "needs_action", []),
    ("Node raspberrypi3 NotReady, etcd quorum at risk", "escalate", []),
    ("Pod smoke-test-runner-7d9 in namespace ci is crash-looping", "resolved", []),
]


def test_no_reason_is_a_v1_canned_string():
    """The regression itself. Any of the four v1 templates coming back means
    the dataset would train the lookup again."""
    for trigger, outcome, sp in _ALERTS:
        derived, _ = _derive(trigger, outcome, sp)
        assert derived is not None, trigger
        reason = derived[2].strip().lower()
        assert reason not in {c.lower() for c in V1_CANNED_REASONS}, trigger


def test_reasons_are_distinct_across_alerts():
    """Six different alerts must not collapse to four strings."""
    reasons = {_derive(t, o, sp)[0][2] for t, o, sp in _ALERTS}
    assert len(reasons) == len(_ALERTS), sorted(reasons)


def test_every_reason_names_something_from_its_alert():
    """Grounding: the reason has to contain a token the alert contains.

    This is Tier 3's `anchors` check applied to the training targets. A model
    cannot learn to cite the alert from data that does not.
    """
    for trigger, outcome, sp in _ALERTS:
        derived, labels = _derive(trigger, outcome, sp)
        reason = derived[2].lower()
        anchors = {v.lower() for v in labels.values()}
        anchors |= {t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]{3,}", trigger)}
        assert any(a in reason for a in anchors), (trigger, derived[2])


def test_confidence_is_not_an_action_alias():
    """v1's confidence took exactly one value per action. At least one class
    must now vary, or it is still a rename of the label."""
    by_action = {}
    for trigger, outcome, sp in _ALERTS:
        action, _, _, conf = _derive(trigger, outcome, sp)[0]
        by_action.setdefault(action, set()).add(conf)
    assert any(len(v) > 1 for v in by_action.values()), by_action


def test_confidence_tracks_precedent_strength_for_notify():
    """A 0.94 precedent is better evidence than a 0.87 one, and the number
    should say so rather than being a constant."""
    strong = _derive("Pod promtail-2xvvb is dropping log entries", "resolved",
                     [{"outcome": "resolved", "similarity": 0.94, "trigger": "x"}])[0]
    weak = _derive("Pod promtail-2xvvb is dropping log entries", "resolved",
                   [{"outcome": "resolved", "similarity": 0.86, "trigger": "x"}])[0]
    assert strong[0] == weak[0] == "notify"
    assert strong[3] > weak[3], (strong[3], weak[3])


def test_near_miss_precedent_is_less_confident_than_no_precedent():
    """Something similar happened and did NOT resolve cheaply -- that is more
    ambiguous than a genuinely novel alert, and the confidence should fall."""
    near = _derive("Pod loki-0 restarting repeatedly", "monitoring",
                   [{"outcome": "monitoring", "similarity": 0.83, "trigger": "x"}])[0]
    novel = _derive("Pod loki-0 restarting repeatedly", "monitoring", [])[0]
    assert near[0] == novel[0] == "investigate"
    assert near[3] < novel[3], (near[3], novel[3])


def test_deep_tier_reroute_stays_unreachable():
    """Deliberate, and asserted so it is a decision rather than an accident:
    nothing emits below the 0.4 deep-investigation reroute threshold. Making
    that path live is a separate behaviour change (CFOP-153 plan)."""
    for trigger, outcome, sp in _ALERTS:
        assert _derive(trigger, outcome, sp)[0][3] >= 0.45


# ── CFOP-153: the Labels stopword bug ───────────────────────────────────────

@pytest.mark.parametrize("trigger", [
    "Pod with high memory usage detected",
    "Pod has been restarting",
    "namespace has been terminating for 10 minutes",
])
def test_prose_words_are_not_taken_as_object_names(trigger):
    """~19% of populated v1 labels were English stopwords: {"pod": "with"}."""
    labels = btd.derive_labels(trigger)
    assert "with" not in labels.values() and "has" not in labels.values(), labels
    assert all(re.search(r"[-0-9]", v) for v in labels.values()), labels


@pytest.mark.parametrize("trigger,expect", [
    ("Pod promtail-2xvvb is dropping log entries", {"pod": "promtail-2xvvb"}),
    ("Pod apps/camera-api-5f exited 255", {"pod": "camera-api-5f", "namespace": "apps"}),
    ("Node raspberrypi3 NotReady", {"node": "raspberrypi3"}),
])
def test_real_object_names_still_extract(trigger, expect):
    """The shape test must not throw away the labels that were working."""
    got = btd.derive_labels(trigger)
    for k, v in expect.items():
        assert got.get(k) == v, (trigger, got)


@pytest.mark.parametrize("trigger,key,expect", [
    # Caught in development: the first shape test required a digit or hyphen
    # everywhere, which dropped all three of these. Namespaces are plain
    # words and one node is literally named `raspberrypi`, so the shape test
    # applies only to captures with no structural anchor.
    ("Pod apps/camera-api-5f exited 255", "namespace", "apps"),
    ("Pod monitoring/loki-0 restarting", "namespace", "monitoring"),
    ("Node raspberrypi NotReady", "node", "raspberrypi"),
])
def test_anchored_captures_keep_plain_names(trigger, key, expect):
    assert btd.derive_labels(trigger).get(key) == expect, btd.derive_labels(trigger)


@pytest.mark.parametrize("trigger", [
    "Pod restarting frequently on the edge node",
    "Pod crashlooping since the deploy",
    "Pod terminating unexpectedly",
])
def test_prose_words_outside_the_stopword_list_are_still_rejected(trigger):
    """The shape test's own case, and it needs to exist.

    `with` and `has` are in _LABEL_STOPWORDS, so the earlier stopword tests
    pass whether or not the digit/hyphen requirement is present -- removing
    it left the whole suite green. These words are NOT in the list, so only
    the shape test rejects them. No stopword list will ever cover English;
    the shape requirement is what makes the guard general.
    """
    assert "pod" not in btd.derive_labels(trigger), btd.derive_labels(trigger)


def test_resolved_precedent_wins_even_when_outranked_by_a_monitoring_one():
    """The notify rule is ANY resolved hit at >=0.85, not "the closest
    precedent is resolved".

    Introduced this bug while adding grounded reasons: switching to the
    top-ranked precedent silently reclassified rows whose nearest match was
    `monitoring` but which had a resolved hit just behind it. 35 notify
    examples vanished (96 -> 61) and every existing test stayed green,
    because none of them put a higher-similarity non-resolved precedent in
    front of a qualifying one. It moves the LABEL while looking like a
    wording change, which is the worst shape for a dataset bug.
    """
    similar = [
        {"outcome": "monitoring", "similarity": 0.91, "trigger": "noisy neighbour"},
        {"outcome": "resolved", "similarity": 0.88, "trigger": "same fault, fixed last week"},
    ]
    action, basis, reason, conf = btd.derive_label(
        "Pod loki-0 restarting repeatedly", "resolved", similar,
        btd.derive_labels("Pod loki-0 restarting repeatedly"))
    assert action == "notify", (action, reason)
    assert basis == "resolved-precedent"
    # and it must cite the resolved one, not the closer monitoring one
    assert "0.88" in reason and "fixed last week" in reason, reason


def test_slash_anchored_namespace_survives_an_unusable_pod_name():
    """PR #240 review. The namespace extraction was nested under `if pod`, so
    a pod name that fails the prose shape test took the namespace down with
    it -- discarding a slash-anchored fact because of an unrelated one, and
    contradicting the contract _plausible_k8s_name documents."""
    assert btd.derive_labels("Pod apps/prometheus is unavailable") == {"namespace": "apps"}


@pytest.mark.parametrize("trigger", [
    "Certificate expiration approaching",
    "Disk usage high on storage volume",
    "Backup did not complete",
])
def test_alerts_naming_no_object_still_get_a_grounded_subject(trigger):
    """PR #240 review. Falling back to "this alert" made 20% of v2 reasons
    generic; they satisfied a token-overlap check only via the similarity
    number, which is grounding in the letter and not the spirit. Plenty of
    real alerts name no pod, node or namespace, and their own words are
    always on-topic."""
    labels = btd.derive_labels(trigger)
    subject = btd._alert_subject(trigger, labels)
    assert subject != "this alert"
    assert subject.split()[0].lower() in trigger.lower()
    reason = btd.derive_label(trigger, "monitoring", [], labels)[2]
    assert "this alert" not in reason, reason
