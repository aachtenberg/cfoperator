#!/usr/bin/env python3
"""CFOP-80: structured FIX beside RECOMMENDATION.

Parse-or-None (never salvage). A valid FIX skips the classifier; a missing
or malformed FIX still classifies. Class of regression, not output pins.
"""

import json
import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import (  # noqa: E402
    _AUTO_REMEDIATION_CLASSES,
    normalize_remediation_fields,
    remediation_is_auto_eligible,
)
from agent import CFOperator  # noqa: E402
from agent.agent import (  # noqa: E402
    _FIX_KIND_TO_CLASS,
    _FORK_SHAPED,
    _class_from_fix_kind,
    _delivery_guidance,
    _delivery_unset_warning,
    _fix_targets_dedupe_key,
    _hints_from_structured_fix,
    _parse_structured_fix,
    _resolve_fix_repo,
)
from test_remediation_queue import _na_op  # noqa: E402


def _valid_fix(**overrides):
    fix = {
        "targets": [{
            "kind": "gitops-manifest",
            "id": "apps/promtail.yaml",
            "repo": "aachtenberg/homelab-infra",
        }],
        "observed": [{
            "source": "kubectl -n monitoring get deploy promtail -o yaml",
            "value": "resources.limits.memory: 256Mi",
        }],
        "steps": ["raise memory limit to 512Mi"],
        "verify": {
            "command": "kubectl -n monitoring get deploy promtail -o yaml",
            "expect": "memory: 512Mi",
        },
        "rejected": [{
            "alternative": "delete the pod",
            "why_not": "it will OOM again",
        }],
        "risk": "low",
    }
    fix.update(overrides)
    return fix


def _report(fix=None, rec="raise memory limit to 512Mi"):
    body = (
        "STATUS: needs_action\n"
        f"RECOMMENDATION: {rec}\n"
    )
    if fix is not None:
        body += "FIX: " + json.dumps(fix)
    return body


def _auto(hints):
    nclass, nrisk = normalize_remediation_fields(
        hints["remediation_class"], hints["risk"])
    return remediation_is_auto_eligible(nclass, nrisk, hints["confidence"])


# ---- parse -------------------------------------------------------------------


def test_parse_fix_object_and_fenced_json():
    parsed = _parse_structured_fix(_report(_valid_fix()))
    assert parsed is not None
    assert parsed["targets"][0]["kind"] == "gitops-manifest"
    assert parsed["steps"] == ["raise memory limit to 512Mi"]
    assert parsed["verify"]["command"]
    assert parsed["verify"]["expect"]
    assert parsed["risk"] == "low"

    fenced = (
        "STATUS: needs_action\nRECOMMENDATION: raise memory\nFIX:\n"
        "```json\n" + json.dumps(_valid_fix()) + "\n```\n"
    )
    assert _parse_structured_fix(fenced)["targets"][0]["id"] == "apps/promtail.yaml"
    # nudge reply may be the object alone
    assert _parse_structured_fix(json.dumps(_valid_fix())) is not None


def test_malformed_fix_is_none_never_salvaged():
    """Missing required fields, bad types, bad risk → None. Do not fill them in."""
    assert _parse_structured_fix("") is None
    assert _parse_structured_fix("STATUS: needs_action\nRECOMMENDATION: x") is None
    assert _parse_structured_fix("FIX: {not json") is None
    assert _parse_structured_fix(_report({"steps": ["x"]})) is None  # no targets
    assert _parse_structured_fix(_report({**_valid_fix(), "targets": []})) is None
    assert _parse_structured_fix(_report({**_valid_fix(), "targets": "host"})) is None
    assert _parse_structured_fix(_report({**_valid_fix(), "targets": [{"kind": "host"}]})) is None
    assert _parse_structured_fix(_report({**_valid_fix(), "steps": []})) is None
    assert _parse_structured_fix(_report({**_valid_fix(), "verify": {"command": "x"}})) is None
    assert _parse_structured_fix(_report({**_valid_fix(), "risk": "critical"})) is None
    # extra keys are dropped, not treated as salvage of missing required ones
    extra = _valid_fix()
    extra["note"] = "ignore me"
    assert _parse_structured_fix(_report(extra))["targets"]


def test_hotfix_prose_does_not_steal_a_later_fix():
    """'hotfix:' contains the substring 'fix:'; the parser must still take
    the line-anchored FIX: object (and must not accept the findings JSON)."""
    body = (
        "STATUS: needs_action\n"
        "Consider the hotfix: {\"pod\": \"promtail\", \"restarts\": 4}\n"
        "RECOMMENDATION: raise memory limit to 512Mi\n"
        "FIX: " + json.dumps(_valid_fix())
    )
    parsed = _parse_structured_fix(body)
    assert parsed is not None
    assert parsed["targets"][0]["id"] == "apps/promtail.yaml"


def test_kind_maps_to_class_unknown_is_manual():
    for kind, rclass in _FIX_KIND_TO_CLASS.items():
        assert _class_from_fix_kind(kind) == rclass
        assert _class_from_fix_kind(kind.upper()) == rclass
    assert _class_from_fix_kind("sprocket") == "manual"
    assert _class_from_fix_kind("") == "manual"
    assert _class_from_fix_kind(None) == "manual"
    # unknown kind still parses; class becomes manual at enqueue, not at parse
    odd = _valid_fix(targets=[{"kind": "sprocket", "id": "x"}])
    parsed = _parse_structured_fix(_report(odd))
    assert parsed is not None
    assert _hints_from_structured_fix(parsed)["remediation_class"] == "manual"


# ---- skip classifier (mutation check) ----------------------------------------


