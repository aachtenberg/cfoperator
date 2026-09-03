"""Tier 3 of the eval: grade the `reason`, not only the `action`.

These exist because the v2 fine-tune passed this harness 496/504 while
fabricating its evidence. On `novel-oom` it scored 36/36 on the action and
*every* reason cited a precedent that does not exist -- a different invented
pod each sample, on a case with no precedents at all.

The two reason checks docs/triage-eval-v2-plan.md specifies would not have
caught it either: the reason is not a v1 canned string, and it does contain a
token from the alert (it names the real pod, then appends a fictional one).
Grounding is not the absence of fabrication, and the tests below pin that
distinction -- a reason can be grounded and fabricating at the same time.
"""

from repo_paths import REPO_ROOT
import os
import sys

sys.path.insert(0, os.path.join(str(REPO_ROOT), "benchmarks"))
sys.path.insert(0, os.path.join(str(REPO_ROOT), "agent"))
sys.path.insert(0, str(REPO_ROOT))

import triage_eval as te  # noqa: E402


def _prompt(case_name):
    case = next(c for c in te.CASES if c["name"] == case_name)
    return te.build_user_message(case)


# Verbatim from the v2-q4 gate run, benchmarks/reason_compare.py, 2026-09-02.
V2_FABRICATED_OOM = (
    "no earlier investigation of paperless-ngx-7d9c4b8f5-nq2wm in history "
    "(nearest was paperless-ngx-7ccf888b4-85484...), needs a first look")
V2_FABRICATED_OUTAGE = (
    "nothing in history repeats ingress-nginx; closest was "
    "ingress-nginx-645cc7f7f8-2xjvx...")
V2_CORRECT_OUTAGE = (
    "severity=critical AND multiple services down (ingress-nginx, postgres, "
    "authentik); page an operator now")


def test_the_v2_fabrications_are_caught():
    grounded, fabricated = te.grade_reason(V2_FABRICATED_OOM,
                                           _prompt("novel-oom"))
    assert fabricated == ["paperless-ngx-7ccf888b4-85484"], fabricated
    grounded2, fabricated2 = te.grade_reason(V2_FABRICATED_OUTAGE,
                                             _prompt("correlated-outage"))
    assert fabricated2 == ["ingress-nginx-645cc7f7f8-2xjvx"], fabricated2


def test_a_reason_can_be_grounded_and_fabricating_at_once():
    """The precise reason the planned Tier 3 check was not enough. Both of the
    strings above name the real pod from the alert, so an "alert-grounded"
    check passes them while they invent a precedent."""
    for reason, case in ((V2_FABRICATED_OOM, "novel-oom"),
                         (V2_FABRICATED_OUTAGE, "correlated-outage")):
        grounded, fabricated = te.grade_reason(reason, _prompt(case))
        assert grounded, "the planned check would have passed this"
        assert fabricated, "and it is still a fabrication"


def test_a_correct_reason_passes_clean():
    grounded, fabricated = te.grade_reason(V2_CORRECT_OUTAGE,
                                           _prompt("correlated-outage"))
    assert grounded and fabricated == [], (grounded, fabricated)


def test_the_v1_canned_string_is_ungrounded():
    """The original CFOP-153 defect, still caught -- it names nothing."""
    grounded, fabricated = te.grade_reason(
        "no resolved precedent for this pattern", _prompt("novel-oom"))
    assert not grounded
    assert fabricated == []      # uninformative, but it never lied


def test_durations_exit_codes_and_cosines_are_not_citations():
    """The gate must not cry wolf. None of these are object names, and all of
    them appear in ordinary reasons."""
    _, fabricated = te.grade_reason(
        "pod was OOMKilled (exit code 137), 3 restarts in 10 minutes, "
        "30m window, 0.94 similarity, needs_action", _prompt("novel-oom"))
    assert fabricated == [], fabricated


def test_hyphenated_english_is_not_a_citation():
    """Frame vocabulary like 'known-noise' is not an object name."""
    _, fabricated = te.grade_reason(
        "smoke-test-panic matches the known-noise pattern 'smoke-test-'",
        "Alert summary: Pod smoke-test-panic is restarting")
    assert fabricated == [], fabricated


def test_every_run_persists_its_reason():
    """Tier 3's stated prerequisite. Without it a reason audit cannot be
    retroactive -- the v2 fabrication had to be re-queried live."""
    import inspect
    src = inspect.getsource(te.main)
    for key in ('"reason": reason', '"reason_fabricated"', '"reason_grounded"'):
        assert key in src, f"{key} missing from the persisted result row"
