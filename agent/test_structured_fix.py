#!/usr/bin/env python3
"""CFOP-80: structured FIX beside RECOMMENDATION.

Parse-or-None (never salvage). A valid FIX skips the classifier; a missing
or malformed FIX still classifies. Class of regression, not output pins.
"""

import json
import os
import sys
from unittest.mock import MagicMock

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