def test_valid_fix_skips_classifier():
    """Mutation check: drop the `if fix:` branch in
    _queue_needs_action_remediation and this fails (classifier returns
    manual, enqueue class is no longer gitops-patch)."""
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    rid = op._queue_needs_action_remediation(
        80, "Promtail OOM",
        {"fingerprint": "fp-fix", "labels": {"instance": "raspberrypi5"}},
        "raise memory limit to 512Mi", _report(_valid_fix()),
        provider="ollama/gemma4:26b")
    assert rid == 9
    op._classify_needs_action_recommendation.assert_not_called()
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["remediation_class"] == "gitops-patch"
    assert kw["risk"] == "low"
    assert kw["confidence"] == 0.8


def test_missing_fix_still_calls_classifier():
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.5,
        "host": None, "repo": None})
    rid = op._queue_needs_action_remediation(
        80, "Promtail OOM", {"fingerprint": "fp-plain"},
        "raise memory limit to 512Mi", "report text",
        provider="p")
    assert rid == 9
    op._classify_needs_action_recommendation.assert_called_once()
    # fingerprint-first path is unchanged when FIX is absent
    assert op.kb.queue_remediation.call_args.kwargs["dedupe_key"] == "alert-fp-plain"


def test_malformed_fix_still_calls_classifier():
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    broken = _report({"targets": [], "steps": ["x"],
                      "verify": {"command": "c", "expect": "e"}})
    op._queue_needs_action_remediation(
        80, "t", {}, "raise memory limit to 512Mi", broken, provider="p")
    op._classify_needs_action_recommendation.assert_called_once()


def test_fork_commit_does_not_keep_fix_auto_confidence():
    """Successful CFOP-78 commit rewrites rec to action B; FIX still
    describes the fork (action A). gitops-manifest + risk low must not keep
    confidence 0.8 — that is live row #72 (PR #173).

    Mutation check: if `if still_forked or was_forked` becomes `if still_forked`,
    this fails. Classifier-skip tests that use a non-fork rec stay green.
    """
    rec = ("Truncate the users row, or update the embedding_service "
           "configuration to a larger model.")
    assert _FORK_SHAPED.search(rec)
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    op._commit_forked_recommendation = (
        lambda t, r, report='': ("truncate the users row", False))
    rid = op._queue_needs_action_remediation(
        80, "embedding overflow", {}, rec,
        _report(_valid_fix(), rec=rec),
        provider="p", structured_fix=_valid_fix())
    assert rid == 9
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["confidence"] is None
    assert kw["payload"]["recommendation"] == "truncate the users row"
    # class may stay from target.kind; the auto gate must not
    assert kw["remediation_class"] == "gitops-patch"
    assert not remediation_is_auto_eligible(
        kw["remediation_class"], kw["risk"], kw["confidence"])
    # a non-fork rec still takes FIX auto-confidence (the skip-classifier test)


def test_invalid_structured_fix_kwarg_still_classifies():
    """A truthy but invalid structured_fix must not IndexError in hints."""
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    rid = op._queue_needs_action_remediation(
        80, "t", {}, "raise memory limit to 512Mi", "report text",
        provider="p", structured_fix={"targets": []})
    assert rid == 9
    op._classify_needs_action_recommendation.assert_called_once()


# ---- dedupe ------------------------------------------------------------------


def test_dedupe_key_collapses_wordings_and_differs_from_fingerprint():
    key = CFOperator._investigation_dedupe_key
    a = _valid_fix(steps=["edit the manifest memory field"])
    b = _valid_fix(steps=["bump the limit in the yaml"],
                  rejected=[{"alternative": "restart", "why_not": "it comes back"}])
    alert = {"fingerprint": "same-alert"}
    k1 = key(alert, "wording one about memory", a)
    k2 = key(alert, "a totally different sentence", b)
    assert k1 == k2 and k1.startswith("tgt-")
    # fingerprint-only path (no FIX) is a different key, not a tgt- rewrite
    fp = key(alert, "wording one about memory")
    assert fp == "alert-same-alert"
    assert fp != k1
    # dispatch stamp still wins over FIX
    stamped = {"dedupe_key": "inv-dispatch-x", "fingerprint": "same-alert"}
    assert key(stamped, "rec", a) == "inv-dispatch-x"
    # two-arg callers (no FIX kwarg) stay on the fingerprint path
    assert key({"fingerprint": "f00d"}, "rec") == "alert-f00d"


def test_dedupe_key_is_order_insensitive_on_targets():
    t1 = {"kind": "gitops-manifest", "id": "a.yaml", "repo": "o/r"}
    t2 = {"kind": "host", "id": "pi5"}
    k1 = _fix_targets_dedupe_key({"targets": [t1, t2]})
    k2 = _fix_targets_dedupe_key({"targets": [t2, t1]})
    assert k1 == k2


# ---- auto-eligibility --------------------------------------------------------


def test_multi_target_data_fix_external_system_never_auto_eligible():
    gitops = {"kind": "gitops-manifest", "id": "a.yaml", "repo": "o/r"}
    host = {"kind": "host", "id": "pi5"}
    multi = _hints_from_structured_fix(_valid_fix(
        targets=[gitops, host], risk="low"))
    assert multi["confidence"] is None
    assert _auto(multi) is False

    row = _hints_from_structured_fix(_valid_fix(
        targets=[{"kind": "database-row", "id": "users.id=12"}], risk="low"))
    assert row["remediation_class"] == "data-fix"
    assert row["confidence"] is None
    assert _auto(row) is False
    assert "data-fix" not in _AUTO_REMEDIATION_CLASSES

    ext = _hints_from_structured_fix(_valid_fix(
        targets=[{"kind": "external-system", "id": "cloudflare"}], risk="low"))
    assert ext["remediation_class"] == "external-system"
    assert ext["confidence"] is None
    assert _auto(ext) is False
    assert "external-system" not in _AUTO_REMEDIATION_CLASSES

    # only single gitops-manifest + low may still reach the CFOP-70 judge
    solo = _hints_from_structured_fix(_valid_fix(risk="low"))
    assert solo["remediation_class"] == "gitops-patch"
    assert solo["confidence"] == 0.8
    assert _auto(solo) is True
    # k8s-object is auto-class but FIX does not invent confidence
    obj = _hints_from_structured_fix(_valid_fix(
        targets=[{"kind": "k8s-object", "id": "deploy/x"}], risk="low"))
    assert obj["remediation_class"] == "k8s-action"
    assert obj["confidence"] is None
    assert _auto(obj) is False


