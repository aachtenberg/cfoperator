#!/usr/bin/env python3
"""The CFOP-78 stopgap: repeat folding and fork commitment.

On 2026-08-23 the live queue held six needs-human rows for three problems —
the same CFOP_RUNTIME_TOKEN fix three times (grok-4.6 twice, gemma4 once), the
same stale Cloudflare rule twice, and one genuinely new row whose
recommendation was a fork nothing could execute. The dedupe key's fallback
tier SHA-1s the recommendation sentence, and two models never produce the same
sentence.

The fixtures below are those six rows' actual recommendation texts, verbatim.
That is deliberate: the guard being tested is "the rewordings real models
actually produce fold onto each other", and paraphrasing the fixtures would
test my rewording instead.

Since CFOP-538 the fold is cfop-dedupe's, so whether two rows fold is decided
by the service and pinned by its shared cases in dedupe/testdata/decide/,
which the Go tests and test_dedupe_parity.py both run. What stays here is what
the agent still owns: fork detection and commitment, the choke point's caps,
the fold targets a FIX carries onto its row, and the end-to-end CFOP-494 path.
The identifier helpers are exercised through dedupe_reference, the service's
Python model, because these verbatim rows are the clearest statement of why
the fold rule is what it is.
"""

import copy
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import CFOperator  # noqa: E402
from agent.agent import (  # noqa: E402
    _FORK_SHAPED,
    _fold_targets,
    _fold_targets_of,
    _parse_structured_fix,
)
import dedupe_reference  # noqa: E402
from agent.knowledge_base import payload_carries_dedupe_key  # noqa: E402
from test_remediation_queue import _na_op  # noqa: E402
from test_structured_fix import _report  # noqa: E402

# --- the six live rows, verbatim -------------------------------------------

REC_67 = ("Add `CFOP_RUNTIME_TOKEN` to sealed `cfoperator-secrets` and mount it "
          "on `k3s/base/apps/cfoperator-event-runtime.yml` (`secretKeyRef: "
          "{name: cfoperator-secrets, key: CFOP_RUNTIME_TOKEN}`), then "
          "ArgoCD-sync/rollout so the startup WARNING is gone.")
REC_69 = ("Add `CFOP_RUNTIME_TOKEN` to sealed "
          "`k3s/base/apps/sealed-secrets/cfoperator-secrets.yml` and mount it on "
          "the event-runtime Deployment (`secretKeyRef: {name: "
          "cfoperator-secrets, key: CFOP_RUNTIME_TOKEN}`), then "
          "ArgoCD-sync/rollout until the startup WARNING is gone.")
REC_71 = ("Add `CFOP_RUNTIME_TOKEN` to `cfoperator-secrets` and update the "
          "`cfoperator-event-runtime` Deployment to inject it, then restart "
          "the deployment.")
REC_68 = ("In Cloudflare Zero Trust → Networks → Tunnels → this tunnel, delete "
          "or retarget Public Hostname ingressRule=1 away from "
          "http://192.168.0.131:80; keep only reachable origins (e.g. freshet "
          "via Traefik).")
REC_70 = ("Remove or retarget the Cloudflare Zero Trust ingress rule for "
          "`192.168.0.131:80` in the Cloudflare Dashboard.")
REC_72 = ("Truncate or chunk the text content for `learning_id 2368` in the "
          "underlying database to fit within the model's context window, or "
          "update the `embedding_service` configuration to use a model with a "
          "larger context window.")

extract = dedupe_reference.extract_identifiers


# ---------------------------------------------------------------------------
# identifier extraction
# ---------------------------------------------------------------------------

def test_env_names_ips_and_row_ids_are_extracted():
    assert "CFOP_RUNTIME_TOKEN" in extract(REC_67)
    # The port STAYS: this cluster runs many services per address, so the
    # stale tunnel rule for :80 and a broken service on :22 of the same box
    # are different problems. (PR #169 review — the first version stripped
    # it, and the test pinned the weaker choice.)
    assert "192.168.0.131:80" in extract(REC_68)
    assert "learning_id:2368" in extract(REC_72)


def test_the_same_address_on_different_ports_is_two_problems():
    a = extract("Retarget the tunnel rule for 192.168.0.131:80")
    b = extract("Fix the dead ssh origin 192.168.0.131:22")
    assert not (a <= b or b <= a)


