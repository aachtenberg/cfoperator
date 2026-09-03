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
    # Both a named invention AND an unnamed precedent claim; the second check
    # was added after the 8B v3 made the claim without naming anything.
    assert "paperless-ngx-7ccf888b4-85484" in fabricated, fabricated
    grounded2, fabricated2 = te.grade_reason(V2_FABRICATED_OUTAGE,
                                             _prompt("correlated-outage"))
    assert "ingress-nginx-645cc7f7f8-2xjvx" in fabricated2, fabricated2


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


def test_an_invented_ip_is_a_fabrication():
    """Node addresses are object identifiers here and carry no letters, so the
    alphanumeric rule alone would skip them and an invented IP would pass."""
    prompt = "Alert summary: Node at 192.168.0.210 stopped posting status"
    _, fabricated = te.grade_reason(
        "192.168.0.210 is unlike anything in history (best match 0.69)", prompt)
    assert fabricated == [], fabricated
    _, fabricated = te.grade_reason(
        "closest earlier match was 192.168.0.99 which resolved", prompt)
    assert "192.168.0.99" in fabricated, fabricated


def test_a_cosine_is_still_not_an_ip():
    # known-sdcard carries a real precedent, so the claim itself is fine and
    # the only question is whether "0.94" reads as an address. It must not.
    _, fabricated = te.grade_reason(
        "raspberrypi3 repeats an earlier investigation (0.94 similarity)",
        _prompt("known-sdcard"))
    assert fabricated == [], fabricated


# Verbatim from the 8B v3 Q4 run, benchmarks/reason_compare.py, 2026-09-03.
V3_8B_UNNAMED = ("tmp-restore-verify-9x2kd: the closest earlier investigation "
                 "I found ended monitoring — no resolved precedent to lean on")
V3_8B_HONEST = ('"paperless-ngx-7d9c4b8f5-nq2wm" is unlike anything in history '
                "— needs a first look")


def test_an_unnamed_precedent_on_a_prompt_with_none_is_a_fabrication():
    """No pod name, so the token check cannot see it; the prompt has no
    similar-past block, so the precedent it describes does not exist."""
    _, fabricated = te.grade_reason(V3_8B_UNNAMED, _prompt("tmp-pod-critical"))
    assert fabricated == ["<unnamed precedent>"], fabricated


def test_the_no_precedent_frames_are_not_precedent_claims():
    for reason in (V3_8B_HONEST,
                   "paperless-ngx-7d9c4b8f5-nq2wm has no precedent in the "
                   "investigation history — needs a first look"):
        _, fabricated = te.grade_reason(reason, _prompt("novel-oom"))
        assert fabricated == [], (reason, fabricated)


def test_a_precedent_claim_is_fine_when_the_prompt_offers_one():
    """known-sdcard carries a real precedent; citing it is the point."""
    _, fabricated = te.grade_reason(
        "raspberrypi3 repeats an earlier investigation that resolved (0.94 similarity): "
        "raspberrypi3 SD card I/O errors after power loss", _prompt("known-sdcard"))
    assert fabricated == [], fabricated


# 8B v4 (2026-09-03), smoke-test-pod, verbatim. The pod suffix is quoted with
# curly quotes; the token must be read as 2xk4f (in the prompt), not “2xk4f”.
V4_8B_CURLY = ("smoke-test-runner-2xk4f ends in -2xk4f: the “2xk4f” suffix marks it "
               "as a test pod (smoke-test-…) — page nothing")


def test_curly_quoted_token_from_the_prompt_is_not_a_fabrication():
    prompt = ("Alert severity: warning\nAlert summary: Pod smoke-test-runner-2xk4f "
              "in namespace ci is crash-looping\nLabels: {}\n\nClassify.")
    grounded, fabricated = te.grade_reason(V4_8B_CURLY, prompt)
    assert grounded and fabricated == [], fabricated