# ---- payload -----------------------------------------------------------------


def test_payload_carries_steps_verify_beside_target_host():
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    fix = _valid_fix(targets=[{
        "kind": "host", "id": "raspberrypi4",
    }], risk="high")
    op._queue_needs_action_remediation(
        80, "disk full",
        {"fingerprint": "disk-1", "labels": {"instance": "raspberrypi5"}},
        "clean /var/log on raspberrypi4", _report(fix, rec="clean /var/log on raspberrypi4"),
        provider="p")
    op._classify_needs_action_recommendation.assert_not_called()
    kw = op.kb.queue_remediation.call_args.kwargs
    payload = kw["payload"]
    assert payload["steps"] == ["raise memory limit to 512Mi"]
    assert payload["verify"]["command"]
    assert payload["verify"]["expect"]
    assert payload["rejected"]
    assert payload["targets"][0]["kind"] == "host"
    # FIX rides beside target.host, never replaces it
    assert payload["target"]["host"] == "raspberrypi4"
    assert kw["host_id"] == "raspberrypi4"
    assert kw["remediation_class"] == "node-action"
    assert kw["confidence"] is None
    assert kw["dedupe_key"].startswith("tgt-")
    assert payload["dedupe_key"] == kw["dedupe_key"]


# ---- recommendation extraction / nudge ---------------------------------------


def test_extract_recommendation_stops_at_fix():
    text = _report(_valid_fix(), rec="raise memory limit to 512Mi")
    rec = CFOperator._extract_recommendation(text)
    assert rec == "raise memory limit to 512Mi"
    assert "targets" not in rec
    assert "{" not in rec


def test_ensure_structured_fix_nudges_once_then_degrades():
    op = MagicMock()
    # A bare MagicMock's git_repos() iterates empty, which now correctly means
    # "nothing resolves" and would sink every gitops-manifest FIX here for a
    # reason that has nothing to do with nudging (CFOP-85).
    op.git_repos.return_value = _REGISTRY
    rec = "raise memory limit to 512Mi"
    bare = "STATUS: needs_action\nRECOMMENDATION: " + rec
    # already valid — no nudge
    op._nudge_structured_fix = MagicMock()
    fix, out = CFOperator._ensure_structured_fix(op, "needs_action", rec, _report(_valid_fix()))
    assert fix["targets"]
    op._nudge_structured_fix.assert_not_called()
    # missing — one nudge, parsed object is used
    op._nudge_structured_fix = MagicMock(return_value=json.dumps(_valid_fix()))
    fix, out = CFOperator._ensure_structured_fix(op, "needs_action", rec, bare)
    assert fix is not None
    op._nudge_structured_fix.assert_called_once()
    assert "FIX:" in out
    # nudge garbage — degrade, do not fail
    op._nudge_structured_fix = MagicMock(return_value="not json")
    fix, out = CFOperator._ensure_structured_fix(op, "needs_action", rec, bare)
    assert fix is None
    assert out == bare
    # "no action" — no nudge
    op._nudge_structured_fix = MagicMock()
    fix, out = CFOperator._ensure_structured_fix(
        op, "needs_action", "No action needed", bare)
    assert fix is None
    op._nudge_structured_fix.assert_not_called()
    # non-needs_action — no nudge even if FIX missing
    CFOperator._ensure_structured_fix(op, "resolved", rec, bare)
    op._nudge_structured_fix.assert_not_called()


# ---- repo resolution (CFOP-85) -----------------------------------------------
#
# `repo` was the one field the schema waved through: any non-empty string was
# accepted. Remediation #77 carried repo="gitops-patch" -- the remediation
# CLASS, which is also what _class_from_fix_kind('gitops-manifest') returns, so
# a plausible thing for a model to grab with the word in front of it. The
# executor hands that string to GitHub, gets nothing back, and parks the row
# four seconds after an operator approved it.

_REGISTRY = [
    {"name": "homelab-infra", "github": "aachtenberg/homelab-infra"},
    {"name": "cfoperator", "github": "aachtenberg/cfoperator"},
]


def test_a_repo_that_is_not_a_repo_sinks_a_manifest_fix():
    """The #77 case, verbatim.

    A manifest patch whose repo does not resolve is not a near-miss: the
    executor's first act is to list that repo's files, so the row can only
    bounce. Refusing here costs one recommendation and saves an approval that
    was never going to land.
    """
    fix = _valid_fix(targets=[{
        "kind": "gitops-manifest",
        "id": "homelab-root",
        "repo": "gitops-patch",
    }])
    assert _parse_structured_fix(_report(fix), _REGISTRY) is None


def test_a_short_name_resolves_to_the_slug_the_executor_needs():
    """The quieter half of the same bug.

    executor/entrypoint.py asks GitHub for `owner/name`. A bare short name is
    a reasonable thing to emit and would have failed exactly as hard as a
    wrong one, so resolution normalises rather than merely accepting.
    """
    fix = _valid_fix(targets=[{
        "kind": "gitops-manifest",
        "id": "apps/promtail.yaml",
        "repo": "homelab-infra",
    }])
    parsed = _parse_structured_fix(_report(fix), _REGISTRY)
    assert parsed is not None
    assert parsed["targets"][0]["repo"] == "aachtenberg/homelab-infra"