def test_prose_and_dash_words_extract_nothing():
    """The reason hostnames/workloads are excluded: dash-words collide with
    ordinary prose, and a false fold hides a real incident."""
    assert extract("Restart the needs-human worker on grok-4.6 after the "
                   "HTTP 500 STATUS ERROR; see RECOMMENDATION above") == frozenset()
    assert extract("") == frozenset()
    assert extract(None) == frozenset()


def test_the_live_token_rows_share_identifiers():
    """#67/#69/#71: two models, three wordings, one fix. Containment either
    way is the fold rule, so the tightest row must still match the fullest."""
    a, b, c = extract(REC_67), extract(REC_69), extract(REC_71)
    for x, y in [(a, b), (a, c), (b, c)]:
        assert x <= y or y <= x, f"{sorted(x)} vs {sorted(y)}"


def test_the_live_tunnel_rows_share_identifiers():
    """Both live rows spell the port, so keeping it still folds them."""
    a, b = extract(REC_68), extract(REC_70)
    assert a & b == {"192.168.0.131:80"}
    assert a <= b or b <= a


def test_distinct_problems_do_not_contain_each_other():
    """#72 must never fold onto the token rows or the tunnel rows."""
    embedding = extract(REC_72)
    for other in (REC_67, REC_68):
        ids = extract(other)
        assert not (embedding <= ids or ids <= embedding)


# ---------------------------------------------------------------------------
# the fold -- now the service's (CFOP-538)
#
# These were tests of _absorb_repeat_remediation. Each decision is a shared
# case in dedupe/testdata/decide/cfop78_live_rows.json, built from the same
# rows:
#   a reworded repeat folds onto the open row     "#69 (grok) rewords #67 (grok) ..."
#   a third model's rewording still folds          "#71 (gemma4) rewords #67 (grok) ..."
#   a distinct problem does not fold               "#72 shares nothing with the token or tunnel rows"
#   no identifiers is no fold, not every fold      "a recommendation naming nothing never folds onto everything"
#   an open row naming nothing is no fold target   "an open row naming nothing is never a fold target"
#   one shared identifier is not the same problem  "one shared identifier is not containment"
#   an exact-key repeat is left to the exact tier  "an exact-key repeat is left to the exact tier"
# A fold that cannot read the open rows is the fallback's (the exact key):
# test_dedupe_client.py::test_an_unreadable_snapshot_falls_back_without_asking.
# ---------------------------------------------------------------------------


def _row(rid, rec, key="inv-aaaa"):
    return {"id": rid, "status": "needs-human", "payload": {"dedupe_key": key, "recommendation": rec}}


# ---------------------------------------------------------------------------
# CFOP-453: the same FIX target under two keys -- live rows #314 / #315
#
# 2026-10-20, three minutes apart: the alert path keyed #314 on its FIX
# targets (tgt-), the sweep dispatch keyed #315 on its stamped title slug
# (inv-dispatch-). Both are gemma4 recommendations for one ConfigMap in one
# repo; the ids differ only by the `data/` prefix. Payloads verbatim.
# ---------------------------------------------------------------------------

REC_314 = ("Update the `eccc-ingest` ConfigMap to implement retries with exponential "
           "backoff in the `fetch_csv` function to handle transient external "
           "connection resets.")
REC_315 = ("Update the `eccc-ingest-script-mg664c47g9` ConfigMap to implement retry "
           "logic with exponential backoff in the `fetch_csv` function to mitigate "
           "these transient external connection failures.")
TGT_314 = [{"id": "data/eccc-ingest-script-mg664c47g9", "kind": "gitops-manifest",
            "repo": "aachtenberg/homelab-infra"}]
TGT_315 = [{"id": "eccc-ingest-script-mg664c47g9", "kind": "gitops-manifest",
            "repo": "aachtenberg/homelab-infra"}]
KEY_314 = "tgt-7fafc3e87c2d09f3"
KEY_315 = "inv-dispatch-investigate-eccc-ingest-connection-resets"


# Each decision these rows drove is a shared case in
# dedupe/testdata/decide/cfop453_eccc_ingest.json: the dispatch-keyed repeat
# folding onto the target-keyed row and the other way round, a regenerated
# kustomize hash, another repo, the same basename in another directory, a
# wider fix against two fixes that each touch something else, and targets
# never matching a row without them. The alias each fold records is in the
# cases' answers too.


