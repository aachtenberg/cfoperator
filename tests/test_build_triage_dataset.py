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
import collections
import json
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
    # Clause subjects are quoted (see the grammar test below), so compare the
    # unquoted form against the alert.
    assert subject.strip('"').split()[0].lower() in trigger.lower()
    reason = btd.derive_label(trigger, "monitoring", [], labels)[2]
    assert "this alert" not in reason, reason


@pytest.mark.parametrize("trigger", [
    "Pod crash-looping since the deploy",
    "Pod not-ready after restart",
    "Pod out-of-memory killed",
])
def test_hyphenated_prose_is_not_an_object_name(trigger):
    """PR #240 review. The shape test accepted a hyphen as evidence of a
    Kubernetes name, and the comment claimed no English word carries one.
    Single words do not; COMPOUNDS do. The earlier test used the
    unhyphenated `crashlooping`, so it passed while `crash-looping` sailed
    through as {"pod": "crash-looping"}.

    Rejecting hyphens outright, or requiring a digit, was measured against
    the corpus first: it costs four real names whose random suffix happens
    to be all letters. The segment test costs none of them, which is why it
    is a segment test and not a stricter character class.
    """
    assert "pod" not in btd.derive_labels(trigger), btd.derive_labels(trigger)


@pytest.mark.parametrize("name", [
    "node-exporter-zgzxm", "kube-state-metrics", "promtail-fdppw",
    "node-exporter-nvtfv", "smoke-test-panic",
])
def test_real_hyphenated_names_without_digits_survive(name):
    """The four names a digit requirement would have cost, plus one that is
    entirely English-looking but real. Guarding the trade-off, not just the
    fix -- a future tightening should have to fail this deliberately."""
    assert btd.derive_labels(f"Pod {name} is unavailable").get("pod") == name


# ── PR #240 review round 3: what the frames teach ───────────────────────────
#
# These are targets a 14b model will imitate wholesale, including the parts
# not worth imitating. Each of these pins one such part.

def test_escalate_does_not_assert_blast_radius_it_cannot_see():
    """The dangerous canned suffix.

    The escalate LABEL comes from hindsight (the investigation ended
    escalate), but the reason is a triage-time claim. Asserting "severity and
    blast radius both present" on every escalate row is false on a single-pod
    page, and it trains the escalate reflex that the eval's critical-narrow
    and warning-correlated traps exist to catch -- on the highest-consequence
    action, which is the worst place for an unearned claim.
    """
    narrow = btd.derive_label("Pod camera-api-9x exited 255 repeatedly", "escalate", [],
                              btd.derive_labels("Pod camera-api-9x exited 255 repeatedly"))
    assert "blast radius" not in narrow[2], narrow[2]
    assert "page an operator" in narrow[2]

    broad_t = "Node raspberrypi3 NotReady, etcd quorum at risk"
    broad = btd.derive_label(broad_t, "escalate", [], btd.derive_labels(broad_t))
    assert "blast radius" in broad[2], broad[2]
    assert "quorum" in broad[2].lower()


@pytest.mark.parametrize("trigger,token", [
    ("Pod smoke-test-runner-7d9 crash-looping", "smoke-test-"),
    ("Pod tmp-runner-4d is stuck", "tmp-"),
])
def test_log_only_cites_the_token_that_matched(trigger, token):
    """"(test/watchdog)" was a fixed pair on every noise row, so a tmp- pod
    was described as watchdog traffic. The matched token is right there."""
    reason = btd.derive_label(trigger, "resolved", [], btd.derive_labels(trigger))[2]
    assert token in reason, reason
    assert "test/watchdog" not in reason, reason


@pytest.mark.parametrize("trigger", [
    "Backup did not complete",
    "Certificate expiration approaching",
])
def test_clause_subjects_are_quoted_so_the_frames_stay_grammatical(trigger):
    """Unquoted, a clause slotted into a noun-shaped frame is mad-libs:
    "Backup did not complete repeats an earlier investigation". Quoting makes
    one set of frames work for both object names and clauses."""
    subject = btd._alert_subject(trigger, btd.derive_labels(trigger))
    assert subject.startswith('"') and subject.endswith('"'), subject
    reason = btd.derive_label(trigger, "monitoring", [], btd.derive_labels(trigger))[2]
    assert f'"{trigger}"' in reason, reason