def test_a_slug_survives_resolution_unchanged():
    parsed = _parse_structured_fix(_report(_valid_fix()), _REGISTRY)
    assert parsed is not None
    assert parsed["targets"][0]["repo"] == "aachtenberg/homelab-infra"


def test_resolution_ignores_case():
    assert _resolve_fix_repo("HomeLab-Infra", _REGISTRY) == "aachtenberg/homelab-infra"
    assert _resolve_fix_repo("AAchtenberg/Homelab-Infra", _REGISTRY) == (
        "aachtenberg/homelab-infra")


def test_an_unresolvable_repo_is_dropped_rather_than_fatal_off_a_manifest():
    """A host target carries repo incidentally and is actionable without one,
    so a bad value costs the field and not the finding."""
    fix = _valid_fix(targets=[{
        "kind": "host",
        "id": "ubuntu-llm-01",
        "repo": "not-a-repo",
    }])
    parsed = _parse_structured_fix(_report(fix), _REGISTRY)
    assert parsed is not None
    assert "repo" not in parsed["targets"][0]


def test_no_registry_is_not_the_same_as_an_empty_one():
    """A caller that cannot supply the registry must not be treated as one
    reporting that nothing resolves -- the first cannot judge, the second
    judges everything invalid. Every pre-existing caller passes nothing, so
    conflating them would silently void every FIX in the system.
    """
    assert _resolve_fix_repo("gitops-patch", None) == "gitops-patch"
    assert _resolve_fix_repo("gitops-patch", []) is None


def test_an_empty_or_missing_repo_is_still_fatal_only_for_a_manifest():
    no_repo = _valid_fix(targets=[{"kind": "gitops-manifest", "id": "x"}])
    assert _parse_structured_fix(_report(no_repo), _REGISTRY) is None
    host = _valid_fix(targets=[{"kind": "host", "id": "ubuntu-llm-01"}])
    assert _parse_structured_fix(_report(host), _REGISTRY) is not None


def test_an_unresolvable_repo_is_refused_on_the_path_that_actually_enqueues():
    """The bug the first CFOP-85 attempt shipped, caught by review.

    Resolution was threaded into _ensure_structured_fix and stopped there.
    But ensure rejecting a FIX does not remove it from response_text, and
    _queue_needs_action_remediation re-parses that same text -- so the enqueue
    path re-accepted exactly what ensure had just refused, put repo
    "gitops-patch" on the payload, and produced the identical four-second
    executor bounce. The check was inert for the only production path that
    enqueues a row.

    The earlier tests could not see it: they all called _parse_structured_fix
    with a registry directly, which is the one arrangement in which the bug
    does not exist. This drives the queue helper instead.
    """
    op = _na_op()
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "host": None, "repo": None})
    bad = _valid_fix(targets=[{
        "kind": "gitops-manifest",
        "id": "homelab-root",
        "repo": "gitops-patch",
    }])
    rid = op._queue_needs_action_remediation(
        80, "cert-manager CRD ownership",
        {"fingerprint": "fp-77", "labels": {"instance": "homelab-root"}},
        "remove the cert-manager CRDs from homelab-root", _report(bad),
        provider="ollama/gemma4:26b")
    assert rid == 9
    # Degraded to the classifier rather than trusting the FIX...
    op._classify_needs_action_recommendation.assert_called_once()
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["remediation_class"] == "manual"
    # ...and nothing put the unresolvable value on the payload.
    assert kw["payload"].get("repo") != "gitops-patch"
    assert all(t.get("repo") != "gitops-patch"
               for t in (kw["payload"].get("targets") or []))


def test_a_registry_entry_with_no_slug_resolves_to_nothing():
    """Raised in review as a latent hole, and it is one.

    The executor reaches a repo only through a GitHubClient, so an entry with
    no `github` cannot be patched. Returning its short name would hand over a
    second unusable value and move the same failure one step downstream.
    """
    local_only = [{"name": "homelab-infra", "github": ""}]
    assert _resolve_fix_repo("homelab-infra", local_only) is None


# ---- observed: read before you propose (CFOP-88) ------------------------------
#
# Remediation #78 proposed MemoryHigh=24G / MemoryMax=28G for ollama on a box
# with 30 GiB total. It named the file and both target values and never stated
# the current ones -- because it never opened the file, where a comment three
# lines above the setting explains that the 16G/20G cap exists after
# ollama+runners OOM-killed cluster pods. The agent had ssh_execute the whole
# time.
#
# The mechanism is the READ. Requiring the current value forces the call that
# puts that comment in context; the validator only checks a specific claim was
# made. A fabricated value still passes -- verifying against the live target is
# separate plumbing and deliberately not here.


def test_a_fix_that_read_nothing_is_refused():
    """The #78 shape: a confident change with no current value behind it."""
    fix = _valid_fix()
    del fix["observed"]
    assert _parse_structured_fix(_report(fix), _REGISTRY) is None


@pytest.mark.parametrize("bad", [
    [],                                             # read nothing
    "cat override.conf",                            # prose, not entries
    [{"source": "cat override.conf"}],              # looked, no value
    [{"value": "MemoryHigh=16G"}],                  # value, no provenance
    [{"source": "", "value": "MemoryHigh=16G"}],
    [{"source": "cat override.conf", "value": ""}],
    [{"source": "cat override.conf", "value": "16G"}, "extra"],
])
def test_observed_must_say_where_it_looked_and_what_it_saw(bad):
    """Both halves carry weight. A value with no source cannot be re-checked
    by a human; a source with no value records that something was run and not
    what it returned, which is the gap #78 fell through."""
    assert _parse_structured_fix(_report(_valid_fix(observed=bad)), _REGISTRY) is None