def test_the_live_pair_names_no_identifiers():
    """Why the CFOP-78 fold declined: a ConfigMap name and a function name
    match none of its three patterns. Both sets empty, correctly no fold."""
    assert extract(REC_314) == frozenset()
    assert extract(REC_315) == frozenset()


def test_normalization_strips_only_a_kustomize_hash():
    assert dedupe_reference.normalize_target_id("data/eccc-ingest-script-mg664c47g9") == "data/eccc-ingest-script"
    assert dedupe_reference.normalize_target_id("Eccc-Ingest-Script-2g77tmm66h") == "eccc-ingest-script"
    # Ten trailing characters that are not drawn from kustomize's alphabet
    # (it has no vowels) are part of the name, not a generator suffix.
    assert dedupe_reference.normalize_target_id("eccc-ingest-production") == "eccc-ingest-production"
    assert dedupe_reference.normalize_target_id("api-monitoring") == "api-monitoring"


def test_an_alias_answers_for_the_surviving_row():
    """Tomorrow's sweep asks for the dispatch key it stamped; the surviving
    row has to answer for it or the sweep re-investigates every morning."""
    payload = {"dedupe_key": KEY_314, "dedupe_aliases": [KEY_315]}
    assert payload_carries_dedupe_key(payload, KEY_315)
    assert payload_carries_dedupe_key(payload, KEY_314)
    assert not payload_carries_dedupe_key(payload, "inv-dispatch-something-else")
    assert not payload_carries_dedupe_key({"dedupe_key": KEY_314, "dedupe_aliases": "x"},
                                          "x"), "a non-list alias field matches nothing"


# ---------------------------------------------------------------------------
# fork detection
# ---------------------------------------------------------------------------

def test_the_live_fork_is_detected():
    assert _FORK_SHAPED.search(REC_72)


def test_two_verbs_one_target_is_not_a_fork():
    """#68's "delete or retarget" and #70's "Remove or retarget" are wording,
    not alternatives — same rule, either verb. Flagging them would send half
    the queue through the rewrite."""
    assert not _FORK_SHAPED.search(REC_68)
    assert not _FORK_SHAPED.search(REC_70)


def test_ordinary_recommendations_are_not_forks():
    for rec in (REC_67, REC_69, REC_71,
                "Patch the Promtail DaemonSet memory limit from 256Mi to 512Mi "
                "via GitOps."):
        assert not _FORK_SHAPED.search(rec), rec


def test_alternatively_opens_a_fork():
    assert _FORK_SHAPED.search("Restart the pod. Alternatively, raise the "
                               "memory limit.")


# ---------------------------------------------------------------------------
# the commit retry
# ---------------------------------------------------------------------------

def _commit(op, rec, response=None, raises=None):
    if raises is not None:
        op._chat_with_tools_with_fallback = MagicMock(side_effect=raises)
    else:
        op._chat_with_tools_with_fallback = MagicMock(
            return_value={'response': response})
    return CFOperator._commit_forked_recommendation(op, "trigger", rec, report="r")


def test_a_non_fork_passes_through_without_an_llm_call():
    op = MagicMock()
    op._chat_with_tools_with_fallback = MagicMock()
    text, forked = CFOperator._commit_forked_recommendation(op, "t", REC_67)
    assert (text, forked) == (REC_67, False)
    op._chat_with_tools_with_fallback.assert_not_called()


def test_a_fork_that_commits_is_replaced():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response=(
        "Truncate the text content for `learning_id 2368` in the learnings "
        "table so it fits the embedding model's context window."))
    assert forked is False
    assert "learning_id 2368" in text
    assert not _FORK_SHAPED.search(text)


def test_a_retry_that_still_forks_keeps_the_original_and_flags_it():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response=(
        "Truncate the row, or update the configuration to a larger model."))
    assert (text, forked) == (REC_72, True)


def test_an_llm_failure_keeps_the_original_and_flags_it():
    op = MagicMock()
    text, forked = _commit(op, REC_72, raises=RuntimeError("no backend"))
    assert (text, forked) == (REC_72, True)


def test_an_empty_rewrite_is_not_a_commitment():
    op = MagicMock()
    text, forked = _commit(op, REC_72, response="")
    assert (text, forked) == (REC_72, True)


# ---------------------------------------------------------------------------
# the auto-gate cap on a stuck fork
# ---------------------------------------------------------------------------