def test_the_cosine_appears_only_where_it_is_the_reason():
    """On notify the similarity IS why you did not investigate. On the
    no-precedent frame there is nothing to be similar to, so no number --
    a float in *every* frame would teach "a good reason contains a float".
    The near-miss frame does carry it; see the adjacency test below for why
    that reversed."""
    notify = btd.derive_label(
        "Pod promtail-2xvvb is dropping log entries", "resolved",
        [{"outcome": "resolved", "similarity": 0.94, "trigger": "earlier push failure"}],
        btd.derive_labels("Pod promtail-2xvvb is dropping log entries"))
    assert "0.94" in notify[2], notify[2]

    none = btd.derive_label(
        "Pod loki-0 restarting repeatedly", "monitoring", [],
        btd.derive_labels("Pod loki-0 restarting repeatedly"))
    assert not re.search(r"\d\.\d\d", none[2]), none[2]
    assert "no precedent" in none[2]


# Words that announce a similarity relationship. A name appearing just after
# one of these reads as "the thing we matched against" -- i.e. a citation.
# Deliberately not "match": the log_only frame's "matches the known-noise
# pattern 'x-'" is a regex match, not a precedent citation, and its grounding
# is covered by test_log_only_cites_the_token_that_matched. Every real
# citation phrasing carries clos*/near* anyway ("closest match", "nearest").
_CUE_RE = re.compile(
    r"\b(clos\w*|near\w*|similar\w*|resembl\w*|repeat\w*|"
    r"unlike|precedent\w*)\b", re.IGNORECASE)

# Object-name shaped, by the same test the builder uses to accept a name:
# an ordinary English word carries neither a digit nor a hyphen.
_NAME_SHAPED = re.compile(r"(?=.*[\d\-/])[A-Za-z0-9][A-Za-z0-9._/\-]*")

# How far after a cue word a name still reads as its object.
_WINDOW = 4


def _tokens(text):
    return {t.strip("(),;:.'\"") for t in str(text).split()}


def _citation_slots(reason, similar):
    """Yield (cue, name) for every ungrounded name close after a cue word.

    A name here is grounded only if it came from a *precedent* -- quoting the
    precedent you leaned on is the whole point of the notify frame. A name
    that came from the alert's own subject is NOT grounded in this position:
    it reads as "the thing we matched against" while actually being the thing
    being matched, which is the adjacency that taught v2 to invent one.
    """
    allowed = set()
    for s_ in similar or []:
        allowed |= _tokens(s_.get("trigger", ""))
    for m in _CUE_RE.finditer(reason):
        for tok in reason[m.end():].split()[:_WINDOW]:
            bare = tok.strip("(),;:").rstrip(".")
            if re.fullmatch(r"\d\.\d+", bare):
                break            # a cosine closes the slot: it IS the filler
            if _NAME_SHAPED.fullmatch(bare) and bare not in allowed:
                yield m.group(0), bare


@pytest.mark.parametrize("similar", [
    [],
    [{"outcome": "monitoring", "similarity": 0.62, "trigger": "earlier blip"}],
    [{"outcome": "monitoring", "similarity": 0.81, "trigger": "ingester pressure"}],
    [{"outcome": "needs_action", "similarity": 0.88, "trigger": "disk filled up"}],
    [{"outcome": "resolved", "similarity": 0.94,
      "trigger": "Pod loki-0 in monitoring restarted earlier"}],
])
@pytest.mark.parametrize("trigger", [
    "Pod loki-0 in namespace monitoring restarting repeatedly",
    "Node raspberrypi3 NotReady for 10m",
    "Pod apps/paperless-ngx-7d9c4b8f5-nq2wm was OOMKilled",
    "Multiple services unreachable: ingress-nginx, postgres and authentik",
    "Per-host backup failed on ubuntu-itx-01",
])
def test_no_similarity_cue_word_is_followed_by_a_name(trigger, similar):
    """A cue word may only be followed by a cosine, never by an object name.

    This is the defect that shipped in v2. The frame read "closest earlier
    match to {subject}" -- the subject is what the match is measured
    against, but on the surface it is a pod name sitting right after
    "closest", and 439 of 451 investigate rows taught that adjacency. The
    fine-tune reproduced it as "(nearest was <pod>)" on alerts with no
    precedent at all, inventing a different pod each sample. The action was
    still graded correct, so the eval passed a model that fabricates
    citations. Guard the adjacency, not the wording.
    """
    for outcome in ("monitoring", "needs_action", "resolved"):
        got = btd.derive_label(trigger, outcome, similar,
                               btd.derive_labels(trigger))
        if got is None:
            continue
        slots = list(_citation_slots(got[2], similar))
        assert not slots, (
            f"cue {slots[0][0]!r} is followed by {slots[0][1]!r}, which "
            f"reads as a citable object name: {got[2]!r}")


def _row(action, reason, idx=0):
    return {"messages": [
        {"role": "system", "content": "s"},
        {"role": "user", "content": f"alert {idx}"},
        {"role": "assistant", "content": json.dumps(
            {"action": action, "reason": reason, "confidence": 0.6})},
    ]}