def test_the_requirement_is_unconditional_across_target_kinds():
    """Deliberately NOT scoped to steps that change a value.

    Deciding which steps those are means classifying free-form step text, and
    a regex over step wording is the same species as _INVESTIGATE_SHAPED and
    the fork regex. Unconditional removes the boundary rather than policing
    it -- a restart-the-pod FIX records the pod's status, which is evidence
    worth having.
    """
    for kind, tid in (("host", "ubuntu-llm-01"),
                      ("k8s-object", "monitoring/promtail"),
                      ("database-row", "learnings:2368")):
        fix = _valid_fix(targets=[{"kind": kind, "id": tid}])
        del fix["observed"]
        assert _parse_structured_fix(_report(fix), _REGISTRY) is None, kind


def test_what_was_read_reaches_the_payload_an_operator_sees():
    """The row has to show the claimed current state next to the proposed one.

    For #78 that reads `MemoryHigh: 16G -> 24G` on a box whose total the
    reviewer knows, which is the point at which it stops looking reasonable.
    """
    op = _na_op()
    fix = _valid_fix(
        targets=[{"kind": "host", "id": "ubuntu-llm-01"}],
        observed=[{
            "source": "cat /etc/systemd/system/ollama.service.d/override.conf",
            "value": "MemoryHigh=16G\nMemoryMax=20G",
        }],
        steps=["set MemoryHigh=24G and MemoryMax=28G"],
        risk="high",
    )
    op._queue_needs_action_remediation(
        90, "ollama timeouts",
        {"fingerprint": "obs-1", "labels": {"instance": "ubuntu-llm-01"}},
        "raise the ollama memory limits", _report(fix, rec="raise the ollama memory limits"),
        provider="p")
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["observed"] == [{
        "source": "cat /etc/systemd/system/ollama.service.d/override.conf",
        "value": "MemoryHigh=16G\nMemoryMax=20G",
    }]


@pytest.mark.parametrize("bad,reason", [
    (None, "missing or empty"),
    ([], "missing or empty"),
    (["cat override.conf"], "not an object"),
    ([{"value": "MemoryHigh=16G"}], "no source"),
    ([{"source": "cat override.conf"}], "no value"),
    ([{"source": "cat override.conf", "value": "  "}], "no value"),
])
def test_every_observed_refusal_is_logged_not_silent(caplog, bad, reason):
    """Raised in review, and it undermines the whole risk mitigation.

    Requiring `observed` means a non-complying model degrades every FIX to the
    classifier — a real quality drop that otherwise looks like the FIX path
    going idle. The warning is what tells those apart, so it has to cover the
    shapes that actually occur. Once `observed` is named in the prompt, a
    half-filled entry (a source with no value) is likelier than an omitted
    key; logging only the omission would leave the COMMON failure invisible.

    The first version logged the empty case and returned silently for the
    rest.
    """
    fix = _valid_fix()
    if bad is None:
        del fix["observed"]
    else:
        fix["observed"] = bad
    with caplog.at_level(logging.WARNING, logger="cfoperator"):
        assert _parse_structured_fix(_report(fix), _REGISTRY) is None
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("observed" in m for m in warnings), warnings
    assert any(reason in m for m in warnings), warnings
    # The target ids ride along, so the log names which recommendation was lost.
    assert any("apps/promtail.yaml" in m for m in warnings), warnings


# ---- delivery guidance (CFOP-148) -------------------------------------------
#
# These guard the CLASS of regression behind live row #96: the prompt telling
# the model nothing about how a change reaches this cluster, so it picks a
# target kind the site cannot act on. What matters is that the guidance is
# CONFIGURED -- absent when unset, and naming the operator's own repo and
# mechanism rather than any value this repo happened to pick. Deliberately not
# pinned to the wording, which is prompt copy and expected to be reworded.

_DELIVERY_REGISTRY = [
    {"name": "my-manifests", "github": "acme/my-manifests"},
    {"name": "other-repo", "github": "acme/other-repo"},
]


def _cfg(delivery=None, default_repo=None):
    rem = {}
    if delivery is not None:
        rem["delivery"] = delivery
    if default_repo is not None:
        rem["default_repo"] = default_repo
    return {"remediation": rem}


@pytest.mark.parametrize("config", [
    {},                                       # no remediation block at all
    {"remediation": {}},                      # no delivery block
    _cfg({}),                                 # delivery block, no mode
    _cfg({"mode": "none"}),                   # explicitly off
    _cfg({"mode": "None"}),                   # case-folded
    _cfg({"mode": "kubectl"}),                # unrecognised -> silent, not a guess
    _cfg({"mode": None}),
    {"remediation": {"delivery": "gitops"}},  # wrong type
    {"remediation": "nonsense"},
    None,
])
def test_unconfigured_delivery_says_nothing(config):
    """The default is silence. An installation that has not said how it deploys
    gets the pre-CFOP-148 prompt, never a guess about its own cluster."""
    assert _delivery_guidance(config, _DELIVERY_REGISTRY) == ""


def test_gitops_names_the_configured_repo_not_a_baked_one():
    text = _delivery_guidance(
        _cfg({"mode": "gitops", "repo": "my-manifests"}), _DELIVERY_REGISTRY)
    assert "acme/my-manifests" in text     # the resolved SLUG, as the executor uses
    assert "gitops-manifest" in text
    # Mutation check: the value must come from config, not from this codebase.
    assert "homelab-infra" not in text
    assert "aachtenberg" not in text