def _feed_op(*, still_forked, confidence=0.9):
    op = MagicMock()
    op._remediation_flag = lambda name: name == 'queue_feed'
    op._commit_forked_recommendation = lambda t, r, report='': (r, still_forked)
    op._classify_needs_action_recommendation = MagicMock(return_value={
        'remediation_class': 'gitops-patch', 'risk': 'low',
        'confidence': confidence, 'host': None, 'repo': 'aachtenberg/x'})
    op._investigation_dedupe_key = CFOperator._investigation_dedupe_key
    op._maybe_queue_remediation = MagicMock(return_value=42)
    return op


def test_a_stuck_fork_cannot_clear_the_auto_gate():
    """Live row #72 was gitops-patch at 0.80 — one 'low' risk away from the
    executor opening a PR against a repo unrelated to the fix. A fork that
    will not commit must never be executable, whatever the classifier said."""
    op = _feed_op(still_forked=True)
    rid = CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_72, "report", "ollama/gemma4:26b")
    assert rid == 42
    details = op._maybe_queue_remediation.call_args[0][1]
    assert details['confidence'] is None
    assert details['remediation_class'] == 'gitops-patch', (
        "the class stays honest; only the gate is barred")


def test_a_committed_recommendation_keeps_its_confidence():
    op = _feed_op(still_forked=False)
    CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_67, "report", "xai/grok-4.6")
    details = op._maybe_queue_remediation.call_args[0][1]
    assert details['confidence'] == 0.9


def test_the_committed_text_is_what_everything_downstream_sees():
    """The classifier and the dedupe key must both read the committed action —
    the whole point is that nothing downstream reverse-engineers the fork."""
    committed = "Truncate the text for `learning_id 2368` in learnings."
    op = _feed_op(still_forked=False)
    op._commit_forked_recommendation = lambda t, r, report='': (committed, False)
    CFOperator._queue_needs_action_remediation(
        op, 2281, "trigger", {}, REC_72, "report", "ollama/gemma4:26b")
    classified = op._classify_needs_action_recommendation.call_args[0][1]
    details = op._maybe_queue_remediation.call_args[0][1]
    assert classified == committed
    assert details['recommendation'] == committed
    assert details['dedupe_key'] == CFOperator._investigation_dedupe_key(
        {}, committed)


# ---------------------------------------------------------------------------
# the choke point (PR #169 review)
# ---------------------------------------------------------------------------

from test_remediation_queue import (  # noqa: E402
    _confirming_judge, _no_node_incident, _wire_flags)


def _choke_op(**flags):
    op = _confirming_judge(_no_node_incident(_wire_flags(MagicMock())))
    op.config = {"remediation": {"queue_feed": True, **flags}}
    op.kb.queue_remediation.return_value = 99
    op.kb.list_open_remediations_for_dedupe.return_value = []
    return op


def test_the_fold_is_skipped_while_the_node_collapse_owns_the_row():
    """The first NotReady symptom arrives before any incident row exists. If
    the identifier fold ran on it, a rec naming 192.168.0.131 while the
    tunnel row is open would be absorbed THERE, and the node-incident row
    never created. The decision is the service's ("the node tier claims the
    row before the identifier fold can"); this pins that the choke point
    then opens the incident row, under the node's key and name."""
    op = _choke_op()
    op._notready_nodes = lambda: {"raspberrypi4"}
    op.kb.list_open_remediations_for_dedupe.return_value = [_row(68, REC_68)]
    rid = CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "manual", "risk": "high", "confidence": None,
        "recommendation": "Restore connectivity to 192.168.0.131:80",
        "host": "raspberrypi4", "dedupe_key": "inv-fresh"})
    assert rid == 99, "the incident row must still be created"
    kw = op.kb.queue_remediation.call_args.kwargs
    assert kw["dedupe_key"] == "node-down-raspberrypi4" and kw["host_id"] == "raspberrypi4"
    op.kb.record_remediation_absorbed.assert_not_called()


def test_a_fork_reaching_the_choke_point_cannot_carry_confidence():
    """The rewrite lives in the investigation feed; the CAP lives here, so a
    feed that never called the rewrite — sweep, deep-investigation, whatever
    comes third — still cannot hand the executor a fork."""
    op = _choke_op()
    CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "recommendation": REC_72, "host": None, "dedupe_key": "inv-fork"})
    assert op.kb.queue_remediation.call_args.kwargs["confidence"] is None