def test_cap_leaves_thin_classes_alone():
    """The cap exists to shrink the majority class, so it must not shrink the
    class it is protecting. Capping per FRAME rather than per action is what
    makes that automatic: escalate's largest frame is smaller than the cap."""
    rows = ([_row("investigate", f"pod-{i} has no precedent", i) for i in range(50)]
            + [_row("escalate", f"Node node-{i} not ready — page now", i)
               for i in range(6)]
            + [_row("log_only", "smoke-test-x matches the pattern", 0)])
    kept, dropped = btd.cap_per_frame(rows, 8)
    got = collections.Counter(
        json.loads(r["messages"][2]["content"])["action"] for r in kept)
    assert got["investigate"] == 8, got
    assert got["escalate"] == 6, got      # under the cap, untouched
    assert got["log_only"] == 1, got
    assert dropped == 42


def test_cap_keeps_rows_spread_over_time_not_the_oldest():
    """The train/val split is temporal — newest slice is validation. Keeping
    the first N of every capped frame would leave validation with none of the
    frames that were capped, which is exactly the ones worth validating."""
    rows = [_row("investigate", f"pod-{i} has no precedent", i) for i in range(40)]
    kept, _ = btd.cap_per_frame(rows, 4)
    idxs = [int(r["messages"][1]["content"].split()[1]) for r in kept]
    assert idxs[0] == 0 and idxs[-1] == 39, idxs   # spans the whole range
    assert max(idxs) - min(idxs) == 39, idxs
    assert idxs != list(range(4)), "kept the oldest four, not a spread"


def test_cap_is_a_no_op_when_disabled():
    rows = [_row("investigate", "same frame here", i) for i in range(30)]
    kept, dropped = btd.cap_per_frame(rows, 0)
    assert len(kept) == 30 and dropped == 0


def test_cap_targets_repetition_not_class_size():
    """Per FRAME, not per action. A class with many rows is fine if they are
    genuinely different sentences; what wastes a retrain is the same sentence
    with a different name substituted. Capping per action would punish a
    varied class as hard as a repetitive one, and would shrink escalate for
    the crime of existing."""
    varied = [_row("notify", f"pod-{i} repeats an earlier investigation "
                             f"that resolved: distinct precedent {w}", i)
              for i, w in enumerate("alpha bravo charlie delta echo foxtrot "
                                    "golf hotel india juliet kilo lima".split())]
    repetitive = [_row("investigate", f"pod-{i} has no precedent", 100 + i)
                  for i in range(12)]
    kept, _ = btd.cap_per_frame(varied + repetitive, 8)
    got = collections.Counter(
        json.loads(r["messages"][2]["content"])["action"] for r in kept)
    assert got["notify"] == 12, f"varied rows were capped: {got}"
    assert got["investigate"] == 8, f"repetitive rows were not capped: {got}"


@pytest.mark.parametrize("trigger,expect", [
    # The word before "namespace" is the namespace at least as often as the
    # word after it. Missing this labelled kube-system as "experiencing".
    ("Traefik pod in kube-system namespace experiencing I/O timeouts",
     "kube-system"),
    ("Deployment in the monitoring namespace is degraded", "monitoring"),
    # ...and the trailing form still wins when the leading capture is only a
    # preposition, which the stopword list rejects.
    ("Pod foo-1 in namespace apps is stuck in Pending", "apps"),
])
def test_namespace_is_found_on_either_side_of_the_word(trigger, expect):
    assert btd.derive_labels(trigger).get("namespace") == expect


@pytest.mark.parametrize("trigger", [
    # "api" contains "pi". As a bare substring the marker also accepts
    # "rapid" and "capital"; a hostname in prose is not a node.
    "LLM provider is failing with HTTP 403 Forbidden on api.x.ai",
    "backup stalled on rapid-sync stage",
])
def test_a_word_merely_containing_a_node_marker_is_not_a_node(trigger):
    assert "node" not in btd.derive_labels(trigger), btd.derive_labels(trigger)


@pytest.mark.parametrize("trigger,expect", [
    ("Node raspberrypi3 NotReady for 10m", "raspberrypi3"),
    ("Node raspberrypi unreachable", "raspberrypi"),
    ("Per-host backup failed on ubuntu-cm5-01", "ubuntu-cm5-01"),
    ("Pod x on headless-gpu was OOMKilled", "headless-gpu"),
    ("agent restarted on ubuntu-llm-01", "ubuntu-llm-01"),
])
def test_real_node_names_still_extract(trigger, expect):
    assert btd.derive_labels(trigger).get("node") == expect