def test_a_second_installation_gets_its_own_repo():
    """Two sites, two repos. The guidance is a pure function of config, so
    nothing from one installation can leak into another's prompt."""
    a = _delivery_guidance(_cfg({"mode": "gitops", "repo": "my-manifests"}), _DELIVERY_REGISTRY)
    b = _delivery_guidance(_cfg({"mode": "gitops", "repo": "other-repo"}), _DELIVERY_REGISTRY)
    assert "acme/my-manifests" in a and "acme/other-repo" not in a
    assert "acme/other-repo" in b and "acme/my-manifests" not in b


def test_gitops_repo_falls_back_to_default_repo():
    """`default_repo` already names the manifest repo; delivery must not make
    an operator write the same repo down twice."""
    text = _delivery_guidance(
        _cfg({"mode": "gitops"}, default_repo="my-manifests"), _DELIVERY_REGISTRY)
    assert "acme/my-manifests" in text


def test_gitops_rules_out_direct_to_cluster_steps():
    """Row #96 wrote "Apply the updated manifest to the cluster" on a GitOps
    fleet. The guidance has to say that lands nothing there."""
    text = _delivery_guidance(
        _cfg({"mode": "gitops", "repo": "my-manifests"}), _DELIVERY_REGISTRY)
    assert "kubectl" in text and "pull request" in text


def test_gitops_tool_is_free_text_and_optional():
    """No syncer is special-cased. The tool name only shapes wording, and
    omitting it must not break the guidance."""
    named = _delivery_guidance(
        _cfg({"mode": "gitops", "repo": "my-manifests", "tool": "Flux"}), _DELIVERY_REGISTRY)
    assert "Flux" in named
    bare = _delivery_guidance(
        _cfg({"mode": "gitops", "repo": "my-manifests"}), _DELIVERY_REGISTRY)
    assert bare and "Flux" not in bare
    assert "ArgoCD" not in bare        # nothing ArgoCD-shaped is baked in


def test_unresolvable_repo_gives_no_guidance_at_all():
    """PR #229 review. An earlier cut kept the "prefer `gitops-manifest`"
    steer when the repo did not resolve, and that steers into a wall:
    _validate_structured_fix REFUSES a gitops-manifest target whose repo is
    missing or unresolvable (CFOP-85), so the FIX cannot enqueue and the row
    falls through to the classifier -- row #96's path, one layer over.

    A gitops site whose manifest repo does not resolve has no working gitops
    lane, so the honest output is silence (plus a log for the operator), not
    advice naming a kind that will be refused."""
    text = _delivery_guidance(
        _cfg({"mode": "gitops", "repo": "nowhere"}), _DELIVERY_REGISTRY)
    assert text == ""


def test_unresolvable_repo_is_logged_not_silent(caplog):
    """Silence in the prompt must not mean silence to the operator: setting
    mode: gitops and getting no behaviour change is otherwise unexplainable."""
    import logging
    with caplog.at_level(logging.WARNING):
        _delivery_guidance(_cfg({"mode": "gitops", "repo": "nowhere"}),
                           _DELIVERY_REGISTRY)
    assert any("gitops" in r.message and "resolve" in r.message
               for r in caplog.records), caplog.records


def test_direct_mode_steers_at_the_class_that_parks():
    """A cluster with no manifest repo -- plain kubectl, possibly not k3s.

    PR #229 re-review: an earlier cut offered `k8s-object` here, which maps to
    the k8s-action CLASS. That class is auto-eligible and is not in the
    executor's _NO_EXECUTOR_PATH, so it reaches run_gitops -- whose whole job
    is opening a pull request against a manifest repo the site does not have.
    `k8s-imperative` is the class that parks with a legible message.
    """
    text = _delivery_guidance(_cfg({"mode": "direct"}), _DELIVERY_REGISTRY)
    assert "k8s-imperative" in text
    assert "`k8s-object`" in text and "Do NOT emit" in text
    assert "gitops-manifest" in text


def test_direct_mode_never_recommends_a_pr_delivered_class():
    """The guard that matters, stated against the executor's own routing
    rather than against wording: nothing a `direct` site is steered toward may
    be a class that run_gitops would try to open a PR for."""
    import re

    no_executor_path = _executor_no_executor_path()
    text = _delivery_guidance(_cfg({"mode": "direct"}), _DELIVERY_REGISTRY)
    # The kinds this text tells the model to USE (backticked, not preceded by
    # a "Do NOT emit" clause). Parse rather than hardcode so a reworded
    # sentence cannot quietly reintroduce the defect.
    recommended = set()
    for sentence in re.split(r'(?<=[.:])\s+', text):
        if 'do not' in sentence.lower():
            continue
        recommended.update(re.findall(r'`([a-z0-9-]+)`', sentence))
    assert recommended, "direct mode must name at least one usable kind"
    for kind in recommended:
        rclass = _FIX_KIND_TO_CLASS.get(kind, kind)
        assert rclass in no_executor_path, (
            f"direct mode recommends `{kind}` -> class {rclass}, which the "
            "executor delivers by opening a PR; there is no manifest repo "
            "on a direct site")
        assert rclass not in _AUTO_REMEDIATION_CLASSES, (
            f"direct mode recommends `{kind}` -> class {rclass}, which can "
            "auto-execute")


def test_direct_mode_never_names_a_repo():
    """There is no manifest repo in this mode; a stale `repo` or `default_repo`
    left in config must not conjure one into the prompt."""
    text = _delivery_guidance(
        _cfg({"mode": "direct", "repo": "my-manifests"}, default_repo="other-repo"),
        _DELIVERY_REGISTRY)
    assert "my-manifests" not in text and "other-repo" not in text


@pytest.mark.parametrize("mode", ["gitops", "direct"])
def test_notes_are_appended_to_either_mode(mode):
    text = _delivery_guidance(
        _cfg({"mode": mode, "repo": "my-manifests", "notes": "Staging first."}),
        _DELIVERY_REGISTRY)
    assert text.endswith("Staging first.")