def test_a_committed_recommendation_keeps_confidence_at_the_choke_point():
    op = _choke_op()
    CFOperator._maybe_queue_remediation(op, 5, {
        "remediation_class": "gitops-patch", "risk": "low", "confidence": 0.9,
        "recommendation": REC_67, "host": None, "dedupe_key": "inv-ok"})
    assert op.kb.queue_remediation.call_args.kwargs["confidence"] == 0.9


# ---------------------------------------------------------------------------
# CFOP-494: a refused FIX keeps its subject -- live rows #334-#347
#
# 2026-10-25, Opus investigations of one nginx crashloop, all night. Every
# FIX named the same file verbatim and every FIX was refused (observed
# "contradicted" by CFOP-495's comment parse, or elided), so no row carried
# targets, the CFOP-453 fold matched nothing, and thirteen rows were filed.
# Investigation 4817's FIX and both recommendations are verbatim.
# ---------------------------------------------------------------------------

FIX_4817 = json.loads(r'''{"targets": [{"kind": "gitops-manifest", "id": "apps/files/freshet-dashboard/nginx.conf", "repo": "aachtenberg/freshet-deploy"}], "observed": [{"source": "k8s_get_pod_logs nginx (freshet-dashboard-5cc6665dd-k2h2b), now", "value": "nginx: [emerg] zone \"postgrest_up\" is too small in /etc/nginx/conf.d/default.conf:33"}, {"source": "k8s_get_pods apps freshet-dashboard-5cc6665dd-k2h2b", "value": "nodeName raspberrypi5; nginx CrashLoopBackOff; restartCount 19; Ready=False ContainersNotReady [nginx]; exitCode 1"}, {"source": "ssh raspberrypi5 getconf PAGESIZE", "value": "16384"}, {"source": "github_get_pr freshet-deploy #50", "value": "state open, merged_at null, 1 file changed +6/-1"}, {"source": "github_get_file_contents nginx.conf @ fix/dashboard-nginx-zone-16k-pages", "value": "zone postgrest_up 1m;"}], "steps": ["Merge freshet-deploy PR #50 (fix/dashboard-nginx-zone-16k-pages) into main", "Let the freshet-deploy image build pipeline publish the rebuilt freshet-dashboard image and bump the deployed tag", "Allow ArgoCD to sync the new image tag into the apps namespace, replacing sha-b1491d94cfbf"], "verify": {"command": "kubectl -n apps get pods -l app.kubernetes.io/name=freshet-dashboard -o wide", "expect": "the new pod is Running and Ready 1/1 on raspberrypi5 with restartCount stable (no new restarts); old 77c4b74d9d pod retired"}, "rejected": [{"alternative": "Delete/restart the crashlooping pod", "why_not": "Reschedules the same 64k config; if it lands on raspberrypi5 again it crashes identically \u2014 the zone size is the defect, not a transient state"}, {"alternative": "Cordon raspberrypi5 or add nodeAffinity to keep the pod off 16 KB-page nodes", "why_not": "Masks the bug and reduces capacity; the 1m zone is correct on all page sizes"}, {"alternative": "Edit the image tag in homelab-infra directly", "why_not": "The config lives in freshet-deploy and must be rebuilt into the image first; no fixed image exists until PR #50 merges"}], "risk": "low"}''')
REC_4817 = ("Merge `freshet-deploy` PR #50 (`fix/dashboard-nginx-zone-16k-pages`), "
            "which sets `zone postgrest_up 1m;` in "
            "`apps/files/freshet-dashboard/nginx.conf`. That rebuilds the dashboard "
            "image; once CI publishes the new tag and ArgoCD syncs it into apps, "
            "confirm the new pod on raspberrypi5 reaches Running + Ready 1/1 with no "
            "further restarts, and the old 77c4b74d9d pod retires.")
REC_4822 = ("Merge freshet-deploy PR #50 (`fix/dashboard-nginx-zone-16k-pages`), "
            "which sets `zone postgrest_up 1m;` in "
            "`apps/files/freshet-dashboard/nginx.conf`. This rebuilds the image and "
            "lets ArgoCD complete the rollout; the new pod will then start on 16 "
            "KB-page nodes and the old ReplicaSet scales to 0.")
NGINX_TARGET = [{"kind": "gitops-manifest", "id": "apps/files/freshet-dashboard/nginx.conf",
                 "repo": "aachtenberg/freshet-deploy"}]
# A file that disagrees with a claimed `valid=60s`, for the contradicted case.
NGINX_RESOLVER = "resolver ${NGINX_LOCAL_RESOLVERS} valid=30s;\n"