def test_guidance_is_wired_into_both_fix_prompts():
    """The helper existing is not the fix -- row #96 failed because the prompt
    never carried the steer. Both places that ask for a FIX must render it, and
    they must agree: the nudge is a second attempt at the SAME object, so one
    that forgot the site's delivery mechanism would quietly undo the steer that
    produced the retry.

    Asserted against the source rather than a captured prompt string because
    what regresses here is the CALL being dropped in a refactor, and that is
    exactly what this reads.
    """
    import inspect
    from agent.agent import CFOperator as _CFOp

    for fn in (_CFOp._act, _CFOp._nudge_structured_fix):
        src = inspect.getsource(fn)
        assert '_FIX_JSON_SCHEMA' in src, f"{fn.__name__} no longer asks for a FIX"
        assert '_delivery_guidance' in src, (
            f"{fn.__name__} asks for a FIX without telling the model how "
            "changes are delivered here")


def test_shared_rubric_carries_no_baked_repo():
    """PR #229 review finding 1. _REMEDIATION_CLASS_RUBRIC goes verbatim to
    the needs_action classifier AND the morning summary. It used to name this
    project's own repos, so every other installation was told to file its
    manifest changes against two repositories it does not have -- the exact
    assumption CFOP-148 removed from the FIX prompt, one feed over."""
    from agent.agent import _REMEDIATION_CLASS_RUBRIC
    lowered = _REMEDIATION_CLASS_RUBRIC.lower()
    assert "aachtenberg" not in lowered
    assert "homelab-infra" not in lowered
    assert "cfoperator-deploy" not in lowered
    # Still the single definition of the classes -- de-baking must not have
    # dropped the bullet the classifier needs.
    assert "- gitops-patch:" in _REMEDIATION_CLASS_RUBRIC


def test_rubric_takes_its_repo_from_the_same_config_as_the_fix_prompt():
    """One site, one answer. The rubric and the FIX prompt must not disagree
    about where a manifest change goes."""
    from agent.agent import _remediation_class_rubric
    cfg = _cfg({"mode": "gitops", "repo": "my-manifests"})
    rubric = _remediation_class_rubric(cfg, _DELIVERY_REGISTRY)
    assert "acme/my-manifests" in rubric
    assert "acme/my-manifests" in _delivery_guidance(cfg, _DELIVERY_REGISTRY)


def test_rubric_direct_mode_rules_out_every_pr_delivered_class():
    """The classifier feed is the dangerous one: unlike a FIX-derived
    k8s-action (confidence always None), a classifier-derived one can clear
    the auto gate and reach run_gitops. On a site with no manifest repo the
    appendix must name neither gitops-patch nor k8s-action as the answer.

    PR #229 re-review -- the first cut said "an in-cluster change is
    k8s-action or k8s-imperative", steering the one auto-executing class at a
    delivery lane that does not exist there.
    """
    from agent.agent import _remediation_class_rubric
    rubric = _remediation_class_rubric(_cfg({"mode": "direct"}), _DELIVERY_REGISTRY)
    appendix = rubric[len(_bare_rubric()):]
    assert "no GitOps repository" in appendix
    assert "acme/" not in appendix
    assert "k8s-imperative" in appendix
    # Both PR-delivered classes must appear only as ruled OUT.
    for rclass in _AUTO_REMEDIATION_CLASSES:
        assert rclass not in appendix.split("An in-cluster change is")[-1], (
            f"{rclass} is offered as the answer on a site with no repo")


def _bare_rubric():
    from agent.agent import _REMEDIATION_CLASS_RUBRIC
    return _REMEDIATION_CLASS_RUBRIC


@pytest.mark.parametrize("config", [
    None,
    {},
    {"remediation": {}},
    _cfg({"mode": "none"}),
    _cfg({"mode": "gitops", "repo": "nowhere"}),   # unresolvable -> no site line
])
def test_rubric_unconfigured_is_the_bare_rubric(config):
    """Same posture as the FIX prompt: an installation that has not said how
    it deploys gets the neutral rubric, never a guess."""
    from agent.agent import _REMEDIATION_CLASS_RUBRIC, _remediation_class_rubric
    assert _remediation_class_rubric(config, _DELIVERY_REGISTRY) == \
        _REMEDIATION_CLASS_RUBRIC


def test_both_rubric_feeds_read_the_renderer():
    """The classifier and the morning summary are the two feeds the rubric
    exists to keep in step; a call site left on the bare constant would miss
    the site line and silently drift.

    Unconditional on purpose. The first cut of this guard only asserted when
    one of the two names already appeared in the source, so DELETING the call
    outright made the condition false and the test pass -- it caught a
    reversion to the constant (the mutation that was checked) and nothing
    else. Flagged on the PR #229 re-review.
    """
    import inspect
    from agent.agent import CFOperator as _CFOp

    for fn in (_CFOp._classify_needs_action_recommendation,
               _CFOp._generate_morning_summary):
        src = inspect.getsource(fn)
        assert '_remediation_class_rubric(' in src, (
            f"{fn.__name__} does not render the shared rubric through the "
            "site-aware renderer")