def _refused(how):
    fix = copy.deepcopy(FIX_4817)
    if how == "contradicted":
        fix["observed"] = [{"source": "nginx.conf", "value": "valid=60s"}]
    elif how == "elided":
        fix["observed"][0]["value"] = 'nginx: [emerg] zone "postgrest_up" is too small ...'
    elif how == "no-verify":
        del fix["verify"]
    return fix


def _nginx_op():
    op = _na_op()
    op.git_repos.return_value = op.git_repos.return_value + [
        {"name": "freshet-deploy", "github": "aachtenberg/freshet-deploy"}]
    # What the classifier said for #335, the path a refused FIX takes.
    op._classify_needs_action_recommendation = MagicMock(return_value={
        "remediation_class": "gitops-patch", "risk": "low", "confidence": None,
        "host": None, "repo": "freshet-deploy"})
    op._read_gitops_target = lambda repo, path: NGINX_RESOLVER
    return op


def _nginx_feed(op, inv, rec, fix):
    """One investigation's enqueue, shaped as production calls it: the
    caller passes whatever _ensure_structured_fix accepted. The follow-up
    guard is not under test, so the alert says it is already a follow-up."""
    report = _report(fix, rec)
    return op._queue_needs_action_remediation(
        inv, "freshet-dashboard nginx CrashLoopBackOff on raspberrypi5",
        {"followup_of": inv - 1}, rec, report,
        provider="anthropic/claude-opus-4-8",
        structured_fix=_parse_structured_fix(report, op.git_repos()))


def test_the_live_recommendations_name_no_identifiers_and_are_not_forks():
    """Why nothing but the target could have folded these."""
    for rec in (REC_4817, REC_4822):
        assert extract(rec) == frozenset()
        assert not _FORK_SHAPED.search(rec)


@pytest.mark.parametrize("how", ["contradicted", "elided", "no-verify"])
def test_a_refused_fix_still_folds_its_repeat(how):
    """The class guard: whatever refuses the FIX, the next rewording of the
    same fix lands on the open row instead of filing another. Mutation
    check: stop storing fold_targets and the second enqueue makes a second
    row. Reading them is the service's target fold (nginx_334_347.json)."""
    op = _nginx_op()
    assert _nginx_feed(op, 4817, REC_4817, _refused(how)) == 9
    first = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert first["fold_targets"] == NGINX_TARGET
    assert "targets" not in first, "a refused FIX must not look accepted"
    assert first["dedupe_key"].startswith("inv-"), "the key tier is unchanged"

    op.kb.list_open_remediations_for_dedupe.return_value = [
        {"id": 9, "status": "needs-human", "payload": first}]
    assert _nginx_feed(op, 4822, REC_4822, _refused(how)) == 9
    assert op.kb.queue_remediation.call_count == 1
    assert op.kb.record_remediation_absorbed.call_args[0][0] == 9


def test_an_accepted_fix_does_not_also_carry_fold_targets():
    op = _nginx_op()
    op._read_gitops_target = lambda repo, path: None     # unread: not refused
    _nginx_feed(op, 4817, REC_4817, FIX_4817)
    payload = op.kb.queue_remediation.call_args.kwargs["payload"]
    assert payload["targets"] == NGINX_TARGET
    assert "fold_targets" not in payload


# Refused and accepted rows folding onto each other is the service's:
# "nginx #335 lands on #334" and "nginx #337's accepted FIX lands on refused
# #334" in dedupe/testdata/decide/nginx_334_347.json.


def test_fold_targets_refuse_a_partial_set():
    """Dropping an unusable target would shrink the set, and containment
    would then fold a FIX that touches one more thing than the open row."""
    fix = copy.deepcopy(FIX_4817)
    fix["targets"].append({"kind": "gitops-manifest"})
    assert _fold_targets_of(fix) == []
    assert _fold_targets("FIX: " + json.dumps(fix)) == []


def test_fold_targets_keep_an_unregistered_repo_verbatim():
    fix = copy.deepcopy(FIX_4817)
    fix["targets"][0]["repo"] = "aachtenberg/homlab-infra"
    assert _fold_targets_of(fix, known_repos=[]) == [{
        "kind": "gitops-manifest", "id": "apps/files/freshet-dashboard/nginx.conf",
        "repo": "aachtenberg/homlab-infra"}]