def _executor_no_executor_path():
    """executor/entrypoint.py's _NO_EXECUTOR_PATH, read from source.

    Not imported: that module uses bare imports needing executor/ on
    sys.path, and CI runs this suite with PYTHONPATH=<root>/agent:<root>, so
    importing it passes locally and ModuleNotFoundErrors in CI. Read via ast
    so the test still tracks the real routing table rather than a copy of it
    that can drift.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "executor" / "entrypoint.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_NO_EXECUTOR_PATH":
                return set(ast.literal_eval(node.value))
    raise AssertionError("_NO_EXECUTOR_PATH not found in executor/entrypoint.py")


# ---------------------------------------------------------------------------
# CFOP-154: the guidance is only as good as somebody having switched it on.
#
# _delivery_guidance returning '' is correct and silent, and that silence is
# what let this installation run PR #229's code for twelve hours with the key
# unset. Row #97 was the only evidence, and it read like an ordinary bad
# recommendation. These guard the class of failure -- a remediating install
# that never chose a delivery mode should say so once, at startup.


def _feed_cfg(delivery=None, feed=True):
    rem = {"queue_feed": feed}
    if delivery is not None:
        rem["delivery"] = delivery
    return {"remediation": rem}


@pytest.mark.parametrize("config", [
    _feed_cfg(),                                  # no delivery block at all
    _feed_cfg({}),                                # delivery block, no mode
    _feed_cfg({"mode": ""}),                      # blank
    _feed_cfg({"mode": "gitpos"}),                # typo, not a decision
    {"remediation": {"queue_feed": True, "delivery": "gitops"}},  # wrong type
])
def test_unset_delivery_warns_when_rows_can_be_queued(config):
    """The whole point: an install that feeds the queue and never said how it
    deploys is told so, rather than discovering it from a parked row."""
    msg = _delivery_unset_warning(config)
    assert msg, "a queue-feeding install with no delivery mode said nothing"
    assert "delivery.mode" in msg
    assert "docs/config-reference.md" in msg


@pytest.mark.parametrize("config", [
    _feed_cfg({"mode": "gitops"}),
    _feed_cfg({"mode": "direct"}),
    _feed_cfg({"mode": "GitOps"}),                # case-folded, still a choice
    _feed_cfg({"mode": "none"}),                  # deliberate silence
    _feed_cfg({"mode": "None"}),
])
def test_a_chosen_mode_never_warns(config):
    """`none` is a documented decision, not an omission. A warning an operator
    cannot silence by deciding is noise, and noise is how warnings stop being
    read -- which is the failure this whole issue is about."""
    assert _delivery_unset_warning(config) is None


@pytest.mark.parametrize("config", [
    {},
    None,
    {"remediation": {}},                          # remediation off entirely
    _feed_cfg(feed=False),                        # investigate-only install
    _feed_cfg({"mode": "gitpos"}, feed=False),    # typo, but nothing consumes it
    {"remediation": "nonsense"},
])
def test_no_warning_without_a_queue_to_feed(config):
    """Gated on queue_feed, the flag that turns a FIX into a row. An
    investigate-only install has no cost to warn about, and the loader has
    already clamped the flag to the profile -- so this needs no profile logic
    of its own."""
    assert _delivery_unset_warning(config) is None


def test_the_warning_is_actually_emitted_at_startup():
    """A pure helper nobody calls is exactly the shape of the bug it exists to
    prevent. Read the source rather than boot an agent: what regresses is the
    CALL being dropped, and that is what this reads."""
    import inspect
    from agent.agent import CFOperator as _CFOp

    src = inspect.getsource(_CFOp.__init__)
    assert '_delivery_unset_warning' in src, (
        "__init__ no longer checks whether this install said how it deploys")


# --- and the same helper through the REAL loader -------------------------
#
# PR #232 review. The hand-built cases above pass a dict straight in; every
# real caller passes load_config() output, i.e. deep_merge(DEFAULT_CONFIG,
# file). The schema used to fill in `delivery: {"mode": "none"}`, which erased
# the difference between "chose silence" and "said nothing" before the helper
# ever saw it -- so the warning returned None for the exact production shape
# it was written for, while every test above stayed green. Testing the merge
# is the only way this stays fixed.


def _prod_fixture_path():
    import os
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures", "production_shaped_config.yaml")


def test_schema_supplies_no_delivery_mode():
    """The regression, guarded at its source. A default here is not a harmless
    convenience: it is indistinguishable downstream from an operator's answer,
    and `repo`/`tool` are already absent for the same reason."""
    from cfshared.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["remediation"]["delivery"] == {}


def test_omitted_delivery_warns_after_merge():
    """The merge is where this broke. A config that never mentions delivery
    must still look unset after the schema has been merged over it."""
    from cfshared.config import DEFAULT_CONFIG, deep_merge
    cfg = deep_merge(DEFAULT_CONFIG, {"remediation": {"queue_feed": True}})
    assert _delivery_unset_warning(cfg)


def test_omitted_delivery_warns_through_load_config():
    """The live shape, not a reconstruction of it. production_shaped_config is
    the redacted clone of the deployed ConfigMap: queue_feed on, no delivery
    block -- the twelve hours in which CFOP-148 rendered nothing."""
    from cfshared.config import load_config
    cfg = load_config(_prod_fixture_path())
    assert cfg["remediation"]["queue_feed"] is True
    assert _delivery_unset_warning(cfg), (
        "the config that produced row #97 did not warn")


def test_explicit_none_in_the_file_still_silences_after_merge(tmp_path):
    """The other half. Removing the default must not turn a deliberate
    `mode: none` into a warning the operator cannot switch off."""
    import yaml
    from cfshared.config import load_config
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(
        {"remediation": {"queue_feed": True, "delivery": {"mode": "none"}}}))
    assert _delivery_unset_warning(load_config(str(p))) is None


@pytest.mark.parametrize("delivery", [None, {}, {"mode": "none"}])
def test_removing_the_default_did_not_start_prompting(delivery):
    """Behaviour that must NOT change. Dropping the schema default is a
    warning-only change: the prompt still says nothing about delivery unless
    an installation chose gitops or direct."""
    from cfshared.config import DEFAULT_CONFIG, deep_merge
    rem = {"queue_feed": True}
    if delivery is not None:
        rem["delivery"] = delivery
    cfg = deep_merge(DEFAULT_CONFIG, {"remediation": rem})
    assert _delivery_guidance(cfg, _DELIVERY_REGISTRY) == ""
