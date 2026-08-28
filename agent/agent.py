#!/usr/bin/env python3
"""
CFOperator - Continuous Feedback Operator
==========================================

Single central agent with dual-mode OODA loop:
- Reactive: Responds to alerts with LLM-driven investigations
- Proactive: Periodic deep sweeps to catch issues before they alert

Version: 1.0.8
"""

import os
import re
import sys
import time
import json
import uuid
import yaml
import logging
import hashlib
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
import queue
import threading
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

# Prometheus metrics
from prometheus_client import Counter, Gauge, Histogram, Info

# Import core components
from knowledge_base import ResilientKnowledgeBase, learning_has_trigger_condition, is_ephemeral_job_pod, normalize_finding_signature, normalize_remediation_fields, normalize_service_name, remediation_is_auto_eligible
from llm_fallback import LLMFallbackManager as LLMFallback
from embedding_service import EmbeddingService, vector_literal

# Import pluggable observability backends
from observability import (
    PrometheusMetrics,
    LokiLogs,
    DockerContainers,
    KubernetesContainers,
    CompositeContainerBackend,
    AlertmanagerAlerts,
    AlertmanagerNotifications,
    SlackNotifications,
    DiscordNotifications
)

# Import web server
from web_server import WebServer
from cfshared.version import build_version

# Import tool registry
from tools import ToolRegistry

# Import Ollama pool (for parallel sweeps)
from ollama_pool import OllamaPool
from remediation import RemediationProposer
from change_record_client import (
    ChangeRecordClientError,
    get_approval as change_record_approval,
    open_record as change_record_open,
)
from node_action_plan import (
    build_command_prompt as _na_build_command_prompt,
    normalize_plan as _na_normalize_plan,
    parse_command_plan as _na_parse_command_plan,
    validate_plan as _na_validate_plan,
)

# Config semantics shared with event_runtime — one loader, one default schema.
from cfshared import config as shared_config
from cfshared import repos as shared_repos

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "component": "%(name)s", "msg": "%(message)s"}'
)
logger = logging.getLogger("cfoperator")

# Prometheus metrics
OODA_CYCLES = Counter('cfoperator_ooda_cycles_total', 'Total OODA cycles executed')
SWEEPS = Counter('cfoperator_sweeps_total', 'Total sweeps executed', ['mode'])  # reactive/proactive
TOOL_CALLS = Counter('cfoperator_tool_calls_total', 'Tool executions', ['tool_name', 'result'])


def _tool_call_result_label(result) -> str:
    """Map a tool return value to the TOOL_CALLS ``result`` label.

    ``success`` or ``error`` only. Tools do not raise out of
    ``ToolRegistry.execute`` — they return ``{error: ...}`` or
    ``{success: False}`` — and until CFOP-101 every one of those was counted
    as success, so HighToolFailureRate could not fire.

    A valid empty read (empty pod list, PromQL with no series, ``success:
    True``) is success. A tool that could not do the job is error.

    ``ssh_execute`` and ``_run_kubectl`` (every ``k8s_*`` tool) set
    ``success`` from the process exit code. A command that ran and answered
    non-zero — ``k8s_get_pod_logs(previous=True)`` on a pod that has not
    restarted, ``systemctl is-active`` on a stopped unit — carries
    ``exit_code`` and never ``error``. That is a result, not a tool failure;
    counting it as error would make HighToolFailureRate fire on every
    CrashLoopBackOff investigation (the same "useless in the opposite
    direction" outcome a ``not_found`` label was rejected to avoid).
    Timeouts and exceptions set ``error`` and omit ``exit_code``.
    """
    if isinstance(result, dict):
        if result.get('success') is False:
            # ssh_execute / _run_kubectl map the remote exit code onto
            # success; a command that ran and answered non-zero is a result,
            # not a tool failure. Those paths carry exit_code and never error.
            if 'exit_code' in result and not result.get('error'):
                return 'success'
            return 'error'
        if result.get('success') is True:
            return 'success'
        if result.get('error'):
            return 'error'
        return 'success'
    # execute() returns a dict, or a list (find_learnings). A bare string is
    # not a registered tool shape; do not heuristic-match "Error: ...".
    return 'success'


TOOLS_REGISTERED = Gauge('cfoperator_tools_registered', 'Number of registered tools')
INVESTIGATIONS = Counter('cfoperator_investigations_total', 'Total investigations', ['outcome'])
LOG_MESSAGES = Counter('log_messages_total', 'Log messages', ['level', 'component'])
INVESTIGATION_QUEUE_DEPTH = Gauge('cfoperator_investigation_queue_depth', 'Pending HTTP-triggered investigations')
INVESTIGATION_QUEUE_REJECTED = Counter('cfoperator_investigation_queue_rejected_total', 'HTTP investigations rejected because queue was full')
INVESTIGATION_POSTBACK = Counter('cfoperator_investigation_postback_total', 'Investigation completions posted back to event_runtime', ['status'])
REMEDIATION_QUEUE = Gauge('cfoperator_remediation_queue', 'Remediation queue rows by status', ['status'])
REMEDIATION_ENQUEUED = Counter('cfoperator_remediation_enqueued_total', 'Remediations enqueued', ['source', 'remediation_class', 'eligible'])
# result: ok (first try) | nudged (corrective retry) | escalated (distinct
# provider) | degraded (ladder exhausted -> manual/high). CFOP-48: how often
# each rung rescues a classification, so the ladder is tuned on data.
REMEDIATION_CLASSIFIER = Counter('cfoperator_remediation_classifier_total', 'needs_action classifier outcomes', ['result'])
# reason: repeat (identifier fold onto an open row, CFOP-78) |
# fork_committed (a forked recommendation was rewritten to one action) |
# fork_stuck (the rewrite refused; the row is capped out of the auto gate) |
# investigate_followup (a checklist rec became a follow-up investigation
# instead of a row, CFOP-108).
REMEDIATION_FOLDED = Counter('cfoperator_remediation_folded_total',
                             'Rows folded or reshaped before enqueue', ['reason'])

# The classifier prompt's worked example. Deliberately a SAFE object — manual/
# high, sub-gate confidence, no real repo — because the few-shot failure mode
# of small local models is parroting the example, and a parroted example must
# land needs-human, never auto-queue a PR (PR #134 review). Guarded by a test
# that asserts this object can never clear the auto gate.
_CLASSIFIER_SAFE_EXAMPLE = {"remediation_class": "manual", "risk": "high",
                            "confidence": 0.4, "host": "", "repo": ""}
# verdict: confirm | downgrade | reject | unavailable | unparseable (CFOP-70).
# The judge only ever runs on rows that would otherwise auto-execute, so this
# counter is also the denominator for "how often did we nearly open a wrong PR".
REMEDIATION_JUDGE = Counter('cfoperator_remediation_judge_total', 'Frontier-model verdicts on auto-eligible mutations', ['verdict'])
REMEDIATION_SPAWNED = Counter('cfoperator_remediation_executor_spawned_total', 'Executor Jobs spawned by the drainer', ['result'])
REMEDIATION_OUTCOME = Counter('cfoperator_remediation_outcome_total', 'Terminal remediation outcomes', ['outcome'])
REMEDIATION_REAPED = Counter('cfoperator_remediation_reaped_total', 'Remediations recovered from dead executor leases')

# The one definition of "the model we trust with a cluster mutation". Two
# users, both of which must never inherit a cost downgrade of the generic
# executor model:
#   - the node-action executor (the only path that runs shell on a host), when
#     remediation.executor.node_action.model is unset;
#   - the mutation judge (CFOP-70), which decides whether an auto-eligible
#     remediation may open a PR unattended at all.
# The node-action comment used to say "the only path that mutates the cluster".
# That premise was too narrow: a merged GitOps PR mutates the cluster just as
# surely as a shell command, only with ArgoCD holding the knife.
_ANTHROPIC_DEFAULT_EXEC_MODEL = "claude-opus-4-8"

# One pinned model per judge backend. Not config-overridable, for the same
# reason node-action pins its own — the model holding the veto must not inherit
# a cost downgrade.
#
# Failing OVER between vendors when one is unreachable is NOT the "escalate to a
# lesser model" rung CFOP-70 refused. That rung was refused because it would
# have reached the cheap LOCAL primary whose judgement is the thing under
# review; reaching another vendor's hosted model is a different question, and it
# is what stops one missing API key from parking every remediation.
#
# All three are the frontier tier of their vendor, so they are true PEERS and
# the order is availability preference, not a quality ranking. Keep it that way:
# the moment a fast tier appears in this map, whichever entry it is starts doing
# routine judging the first time the peers above it are unreachable, which is
# most of the way back to the bug CFOP-70 exists to fix.
#
# What failover must NEVER do is shop for a permissive answer: see
# _judge_mutation_remediation for why an unparseable verdict parks on the spot
# instead of asking the next provider.
_JUDGE_MODEL_FLOOR = {
    'anthropic': _ANTHROPIC_DEFAULT_EXEC_MODEL,
    'xai': 'grok-4.5',
    # The exact id Google serves, not the 'gemini-pro-latest' alias. The alias
    # keeps the tier but lets Google swap the model beneath a verdict without
    # anything here changing, and a verdict that cannot be reproduced against
    # the model that gave it is worth less as a record. The bare
    # 'gemini-3.1-pro' this held until CFOP-107 was never served: it 404'd the
    # first time the two peers above it were both down. Confirmed against
    # GET /v1beta/openai/models on 2026-08-28; the retired-id denylist in
    # test_remediation_queue.py is what catches the next retirement.
    'gemini': 'gemini-3.1-pro-preview',
    # CFOP-121. Priced ~2-3x under the Anthropic rung per verdict (measured:
    # $0.002-0.004 against $0.0065, reasoning tokens included — v4-pro thinks
    # before it answers, which eats most of the headline price difference).
    # The money is small either way: the judge fires only on rows that would
    # auto-execute, twice in the last twenty-five. It leads the order for
    # availability as much as cost, after 2026-08-28 put all three previous
    # rungs down at once (400 / 403 / 404).
    'deepseek': 'deepseek-v4-pro',
}
# DeepSeek first: cheapest capable rung, with the frontier peers as failover.
_JUDGE_DEFAULT_ORDER = ('deepseek', 'anthropic', 'xai', 'gemini')

# Model-name tokens that mark a vendor's FAST tier, refused in the judge seat
# (CFOP-121). Tokens, not substrings — 'mini' is inside 'gemini'.
#
# Be precise about what this is: a DENYLIST of the names vendors actually use
# for their cheap tier, not a frontier allowlist. It stops the obvious
# demotion — pointing the veto at a flash/mini/nano id — and it does NOT prove
# a configured model is frontier-class: a mid-tier or superseded id carrying
# none of these markers is accepted, and the operator is trusted for that.
# An allowlist was considered and rejected: enumerating each vendor's current
# top model is the failure CFOP-107 already hit twice, where a pinned id the
# vendor had retired 404'd in production. A list that must be edited every
# time a vendor ships is a worse guard than one that never goes stale.
_JUDGE_FAST_TIER_TOKENS = frozenset({
    'flash', 'mini', 'nano', 'micro', 'tiny', 'fast',
    'haiku', 'lite', 'turbo', 'instant', 'small',
})


def _is_fast_tier_model(model: str) -> bool:
    """True when a model name carries a known fast-tier marker."""
    return bool(set(re.split(r'[^a-z0-9]+', str(model or '').lower()))
                & _JUDGE_FAST_TIER_TOKENS)


def _judge_is_self_review(reporter: str, backend: str, model: str) -> bool:
    """True when this judge peer is the LLM that wrote the recommendation.

    VENDOR-level, not snapshot-level, and that is the whole point. CFOP-70's
    "the implementer is the wrong seat for the veto" is about who produced the
    recommendation, not about which dated id they produced it under — and
    since CFOP-121 lets an operator re-point a backend, matching the exact
    'backend/model' string would mean setting judge_model_deepseek to any
    other DeepSeek id silently re-opens that seat. A knob must not be able to
    switch off a safety guard as a side effect.

    Handles all three shapes _llm_provider_tag emits: 'backend/model', a bare
    'backend' when no model was recorded, and a bare model when no backend
    was. Google's listing ids also carry a 'models/' infix
    ('gemini/models/gemini-3.1-pro-preview'), which the head split absorbs.
    """
    tag = str(reporter or '').strip().lower()
    if not tag:
        return False
    if tag.split('/', 1)[0] == str(backend or '').strip().lower():
        return True
    # No backend recorded: the tag IS the model, so compare against ours.
    return '/' not in tag and tag == str(model or '').strip().lower()

# The verdict itself is two short fields, so this is almost all headroom — and
# that is the point. Models that reason before answering spend this budget
# before emitting any JSON, and a truncated verdict is (correctly) treated as a
# substantive failure and parks the row. At 1024 a thinking model could look
# like a permanently stuck gate rather than a judge.
_JUDGE_MAX_TOKENS = 4096


def _raise_for_status_with_body(resp) -> None:
    """``resp.raise_for_status()``, with the start of the body in the message.

    requests' HTTPError carries the status line and nothing else, and for a
    judge call the status line is the least useful part: WHICH thing was
    wrong — a rejected parameter, a retired model id, an exhausted balance —
    is in the body. The judge's exception text is what lands on a parked
    row's reason (CFOP-117: three vendors failed three different ways and the
    console said "404" for the last of them), so the body is the difference
    between a status code and an operator knowing what to fix.
    """
    import requests as req
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        snippet = ' '.join(str(getattr(resp, 'text', '') or '').split())[:200]
        if not snippet:
            raise
        raise req.HTTPError(f"{e}: {snippet}", response=resp) from None

# The morning summary is authored by the cheap, unverified primary model, so a
# mutation-class rec from it is a HYPOTHESIS, not a diagnosis. These are routed
# through the investigation pipeline (capable model + real tools) instead of
# becoming a remediation directly, and the model's self-reported confidence is
# clamped so a confident hallucination can't look authoritative in the queue.
_SUMMARY_MUTATION_CLASSES = ('node-action', 'gitops-patch', 'k8s-action',
                             'k8s-imperative')
_SUMMARY_CONFIDENCE_CAP = 0.5
# Agent-settings key recording the date (YYYY-MM-DD) the morning summary was
# last sent. Persisted so a pod restart inside the summary window does not
# re-run the report — the in-memory mark alone let every deploy between
# hour_start and hour_end generate another one (2026-08-28: three summaries,
# two of them from back-to-back rollouts, each feeding the remediation queue).
_MORNING_SUMMARY_SENT_SETTING = 'morning_summary_sent_on'

# Sweep/summary recs that say "check/verify/…" are evidence-gathering the agent
# can do itself — never park them as needs-human. Exclude physically-human work
# even when the text also contains a check/verify verb.
_INVESTIGATE_SHAPED = re.compile(
    r'\b(check|verify|confirm|investigate|monitor|look\s+into|examine|'
    r'inspect|capture)\b', re.I)
# A recommendation offering ALTERNATIVE fixes ("truncate the row, or update the
# config") — the fork shape nothing downstream can execute (CFOP-78). Matched
# narrowly: a comma/semicolon before "or" plus an imperative verb after it, or a
# sentence opening with Alternatively/Either. Deliberately NOT bare " or ":
# "delete or retarget the rule" is two verbs on ONE target — a wording choice,
# not a fork — and flagging it would send half the queue through the rewrite.
_FORK_SHAPED = re.compile(
    r'(?:,|;)\s+or\s+(?:update|add|remove|delete|set|change|switch|use|'
    r'configure|increase|decrease|truncate|chunk|restart|scale|migrate|rotate|'
    r'adjust|modify|patch|upgrade|downgrade|enable|disable|replace|move|'
    r'retarget|recreate)\b'
    r'|(?:^|\.\s+)(?:Alternatively|Either)\b')

# Hard identifiers a recommendation names — the things two differently-worded
# recommendations for the same fix reliably share (CFOP-78). Deliberately only
# shapes with near-zero prose collision: dotted-quad IPs, UPPER_SNAKE env/config
# keys (the underscore requirement keeps out STATUS/ERROR/HTTP), and
# <table>_id <n> row references. Bare hostnames and workload names are NOT
# extracted: "needs-human" and "grok-4.6" are dash-words too, and a false fold
# hides a real incident, which is strictly worse than a duplicate row.
# The port stays when present: this cluster runs many services per address,
# so a bare IP is a weak identity — "restart nginx on 192.168.0.131" and the
# stale tunnel rule for 192.168.0.131:80 are different problems on one box.
# Both live tunnel rows spell the port, so they still fold.
_IDENT_IP = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?)\b')
_IDENT_ENV = re.compile(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b')
_IDENT_ROW = re.compile(r'\b([A-Za-z][A-Za-z0-9]*_id)\W{0,3}(\d+)')

_HUMAN_ONLY_SHAPED = re.compile(
    r'\b(physically|hardware|power\s+supply|power\s+strip|sd\s+card|'
    r'replace|swap\s+it|wiring|console|hard-?cycle)\b', re.I)
# A concrete change named in a recommendation or FIX step — the complement of
# _INVESTIGATE_SHAPED for the follow-up branch (CFOP-108). "Verify the limit,
# then raise it" is a conditional fix and stays a row; "verify the limit" is a
# checklist and does not. Deliberately without "open" (a check that "port 10250
# is open") and "set" ("ensure X is set" is a check too).
_MUTATION_SHAPED = re.compile(
    r'\b(update|add|remove|delete|change|switch|configure|increase|decrease|'
    r'truncate|restart|reboot|scale|migrate|rotate|adjust|modify|patch|apply|'
    r'upgrade|downgrade|enable|disable|replace|move|retarget|recreate|'
    r'rollback|roll\s+back|restore|install|uninstall|reinstall|edit|rewrite|'
    r'raise|lower|bump|cordon|drain|evict|kill|reschedule)\b', re.I)


def _fix_is_checklist(fix) -> bool:
    """True when a FIX changes nothing: no diff, and every step is a check.

    The live #80 shape — target.kind=host, steps "check port", "verify config",
    "check iptables". A FIX like that classifies to node-action by kind alone
    (CFOP-80), which is how a list of commands the executor could never run
    (its allowlist is mutating commands only) ends up parked for a human.
    """
    if not isinstance(fix, dict):
        return True
    if str(fix.get('proposed_diff') or '').strip():
        return False
    steps = [str(s) for s in (fix.get('steps') or []) if str(s or '').strip()]
    if not steps:
        return True
    return all(_INVESTIGATE_SHAPED.search(s) and not _MUTATION_SHAPED.search(s)
               for s in steps)


def _note_followup(op, investigation_id: int, key: str) -> None:
    """Remember that this investigation's work moved to a follow-up.

    _queue_needs_action_remediation returns None on a dispatch, which the
    caller would otherwise record as "nothing proposed"; run_investigation
    pops this into findings['followup_dispatched'] so the console says why
    there is no remediation id. Tolerates a MagicMock op, which hands back
    a non-dict for the attribute.
    """
    d = getattr(op, '_checklist_followups', None)
    if not isinstance(d, dict):
        d = {}
        try:
            op._checklist_followups = d
        except Exception:
            return
    d[investigation_id] = key


def _dispatch_checklist_followup(op, investigation_id: int, trigger: str,
                                 alert_info, details: Dict[str, Any], fix) -> bool:
    """Re-run a needs_action whose "fix" is a checklist, instead of parking it.

    CFOP-108. Sweep recs shaped "check / verify …" are already dispatched as
    investigations rather than rows (_feed_remediations_from_sweeps); this is
    the same rule on the path every investigation takes. A row made of checks
    helps nobody: node-action is never auto-eligible and the executor runs
    mutating commands only, so the human it waits for would be typing the
    `ss` and `nslookup` the agent can run itself.

    Exactly one extra pass. The follow-up alert carries ``followup_of`` and
    this returns False for any alert that already has it, so a model that
    answers the second pass with another checklist bottoms out at a human,
    not in a loop. Also defers to the CFOP-71 node-incident collapse (a
    NotReady host's symptoms fold, they are not re-investigated one by one)
    and to an open row under the same key (the sweep branch's guard: the
    evidence was gathered and parked already).

    Module-level, like _parse_structured_fix, so a MagicMock op in the tests
    cannot make it truthy by accident. Returns True when a follow-up was
    queued — the caller then enqueues nothing.
    """
    ai = alert_info or {}
    if ai.get('followup_of'):
        return False
    rec = str(details.get('recommendation') or '')
    if not rec or _HUMAN_ONLY_SHAPED.search(rec) or not _INVESTIGATE_SHAPED.search(rec):
        return False
    if _MUTATION_SHAPED.search(rec) or not _fix_is_checklist(fix):
        return False
    if op._collapse_key_for_node_incident(details):
        return False
    dedupe_key = str(details.get('dedupe_key') or '')
    if dedupe_key and op._open_remediation_for_key(dedupe_key):
        return False
    host = str(details.get('host') or '')
    summary = (
        f"Follow-up to investigation #{investigation_id}: {str(trigger or '')[:200]}. "
        f"That pass ended with a checklist instead of running it — \"{rec[:400]}\". "
        "Run those checks now with your tools (ssh, kubectl, prometheus, logs). "
        "Conclude with either a concrete change (a FIX that modifies something) "
        "or no action with the evidence. Do not recommend further verification."
    )
    # Keyed: enqueue_investigation dedupes on idempotency_key only, and this
    # path never creates a row for the open-row guard above to find. Two
    # needs_action finishes for the same problem (a retry, a manual re-run)
    # must not each spawn a follow-up — the first one already owns it.
    idem = f"investigate-followup:{dedupe_key}" if dedupe_key else ''
    alert = {
        'summary': summary,
        'source': 'investigation-followup',
        'host': host,
        'labels': dict(details.get('alert_labels') or {}),
        'dedupe_key': dedupe_key,
        'followup_of': investigation_id,
    }
    if idem:
        alert['idempotency_key'] = idem
    try:
        result = op.enqueue_investigation(alert)
    except Exception as e:
        # queue.Full or a dead worker: the finding must not vanish. Fall
        # through to today's row so a human still sees it.
        logger.warning(f"Investigation #{investigation_id}: could not queue the follow-up "
                       f"investigation ({e}); enqueuing the row instead")
        return False
    status = str(result.get('status') or '') if isinstance(result, dict) else ''
    if status == 'deduped':
        logger.info(f"Investigation #{investigation_id} needs_action is a checklist; a "
                    f"follow-up for {idem} is already queued — not enqueuing a row")
    else:
        REMEDIATION_FOLDED.labels(reason='investigate_followup').inc()
        logger.info(f"Investigation #{investigation_id} needs_action is a checklist "
                    f"({rec[:80]!r}); dispatched a follow-up investigation instead of a row")
    _note_followup(op, investigation_id, idem or summary[:160])
    return True

# The one definition of what the remediation classes mean. Shared verbatim by
# the morning-summary prompt and the needs_action recommendation classifier so
# the two feeds cannot drift apart on what "gitops-patch" or "manual" covers.
_REMEDIATION_CLASS_RUBRIC = (
    "Classify remediation_class honestly:\n"
    "- investigate: the next step is to GATHER EVIDENCE you can collect "
    "yourself — check pod/job logs, query metrics/Loki, confirm an endpoint "
    "responds, look for a pattern. PREFER THIS over manual for anything "
    "'check/verify/confirm/investigate/monitor'; the agent will investigate "
    "autonomously rather than ask a human.\n"
    "- gitops-patch: a single manifest change in a GitOps repo (set repo: "
    "aachtenberg/homelab-infra for cluster apps, aachtenberg/cfoperator-deploy "
    "for cfoperator/event-runtime itself).\n"
    "- k8s-action: an in-cluster change that can be expressed as a MANIFEST "
    "EDIT in the GitOps repo — scale a deployment (replicas), a rollout "
    "restart (annotation bump), a resource-limit change. The executor "
    "applies these by opening a PR, so if the fix cannot be written as a "
    "file change it is NOT this class.\n"
    "- k8s-imperative: a one-off kubectl verb with no manifest equivalent — "
    "create a Job from a CronJob to capture logs, delete a pod, cordon a "
    "node. Real and often correct, but nothing executes it today: it parks "
    "for a human. Choose it anyway when it is the honest answer; a wrong "
    "k8s-action wastes an executor run and still parks.\n"
    "- node-action: a host change over ssh/ansible (DNS, files, systemd).\n"
    "- data-fix: a change to a database row (UPDATE/DELETE/truncate). Real "
    "and often the honest answer, but nothing executes it today: it parks "
    "for a human. Do not relabel it gitops-patch to make it look runnable.\n"
    "- external-system: a change in a system we do not operate (Cloudflare "
    "dashboard, a vendor console, DNS at the registrar). Parks for a human. "
    "Choose it when the work is there, not in our GitOps repo or cluster.\n"
    "- manual: genuinely needs a human's hands or judgement (hardware, wiring, "
    "a risky decision) — NOT something you could investigate first.\n"
)

# CFOP-80: investigation kinds are not queue classes. Mapped at enqueue.
# Unknown kind → manual (never salvage a class from a typo).
_FIX_KIND_TO_CLASS = {
    'gitops-manifest': 'gitops-patch',
    'k8s-object': 'k8s-action',
    'k8s-imperative': 'k8s-imperative',
    'host': 'node-action',
    'database-row': 'data-fix',
    'external-system': 'external-system',
}

_FIX_JSON_SCHEMA = (
    '{"targets": [{"kind": "gitops-manifest|k8s-object|k8s-imperative|'
    'host|database-row|external-system", "id": "path, name, or host", '
    '"repo": "a linked repo as owner/name, or omit"}], '
    '"observed": [{"source": "the command or file you READ", '
    '"value": "what it actually said, verbatim"}], '
    '"steps": ["ordered action"], '
    '"verify": {"command": "check", "expect": "success signal"}, '
    '"rejected": [{"alternative": "what you considered", '
    '"why_not": "why not"}], "risk": "low|med|high"}'
)


def _class_from_fix_kind(kind) -> str:
    return _FIX_KIND_TO_CLASS.get(str(kind or '').strip().lower(), 'manual')


def _json_object_at(text: str, start: int = 0):
    """Parse one JSON object at/after start, or None. Never salvage."""
    brace = text.find('{', start)
    if brace == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[brace:])
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _resolve_fix_repo(value, known_repos) -> Optional[str]:
    """A FIX target's ``repo`` resolved against the registry, or None.

    Accepts either form an operator would recognise -- the registry's short
    name (``homelab-infra``) or the GitHub slug (``aachtenberg/homelab-infra``)
    -- and always returns the SLUG, because that is what the executor hands to
    GitHub (executor/entrypoint.py: ``list_repo_files(client, repo, base)``).
    A short name is a perfectly reasonable thing for the model to emit and
    would have failed there just as surely as a wrong one, so normalising here
    fixes a second, quieter bug than the one that prompted this.

    ``known_repos`` of None means "the caller does not know the registry",
    which is not the same as an empty registry: the first cannot judge, the
    second resolves nothing. None therefore passes the value through.
    """
    text = str(value or '').strip()
    if not text:
        return None
    if known_repos is None:
        return text
    lowered = text.lower()
    for repo in known_repos:
        if not isinstance(repo, dict):
            continue
        slug = str(repo.get('github') or '').strip()
        name = str(repo.get('name') or '').strip()
        if not (slug or name):
            continue
        if lowered not in (slug.lower(), name.lower()):
            continue
        # Only the slug. A registry entry with no `github` cannot be reached
        # by the executor at all -- run_gitops drives a GitHubClient -- so
        # handing back its short name would swap one unusable value for
        # another and move the failure downstream, which is the bug this
        # function exists to end.
        return slug or None
    return None


def _validate_structured_fix(obj: dict,
                             known_repos=None) -> Optional[Dict[str, Any]]:
    """Schema check: parse-or-None, never fill in missing required fields.

    ``known_repos`` is the git registry. Supplying it makes ``repo`` a checked
    field rather than free text -- it was the one hole left in an otherwise
    strict type, and an unresolvable value produced a payload the executor
    could only bounce (CFOP-85). Kept a parameter rather than read from global
    state so this stays a pure function.
    """
    if not isinstance(obj, dict):
        return None
    targets = obj.get('targets')
    if not isinstance(targets, list) or not targets:
        return None
    clean_targets = []
    for t in targets:
        if not isinstance(t, dict):
            return None
        kind = str(t.get('kind') or '').strip().lower()
        tid = str(t.get('id') or '').strip()
        if not kind or not tid:
            return None
        item = {'kind': kind, 'id': tid}
        resolved = _resolve_fix_repo(t.get('repo'), known_repos)
        if resolved:
            item['repo'] = resolved
        elif kind == 'gitops-manifest':
            # A manifest patch without a repo that resolves is not a
            # near-miss, it is unexecutable: the executor's first act is to
            # list that repo's files. Refusing the FIX costs one recommendation
            # and saves an approval that could only ever bounce.
            #
            # Logged rather than dropped quietly. The row used to fail loudly
            # in the executor, where an operator could at least read
            # last_error; refusing it here removes that trail, and an
            # unexplained disappearance is the failure mode this codebase
            # keeps relearning. If this fires often the lever is the prompt,
            # and the log is what says so.
            logger.warning(
                "FIX rejected: gitops-manifest target %r names repo %r, "
                "which is not in the git registry", tid, t.get('repo'))
            return None
        # Any other kind carries repo incidentally -- a host or k8s-object
        # target is actionable without one -- so an unresolvable value is
        # dropped rather than sinking a real finding.
        clean_targets.append(item)
    steps = obj.get('steps')
    if not isinstance(steps, list) or not steps:
        return None
    if not all(isinstance(s, str) and s.strip() for s in steps):
        return None
    verify = obj.get('verify')
    if not isinstance(verify, dict):
        return None
    command = str(verify.get('command') or '').strip()
    expect = str(verify.get('expect') or '').strip()
    if not command or not expect:
        return None
    # CFOP-88: what was READ before proposing a change. Required and
    # unconditional -- deliberately NOT "required only for steps that change a
    # value", because deciding which steps those are means classifying
    # free-form step text, and a regex over step wording is the same species
    # as the parsers this file is trying to stop accumulating. A restart-the-
    # pod FIX records the pod's status and restart count, which is evidence
    # worth having rather than a tax.
    #
    # The mechanism is the READ, not this check. Remediation #78 proposed
    # MemoryHigh=24G/MemoryMax=28G on a 30 GiB box; the file it wanted to edit
    # explains three lines above the setting that the 16G/20G cap exists
    # because ollama+runners once OOM-killed cluster pods. Requiring the
    # current value forces the call that puts that comment in context.
    # Validation only checks that a specific claim was made -- a fabricated
    # value still passes, and verifying it against the live target is a
    # separate piece of plumbing.
    # Every refusal here is logged, none silently. Requiring this field means
    # a non-complying model degrades every FIX to the classifier, which is a
    # real quality drop that would otherwise look like the FIX path simply
    # going quiet. The log is what tells the difference, so it has to cover
    # the shapes that actually occur -- and once `observed` is named in the
    # prompt, a HALF-FILLED entry (a source with no value) is likelier than an
    # omitted key. Logging only the omission would leave the common failure
    # invisible, which is the mitigation failing at exactly the moment it is
    # needed. Behaviour is unchanged: parse-or-None, refuse.
    tids = [t.get('id') for t in clean_targets]

    def _no_observed(reason: str):
        logger.warning("FIX rejected: %s — cannot show what was read before "
                       "proposing a change (targets=%r)", reason, tids)
        return None

    observed = obj.get('observed')
    if not isinstance(observed, list) or not observed:
        return _no_observed("`observed` missing or empty")
    clean_obs = []
    for o in observed:
        if not isinstance(o, dict):
            return _no_observed("`observed` entry is not an object")
        source = str(o.get('source') or '').strip()
        value = str(o.get('value') or '').strip()
        if not source:
            return _no_observed("`observed` entry has no source")
        if not value:
            # The likeliest half-compliance: it ran something and did not say
            # what came back, which is precisely the gap #78 fell through.
            return _no_observed("`observed` entry has no value")
        clean_obs.append({'source': source, 'value': value})
    rejected = obj.get('rejected')
    if rejected is None:
        rejected = []
    if not isinstance(rejected, list):
        return None
    clean_rej = []
    for r in rejected:
        if not isinstance(r, dict):
            return None
        alt = str(r.get('alternative') or '').strip()
        why = str(r.get('why_not') or '').strip()
        if not alt or not why:
            return None
        clean_rej.append({'alternative': alt, 'why_not': why})
    out = {
        'targets': clean_targets,
        'observed': clean_obs,
        'steps': [s.strip() for s in steps],
        'verify': {'command': command, 'expect': expect},
        'rejected': clean_rej,
    }
    risk = obj.get('risk')
    if risk is not None:
        risk = str(risk).strip().lower()
        if risk not in ('low', 'med', 'high'):
            return None
        out['risk'] = risk
    return out


def _parse_structured_fix(response_text: str,
                          known_repos=None) -> Optional[Dict[str, Any]]:
    """Pull a FIX object from an investigation (or nudge) reply.

    Looks for ``FIX:`` JSON or a fenced json block after STATUS. A nudge
    reply may be the object alone. Malformed → None; never salvage.
    """
    if not response_text or not str(response_text).strip():
        return None
    text = str(response_text)
    lower = text.lower()
    region_from = 0
    status_at = lower.rfind('status:')
    if status_at != -1:
        region_from = status_at
    region = text[region_from:]

    obj = None
    # Line-anchored FIX: — substring 'fix:' matches hotfix:/bugfix: and then
    # the next `{` (often findings JSON) fails validation and drops a real
    # FIX: later in the same STATUS region.
    marked = re.search(r'(?im)(?:^|\n)\s*FIX\s*:', region)
    if marked:
        after = region[marked.end():].lstrip()
        if after.startswith('```'):
            nl = after.find('\n')
            after = after[nl + 1:] if nl != -1 else after[3:]
        obj = _json_object_at(after, 0)
        if not (isinstance(obj, dict)
                and _validate_structured_fix(obj, known_repos)):
            obj = None
    if obj is None:
        fence = re.search(r'```(?:json)?\s*\n(\s*\{)', region, re.I)
        if fence:
            obj = _json_object_at(region, fence.start(1))
            if not (isinstance(obj, dict)
                    and _validate_structured_fix(obj, known_repos)):
                obj = None
    if obj is None:
        stripped = text.strip()
        if stripped.startswith('```'):
            stripped = re.sub(r'^```(?:json)?\s*', '', stripped, count=1, flags=re.I)
            stripped = re.sub(r'\s*```$', '', stripped)
        if stripped.startswith('{'):
            obj = _json_object_at(stripped, 0)
    return (_validate_structured_fix(obj, known_repos)
            if isinstance(obj, dict) else None)


def _fix_targets_dedupe_key(fix: Dict[str, Any]) -> str:
    """tgt- + sha1 of sorted (kind, id, repo). Two wordings of the same
    targets collapse; missing repo is the empty string, not a wildcard."""
    tuples = sorted(
        (str(t.get('kind') or ''), str(t.get('id') or ''), str(t.get('repo') or ''))
        for t in (fix.get('targets') or [])
    )
    blob = json.dumps(tuples, separators=(',', ':'))
    return 'tgt-' + hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


def _hints_from_structured_fix(fix: Dict[str, Any]) -> Dict[str, Any]:
    """Queue hints from a valid FIX. Class from the first target's kind.

    Multi-target never auto-executes (confidence None). Single gitops-manifest
    + risk low may still reach the CFOP-70 judge at 0.8. Everything else
    parks — do not invent high auto-confidence from gemma4.
    """
    targets = fix['targets']
    first = targets[0]
    rclass = _class_from_fix_kind(first['kind'])
    risk = fix.get('risk') or 'high'
    if risk not in ('low', 'med', 'high'):
        risk = 'high'
    if (len(targets) == 1 and first['kind'] == 'gitops-manifest'
            and risk == 'low'):
        confidence = 0.8
    else:
        confidence = None
    host = None
    repo = None
    for t in targets:
        if t['kind'] == 'host' and not host:
            host = t['id']
        if t.get('repo') and not repo:
            repo = t['repo']
    return {
        'remediation_class': rclass,
        'risk': risk,
        'confidence': confidence,
        'host': host,
        'repo': repo,
        'classifier_backend': None,
        'classifier_model': None,
        'targets': targets,
        'observed': list(fix.get('observed') or []),
        'steps': list(fix.get('steps') or []),
        'verify': dict(fix.get('verify') or {}),
        'rejected': list(fix.get('rejected') or []),
    }

# Triggers that describe a *recoverable* runtime condition — if the pod is
# healthy now, the thing the alert worried about has cleared. Used by the
# Tier-1 noise filter (early-exit + needs_action downgrade). See
# docs/noise-reduction.md.
#
# Kept as two classes because they need different flapping guards. The restart
# class leaves a trace in restartCount, so `recovered_restart_threshold` can
# tell a settled pod from a flapping one. The probe class leaves none — a
# readiness probe restarts nothing, so restartCount is structurally 0 however
# badly the probe is flapping. _PROBE_TRIGGER routes that class to its own
# guard (how long the pod has held Ready); see _recovered_and_healthy.
#
# The probe class names the three kubelet probe types explicitly rather than
# matching bare "probe"/"unhealthy". Those wider words reach findings that are
# not about a kubelet probe at all — "unhealthy upstream", "volume unhealthy",
# or a blackbox probe against an external URL — where the named pod being Ready
# says nothing about whether the reported problem is real, so silencing on pod
# health would be wrong. All six triggers from the incident match `readiness`.
_RESTART_CLASS = r"restart|terminat|exit\s*code|not\s*ready|notready|oom|crashloop|back-?off"
_PROBE_CLASS = r"readiness|liveness|startup\s*probe"
_RECOVERABLE_TRIGGER = re.compile(_RESTART_CLASS + "|" + _PROBE_CLASS, re.I)
_PROBE_TRIGGER = re.compile(_PROBE_CLASS, re.I)

# Workload names as they appear in free-form sweep prose ("plane-api",
# "faster-whisper"). Dashed identifiers only: bare words like "plane" or "pod"
# match half the cluster. Used by _resolve_pod_from_cluster.
_WORKLOAD_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


class _MetricsLogHandler(logging.Handler):
    """Logging handler that increments LOG_MESSAGES Prometheus counter."""
    def emit(self, record):
        try:
            level = record.levelname
            component = record.name or 'cfoperator'
            LOG_MESSAGES.labels(level=level, component=component).inc()
        except Exception:
            pass


logging.getLogger().addHandler(_MetricsLogHandler())


def _llm_provider_tag(result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Format ``provider/model`` (or either alone) from an LLM result or meta dict.

    Accepts the chat/summary result shape (``backend`` + ``model``) and the
    sweep_meta shape (``provider`` + ``model``). Returns None when neither
    field is present so callers can omit the tag rather than invent "unknown".
    """
    if not isinstance(result, dict):
        return None
    backend = str(result.get('backend') or result.get('provider') or '').strip()
    model = str(result.get('model') or '').strip()
    if backend and model:
        return f"{backend}/{model}"
    return backend or model or None


def _append_llm_attribution(text: str, result: Dict[str, Any]) -> str:
    """Append a "_Generated by: backend/model_" footer to LLM-produced text.

    The fallback chain reports which provider actually served the call
    (the configured primary may have cold-started and been bypassed), so
    operators can correlate output quality with the served model. Both
    fields can be missing on the safe-default path — degrade gracefully
    instead of emitting a bare "Generated by: /" line.
    """
    attribution = _llm_provider_tag(result)
    if not attribution:
        return text
    return f"{text}\n\n_Generated by: {attribution}_"



AGENT_INFO = Info('cfoperator_agent', 'CFOperator agent information')
AGENT_UPTIME = Gauge('cfoperator_uptime_seconds', 'Agent uptime in seconds')
MONITORED_HOSTS = Gauge('cfoperator_monitored_hosts', 'Number of monitored hosts')
RUNNING_CONTAINERS = Gauge('cfoperator_running_containers', 'Number of running containers across fleet')
ERROR_RATE = Counter('cfoperator_errors_total', 'Total errors')

# LLM Observability metrics
LLM_REQUESTS = Counter('cfoperator_llm_requests_total', 'Total LLM requests', ['provider', 'model', 'result'])
LLM_TOKENS = Counter('cfoperator_llm_tokens_total', 'Total tokens used', ['provider', 'model', 'type'])  # type: prompt/completion
# Buckets span 1s..600s: LLM chat turns (incl. tool-calling iterations) routinely
# run tens of seconds and reasoning models reach several minutes. The Histogram
# default buckets top out at 10s, so every real request landed in +Inf and
# histogram_quantile() reported a flat 10.0 for every percentile.
LLM_LATENCY = Histogram(
    'cfoperator_llm_latency_seconds', 'LLM request latency', ['provider', 'model'],
    buckets=(1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 450, 600, float('inf')),
)
LLM_ERRORS = Counter('cfoperator_llm_errors_total', 'LLM errors by provider', ['provider', 'error_type'])
LLM_FALLBACKS = Counter('cfoperator_llm_fallbacks_total', 'LLM fallback chain activations', ['from_provider', 'to_provider'])
# Empty final responses from the tool loop (see _handle_empty_final). The
# `disposition` label keeps two very different signals apart:
#   nudged    - first empty of the turn. EMPTY_RESPONSE_NUDGE sent, one bonus
#               round granted; the benchmark recovered 19/19 this way. A
#               formatting quirk the loop absorbs.
#   exhausted - second empty. EmptyLLMResponseError raised and the provider
#               chain rotates. The model failing the task, at the cost of a
#               whole extra provider attempt.
# Collapsing them into one number cannot distinguish "gemma4 needs a second
# prompt sometimes" from "gemma4 cannot finish the job". Divide by
# cfoperator_llm_requests_total (incremented once per _chat_with_tools call,
# success and error alike) for the per-model rate.
LLM_EMPTY_FINALS = Counter('cfoperator_llm_empty_final_responses_total', 'Tool-loop turns that ended with an empty final message', ['provider', 'model', 'disposition'])
# result: success | error (retryable — timeout, down endpoint, missing
# model) | truncated (input exceeded the model's context and was sent
# head-first) | unembeddable (a deterministic input failure we now refuse
# to re-send; CFOP-81). truncated and unembeddable are the two worth
# alerting on: they mean the knowledge base is holding records it cannot
# index faithfully.
EMBEDDING_REQUESTS = Counter('cfoperator_embedding_requests_total', 'Embedding generation requests', ['result'])
EMBEDDING_CACHE_HITS = Counter('cfoperator_embedding_cache_hits_total', 'Embedding cache hits vs misses', ['result'])

# OpenAI-compatible cloud LLM providers. They share an identical request /
# response shape (chat/completions, OpenAI-style tool calling) and differ only
# in base URL and API-key env var, so one code path serves all of them.
OPENAI_COMPAT_PROVIDERS = {
    'groq': {
        'label': 'Groq',
        'base_url': 'https://api.groq.com/openai/v1',
        'key_env': 'GROQ_API_KEY',
    },
    'xai': {
        'label': 'xAI Grok',
        'base_url': 'https://api.x.ai/v1',
        'key_env': 'XAI_API_KEY',
    },
    # config.yaml, config.yaml.example ("Shipped providers: ... gemini ...")
    # and docs/config-reference.md have all named gemini as supported, but it
    # existed in NO code path — not here, not in _get_provider_chain's
    # fallback_order, not in _resolve_provider's accepted backends. A gemini
    # entry in the chain was silently inert. Registered here because Google
    # ships an OpenAI-compatible surface, so it needs no branch of its own.
    'gemini': {
        'label': 'Google Gemini',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
        'key_env': 'GEMINI_API_KEY',
    },
    # DeepSeek serves the OpenAI wire under /v1 (the Groq/xAI shape, not the
    # Gemini no-/v1 shape), so it is one more registry row and no new branch.
    # default_model is the model _resolve_provider falls back to when neither
    # the console (deepseek_selected_model) nor llm.fallback names one — so a
    # key alone makes the provider usable. Only this entry carries one: the
    # other three would be guesses, and guessed model names are how the
    # first-run stubs shipped retired Gemini models twice (#199, #201).
    # Confirmed against GET /models on 2026-08-26.
    'deepseek': {
        'label': 'DeepSeek',
        'base_url': 'https://api.deepseek.com/v1',
        'key_env': 'DEEPSEEK_API_KEY',
        'default_model': 'deepseek-v4-pro',
    },
}

# Sent once when a model ends the tool loop with an empty message (no tool
# calls, no text). gemma4:26b does this on virtually every healthy-cluster
# investigation (benchmarks/empty_response_sim.py: 10/10 empty finals, and
# 19/19 recovered by this nudge); without it the empty response used to be
# stored verbatim and _extract_status('') silently defaulted to 'monitoring'.
EMPTY_RESPONSE_NUDGE = (
    "You have gathered enough data. Do NOT call any more tools. "
    "Respond NOW with a short summary of what you found, followed by your "
    "final answer in exactly the format the instructions above require."
)


class EmptyLLMResponseError(RuntimeError):
    """Model returned an empty final message even after the nudge retry.

    Must propagate out of _chat_with_tools_inner (never be swallowed into a
    synthetic response) so _chat_with_tools_with_fallback rotates to the
    next provider in the chain.
    """


@dataclass
class _ToolLoopStats:
    """Counters accumulated over one _chat_with_tools_inner tool loop.

    Shared by every provider branch so the loop's several exit points all
    report the same shape via ``result()``.
    """

    tool_calls: int = 0
    cached_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    learning_ids: List[str] = field(default_factory=list)
    # PRs the model opened itself through github_create_pr (html_url, in call
    # order). The tool result is the only evidence such a PR exists; a queue
    # row that does not carry it lands needs-human with the URL buried in
    # prose and is offered to the executor, which opens a second one
    # (CFOP-116, row #85).
    opened_prs: List[str] = field(default_factory=list)

    def result(self, response: str) -> Dict[str, Any]:
        return {
            'response': response,
            'tool_calls': self.tool_calls,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'learning_ids': self.learning_ids,
            'cached_tool_hits': self.cached_hits,
            'opened_prs': list(self.opened_prs),
        }


def _opened_pr_url(result) -> Optional[str]:
    """html_url of a PR that a github_create_pr call actually opened, or None.

    tools.github.create_pr answers ``{"success": True, "html_url": ...}`` on
    the happy path and ``{"success": False, "error": ...}`` otherwise; the
    tool layer may also hand back a string. Only the first shape is a PR.
    """
    if not isinstance(result, dict) or not result.get('success'):
        return None
    url = str(result.get('html_url') or '').strip()
    return url or None


def _with_classifier_identity(hints: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp the (backend, model) that produced a classification onto its hints.

    So the row records which model decided a change was safe to make unattended
    rather than leaving it to be reconstructed from the provider chain months
    later (CFOP-70 needed exactly that reconstruction).
    """
    out = dict(hints)
    out['classifier_backend'] = str((result or {}).get('backend') or '') or None
    out['classifier_model'] = str((result or {}).get('model') or '') or None
    return out


class CFOperator:
    """
    Continuous Feedback Operator

    Dual-mode OODA loop:
    1. Reactive: Handle firing alerts immediately
    2. Proactive: Deep system sweeps every 30 minutes
    """

    def __init__(self, config_path: str = "config.yaml"):
        logger.info("Initializing CFOperator...")

        # Load configuration
        self.config = self._load_config(config_path)

        # Initialize core components
        # Build database URL for ResilientKnowledgeBase
        db_url = f"postgresql://{self.config['database']['user']}:{self.config['database']['password']}@{self.config['database']['host']}:{self.config['database']['port']}/{self.config['database']['database']}"
        self.kb = ResilientKnowledgeBase(
            db_url=db_url,
            host_id='cfoperator'  # Single central agent
        )

        # Initialize database schema (creates tables if they don't exist)
        self.kb.initialize_schema()

        # Initialize LLM fallback chain
        self.llm = LLMFallback(
            db_session_factory=self.kb.session_scope,
            settings_getter=self._get_agent_settings
        )

        # LLM request timeout (generous default for cold model loads)
        self.llm_timeout = self.config.get('llm', {}).get('primary', {}).get('timeout', 180)

        # Initialize embeddings service for vector search
        embedding_config = self.config.get('llm', {}).get('embeddings', {})
        self.embeddings = EmbeddingService(
            ollama_url=embedding_config.get('url') or self.config.get('llm', {}).get('primary', {}).get('url') or os.getenv('OLLAMA_URL', 'http://localhost:11434'),
            model=embedding_config.get('model'),
            db_session_factory=self.kb.session_scope
        )

        # Initialize pluggable observability backends
        self._init_observability_backends()

        # Resolve the linked-repo registry before the tools are built: the
        # GitHub/git tools are constructed from it, and their schema
        # descriptions name the repos, so a stale list is a stale prompt.
        self._load_git_registry()

        # Initialize tool registry
        self.tools = ToolRegistry(self)

        # Load skills from skills/ directory
        self.skills = self._load_skills()

        # OODA state
        self.current_investigation = None
        self.last_sweep = 0
        self.last_reap = 0    # remediation reaper tick
        self.last_drain = 0   # remediation drainer tick
        self.last_verify = 0   # remediation PR-reconcile tick
        self.last_node_recovery = 0  # node-incident auto-resolve tick (CFOP-71)
        self.last_metrics = 0  # remediation gauge refresh tick
        self.last_cockpit_reap = 0  # cockpit janitor tick (CFOP-36)
        self.start_time = time.time()
        # Initialized to start_time so the first heartbeat fires after the
        # configured interval rather than immediately after the bootstrap
        # banner — avoids redundant chatter on the first cycle.
        self.last_heartbeat = self.start_time

        # HTTP-driven investigation queue (POST /v1/investigate).
        # Bounded; full queue rejects with 503 so event_runtime's worker retries.
        ooda_cfg = self.config.get('ooda', {})
        queue_size = max(1, int(ooda_cfg.get('investigation_queue_size', 32)))
        self._investigation_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=queue_size)
        self._investigation_worker_thread: Optional[threading.Thread] = None
        # Idempotent enqueue: retries from Slack bridges / MCP hosts carrying
        # the same idempotency_key (or alert_id) within the TTL are absorbed
        # instead of double-enqueued. In-memory by design — a restart clearing
        # the window only risks a duplicate investigation, never a lost one.
        self._enqueue_dedup_ttl = float(ooda_cfg.get('investigation_dedup_ttl_seconds', 3600))
        self._enqueue_dedup_keys: Dict[str, float] = {}
        self._enqueue_dedup_lock = threading.Lock()
        # Serializes investigations across the reactive poll (main thread)
        # and the HTTP worker thread. Without this, both paths could race
        # on self.current_investigation and other non-thread-safe state.
        self._investigation_lock = threading.Lock()
        # Reactive Alertmanager poll is preserved by default; PR C flips this to false.
        self._reactive_poll_enabled = bool(ooda_cfg.get('reactive_poll', True))

        # Initialize web server
        chat_config = self.config.get('chat', {})
        if chat_config.get('enabled', True):
            self.web_server = WebServer(
                operator=self,
                host='0.0.0.0',
                port=chat_config.get('port', 8083)
            )
        else:
            self.web_server = None

        # Initialize Ollama pool for parallel sweeps (if configured)
        pool_config = self.config.get('ollama_pool', {}).get('instances', [])
        if pool_config:
            self.ollama_pool = OllamaPool(pool_config, kb=self.kb)
            logger.info(f"Ollama pool initialized with {len(pool_config)} instances")
        else:
            self.ollama_pool = None

        # Update Prometheus metrics
        TOOLS_REGISTERED.set(len(self.tools.tools))
        MONITORED_HOSTS.set(len(self.config.get('infrastructure', {}).get('hosts', {})))
        AGENT_INFO.info({
            'version': build_version(),
            'host_id': 'cfoperator',
            'mode': 'dual_ooda'
        })

        logger.info("CFOperator initialized successfully")

    def reload_config(self) -> Dict[str, Any]:
        """Reload configuration from disk without restarting."""
        config_path = os.getenv('CONFIG_PATH', 'config.yaml')
        old_hosts = set(self.config.get('infrastructure', {}).get('hosts', {}).keys())
        self.config = self._load_config(config_path)
        new_hosts = set(self.config.get('infrastructure', {}).get('hosts', {}).keys())
        MONITORED_HOSTS.set(len(new_hosts))
        added = new_hosts - old_hosts
        removed = old_hosts - new_hosts
        # Re-apply the console-managed registry over the file we just re-read,
        # or "Reload config.yaml" would silently revert the running process to
        # the file's repo list while the DB still says otherwise (CFOP-77).
        self._load_git_registry()
        self._refresh_git_tools()
        logger.info(f"Config reloaded: {len(new_hosts)} hosts (added={added or 'none'}, removed={removed or 'none'})")
        return {
            'hosts': len(new_hosts),
            'added': list(added),
            'removed': list(removed),
            'repos': len(self.git_repos()),
            'repos_source': self._git_repos_source,
        }

    # ------------------------------------------------------------------
    # Linked repo registry (CFOP-77)
    #
    # config['git']['repos'] stays the one place every consumer reads the
    # registry from — the tool registry, the remediation proposer, the git
    # context enricher. What changes is that its contents are now *resolved*
    # (DB setting over config file) rather than copied straight out of the
    # YAML, so the file's own list is kept alongside it for the console's
    # "what is config.yaml still saying" view.
    # ------------------------------------------------------------------

    def _load_git_registry(self) -> List[Dict[str, Any]]:
        """Snapshot config.yaml's repo list, then apply the DB override.

        Must be called after every config load and never twice against the
        same load: the second call would read back the effective list as if
        it were the file's.
        """
        self._file_git_repos = shared_repos.sanitize((self.config.get('git') or {}).get('repos'))
        raw = None
        try:
            raw = self.kb.get_setting(shared_repos.SETTING_KEY, '')
        except Exception as e:
            # A DB that is down must not unlink every repo — config.yaml is
            # the fallback, exactly as it is for the triage model.
            logger.warning(f"Could not read the {shared_repos.SETTING_KEY} setting, using config.yaml: {e}")
        repos, source = shared_repos.resolve(self._file_git_repos, raw)
        self.config.setdefault('git', {})['repos'] = repos
        self._git_repos_source = source
        logger.info(f"Linked repos: {len(repos)} (source={source})")
        return repos

    def apply_git_repos(self, repos: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Point the running process at a repo list. ``None`` = config.yaml.

        The console calls this after persisting a write so the change is live
        for chat, investigations and remediation proposals without a restart —
        the whole point of storing the registry in the DB.
        """
        if repos is None:
            effective = list(self._file_git_repos)
            self._git_repos_source = 'config'
        else:
            effective = shared_repos.sanitize(repos)
            self._git_repos_source = 'db'
        self.config.setdefault('git', {})['repos'] = effective
        self._refresh_git_tools()
        logger.info(f"Linked repos updated: {len(effective)} (source={self._git_repos_source})")
        return effective

    def _refresh_git_tools(self) -> None:
        """Rebuild the git/github tools against the current registry."""
        tools = getattr(self, 'tools', None)
        if tools is None:
            return
        try:
            tools.refresh_git_tools()
        except Exception as e:
            logger.warning(f"Could not refresh git tools after a registry change: {e}")

    def git_repos(self) -> List[Dict[str, Any]]:
        """The effective registry."""
        return (self.config.get('git') or {}).get('repos') or []

    def file_git_repos(self) -> List[Dict[str, Any]]:
        """What config.yaml itself declares, shadowed or not."""
        return list(getattr(self, '_file_git_repos', []))

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration, merged over the shared default schema.

        Delegates to ``cfshared.config`` so the agent and the event runtime
        resolve the same file the same way. Before CFOP-26 a config file that
        existed at all bypassed ``_default_config()`` entirely, so every omitted
        setting fell through to whatever literal was written at its call site.
        """
        return shared_config.load_config(config_path)

    def _load_env_file(self, config_path: str) -> None:
        """Load a colocated .env file so config.yaml placeholders resolve consistently."""
        shared_config.load_env_file(config_path)

    def _expand_env_vars(self, config: Any) -> Any:
        """Recursively expand ${VAR} references in config."""
        return shared_config.expand_env_vars(config)

    def _default_config(self) -> Dict[str, Any]:
        """Return default configuration.

        Now the same schema every config is merged over, rather than a second,
        incomplete opinion that only applied when the file was missing (it had
        no ``llm`` section at all, so a fileless start had no model to call).
        """
        return shared_config.default_config()

    def _load_skills(self) -> Dict[str, Dict[str, Any]]:
        """
        Load skills from skills/ directory.

        Each skill is in its own subdirectory with a SKILL.md file containing:
        - YAML frontmatter (name, description)
        - Markdown instructions for the LLM

        Returns:
            Dict mapping skill name to {name, description, instructions}
        """
        skills = {}
        skills_dir = Path('skills')

        if not skills_dir.exists():
            logger.warning("Skills directory not found - skills disabled")
            return skills

        for skill_path in skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            skill_file = skill_path / 'SKILL.md'
            if not skill_file.exists():
                logger.warning(f"Skipping {skill_path.name} - no SKILL.md file")
                continue

            try:
                content = skill_file.read_text()

                # Parse YAML frontmatter
                if content.startswith('---'):
                    parts = content.split('---', 2)
                    if len(parts) >= 3:
                        frontmatter = yaml.safe_load(parts[1])
                        instructions = parts[2].strip()

                        skill_name = frontmatter.get('name')
                        if skill_name:
                            # `argument-hint` is the SKILL.md convention for
                            # what follows the command. Every skill accepts a
                            # free-text target (the MCP prompt argument, the
                            # cfassist `/skill name target` shape), so an
                            # absent hint means "[target]", not "nothing".
                            arg_hint = frontmatter.get('argument-hint')
                            skills[skill_name] = {
                                'name': skill_name,
                                'description': frontmatter.get('description', ''),
                                'args': '[target]' if arg_hint is None else str(arg_hint).strip(),
                                'instructions': instructions
                            }
                            logger.info(f"Loaded skill: {skill_name}")
                        else:
                            logger.warning(f"Skipping {skill_path.name} - no 'name' in frontmatter")
                else:
                    logger.warning(f"Skipping {skill_path.name} - missing YAML frontmatter")
            except Exception as e:
                logger.error(f"Failed to load skill from {skill_path.name}: {e}")

        logger.info(f"Loaded {len(skills)} skills: {list(skills.keys())}")
        return skills

    def _init_observability_backends(self):
        """Initialize pluggable observability backends based on config."""
        obs_config = self.config.get('observability', {})

        # Metrics backend. An empty URL is the configured-off state, not an
        # error: since CFOP-26 every config is merged over a default schema, so
        # these sections always exist and it is the URL that says whether the
        # operator actually has one. Logs in particular are optional.
        metrics_config = obs_config.get('metrics', {})
        if metrics_config.get('backend') == 'prometheus' and metrics_config.get('url'):
            self.metrics = PrometheusMetrics(url=metrics_config.get('url'))
            logger.info(f"Initialized Prometheus metrics backend: {metrics_config.get('url')}")
        elif not metrics_config.get('url'):
            logger.info("Metrics backend disabled (no observability.metrics.url configured)")
            self.metrics = None
        else:
            logger.warning(f"Unsupported metrics backend: {metrics_config.get('backend')}")
            self.metrics = None

        # Logs backend
        logs_config = obs_config.get('logs', {})
        if logs_config.get('backend') == 'loki' and logs_config.get('url'):
            self.logs = LokiLogs(url=logs_config.get('url'))
            logger.info(f"Initialized Loki logs backend: {logs_config.get('url')}")
        elif not logs_config.get('url'):
            logger.info("Logs backend disabled (no observability.logs.url configured)")
            self.logs = None
        else:
            logger.warning(f"Unsupported logs backend: {logs_config.get('backend')}")
            self.logs = None

        # Container backend(s) — supports list (like notifications) or single dict
        container_configs = obs_config.get('containers', [])
        if isinstance(container_configs, dict):
            container_configs = [container_configs]  # backward compat
        self._container_configs = container_configs  # stash for drift check

        container_backends = []
        for container_config in container_configs:
            backend_type = container_config.get('backend')
            if backend_type == 'prometheus':
                from observability.prometheus_containers import PrometheusContainers
                prometheus_url = metrics_config.get('url')
                ssh_user = container_config.get('ssh_user', 'sre')
                backend = PrometheusContainers(prometheus_url=prometheus_url, ssh_user=ssh_user)
                container_backends.append(backend)
                logger.info(f"Initialized Prometheus container backend (SSH user: {ssh_user})")
            elif backend_type == 'docker':
                backend = DockerContainers(hosts=container_config.get('hosts', {}))
                container_backends.append(backend)
                logger.info(f"Initialized Docker backend with {len(container_config.get('hosts', {}))} hosts")
            elif backend_type == 'kubernetes':
                k8s_config = self.config.get('kubernetes', {})
                backend = KubernetesContainers(
                    kubeconfig=container_config.get('kubeconfig', k8s_config.get('kubeconfig')),
                    context=container_config.get('context', k8s_config.get('context'))
                )
                container_backends.append(backend)
                logger.info("Initialized Kubernetes container backend")
            else:
                if backend_type:
                    logger.warning(f"Unsupported container backend: {backend_type}")

        if container_backends:
            self.containers = CompositeContainerBackend(container_backends)
        else:
            self.containers = None

        # Alerts backend
        alerts_config = obs_config.get('alerts', {})
        if alerts_config.get('backend') == 'alertmanager' and alerts_config.get('url'):
            self.alerts = AlertmanagerAlerts(url=alerts_config.get('url'))
            logger.info(f"Initialized Alertmanager backend: {alerts_config.get('url')}")
        elif not alerts_config.get('url'):
            logger.info("Alerts backend disabled (no observability.alerts.url configured)")
            self.alerts = None
        else:
            logger.warning(f"Unsupported alerts backend: {alerts_config.get('backend')}")
            self.alerts = None

        # Notifications backend(s)
        self.notifications = []
        for notif_config in obs_config.get('notifications', []):
            webhook = notif_config.get('webhook_url', '')
            if notif_config.get('backend') == 'slack':
                if not webhook:
                    logger.info("Slack notifications skipped (no webhook URL)")
                    continue
                notif = SlackNotifications(webhook_url=webhook)
                self.notifications.append(notif)
                logger.info("Initialized Slack notifications")
            elif notif_config.get('backend') == 'discord':
                if not webhook:
                    logger.info("Discord notifications skipped (no webhook URL)")
                    continue
                notif = DiscordNotifications(webhook_url=webhook)
                self.notifications.append(notif)
                logger.info("Initialized Discord notifications")
            elif notif_config.get('backend') == 'alertmanager':
                notif = AlertmanagerNotifications(url=notif_config.get('url', alerts_config.get('url', '')))
                self.notifications.append(notif)
                logger.info("Initialized Alertmanager notifications")

    def run(self):
        """
        Main OODA loop - dual mode operation.

        Runs continuously with:
        - Reactive: Check for alerts every 10 seconds
        - Proactive: Deep sweep every 30 minutes
        """
        logger.info("="*60)
        logger.info("Starting CFOperator OODA loop")
        alert_interval = self._get_alert_check_interval()
        sweep_interval = self._get_sweep_interval()
        logger.info(f"Reactive poll: {'enabled' if self._reactive_poll_enabled else 'disabled'} (check alerts every {alert_interval}s)")
        logger.info(f"Proactive: deep sweep every {sweep_interval}s ({sweep_interval//60} minutes)")
        logger.info("="*60)

        # Start the HTTP investigation worker before the web server so the
        # POST /v1/investigate endpoint has something to drain into.
        self._start_investigation_worker()

        # Remediation reaper/drainer/verify in their own thread (see loop note).
        self._start_remediation_worker()

        # Start web server in background thread
        if self.web_server:
            self.web_server.run_threaded()
            logger.info(f"Web UI available at http://0.0.0.0:{self.config.get('chat', {}).get('port', 8083)}")
            self._start_cockpit_bridge()

        while True:
            try:
                # Update uptime metric
                AGENT_UPTIME.set(time.time() - self.start_time)
                OODA_CYCLES.inc()

                # Heartbeat — proves the loop is alive between events.
                if time.time() - self.last_heartbeat >= self._get_heartbeat_interval():
                    logger.info(self._format_heartbeat())
                    self.last_heartbeat = time.time()

                # MODE 1: Reactive - handle alerts immediately
                if self._reactive_poll_enabled and self.alerts:
                    alerts = self._check_alerts()
                    if alerts:
                        logger.info(f"Alerts detected: {len(alerts)}")
                        SWEEPS.labels(mode='reactive').inc()
                        for alert in alerts:
                            self._handle_alert_reactive(alert)

                # MODE 2: Proactive - periodic deep sweep
                if time.time() - self.last_sweep > self._get_sweep_interval():
                    logger.info("="*60)
                    logger.info("PROACTIVE MODE: Starting deep system sweep")
                    logger.info("="*60)
                    SWEEPS.labels(mode='proactive').inc()
                    self._deep_system_sweep()
                    self.last_sweep = time.time()

                # MODE 3: Morning summary (TPS report style)
                self._check_morning_summary()

                # Remediation reaper/drainer/verify run in their own daemon thread
                # (_remediation_worker_loop) so a long proactive sweep can't starve
                # them — the OODA loop is single-threaded and a sweep blocks for
                # minutes. Metrics gauge refresh is cheap, so it stays inline.
                self._update_remediation_metrics()

                time.sleep(self._get_alert_check_interval())

            except KeyboardInterrupt:
                logger.info("Shutting down CFOperator...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                ERROR_RATE.inc()
                time.sleep(30)  # Back off on errors

    def _check_alerts(self) -> List[Dict[str, Any]]:
        """Check for firing alerts from Alertmanager."""
        try:
            return self.alerts.get_firing_alerts()
        except Exception as e:
            # Only log alert errors once per minute to avoid spam
            if not hasattr(self, '_last_alert_error') or time.time() - self._last_alert_error > 60:
                logger.warning(f"Alertmanager unavailable: {type(e).__name__} - reactive mode disabled")
                self._last_alert_error = time.time()
            return []

    def _handle_alert_reactive(self, alert: Dict[str, Any]):
        """
        Reactive mode: Handle a firing alert by running an investigation.

        The orient/decide/act split lives inside run_investigation. This path
        ignores the returned ActionResult — Slack notification is owned by the
        agent's own notifier today. When event_runtime drives investigations
        over HTTP, the result is posted back instead.
        """
        logger.info(f"REACTIVE MODE: Handling alert: {alert.get('labels', {}).get('alertname', 'unknown')}")
        try:
            self.run_investigation(alert)
        except Exception:
            logger.exception("Reactive investigation failed")

    def _observe_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """OBSERVE phase: Gather context about the alert.

        Accepts both event_runtime Alert dicts (top-level ``summary``) and
        raw Alertmanager payloads (``annotations.summary``). Without this
        fallback, HTTP-driven investigations ran with trigger='Unknown alert'.
        """
        trigger = (
            alert.get('summary')
            or alert.get('annotations', {}).get('summary')
            or 'Unknown alert'
        )
        return {
            'alert': alert,
            'timestamp': datetime.now(),
            'trigger': trigger,
        }

    def _orient(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ORIENT phase: Understand what's happening.

        - Search knowledge base for similar issues
        - Search learnings for known solutions
        - Get current baseline state
        """
        trigger = context.get('trigger', '')

        # Generate embedding once for both learning and investigation search
        query_embedding = None
        try:
            if self.embeddings.is_available():
                query_embedding = self.embeddings.generate_embedding(trigger)
        except Exception as e:
            logger.warning(f"Query embedding failed, falling back to FTS search: {e}")

        # Search for relevant learnings (hybrid if embedding available, FTS otherwise)
        try:
            if query_embedding:
                learnings = self.kb._kb.find_learnings_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3
                )
            else:
                learnings = self.kb.find_learnings(query=trigger, limit=3)
            if learnings:
                logger.info(f"Found {len(learnings)} relevant learnings for: {trigger[:60]}")
            context['known_learnings'] = learnings
        except Exception as e:
            logger.warning(f"Learning search failed: {e}")
            context['known_learnings'] = []

        # Search for similar past investigations using embeddings (semantic) + FTS
        try:
            if query_embedding:
                similar = self.kb._kb.find_similar_investigations_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3
                )
                if similar:
                    logger.info(f"Found {len(similar)} similar investigations via hybrid search")
                context['similar_investigations'] = similar
            else:
                context['similar_investigations'] = []
        except Exception as e:
            logger.warning(f"Similar investigation search failed: {e}")
            context['similar_investigations'] = []

        return context

    @staticmethod
    def _similar_past_citations(context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Trim the orient-phase similar-investigation hits for persistence.

        The hits already went into the prompt, but a citation that lives only
        in the prompt is invisible afterwards — whether the report mentions it
        is up to the LLM. Persisting the hits into ``findings['similar_past']``
        makes "this investigation was informed by these past ones" a recorded
        fact the console and the kind demo can assert on (CFOP-31).
        """
        cited = []
        for inv in (context.get('similar_investigations') or [])[:3]:
            try:
                cited.append({
                    'id': inv.get('id'),
                    'trigger': str(inv.get('trigger') or '')[:200],
                    'outcome': inv.get('outcome'),
                    'similarity': inv.get('similarity')
                        or inv.get('combined_score')
                        or inv.get('vector_similarity')
                        or 0,
                })
            except AttributeError:
                continue
        return cited

    def run_investigation(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Run one investigation end-to-end for a single alert dict.

        Wraps observe + orient + act so it can be invoked from either the
        reactive Alertmanager poll loop or the HTTP /v1/investigate path.
        Held under ``_investigation_lock`` so the two paths can't race on
        shared state (``current_investigation``, KB session, embeddings).
        Returns an ActionResult-shaped dict (see event_runtime.models.ActionResult).
        """
        with self._investigation_lock:
            context = self._observe_alert(alert)
            context = self._orient(context)
            return self._act(context)

    def run_triage(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Classify an alert without running a full investigation.

        Called from event_runtime's HTTPTriageDecisionEngine before it
        decides whether to dispatch the alert to /v1/investigate. The LLM
        sees the alert plus the top 3 similar past investigations from the
        embeddings index, and returns one of four actions:

          - log_only:    known noise; record and move on
          - notify:      operator should see the alert but no LLM dive needed
          - investigate: novel pattern, do a full LLM investigation
          - escalate:    page-worthy, severity is high and pattern is bad

        Returns ``{"action": ..., "reason": ..., "confidence": ...}``. On
        any failure (LLM unreachable, parse error, etc.) returns
        ``action="investigate"`` so we err on the side of investigating
        rather than dropping an alert. Tight prompt + max_iterations=1 +
        cheap-model preference keeps this fast.
        """
        # Build a one-shot classification prompt. No tools — the LLM should
        # not actually investigate; it should decide whether to.
        trigger = (
            alert.get('summary')
            or alert.get('annotations', {}).get('summary')
            or 'Unknown alert'
        )
        severity = alert.get('severity', 'unknown')
        labels = alert.get('labels') or alert.get('details', {}).get('labels', {}) or {}

        # Resolution alerts ("finding X has cleared since last sweep") are
        # synthesized by the sweep, not externally observed — there is no
        # classification to make. Short-circuit to notify so Slack gets the
        # ":white_check_mark: Resolved: …" line without spending an LLM call.
        details = alert.get('details') or {}
        if isinstance(details, dict) and details.get('resolution'):
            return {
                'action': 'notify',
                'reason': 'finding cleared since previous sweep',
                'confidence': 1.0,
                'backend': None,
                'model': None,
            }

        similar_context = ""
        try:
            if self.embeddings.is_available():
                query_embedding = self.embeddings.generate_embedding(trigger)
                similar = self.kb._kb.find_similar_investigations_hybrid(
                    query_text=trigger,
                    query_embedding=query_embedding,
                    limit=3,
                )
                if similar:
                    lines = []
                    for inv in similar:
                        sim = inv.get('similarity') or inv.get('vector_similarity', 0)
                        lines.append(
                            f"- [{inv.get('outcome','?'):10}] "
                            f"{inv.get('trigger','')[:100]} (similarity: {sim:.2f})"
                        )
                    similar_context = "\n\nSimilar past investigations:\n" + "\n".join(lines)
        except Exception:
            pass  # Best-effort; missing context is not a triage blocker.

        system_prompt = """You are a triage classifier for infrastructure alerts.
Decide the cheapest correct response. Respond ONLY with a JSON object,
no other text:
{
  "action":     "log_only" | "notify" | "investigate" | "escalate",
  "reason":     "<one short sentence>",
  "confidence": <0.0 to 1.0>
}

Action rubric:
  log_only    Known noise. Test pods (smoke-test-*, tmp-*), Alertmanager
              Watchdog, intentionally-failing canaries.
  notify      Operator should see this, but a full LLM investigation is
              waste. Use when a similar past investigation resolved with
              little effort, when severity=info, or when the pattern is
              one the operator already understands (e.g. raspberrypi
              SD-card warning that's been known for weeks). Requires a
              clear precedent: do NOT use notify for pod failures
              (CrashLoop, OOMKilled, ImagePullBackOff, NotReady) unless
              a similar past investigation is listed in the alert
              context.
  investigate Novel pattern, no similar resolved precedent, or pattern
              that previous investigations classified as 'monitoring'.
              A pod failure with no similar past investigation listed is
              novel by definition — investigate it. Default if
              uncertain.
  escalate    Severity=critical AND impact is broad (NodeNotReady on a
              control plane, data-loss patterns, multiple correlated
              services down). Operator should page in.

Prefer notify and log_only when there is a clear precedent. Prefer
investigate when uncertain. Use escalate only for genuinely urgent."""

        user_msg = (
            f"Alert severity: {severity}\n"
            f"Alert summary: {trigger}\n"
            f"Labels: {json.dumps(labels, default=str)[:500]}"
            f"{similar_context}\n\n"
            "Classify."
        )

        # CFOP-57: a dedicated triage model (the fine-tuned local classifier)
        # takes the first shot when configured. This is deliberately a single
        # targeted attempt rather than a model override on the fallback call:
        # _get_provider_chain never emits two ollama entries, so an override
        # that failed would skip the primary local model and land directly on
        # paid providers. Failing here instead drops into the untouched call
        # below, so the chain after a triage-model failure is byte-identical
        # to the no-override configuration.
        result = None
        triage_model = self._triage_model()
        if triage_model:
            primary_cfg = self.config.get('llm', {}).get('primary', {}) or {}
            triage_url = primary_cfg.get('url', os.getenv('OLLAMA_URL', ''))
            try:
                result = self._chat_with_tools(
                    provider_type='ollama', url=triage_url, model=triage_model,
                    messages=[{'role': 'user', 'content': user_msg}],
                    system_context=system_prompt,
                    max_iterations=1,  # one-shot classification — no tool loop
                )
                result['backend'] = 'ollama'
                result['model'] = triage_model
                # Unparseable output counts as failure too, not just raised
                # exceptions: this family's known failure mode is a 200 whose
                # tool-call syntax ollama dumps as raw text (ollama#16934).
                # Without this check that lands on the "unparseable ->
                # investigate" default below and the standard chain is never
                # consulted, silently degrading every triage to investigate.
                if self._parse_triage_response(
                        result.get('response', '').strip()) is None:
                    logger.warning(
                        f"Triage model {triage_model} returned unparseable "
                        "response; using standard provider chain")
                    result = None
            except Exception as e:
                logger.warning(
                    f"Triage model {triage_model} failed "
                    f"({type(e).__name__}: {e}); using standard provider chain")
                result = None

        if result is None:
            try:
                result = self._chat_with_tools_with_fallback(
                    messages=[{'role': 'user', 'content': user_msg}],
                    system_context=system_prompt,
                    max_iterations=1,  # one-shot classification — no tool loop
                )
            except Exception as e:
                logger.warning(f"Triage LLM unavailable, defaulting to investigate: {e}")
                return {
                    'action': 'investigate',
                    'reason': f'triage LLM unavailable ({type(e).__name__})',
                    'confidence': 0.0,
                    'backend': None,
                    'model': None,
                }

        # The fallback chain reports which provider actually served the call,
        # not just the configured primary — surface it so Slack can show
        # "triaged by groq/openai/gpt-oss-120b" when Ollama cold-started and
        # we fell over. Without this, operators can't tell which LLM
        # classified an alert (matters for cost attribution + debugging
        # disagreements between models).
        served_backend = result.get('backend')
        served_model = result.get('model')

        response_text = result.get('response', '').strip()
        # The LLM sometimes wraps JSON in fenced code blocks or prose; pull
        # the first JSON object out instead of relying on perfect output.
        decision = self._parse_triage_response(response_text)
        if decision is None:
            logger.warning(f"Triage LLM returned unparseable response, defaulting to investigate: {response_text[:200]}")
            return {
                'action': 'investigate',
                'reason': 'triage response unparseable',
                'confidence': 0.0,
                'backend': served_backend,
                'model': served_model,
            }
        decision['backend'] = served_backend
        decision['model'] = served_model
        return decision

    @staticmethod
    def _parse_triage_response(response_text: str) -> Optional[Dict[str, Any]]:
        """Extract a valid triage decision dict from raw LLM output.

        Returns None if no valid JSON with the required fields is found.
        Tolerates markdown code fences and trailing prose.
        """
        if not response_text:
            return None
        # Strip optional markdown code fence (```json ... ``` or ``` ... ```).
        text = response_text.strip()
        if text.startswith("```"):
            # Drop the first line (fence + optional language) and trailing fence.
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        # Find the first {...} JSON object in the text.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        action = payload.get("action")
        if action not in {"log_only", "notify", "investigate", "escalate"}:
            return None
        return {
            "action": action,
            "reason": str(payload.get("reason", ""))[:280],
            "confidence": float(payload.get("confidence", 0.5)),
        }

    def enqueue_investigation(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """Non-blocking enqueue for an HTTP-triggered investigation.

        Raises queue.Full when the queue has no slot; caller should map that
        to HTTP 503 so the event_runtime worker retries with backoff. The
        rejection counter is incremented here so callers don't reach into
        module-level metrics.

        Alerts carrying idempotency_key (preferred) or alert_id are deduped
        within a TTL window: a repeat within the window returns
        status='deduped' without enqueuing.
        """
        dedup_key = alert.get('idempotency_key') or alert.get('alert_id')
        if dedup_key:
            now = time.time()
            with self._enqueue_dedup_lock:
                self._enqueue_dedup_keys = {
                    k: t for k, t in self._enqueue_dedup_keys.items()
                    if now - t < self._enqueue_dedup_ttl
                }
                if dedup_key in self._enqueue_dedup_keys:
                    return {
                        'status': 'deduped',
                        'queue_depth': self._investigation_queue.qsize(),
                        'alert_id': alert.get('alert_id'),
                    }
                self._enqueue_dedup_keys[str(dedup_key)] = now
        try:
            self._investigation_queue.put_nowait(alert)
        except queue.Full:
            INVESTIGATION_QUEUE_REJECTED.inc()
            # The rejected alert never entered the queue — drop its dedup
            # claim so the caller's retry isn't absorbed as 'deduped'.
            if dedup_key:
                with self._enqueue_dedup_lock:
                    self._enqueue_dedup_keys.pop(str(dedup_key), None)
            raise
        INVESTIGATION_QUEUE_DEPTH.set(self._investigation_queue.qsize())
        return {
            'status': 'queued',
            'queue_depth': self._investigation_queue.qsize(),
            'alert_id': alert.get('alert_id'),
        }

    def _start_investigation_worker(self) -> None:
        """Spawn the single background thread that drains the investigation queue."""
        if self._investigation_worker_thread and self._investigation_worker_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._investigation_worker_loop,
            daemon=True,
            name='cfoperator-investigation-worker',
        )
        thread.start()
        self._investigation_worker_thread = thread
        logger.info("Investigation worker thread started")

    def _investigation_worker_loop(self) -> None:
        """Drain the investigation queue. One request at a time — LLM throughput is the bottleneck."""
        while True:
            try:
                alert = self._investigation_queue.get()
            except Exception:
                logger.exception("Investigation queue read failed; worker exiting")
                return
            try:
                INVESTIGATION_QUEUE_DEPTH.set(self._investigation_queue.qsize())
                result = self.run_investigation(alert)
                self._post_action_result_to_event_runtime(alert, result)
            except Exception:
                logger.exception("HTTP-triggered investigation failed")
            finally:
                self._investigation_queue.task_done()

    def _post_action_result_to_event_runtime(self, alert: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Best-effort post-back of completed ActionResult to event_runtime.

        Sends ``{"alert": <alert>, "result": <ActionResult>}`` so the
        completion endpoint can fire its Slack notification with the
        original alert's severity and summary. No-op when
        CFOP_EVENT_RUNTIME_URL is unset or the completion endpoint is
        unavailable (it ships in a follow-up PR). Failures are logged at
        debug — durability lives in the agent's investigation row, not here.
        """
        url = os.getenv('CFOP_EVENT_RUNTIME_URL', '').strip()
        if not url:
            return
        alert_id = alert.get('alert_id')
        if not alert_id:
            return
        endpoint = f"{url.rstrip('/')}/v1/investigations/{alert_id}/complete"
        body = json.dumps({'alert': alert, 'result': result}, default=str).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        # Shared secret matches event_runtime's CFOP_COMPLETION_SHARED_SECRET.
        # Without the header, event_runtime returns 401 (when its secret is set)
        # so completion notifications can't be spoofed by other cluster pods.
        secret = os.getenv('CFOP_COMPLETION_SHARED_SECRET', '').strip()
        if secret:
            headers['X-CFOP-Token'] = secret
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError
        req = Request(endpoint, data=body, headers=headers, method='POST')
        try:
            with urlopen(req, timeout=5) as resp:
                status = 'ok' if 200 <= resp.status < 300 else f'http_{resp.status}'
        except HTTPError as exc:
            status = f'http_{exc.code}'
            logger.debug(f"Post-back to event_runtime returned {exc.code}: {endpoint}")
        except (URLError, TimeoutError, OSError) as exc:
            status = 'transport_error'
            logger.debug(f"Post-back to event_runtime failed ({type(exc).__name__}): {endpoint}")
        INVESTIGATION_POSTBACK.labels(status=status).inc()

    def _act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ACT phase: Investigate and fix.

        - Create investigation record
        - Run LLM investigation loop with tools
        - Extract learnings from resolved investigations

        Returns an ActionResult-shaped dict so callers (HTTP path, future
        event_runtime post-back) can surface the real outcome rather than
        a stub success message.
        """
        trigger = context.get('trigger', 'Unknown trigger')
        logger.info(f"Starting investigation: {trigger[:100]}")

        # Create investigation record
        inv_id = self.kb.start_investigation(trigger=trigger)
        self.current_investigation = inv_id
        start_time = time.time()
        outcome = 'failed'
        message = f"Investigation failed: {trigger[:200]}"
        details: Dict[str, Any] = {'investigation_id': inv_id, 'outcome': outcome}

        try:
            # Build investigation prompt with learnings and similar investigations context
            learnings_text = ""
            if context.get('known_learnings'):
                learnings_text = "\n\nRelevant past learnings:\n"
                for l in context['known_learnings']:
                    learnings_text += f"- [{l['learning_type']}] {l['title']}: {l['description'][:200]}\n"

            similar_text = ""
            if context.get('similar_investigations'):
                similar_text = "\n\nSimilar past investigations:\n"
                for inv in context['similar_investigations'][:3]:
                    sim_score = inv.get('similarity') or inv.get('vector_similarity', 0)
                    similar_text += f"- [{inv.get('outcome', '?')}] {inv.get('trigger', '')[:100]} (similarity: {sim_score})\n"

            alert_info = context.get('alert', {})

            # Tier-1 noise filter (1b): if the alert is about a recoverable
            # runtime condition and the pod is healthy now with only a few
            # restarts, don't spend a full investigation on it — record a
            # 'monitoring' result and return. Flapping (high restart count) and
            # still-broken pods fall through to a real investigation.
            # _recovered_and_healthy applies the probe class's own flapping
            # guard internally, since restart_thresh cannot see that class.
            noise_cfg = self._noise_config()
            noise_on = noise_cfg.get('enabled', True)
            restart_thresh = int(noise_cfg.get('recovered_restart_threshold', 3))
            if noise_on:
                pre_recovered, pre_note, pre_restarts = self._recovered_and_healthy(alert_info, trigger)
                if pre_recovered and pre_restarts <= restart_thresh:
                    return self._early_exit_monitoring(inv_id, trigger, start_time, pre_note)

            system_prompt = f"""You are CFOperator investigating an infrastructure alert.

Alert: {trigger}
Alert details: {json.dumps(alert_info, default=str)[:1000]}
{learnings_text}{similar_text}

Investigate this alert using the available tools. Check metrics, logs, and container/service status.
First give a short summary of what you found. Then end your response with:

STATUS: <one of: resolved | needs_action | monitoring | escalate>
  - resolved: the resource is healthy RIGHT NOW — the problem is gone, or you fixed it during this investigation. Do NOT use resolved just because you identified a fix that someone still has to apply.
  - needs_action: you found the problem but it needs a change you could not make yourself; your RECOMMENDATION says what to do.
  - monitoring: transient or inconclusive; worth watching, no action yet.
  - escalate: urgent; a human should look now.
RECOMMENDATION: <the single most useful operator-facing next step — a concrete command or config change, or "No action needed" when the resource is genuinely healthy>
When STATUS is needs_action, also emit a FIX object after RECOMMENDATION (JSON, not inside the RECOMMENDATION line). targets is a list so a two-repo ordered fix can be stated. Omit FIX only when no change is required.
FIX: {_FIX_JSON_SCHEMA}"""

            # Run LLM investigation with tools, with provider fallback so a
            # transient Ollama timeout (e.g. GPU cold-start) doesn't abort
            # the investigation.
            try:
                result = self._chat_with_tools_with_fallback(
                    messages=[{'role': 'user', 'content': f'Investigate this alert: {trigger}'}],
                    system_context=system_prompt,
                )
            except RuntimeError as e:
                if "No LLM providers available" not in str(e):
                    raise
                logger.error("No LLM provider available for investigation")
                duration = time.time() - start_time
                self.kb.update_investigation(
                    investigation_id=inv_id,
                    completed_at=datetime.now(),
                    findings={'error': 'No LLM provider available'},
                    outcome='failed',
                    duration_seconds=duration
                )
                INVESTIGATIONS.labels(outcome='failed').inc()
                details.update({'duration_s': round(duration, 1), 'error': 'no_llm_provider'})
                return self._build_action_result(
                    success=False,
                    message=f"Investigation failed (no LLM provider): {trigger[:200]}",
                    details=details,
                )

            provider_type = result.get('backend', 'unknown')
            model = result.get('model', 'unknown')

            response_text = result.get('response', '')
            tool_calls_count = result.get('tool_calls', 0)
            duration = time.time() - start_time

            # Classify outcome from the model's explicit STATUS verdict rather
            # than keyword-sniffing the whole response. The old heuristic matched
            # 'resolved'/'healthy'/'normal' anywhere in the text, so any thorough
            # investigation ("CPU is normal", "can be resolved by...") was
            # mislabeled resolved — even for a pod still stuck Pending.
            outcome = self._extract_status(response_text)

            # B1: don't take "resolved" on faith — confirm against live cluster
            # state. If the alert pins to a pod that is still Pending/CrashLoop,
            # downgrade resolved -> needs_action so we never announce a fix that
            # didn't happen.
            outcome, verify_note = self._verify_investigation_outcome(outcome, alert_info, trigger)

            # Tier-1 noise filter (1a): if the investigation lands on needs_action
            # but the alerted runtime condition has recovered (pod healthy now,
            # few restarts) — including pods that recovered *during* the
            # investigation — downgrade to monitoring so it doesn't page red.
            if noise_on and outcome == 'needs_action':
                post_recovered, post_note, post_restarts = self._recovered_and_healthy(alert_info, trigger)
                if post_recovered and post_restarts <= restart_thresh:
                    outcome = 'monitoring'
                    verify_note = ((verify_note + '; ') if verify_note else '') + f"recovered — {post_note}"
                    logger.info(f"Noise filter: needs_action -> monitoring ({post_note})")

            # The investigation prompt asks the LLM to end with a
            # "RECOMMENDATION:" line; surface it as the operator-facing next
            # step so a direct /investigate carries actionable guidance (not
            # just a bare "Resolved"), matching what the sweep path already does.
            recommendation = self._extract_recommendation(response_text)
            # CFOP-80: one JSON nudge if needs_action and FIX is missing, then
            # today's classifier. Never fail the investigation on a bad FIX.
            structured_fix = None
            if outcome == 'needs_action':
                structured_fix, response_text = self._ensure_structured_fix(
                    outcome, recommendation, response_text, trigger)

            # Phase-B remediation: for a confirmed needs_action, see if this is a
            # case we can propose a concrete fix for. Default off; dry-run
            # unless `remediation.open_prs` is set, in which case this can open
            # a real (human-merge-gated) PR. Never touches the running cluster
            # either way. Conservative by design — it mostly turns a vague
            # "needs_action" into either a candidate patch or a precise decline
            # reason (see remediation.py + the design doc).
            proposal = self._maybe_propose_remediation(outcome, alert_info, trigger)

            # CFOP-46: a needs_action recommendation must land somewhere
            # actionable, not dead-end in a read-only list. Classify it (cheap
            # one-shot call) and enqueue through the existing queue gates; the
            # row links back here via investigation_id and lands needs-human.
            # CFOP-80: a valid FIX skips that classifier (class from target.kind).
            # CFOP-116: a PR the model opened itself (github_create_pr) is
            # evidence the tool loop holds, not the prose. It rides to the
            # queue row and into findings, or the console shows a row with
            # no PR and an Approve button that would open a second one.
            opened_prs = [u for u in (result.get('opened_prs') or []) if u]
            remediation_id = None
            if outcome == 'needs_action':
                remediation_id = self._queue_needs_action_remediation(
                    inv_id, trigger, alert_info, recommendation, response_text,
                    provider=f"{provider_type}/{model}", proposal=proposal,
                    structured_fix=structured_fix, opened_prs=opened_prs)

            findings = {
                'response': response_text[:5000],
                'tool_calls': tool_calls_count,
                'provider': f"{provider_type}/{model}",
                'recommendation': recommendation,
            }
            if structured_fix:
                findings['fix'] = structured_fix
            if opened_prs:
                findings['opened_prs'] = opened_prs
            similar_past = self._similar_past_citations(context)
            if similar_past:
                findings['similar_past'] = similar_past
            if verify_note:
                findings['outcome_verification'] = verify_note
            if proposal is not None:
                findings['remediation_proposal'] = proposal.to_details()
            if remediation_id:
                findings['remediation_id'] = remediation_id
            followups = getattr(self, '_checklist_followups', None)
            if isinstance(followups, dict) and inv_id in followups:
                # CFOP-108: no remediation id because the work moved to a
                # follow-up investigation. Say so, rather than letting the
                # console read "nothing proposed" off a row whose child is
                # doing the work.
                findings['followup_dispatched'] = followups.pop(inv_id)

            # Update investigation record
            self.kb.update_investigation(
                investigation_id=inv_id,
                completed_at=datetime.now(),
                findings=findings,
                outcome=outcome,
                duration_seconds=duration,
                tool_calls_count=tool_calls_count
            )
            INVESTIGATIONS.labels(outcome=outcome).inc()
            logger.info(f"Investigation #{inv_id} completed: {outcome} ({duration:.1f}s, {tool_calls_count} tool calls)")

            # Extract learnings from resolved investigations
            if outcome == 'resolved':
                self._extract_learnings(inv_id, trigger, findings)

            # Generate embedding for this investigation (async, non-blocking)
            self._embed_investigation(inv_id, trigger, findings, outcome)

            message = self._action_message(outcome, trigger, duration, tool_calls_count)
            details.update({
                'outcome': outcome,
                'duration_s': round(duration, 1),
                'tool_calls': tool_calls_count,
                'provider': f"{provider_type}/{model}",
                'findings_snippet': response_text[:500],
            })
            # event_runtime renders details['remediation'] as the
            # "Recommendation:" line on the completion notification.
            if recommendation:
                details['remediation'] = recommendation
            if proposal is not None:
                details['remediation_proposal'] = proposal.to_details()
            if remediation_id:
                details['remediation_id'] = remediation_id
            return self._build_action_result(
                success=outcome != 'failed',
                message=message,
                details=details,
            )

        except Exception as e:
            logger.error(f"Investigation #{inv_id} failed: {e}", exc_info=True)
            duration = time.time() - start_time
            try:
                self.kb.update_investigation(
                    investigation_id=inv_id,
                    completed_at=datetime.now(),
                    findings={'error': str(e)},
                    outcome='failed',
                    duration_seconds=duration
                )
            except Exception as persist_err:
                logger.warning(f"Could not persist failure record for investigation #{inv_id}: {persist_err}")
            INVESTIGATIONS.labels(outcome='failed').inc()
            details.update({
                'outcome': 'failed',
                'duration_s': round(duration, 1),
                'error': str(e)[:500],
            })
            return self._build_action_result(
                success=False,
                message=f"Investigation failed: {type(e).__name__}: {str(e)[:200]}",
                details=details,
            )
        finally:
            self.current_investigation = None

    @staticmethod
    def _extract_recommendation(response_text: str) -> str:
        """Pull the operator-facing next step out of an investigation response.

        The investigation prompt asks the LLM to end with a line prefixed
        ``RECOMMENDATION:``. We surface that as the notification's
        "Recommendation:" line. Uses the *last* occurrence so a passing
        mention earlier in the reasoning doesn't win over the final verdict.
        Returns "" when absent so callers can omit the field entirely.
        """
        if not response_text:
            return ""
        marker = 'recommendation:'
        idx = response_text.lower().rfind(marker)
        if idx == -1:
            return ""
        tail = response_text[idx + len(marker):].strip()
        # CFOP-80: stop at FIX: so the JSON cannot swallow the prose.
        tail = re.split(r'\n\s*FIX\s*:', tail, maxsplit=1, flags=re.I)[0]
        # Stop at the first blank line so we capture just the recommendation
        # paragraph, then cap length for a one-line notification.
        return tail.split('\n\n')[0].strip()[:400]

    def _ensure_structured_fix(self, outcome: str, recommendation: str,
                               response_text: str, trigger: str = ''):
        """Parse FIX; one JSON nudge if needs_action and missing/malformed.

        Returns (fix_or_None, response_text). Never raises — a nudge failure
        degrades to today's classifier. Module-level parse so MagicMock tests
        cannot intercept it.
        """
        fix = _parse_structured_fix(response_text, self.git_repos())
        if outcome != 'needs_action' or fix:
            return fix, response_text
        rec = str(recommendation or '').strip()
        if (not rec or rec.lower().startswith('no action')
                or rec.lower() in ('none', 'n/a', 'nothing')):
            return None, response_text
        nudged = self._nudge_structured_fix(response_text, rec, trigger)
        if not nudged:
            return None, response_text
        fix = _parse_structured_fix(nudged, self.git_repos())
        if not fix:
            return None, response_text
        appended = response_text.rstrip() + '\nFIX: ' + json.dumps(fix)
        return fix, appended

    def _nudge_structured_fix(self, response_text: str, recommendation: str,
                              trigger: str = '') -> Optional[str]:
        """One-shot ask for a FIX JSON object. Parse-or-None; never salvage."""
        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{
                    'role': 'user',
                    'content': (
                        f"Alert: {str(trigger)[:300]}\n"
                        f"Recommendation: {str(recommendation)[:800]}\n\n"
                        f"Your previous ending:\n{str(response_text)[-1500:]}\n\n"
                        "That reply is missing a valid FIX JSON object. "
                        f"Reply with ONLY a FIX object matching: {_FIX_JSON_SCHEMA}"
                    ),
                }],
                system_context=(
                    "You emit a single FIX JSON object for an infrastructure "
                    "investigation. No other text, no array."
                ),
                max_iterations=1,
            )
            return result.get('response', '')
        except Exception as e:
            logger.warning(f"FIX nudge failed, falling back to classifier: {e}")
            return None

    @staticmethod
    def _extract_status(response_text: str) -> str:
        """Classify the investigation outcome from the model's explicit verdict.

        The prompt requires a final ``STATUS:`` line with one of
        resolved | needs_action | monitoring | escalate. We parse that line
        instead of keyword-sniffing the whole response — the old heuristic
        marked anything mentioning "resolved"/"healthy"/"normal" as resolved,
        which falsely cleared issues that were still broken (e.g. a pod stuck
        Pending whose fix the model only *recommended*).

        Non-resolved tokens are checked first so a line like
        "needs_action — can be resolved by ..." classifies as needs_action,
        not resolved. Falls back to a conservative heuristic (never resolved
        on loose keywords) when the model omits the line.
        """
        text = response_text or ""
        idx = text.lower().rfind('status:')
        if idx != -1:
            line = text[idx + len('status:'):].split('\n', 1)[0].lower()
            if any(k in line for k in ('needs_action', 'needs-action', 'needs action', 'unresolved', 'action needed')):
                return 'needs_action'
            if any(k in line for k in ('escalate', 'escalated', 'urgent')):
                return 'escalated'
            if 'monitor' in line:
                return 'monitoring'
            if any(k in line for k in ('resolved', 'fixed', 'healthy', 'no action', 'no issue')):
                return 'resolved'
        # No usable STATUS line — be conservative. Escalation signals win;
        # otherwise default to monitoring. Never infer 'resolved' here.
        low = text.lower()
        if any(w in low for w in ('escalat', 'urgent')):
            return 'escalated'
        return 'monitoring'

    @staticmethod
    def _identify_pod(alert_info: Dict[str, Any], trigger: str) -> Optional[tuple]:
        """Best-effort (namespace, pod_name) from an alert, or None.

        Tries structured alert fields first, then known trigger shapes:
          - "Pod <ns>/<pod> not ready ..."   (alertmanager)
          - "<pod> on <ns>: status=..."       (sweep finding)
        Returns None when it can't confidently pin a single pod.
        """
        ai = alert_info or {}
        ns = ai.get('namespace') or ai.get('ns')
        name = ai.get('resource_name') or ai.get('pod') or ai.get('pod_name')
        rtype = str(ai.get('resource_type') or '').lower()
        if ns and name and rtype in ('', 'pod', 'pods'):
            return (str(ns), str(name))
        text = trigger or ai.get('summary') or ''
        m = re.search(r'\bPod\s+([a-z0-9-]+)/([a-z0-9][a-z0-9.-]*)', text, re.I)
        if m:
            return (m.group(1), m.group(2))
        m = re.search(r'\b([a-z0-9][a-z0-9.-]*-[a-z0-9]+)\s+on\s+([a-z0-9-]+)\s*:', text, re.I)
        if m:
            return (m.group(2), m.group(1))
        return None

    @staticmethod
    def _pod_is_healthy(status: Dict[str, Any]) -> bool:
        """True only if a pod is actually up right now (Running+Ready, or Succeeded)."""
        phase = status.get('phase')
        if phase == 'Succeeded':
            return True
        if phase != 'Running':
            return False
        for c in status.get('conditions', []):
            if c.get('type') == 'Ready':
                return c.get('status') == 'True'
        return False

    def _noise_config(self) -> Dict[str, Any]:
        """The ``ooda.noise`` settings block, defensively — the noise filter is
        also exercised on instances built without a config."""
        cfg = getattr(self, 'config', None)
        if not isinstance(cfg, dict):
            return {}
        return (cfg.get('ooda', {}) or {}).get('noise', {}) or {}

    def _resolve_pod_from_cluster(self, trigger: str) -> Optional[tuple]:
        """Live-state fallback for _identify_pod: pin (namespace, pod) by
        matching a workload name out of free-form prose against running pods.

        Sweep findings are LLM prose carrying no structured resource fields —
        the finding schema is severity/finding/evidence/remediation — so
        neither _identify_pod's structured branch nor either of its two
        trigger shapes can fire for them. Resolving against the cluster rather
        than adding a third regex keeps the match honest: a name that isn't
        running cannot be matched.

        Gives up whenever the answer is not unique — more than one workload
        named, or more than one pod behind the one workload. A missed filter
        costs one redundant investigation; a wrong pin silences the wrong
        alert.
        """
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        tokens = {t for t in _WORKLOAD_TOKEN.findall((trigger or "").lower()) if len(t) >= 4}
        if not tokens:
            return None
        try:
            res = k8s.get_pods(all_namespaces=True)
        except Exception:
            return None
        # An exact name beats a prefix, so "cert-manager" resolves to
        # cert-manager rather than dying ambiguous against -webhook and
        # -cainjector. Each pod is tested against *every* token before being
        # classified: breaking on the first match would let a shorter prefix
        # token beat an exact one purely on set iteration order.
        exact: Dict[tuple, list] = {}
        prefix: Dict[tuple, list] = {}
        for pod in res.get('pods', []):
            meta = pod.get('metadata') or {}
            name, namespace = meta.get('name'), meta.get('namespace')
            if not name or not namespace:
                continue
            workload = normalize_service_name(name)
            if any(token in (name, workload) for token in tokens):
                exact.setdefault((namespace, workload), []).append(name)
            elif any(workload.startswith(token + '-') for token in tokens):
                prefix.setdefault((namespace, workload), []).append(name)
        hits = exact or prefix
        if len(hits) != 1:
            return None
        (namespace, _workload), pods = next(iter(hits.items()))
        if len(pods) != 1:
            return None  # replicas — can't confidently pin a single pod
        return (namespace, pods[0])

    @staticmethod
    def _ready_stable_seconds(status: Dict[str, Any]) -> Optional[float]:
        """Seconds the pod has continuously held Ready=True, or None when the
        transition time is missing or unparseable.

        None means "can't tell", and callers treat that as not-stable — for a
        noise filter an unknown answer must not license silencing an alert.
        """
        for cond in status.get('conditions', []):
            if cond.get('type') != 'Ready' or cond.get('status') != 'True':
                continue
            raw = cond.get('lastTransitionTime')
            if not raw:
                return None
            try:
                when = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            except (TypeError, ValueError):
                return None
            now = datetime.now(timezone.utc) if when.tzinfo else datetime.now()
            return max(0.0, (now - when).total_seconds())
        return None

    def _recovered_and_healthy(self, alert_info: Dict[str, Any], trigger: str) -> tuple:
        """Tier-1 noise filter: is the alert about a recoverable runtime
        condition whose pod is healthy *right now*? Returns
        (recovered: bool, note: str|None, restart_count: int).

        Only fires for restart/termination/exit-code/not-ready/crashloop/oom
        and probe-failure triggers tied to an identifiable pod that is
        currently Running+Ready. A healthy pod with a *non-runtime* concern
        (mis-config, deprecation) won't match — it keeps its needs_action.
        """
        if not _RECOVERABLE_TRIGGER.search(trigger or ""):
            return (False, None, 0)
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return (False, None, 0)
        ident = self._identify_pod(alert_info, trigger) or self._resolve_pod_from_cluster(trigger)
        if not ident:
            return (False, None, 0)
        namespace, pod_name = ident
        try:
            status = k8s.get_pod_status(namespace, pod_name)
        except Exception:
            return (False, None, 0)
        if not status.get('success') or not self._pod_is_healthy(status):
            return (False, None, 0)
        # Probe-class triggers carry no restart signal, so the caller's restart
        # threshold cannot tell a settled pod from one whose readiness is
        # flapping. Ask how long it has held Ready instead: a flapping probe
        # transitions the condition, one failing below failureThreshold never
        # does.
        stable_note = ""
        if _PROBE_TRIGGER.search(trigger or ""):
            stable = self._ready_stable_seconds(status)
            min_stable = int(self._noise_config().get('recovered_ready_stable_seconds', 600))
            if stable is None or stable < min_stable:
                return (False, None, 0)
            stable_note = f"Ready {int(stable // 60)}m, "
        restarts = max((c.get('restartCount', 0) for c in status.get('containerStatuses', [])),
                       default=0)
        return (True,
                f"{namespace}/{pod_name} healthy now ({stable_note}{restarts} restart(s), recovered)",
                restarts)

    def _ephemeral_service_names(self) -> set:
        """Normalized service names of ephemeral Job/CronJob pods in the cluster.
        Used to keep their scheduled churn out of failure correlations (and to
        purge any that were persisted before the baseline filter landed)."""
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return set()
        try:
            res = k8s.get_pods(all_namespaces=True)
        except Exception:
            return set()
        names = set()
        for p in res.get('pods', []):
            pod_name = (p.get('metadata') or {}).get('name', '')
            if pod_name and is_ephemeral_job_pod(pod_name):
                names.add(normalize_service_name(pod_name))
        return names

    def _restart_finding_is_noise(self, finding_text: str, threshold: int) -> Optional[str]:
        """Reason if a 'container restarted N times' sweep finding is recovered
        noise — the pod is healthy now with <= threshold restarts. None otherwise.

        Mirrors the Tier-1 alert-path filter for the *sweep* path, which
        generates these findings independently (e.g. faster-whisper: healthy
        21h, restartCount 1, re-flagged every sweep)."""
        text = (finding_text or "").lower()
        if 'restart' not in text:
            return None
        cm = re.search(r"container ['\"]([a-z0-9._-]+)['\"]", text)
        nm = re.search(r"namespace ['\"]([a-z0-9-]+)['\"]", text)
        if not (cm and nm):
            return None
        name, ns = cm.group(1), nm.group(1)
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        try:
            res = k8s.get_pods(namespace=ns)
        except Exception:
            return None
        matched = [p for p in res.get('pods', [])
                   if (p.get('metadata') or {}).get('name', '').startswith(name)]
        if not matched:
            return None
        worst = 0
        for p in matched:
            st = p.get('status', {})
            if not self._pod_is_healthy({'phase': st.get('phase'),
                                         'conditions': st.get('conditions', [])}):
                return None  # something still unhealthy — keep the finding
            worst = max(worst, max((c.get('restartCount', 0)
                                    for c in st.get('containerStatuses', [])), default=0))
        if worst > threshold:
            return None  # flapping — keep the finding
        return (f"container '{name}' in {ns} is healthy now with <= {threshold} "
                f"restart(s) — recovered transient, not actionable")

    def _early_exit_monitoring(self, inv_id: int, trigger: str, start_time: float,
                               note: str) -> Dict[str, Any]:
        """Record a lightweight 'monitoring' result without running the LLM loop
        (Tier-1 1b). Used when the alerted condition has already recovered."""
        duration = time.time() - start_time
        rec = f"No action needed — {note}. Skipped deep investigation (noise filter)."
        findings = {'response': rec, 'tool_calls': 0, 'recommendation': rec, 'preflight_skip': True}
        try:
            self.kb.update_investigation(
                investigation_id=inv_id, completed_at=datetime.now(), findings=findings,
                outcome='monitoring', duration_seconds=duration, tool_calls_count=0)
        except Exception as e:
            logger.debug(f"early-exit record skipped: {e}")
        INVESTIGATIONS.labels(outcome='monitoring').inc()
        logger.info(f"Investigation #{inv_id} early-exit (noise filter): monitoring — {note}")
        return self._build_action_result(
            success=True,
            message=self._action_message('monitoring', trigger, duration, 0),
            details={'investigation_id': inv_id, 'outcome': 'monitoring',
                     'duration_s': round(duration, 1), 'tool_calls': 0,
                     'preflight_skip': True, 'remediation': rec},
        )

    def _maybe_propose_remediation(self, outcome: str, alert_info: Dict[str, Any],
                                   trigger: str):
        """Phase-B: for a confirmed needs_action pod, build a dry-run remediation
        proposal (patch candidate or precise decline). Returns a Proposal or None.

        Off unless ``remediation.enabled`` is set in config. Never opens a PR in
        this build — ``open_prs`` is plumbed through but the live path is a
        deferred TODO in remediation.py.
        """
        if outcome != 'needs_action':
            return None
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not rcfg.get('enabled'):
            return None
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return None
        ident = self._identify_pod(alert_info, trigger)
        if not ident:
            return None
        namespace, pod_name = ident
        try:
            open_prs = bool(rcfg.get('open_prs'))
            proposer = RemediationProposer(
                k8s,
                repos=self.config.get('git', {}).get('repos', []),
                open_prs=open_prs,
                default_repo_name=rcfg.get('default_repo', 'homelab-infra'),
                github=self._github_write_client() if open_prs else None,
                max_open_prs=int(rcfg.get('max_open_prs', 3)),
            )
            workload = normalize_service_name(pod_name)
            proposal = proposer.propose_for(namespace, pod_name, workload=workload)
            if proposal is None:
                return None
            logger.info(
                f"Remediation proposal for {namespace}/{pod_name}: "
                f"{proposal.kind} ({proposal.fix_class or 'n/a'})"
            )
            # Live path: only patch proposals, only when open_prs is enabled.
            if proposal.is_patch and open_prs:
                proposal.pr_result = proposer.open_pr(proposal, namespace, workload)
                if proposal.pr_result:
                    logger.info(f"Remediation PR for {namespace}/{workload}: {proposal.pr_result}")
            return proposal
        except Exception as e:
            logger.debug(f"Remediation proposal skipped: {e}")
            return None

    _REMEDIATION_FLAGS = ('queue_feed', 'queue_drain', 'queue_reap', 'queue_verify')

    def _triage_model(self) -> Optional[str]:
        """Resolve the dedicated triage model: DB setting overrides config.

        Mirrors _remediation_flag's DB-over-config semantics so the console
        can change or disable the triage model live, without the deploy
        commit + manual rollout restart a config-only change costs (CFOP-58).

        An empty/absent DB value means "unset, use config" — the flags
        convention — so disabling despite a config value needs an explicit
        word: 'off' (also 'none'/'disabled') returns None regardless of
        config. A DB read failure falls back to config rather than breaking
        the triage hot path.
        """
        val = None
        try:
            val = self.kb.get_setting('triage_model', '')
        except Exception as e:
            logger.debug(f"Could not read triage_model from DB, using config: {e}")
        if val not in (None, '') and isinstance(val, str):
            val = val.strip()
            if val:
                if val.lower() in ('off', 'none', 'disabled'):
                    return None
                return val
        llm_cfg = self.config.get('llm', {}) if isinstance(self.config, dict) else {}
        cfg_val = str(llm_cfg.get('triage_model') or '').strip()
        return cfg_val or None

    def _remediation_flag(self, name: str) -> bool:
        """Resolve a remediation flag: DB setting overrides config.yaml.

        A DB setting (set via the operator console) wins so flags can be toggled
        live without a redeploy/restart; falls back to the config block.

        The profile is a hard ceiling over both. ``load_config`` already zeroed
        the config side, but the DB override is read live and would otherwise be
        a way to escalate past the profile from the console — the same
        privilege-escalation shape ``ROLE_SCOPE_CEILING`` exists to close in
        auth/models.py.

        The profile is read straight off ``self.config`` rather than through a
        helper method: several tests drive this with a ``MagicMock`` operator
        carrying a real config dict, and a helper would be auto-mocked into an
        object that is neither a profile nor ``None``.
        """
        profile = self.config.get('profile') if isinstance(self.config, dict) else None
        if not shared_config.profile_allows(profile, shared_config.SCOPE_REMEDIATE):
            return False
        try:
            val = self.kb.get_setting('remediation_' + name, '')
            if val not in (None, ''):
                return str(val).strip().lower() in ('1', 'true', 'yes', 'on')
        except Exception as e:
            logger.debug(f"Could not read remediation flag '{name}' from DB, using config: {e}")
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        return bool(rcfg.get(name))

    def _start_remediation_worker(self) -> None:
        """Run the remediation reaper/drainer/verify in a daemon thread.

        Off the main OODA loop so a long proactive sweep (minutes, single-thread)
        can't starve the drain tick — same pattern as the HTTP investigation
        worker. Each task self-gates on its flag + interval.
        """
        threading.Thread(target=self._remediation_worker_loop, daemon=True,
                         name="remediation-worker").start()
        logger.info("Remediation worker thread started")

    def _remediation_worker_loop(self) -> None:
        while True:
            try:
                now = time.time()
                if now - self.last_reap > self._get_reap_interval():
                    self._reap_remediations(); self.last_reap = now
                # Own tick, own flag. It used to hang off _reap_remediations,
                # which put the CFOP-71 recovery half behind queue_reap — a flag
                # documented as independently enableable and defaulting to
                # false. With reap off, a recovered node kept its stale
                # needs-human row forever, which is most of what the collapse
                # was supposed to fix. It belongs with the feed that CREATES
                # those rows instead.
                if now - self.last_node_recovery > self._get_reap_interval():
                    self._resolve_recovered_node_incidents(); self.last_node_recovery = now
                if now - self.last_drain > self._get_drain_interval():
                    self._drain_remediation_queue(); self.last_drain = now
                if now - self.last_verify > self._get_verify_interval():
                    self._reconcile_remediation_prs(); self.last_verify = now
                if now - self.last_cockpit_reap > self._get_cockpit_reap_interval():
                    self._reap_cockpits(); self.last_cockpit_reap = now
            except Exception:
                logger.exception("Remediation worker tick failed")
            time.sleep(10)

    def _get_cockpit_reap_interval(self) -> int:
        """Cockpit janitor interval: DB setting → config.yaml → default 900."""
        try:
            val = self.kb.get_setting('cockpit_reap_interval', '')
            if val:
                return max(60, min(86400, int(val)))
        except Exception as e:
            logger.debug(f"Invalid cockpit_reap_interval setting, using default: {e}")
        cockpit = self.config.get('cockpit', {}) if isinstance(self.config, dict) else {}
        return int(cockpit.get('janitor_interval_seconds', 900) or 900)

    def _start_cockpit_bridge(self):
        """Open the cockpit PTY bridge, if it is turned on (CFOP-75).

        Its own listener rather than a route on the console: Waitress cannot
        upgrade a connection, and the console is a thread inside *this*
        process, so pointing a different server at the Flask app would mean
        restructuring how the agent runs.

        Failure to start is logged and swallowed. The bridge is an
        affordance — an operator who cannot open a browser terminal still has
        `cfassist attach`, and an agent that refuses to boot because a
        convenience port is taken would be a worse trade.
        """
        try:
            from cockpit_bridge import CockpitBridge, build_bridge_config

            config = build_bridge_config(self.config)
            bridge = CockpitBridge(
                config,
                resolver=self.web_server.resolve_cockpit_session,
                token_verifier=self.web_server.verify_bridge_token,
                audit=self.web_server.record_bridge_event,
            )
            self.cockpit_bridge = bridge
            if bridge.start():
                logger.info("Cockpit PTY bridge listening on :%s", config.port)
        except Exception as e:
            logger.error(f"cockpit bridge failed to start: {e}", exc_info=True)

    def _reap_cockpits(self) -> int:
        """Sweep hosts for cockpit sessions that outlived their TTL (CFOP-36).

        Kubernetes reaps tier 1 for nothing — activeDeadlineSeconds plus
        ownership GC. A container or a /tmp directory on a Pi has no such
        machinery, and the sessions that leak are exactly the ones nobody is
        watching: the laptop that closed, the VPN that dropped. It runs on this
        thread rather than the OODA loop for the same reason the drainer does —
        a proactive sweep is minutes long and would starve it.

        Silently a no-op when nothing is configured: the sweep is over
        ``infrastructure.hosts``, which a cluster-only install does not have.
        """
        if not self.web_server:
            return 0
        try:
            reaped = self.web_server.reap_cockpits()
        except Exception as e:
            logger.warning(f"Cockpit janitor tick failed: {e}")
            return 0
        if reaped:
            logger.info(f"Cockpit janitor reaped {reaped} expired session(s)")
        return reaped

    def _reap_remediations(self) -> int:
        """Recover remediations whose executor lease expired (gated, safe).

        Off unless ``remediation.queue_reap`` is set. Harmless when the queue is
        empty, so it can be enabled independently of the drainer.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_reap'):
            return 0
        try:
            count = self.kb.requeue_stale_remediations()
            if count:
                REMEDIATION_REAPED.inc(count)
                logger.info(f"Reaped {count} stale remediation(s) back to the queue")
        except Exception as e:
            logger.error(f"Remediation reaper failed: {e}", exc_info=True)
            return 0
        return count

    def _resolve_recovered_node_incidents(self) -> int:
        """Close node-incident rows whose node came back Ready (CFOP-71).

        A node-incident row is a NOTIFICATION — "go physically check the power
        and network cable" is human work the executor cannot mechanize, so the
        row would sit on the worklist until someone clicks it. Recovery closing
        its own paperwork is what actually drives the needs-human count down;
        without it the collapse only trades twelve stale rows for one.

        Only rows carrying this feed's own ``node-down-<host>`` key are
        touched, so nothing a human or another feed created is auto-resolved.
        """
        if not self._remediation_flag('queue_feed'):
            return 0  # nothing creates node-incident rows, so nothing to close
        try:
            k8s = getattr(getattr(self, 'tools', None), 'k8s_tools', None)
            if not k8s:
                return 0
            result = k8s.get_nodes()
            if not result.get('success'):
                return 0
            ready = [str(n.get('name')).lower() for n in result.get('nodes', [])
                     if str(n.get('ready')) == 'True' and n.get('name')]
            closed = self.kb.resolve_node_incidents_for_ready_hosts(ready)
            if closed:
                REMEDIATION_OUTCOME.labels(outcome='resolved').inc(len(closed))
                logger.info(f"Auto-resolved {len(closed)} node incident(s) whose node "
                            f"returned to Ready: {closed}")
            return len(closed)
        except Exception as e:
            logger.warning(f"Node-incident recovery sweep failed: {e}")
            return 0

    def _drain_remediation_queue(self) -> int:
        """Claim auto-eligible remediations and spawn an executor Job per item.

        Off unless ``remediation.queue_drain`` is set. Bounded per tick so one
        cycle can't fan out the whole queue. A spawn failure fails the claim so
        the reaper/retry path recovers it rather than leaving it stuck claimed.
        Returns the number of executor Jobs spawned.

        When ``CFOP_EXEC_CHANGE_URL`` is set, node-action rows are gated on the
        changerecord microservice (open + named approval) before spawn — so an
        unapproved record never reaches ``run_ssh_plan``. Unset URL preserves
        prior console-escalation behavior byte-for-byte.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_drain'):
            return 0
        max_per_tick = max(1, int(rcfg.get('max_drain_per_tick', 3)))
        # CFOP-71: the open-PR cap applies to the executor path too. It was
        # enforced only inside the agent-side proposer (remediation.py), and the
        # executor is a separate portable service that consults nothing of the
        # kind — which is how four cfop/ PRs came to be open against a
        # configured max_open_prs of 3. The drainer is the seat: it already
        # reads config, and refusing here leaves the row queued rather than
        # burning an executor Job and a frontier-model diff to produce a PR
        # that should not exist yet.
        max_open_prs = int(rcfg.get('max_open_prs', 3))
        spawned = 0
        # Rows released mid-tick (awaiting approval / transient recorder errors)
        # go back to queued with the same priority — skip them for the rest of
        # this tick so they cannot starve later items via reclaim churn.
        skip_ids: set = set()
        for _ in range(max_per_tick):
            if max_open_prs >= 0 and self._open_remediation_pr_count() >= max_open_prs:
                # Nothing is lost: the rows stay queued and the next tick
                # re-checks, so a merged PR unblocks the queue with no operator
                # action. Blocking here is the point — three unreviewed PRs
                # already await a human, and a fourth helps nobody.
                logger.info(f"Remediation drain paused: at the open PR cap ({max_open_prs})")
                REMEDIATION_SPAWNED.labels(result='capped').inc()
                break
            job_name = f"cfop-executor-{uuid.uuid4().hex[:10]}"
            try:
                work = self.kb.claim_next_remediation(job_name, exclude_ids=skip_ids)
            except Exception as e:
                logger.error(f"Remediation claim failed: {e}", exc_info=True)
                break
            if not work:
                break  # queue drained
            try:
                gated = self._prepare_node_action_change_record(work)
                if gated is None:
                    # Waiting on approval (or hard gate failure already released/
                    # failed the claim). Do not spawn; continue draining others.
                    skip_ids.add(work['id'])
                    continue
                work = gated
                self._spawn_remediation_executor(job_name, work)
                spawned += 1
                REMEDIATION_SPAWNED.labels(result='ok').inc()
                logger.info(
                    f"Spawned executor {job_name} for remediation #{work['id']} "
                    f"({work.get('remediation_class')}, risk={work.get('risk')})"
                )
            except Exception as e:
                # Don't leave the row stuck 'claimed' — fail it so retry/reaper recovers.
                REMEDIATION_SPAWNED.labels(result='failed').inc()
                logger.error(f"Executor spawn failed for remediation #{work['id']}: {e}")
                self.kb.fail_remediation(work['id'], f"executor spawn failed: {e}")
                skip_ids.add(work['id'])
        return spawned

    def _open_remediation_pr_count(self) -> int:
        """Remediation PRs this pipeline currently has awaiting a human.

        Counted from the queue rather than the GitHub API: the drainer runs
        every tick, and these rows ARE the outstanding PRs.
        _reconcile_remediation_prs clears pr-open/verifying as they are merged
        or closed; the reaper recovers dead claims.

        'claimed' and 'executing' count too, because the executor spawn is
        ASYNC: a row does not reach 'pr-open' until its Job posts back to the
        completion endpoint, which can be many ticks later. Counting only
        already-open PRs let one tick claim max_drain_per_tick rows against a
        stale count and blow straight through the cap — 1 open + 3 spawned = 4
        against a cap of 3, which is the exact defect this cap exists to
        prevent, just triggered by a spawn burst instead of duplicate symptoms.

        Note this makes the cap depend on ``remediation.queue_verify``: with
        the reconciler off, nothing clears 'pr-open', so the count only grows
        and the drainer stops once it reaches the cap. That is not a bug to
        chase — the count is accurate (those PRs really are open and
        unreviewed) and a human Resolving the rows in the console releases it.
        But a "stalled queue" with queue_verify off is this, not a deadlock.

        Fails OPEN: a read failure returns 0 and the tick proceeds. Blocking on
        a transient DB error would stall the whole queue, and this cap is a
        volume guard, not a safety gate — the safety gate is CFOP-70's judge,
        which fails closed precisely because it is one.
        """
        try:
            # count_remediations_by_status is one grouped query. The old
            # list_remediations_by_status call pulled whole rows AND capped at
            # 50, so a cap above 50 would have silently stopped counting.
            counts = self.kb.count_remediations_by_status() or {}
            return sum(int(counts.get(st, 0))
                       for st in ('claimed', 'executing', 'pr-open', 'verifying'))
        except Exception as e:
            logger.warning(f"Could not count open remediation PRs, not capping this tick: {e}")
            return 0

    def _executor_config(self) -> Dict[str, Any]:
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        ec = rcfg.get('executor') if isinstance(rcfg.get('executor'), dict) else {}
        return ec

    def _change_record_url(self) -> str:
        """Base URL of the changerecord Service, or '' when unset (homelab default).

        Prefer process env (wired into the agent Deployment) over nested config
        so the gate is not Job-only.
        """
        env_url = (os.getenv('CFOP_EXEC_CHANGE_URL') or '').strip()
        if env_url:
            return env_url.rstrip('/')
        na = self._executor_config().get('node_action')
        na = na if isinstance(na, dict) else {}
        cr = na.get('change_record') if isinstance(na.get('change_record'), dict) else {}
        return str(cr.get('url') or '').strip().rstrip('/')

    def _complete_node_action_plan(self, prompt: str) -> str:
        """LLM completion for a node-action plan (same model floor as the Job)."""
        import requests as req
        ec = self._executor_config()
        llm = ec.get('llm') if isinstance(ec.get('llm'), dict) else {}
        na = ec.get('node_action') if isinstance(ec.get('node_action'), dict) else {}
        model = str(na.get('model') or llm.get('model') or _ANTHROPIC_DEFAULT_EXEC_MODEL)
        api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY required to plan node-action before open")
        payload = {
            'model': model,
            'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        resp = req.post(
            'https://api.anthropic.com/v1/messages',
            json=payload,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            },
            timeout=120,
        )
        resp.raise_for_status()
        return '\n'.join(
            b.get('text', '') for b in resp.json().get('content', [])
            if b.get('type') == 'text'
        )

    def _generate_node_action_plan(self, work: Dict[str, Any]) -> Dict[str, Any]:
        """Produce a validated {host, commands, explanation} plan for open()/spawn.

        Reuses any plan already persisted on the change_record / payload so a
        reclaim after release does not re-call the LLM.
        """
        result = work.get('result') if isinstance(work.get('result'), dict) else {}
        cr = result.get('change_record') if isinstance(result.get('change_record'), dict) else {}
        for candidate in (cr.get('plan'), work.get('approved_plan'),
                          (work.get('payload') or {}).get('plan') if isinstance(work.get('payload'), dict) else None):
            if isinstance(candidate, dict) and candidate.get('commands'):
                plan = _na_normalize_plan(candidate)
                ok, reason = _na_validate_plan(plan['commands'])
                if ok:
                    return plan
                raise RuntimeError(f"persisted plan failed safety gate: {reason}")
        reply = self._complete_node_action_plan(_na_build_command_prompt(work))
        parsed = _na_parse_command_plan(reply)
        if not parsed:
            raise RuntimeError("model produced no parseable command plan")
        plan = _na_normalize_plan(parsed)
        ok, reason = _na_validate_plan(plan['commands'])
        if not ok:
            raise RuntimeError(f"command plan failed safety gate: {reason}")
        return plan

    def _prepare_node_action_change_record(self, work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Gate node-actions on changerecord approval before executor spawn.

        Generates the concrete command plan *before* open() so the record PR
        a human merges is exactly what the executor will run. Returns the
        (possibly enriched) work order when spawn may proceed, or None when
        the claim was released/failed and spawn must be skipped.
        Non-node-action work and unset URL are pass-through (no behavior change).
        """
        if (work.get('remediation_class') or '') != 'node-action':
            return work
        base_url = self._change_record_url()
        if not base_url:
            return work

        result = work.get('result') if isinstance(work.get('result'), dict) else {}
        cr = dict(result.get('change_record')) if isinstance(result.get('change_record'), dict) else {}
        ref = str(cr.get('ref') or '').strip()
        payload = work.get('payload') if isinstance(work.get('payload'), dict) else {}
        target = payload.get('target') if isinstance(payload.get('target'), dict) else {}
        na = self._executor_config().get('node_action')
        na = na if isinstance(na, dict) else {}
        ec = self._executor_config()
        image = ec.get('image', os.getenv('CFOP_EXECUTOR_IMAGE',
                                         'ghcr.io/aachtenberg/cfoperator-executor:main'))
        flag_snapshot = {
            "node_action.enabled": bool(na.get('enabled')),
            "queue_drain": bool(self._remediation_flag('queue_drain')),
            "change_record.url": base_url,
        }

        try:
            plan = self._generate_node_action_plan(work)
            cr = {**cr, "plan": plan}
            host = (plan.get('host') or str(target.get('host') or na.get('host') or '')).strip()
            if not ref:
                opened = change_record_open(base_url, {
                    "remediation_id": work.get('id'),
                    "investigation_id": work.get('investigation_id'),
                    "host": host,
                    "commands": list(plan.get('commands') or []),
                    "justification": str(payload.get('recommendation') or ''),
                    "image": str(image),
                    "flag_snapshot": flag_snapshot,
                    "risk": str(work.get('risk') or ''),
                    "confidence": work.get('confidence'),
                })
                ref = str(opened['ref'])
                cr = {"ref": ref, "url": opened.get('url'), "plan": plan}
                logger.info(
                    "Opened change record for remediation #%s (ref=%s, %d cmd(s))",
                    work['id'], ref[:24], len(plan.get('commands') or []),
                )

            approval = change_record_approval(base_url, ref)
            if approval is None:
                # Persist ref+plan, release claim — next drain tick reclaims and re-polls.
                # Never spawn: unapproved records must not reach run_ssh_plan.
                self.kb.release_remediation_claim(
                    work['id'],
                    result={"change_record": cr},
                    last_error="awaiting change-record approval",
                )
                return None
        except ChangeRecordClientError as e:
            logger.error("Change-record gate failed for remediation #%s: %s", work['id'], e)
            # Only closed-without-merge (HTTP 409) burns an attempt; transport/5xx
            # release and retry next tick so a recorder blip cannot needs-human.
            if e.status == 409:
                self.kb.fail_remediation(work['id'], f"change record gate: {e}")
            else:
                self.kb.release_remediation_claim(
                    work['id'],
                    result={"change_record": cr} if cr else None,
                    last_error=f"change record transient: {e}",
                )
            return None
        except Exception as e:  # noqa: BLE001
            # Plan generation / validation failures are hard — burn an attempt.
            logger.error("Change-record plan failed for remediation #%s: %s", work['id'], e)
            self.kb.fail_remediation(work['id'], f"change record plan: {e}")
            return None

        # Approved — stamp ref + approval + approved_plan into the Job work order.
        enriched = dict(work)
        enriched['change_record_ref'] = ref
        enriched['change_record_url'] = cr.get('url')
        enriched['change_record_approval'] = approval
        enriched['approved_plan'] = plan
        try:
            self.kb.update_remediation_status(
                work['id'], 'claimed',
                result={"change_record": {**cr, "approval": approval, "plan": plan}},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("could not persist change-record approval: %s", e)
        return enriched

    def _build_executor_manifest(self, job_name: str, work_order: Dict[str, Any]) -> Dict[str, Any]:
        """Build the cfoperator-executor Job manifest for a claimed remediation.

        GitOps classes are read-only toward the cluster (their only mutation is a
        GitHub PR). A node-action additionally SSHes to a host to run a guarded
        command plan, so for that class only we mount the SSH key and flip the
        opt-in env. LLM backend is fully env-driven so the model is swappable per
        the portable-executor design.
        """
        ec = self._executor_config()
        namespace = ec.get('namespace', os.getenv('CFOP_EXECUTOR_NAMESPACE', 'apps'))
        image = ec.get('image', os.getenv('CFOP_EXECUTOR_IMAGE', 'ghcr.io/aachtenberg/cfoperator-executor:main'))
        sa = ec.get('service_account', 'cfoperator-executor')
        secrets_name = ec.get('secrets_name', 'cfoperator-secrets')
        pull_secret = ec.get('image_pull_secret', 'ghcr-pull-secret')
        completion_base = ec.get('completion_base_url', os.getenv(
            'CFOP_EXECUTOR_COMPLETION_BASE_URL',
            'http://cfoperator.apps.svc.cluster.local:8083/v1/remediations'))
        # Per-item repo (work-order payload) wins over the config default, so a
        # cfoperator-deploy fix targets that repo while cluster fixes go to homelab-infra.
        payload_repo = (work_order.get('payload') or {}).get('repo')
        git_repo = payload_repo or ec.get('git_repo', os.getenv('CFOP_GIT_REPO', 'aachtenberg/homelab-infra'))
        git_base = ec.get('git_base', 'main')
        llm = ec.get('llm') if isinstance(ec.get('llm'), dict) else {}
        ttl = int(ec.get('ttl_seconds_after_finished', 3600))
        deadline = int(ec.get('active_deadline_seconds', 900))

        completion_url = f"{completion_base.rstrip('/')}/{work_order['id']}/complete"
        env = [
            {"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "ANTHROPIC_API_KEY", "optional": True}}},
            {"name": "GITHUB_TOKEN", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "GITHUB_TOKEN"}}},
            {"name": "CFOP_COMPLETION_TOKEN", "valueFrom": {"secretKeyRef": {"name": secrets_name, "key": "CFOP_COMPLETION_SHARED_SECRET", "optional": True}}},
            {"name": "CFOP_COMPLETION_URL", "value": completion_url},
            {"name": "CFOP_REMEDIATION_JSON", "value": json.dumps(work_order, default=str)},
            {"name": "CFOP_GIT_REPO", "value": str(git_repo)},
            {"name": "CFOP_GIT_BASE", "value": str(git_base)},
            {"name": "CFOP_EXEC_LLM_BACKEND", "value": str(llm.get('backend', 'anthropic'))},
            {"name": "CFOP_EXEC_LLM_MODEL", "value": str(llm.get('model', ''))},
            {"name": "CFOP_EXEC_LLM_BASE_URL", "value": str(llm.get('base_url', ''))},
        ]
        # node-action only: opt in + mount the SSH key so the executor can run a
        # guarded command plan on a host. GitOps classes stay PR-only (no mount).
        volumes: List[Dict[str, Any]] = []
        volume_mounts: List[Dict[str, Any]] = []
        na = ec.get('node_action') if isinstance(ec.get('node_action'), dict) else {}
        if (work_order.get('remediation_class') or '') == 'node-action' and na.get('enabled'):
            # Reuse the forensics keypair the deep-investigation worker already
            # uses to SSH into hosts. Mount it at a staging dir (group-readable);
            # the executor copies it into ~/.ssh at 0600 (ssh refuses looser).
            ssh_secret = na.get('ssh_secret', 'cfop-forensics-ssh')
            env += [
                {"name": "CFOP_NODE_ACTION_ENABLED", "value": "true"},
                {"name": "CFOP_NODE_ACTION_HOST", "value": str(na.get('host', ''))},
                {"name": "CFOP_SSH_USER", "value": str(na.get('ssh_user', 'sre'))},
                {"name": "CFOP_SSH_SECRET_DIR", "value": "/ssh-secret"},
            ]
            # Change-record close URL for the executor (agent already gated on
            # approval before spawn). Unset → executor skips close entirely.
            cr = na.get('change_record') if isinstance(na.get('change_record'), dict) else {}
            change_url = (
                (os.getenv('CFOP_EXEC_CHANGE_URL') or '').strip()
                or str(cr.get('url') or '').strip()
            ).rstrip('/')
            if change_url:
                env.append({"name": "CFOP_EXEC_CHANGE_URL", "value": change_url})
                # Shared secret for /close (optional; matches changerecord Deployment).
                env.append({
                    "name": "CFOP_CHANGERECORD_SHARED_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": secrets_name,
                            "key": "CFOP_CHANGERECORD_SHARED_SECRET",
                            "optional": True,
                        }
                    },
                })
            # Node-action is the only path that runs shell on a host, so it pins
            # its own model floor: a cost downgrade of the generic executor model
            # must never silently drop the model deciding what to run on hosts.
            na_model = str(na.get('model', '') or _ANTHROPIC_DEFAULT_EXEC_MODEL)
            for e in env:
                if e.get("name") == "CFOP_EXEC_LLM_MODEL":
                    e["value"] = na_model
                    break
            volumes.append({"name": "ssh", "secret": {
                "secretName": ssh_secret, "defaultMode": 0o440}})
            volume_mounts.append({"name": "ssh", "mountPath": "/ssh-secret", "readOnly": True})
        labels = {
            "app.kubernetes.io/managed-by": "cfoperator",
            "cfop.dev/role": "remediation-executor",
        }
        container = {
            "name": "executor",
            "image": image,
            "imagePullPolicy": "Always",
            "env": env,
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        }
        if volume_mounts:
            container["volumeMounts"] = volume_mounts
        pod_spec = {
            "restartPolicy": "Never",
            "serviceAccountName": sa,
            "imagePullSecrets": [{"name": pull_secret}],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
            "containers": [container],
        }
        if volumes:
            pod_spec["volumes"] = volumes
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": namespace, "labels": dict(labels)},
            "spec": {
                "backoffLimit": 0,  # an LLM rerun is a drainer decision, not a retry policy
                "ttlSecondsAfterFinished": ttl,
                "activeDeadlineSeconds": deadline,
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": pod_spec,
                },
            },
        }

    def _kubectl_create(self, manifest: Dict[str, Any]) -> str:
        proc = subprocess.run(
            ["kubectl", "create", "-n", manifest["metadata"]["namespace"], "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"kubectl create failed: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    def _spawn_remediation_executor(self, job_name: str, work_order: Dict[str, Any]) -> None:
        """Spawn the cfoperator-executor Job for a claimed remediation."""
        manifest = self._build_executor_manifest(job_name, work_order)
        self._kubectl_create(manifest)

    @staticmethod
    def _parse_pr_url(url: str) -> Optional[tuple]:
        """Parse https://github.com/<owner>/<repo>/pull/<n> -> ('owner/repo', n)."""
        m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url or "")
        return (m.group(1), int(m.group(2))) if m else None

    def _reconcile_remediation_prs(self) -> int:
        """Advance 'pr-open' remediations by their PR state.

        Off unless ``remediation.queue_verify`` is set. Merged -> resolved (then
        re-investigate to confirm the signal cleared); closed-without-merge ->
        rejected. Returns the number of rows advanced.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_verify'):
            return 0
        try:
            rows = self.kb.list_remediations_by_status('pr-open')
        except Exception as e:
            logger.error(f"Remediation PR reconcile list failed: {e}", exc_info=True)
            return 0
        gh = self._github_write_client() if rows else None
        if gh is None:
            return 0
        advanced = 0
        for row in rows:
            ref = self._parse_pr_url(row.get('pr_url') or '')
            if not ref:
                continue
            repo, number = ref
            resp = gh.request("GET", f"/repos/{repo}/pulls/{number}")
            if not resp.get('success'):
                continue
            data = resp.get('data') or {}
            if data.get('merged'):
                self.kb.update_remediation_status(row['id'], 'resolved', result={'pr_merged': True})
                REMEDIATION_OUTCOME.labels(outcome='resolved').inc()
                self._verify_remediation(row)
                advanced += 1
            elif data.get('state') == 'closed':
                self.kb.update_remediation_status(row['id'], 'rejected',
                                                  last_error='PR closed without merge')
                REMEDIATION_OUTCOME.labels(outcome='rejected').inc()
                advanced += 1
        if advanced:
            logger.info(f"Reconciled {advanced} remediation PR(s)")
        return advanced

    _REMEDIATION_STATUSES = ('queued', 'claimed', 'executing', 'pr-open', 'verifying',
                             'resolved', 'failed', 'needs-human', 'rejected')

    def _update_remediation_metrics(self) -> None:
        """Refresh the cfoperator_remediation_queue gauge (throttled to ~30s).

        Reports every status (0 when empty) so the Grafana panel has stable
        series. Independent of the feed/drain flags.
        """
        if time.time() - self.last_metrics < 30:
            return
        self.last_metrics = time.time()
        try:
            counts = self.kb.count_remediations_by_status()
        except Exception as e:
            logger.debug(f"remediation metrics refresh skipped: {e}")
            return
        for status in self._REMEDIATION_STATUSES:
            REMEDIATION_QUEUE.labels(status=status).set(counts.get(status, 0))

    def _verify_remediation(self, row: Dict[str, Any]) -> None:
        """Best-effort post-merge verification: re-investigate the original signal.

        Enqueues a fresh investigation so the KB (and a human) sees whether the
        merge actually cleared the condition. Non-fatal.
        """
        try:
            rec = str((row.get('payload') or {}).get('recommendation') or 'remediation')
            self.enqueue_investigation({
                'summary': f"verify remediation #{row['id']}: {rec[:120]}",
                'source': 'remediation-verify',
            })
        except Exception as e:
            logger.debug(f"Remediation verify enqueue skipped: {e}")

    def _github_write_client(self):
        """Build a GitHub API client for opening remediation PRs, or None.

        Reuses event_runtime's self-contained GitHubApiClient. Token from
        GITHUB_TOKEN (same as the git context provider). Returns None when no
        token is set so the proposer falls back to dry-run.
        """
        token = os.getenv('GITHUB_TOKEN', '').strip()
        if not token:
            logger.warning("remediation.open_prs is on but GITHUB_TOKEN is unset; staying dry-run")
            return None
        try:
            from event_runtime.github_client import GitHubApiClient
            return GitHubApiClient(token=token)
        except Exception as e:
            logger.warning(f"Could not init GitHub client for remediation: {e}")
            return None

    def store_deep_investigation(self, alert: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest a deep-investigation worker's report into the knowledge base.

        Mirrors the storage tail of ``_act`` (start/update investigation +
        embedding) so the report surfaces in future triage's
        similar-investigation lookups. If the report carries a proposed diff
        and ``remediation.deep_open_prs`` is enabled, route it through the
        existing PR gates — never a parallel path.
        """
        details = result.get('details') if isinstance(result.get('details'), dict) else {}
        summary = str(alert.get('summary') or '(no summary)')
        trigger = f"[deep] {summary}"
        # The worker reports outcome "escalated" (the engine ledger's exact
        # match string); the KB convention from STATUS parsing is "escalate".
        outcome = str(details.get('outcome') or 'needs_action')
        kb_outcome = 'escalate' if outcome == 'escalated' else outcome

        inv_id = self.kb.start_investigation(trigger=trigger)
        provider = f"anthropic/{details.get('model') or 'unknown'}"
        findings = {
            'response': str(details.get('report') or '')[:5000],
            'recommendation': str(details.get('recommendation') or ''),
            'provider': provider,
            'deep': True,
            'host': details.get('host'),
        }
        self.kb.update_investigation(
            investigation_id=inv_id,
            completed_at=datetime.now(),
            findings=findings,
            outcome=kb_outcome,
            duration_seconds=float(details.get('duration_s') or 0.0),
        )
        self._embed_investigation(inv_id, trigger, findings, kb_outcome)
        logger.info(f"Deep investigation #{inv_id} stored: {kb_outcome} (host={details.get('host')})")

        # Feed the remediation queue from the structured hints the worker
        # emitted (remediation_class/risk/confidence). The queue's auto-gate
        # decides drainable vs needs-human; the drainer/executor take it from
        # there. Distinct from the legacy inline deep_open_prs path below.
        # Stamp the reporting LLM onto the queue payload (same string as findings).
        queue_details = dict(details)
        queue_details['provider'] = provider
        rid = self._maybe_queue_remediation(inv_id, queue_details)

        pr_result = None
        diff_text = str(details.get('proposed_diff') or '')
        if diff_text:
            pr_result = self._maybe_open_pr_from_deep_diff(alert, details, diff_text)
            if pr_result:
                logger.info(f"Deep-investigation PR for {details.get('host')}: {pr_result}")
                # Persist the attempt on the remediation row so declines survive
                # log rotation and show in the console (CFOP-22 D).
                if rid:
                    attempt = {k: pr_result[k] for k in ('status', 'detail', 'path', 'branch')
                               if k in pr_result and pr_result[k] is not None}
                    pr_url = pr_result.get('html_url') if pr_result.get('status') == 'opened' else None
                    try:
                        self.kb.merge_remediation_payload(rid, {'pr_attempt': attempt}, pr_url=pr_url)
                    except Exception as e:
                        logger.warning(f"Could not stamp pr_attempt on remediation #{rid}: {e}")

        out: Dict[str, Any] = {'investigation_id': inv_id, 'outcome': kb_outcome}
        if pr_result:
            out['pr_result'] = pr_result
        if rid:
            out['remediation_id'] = rid
        return out

    def _count_enqueued(self, source: str, rclass: str, risk: str, confidence) -> None:
        """Bump the enqueue counter, labelled by source/class and auto-eligibility."""
        nc, nr = normalize_remediation_fields(rclass, risk)
        elig = remediation_is_auto_eligible(nc, nr, confidence)
        REMEDIATION_ENQUEUED.labels(source=source, remediation_class=nc, eligible=str(elig).lower()).inc()

    def _maybe_queue_remediation(self, investigation_id: Optional[int],
                                 details: Dict[str, Any]) -> Optional[int]:
        """Enqueue a remediation from structured classification hints.

        ``investigation_id`` is None for rows with no source investigation —
        the classified sweep-feed path (CFOP-53) enqueues through here too.

        Off unless ``remediation.queue_feed`` is set. No-op when the investigator
        didn't classify the recommendation (no ``remediation_class``).

        ``details.dedupe_key`` (optional) suppresses duplicates while an earlier
        row for the same underlying problem is still open. It must land in both
        the ``queue_remediation`` kwarg and the payload — the KB filters on
        ``payload['dedupe_key']`` and does not inject it itself.

        This is the single choke point both feeds converge on, so it is also
        where the two gates live:

          - CFOP-71 collapse: while a node is NotReady, symptoms attributable
            to it fold onto one node-incident row.
          - CFOP-78 fold: a re-found problem lands on the open row already
            covering it (hard-identifier containment). Fails open.
          - CFOP-78 fork cap: a recommendation still offering alternatives
            cannot carry a confidence, so it can never auto-execute.
          - CFOP-70 judge: a row that would auto-execute is reviewed by a
            frontier model before it can. Fails closed.

        Both are deliberately here rather than at the call sites — a gate a
        future third feed can forget to call is not a gate.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return None
        rclass = details.get('remediation_class')
        if not rclass:
            return None
        risk = str(details.get('risk') or 'high')
        # The fork CAP lives here, at the choke point, with the other gates —
        # a gate a future third feed can forget to call is not a gate. The
        # investigation feed runs the commit-REWRITE upstream (where the
        # classifier and the dedupe key still benefit from the committed
        # text); any feed that skipped it, or a rewrite that failed, lands
        # here still fork-shaped and is barred from the auto gate. No LLM
        # call at this layer: the choke point stays cheap and deterministic.
        # ABOVE the confidence local on purpose — the first version of this
        # cap sat below it, mutated details after the read, and capped
        # nothing; the choke-point test caught it.
        if details.get('confidence') is not None and \
                _FORK_SHAPED.search(str(details.get('recommendation') or '')):
            logger.info("Fork-shaped recommendation at enqueue; confidence "
                        "cleared so it cannot auto-execute")
            REMEDIATION_FOLDED.labels(reason='fork_stuck').inc()
            details['confidence'] = None
        confidence = details.get('confidence')
        # CFOP-116: the model opened the PR itself. Nothing is left to
        # auto-execute, and the mutation judge asks whether a change may be
        # made UNATTENDED — a human merge is not that. Confidence None is the
        # one value that can never clear the gate, so the judge does not run
        # and the row enqueues as pr-open (queue_remediation) for the
        # reconciler to own.
        pr_url = str(details.get('pr_url') or '').strip() or None
        if pr_url and confidence is not None:
            confidence = None
        dedupe_key = str(details.get('dedupe_key') or '') or None
        # CFOP-71: while a node is NotReady, every symptom attributable to it
        # folds onto one node-incident row rather than opening its own. The
        # rewrite happens here, above the key, because the symptoms each carry
        # a legitimately DIFFERENT key — collapsing inside the key function
        # cannot see them as one incident.
        node_key = self._collapse_key_for_node_incident(details)
        if node_key and node_key != dedupe_key:
            logger.info(f"Folding {rclass} for {details.get('host')} onto node incident "
                        f"{node_key} (node is NotReady)")
            absorbed = self._record_absorbed_symptom(node_key, details)
            if absorbed:
                # An incident row already exists; this symptom is now recorded
                # on it. Return its id so the caller links to the incident
                # instead of reporting nothing proposed.
                if pr_url:
                    # CFOP-116: the PR must not vanish with the fold. The
                    # incident row is the one an operator acts on, so it
                    # carries the link and leaves the executor's reach; a row
                    # with a driver keeps its own, and the PR is then
                    # genuinely untracked — say so.
                    try:
                        stamped = self._stamp_opened_pr(self.kb.get_remediation(absorbed), pr_url)
                    except Exception as e:
                        stamped = False
                        logger.warning(f"Could not stamp PR {pr_url} on node incident "
                                       f"#{absorbed}: {e}")
                    if not stamped:
                        logger.warning(f"PR {pr_url} was opened for a symptom folded onto node "
                                       f"incident #{absorbed}; no row tracks it")
                return absorbed
            dedupe_key = node_key
            # Mutated IN PLACE, deliberately: _queue_needs_action_remediation
            # falls back to _open_remediation_for_key(details['dedupe_key'])
            # when this returns None, and with a local copy it would look up
            # the pre-collapse per-alert key and report "none proposed" while
            # the incident row sits there under the node key. Every caller
            # builds details fresh, so there is no aliasing to worry about.
            details['dedupe_key'] = node_key
        # CFOP-78: the same problem re-found under different wording folds onto
        # the open row instead of standing beside it. Skipped ENTIRELY while
        # the node collapse claims the row: with no incident row yet, the
        # FIRST NotReady symptom would otherwise be matched by identifier
        # against every other open row — a rec naming 192.168.0.131 while a
        # tunnel row is open would be absorbed there, and the node-incident
        # row would never be created. The node tier owns the row outright;
        # identifier matching is only for rows no tier above wants.
        absorbed_repeat = None if node_key else self._absorb_repeat_remediation(details)
        if absorbed_repeat:
            if pr_url:
                try:
                    self._stamp_opened_pr(self.kb.get_remediation(absorbed_repeat), pr_url)
                except Exception as e:
                    logger.warning(f"Could not stamp PR {pr_url} on remediation "
                                   f"#{absorbed_repeat}: {e}")
            return absorbed_repeat
        payload = {
            'recommendation': str(details.get('recommendation') or ''),
            'rendered_context': str(details.get('report') or '')[:5000],
            'proposed_diff': str(details.get('proposed_diff') or ''),
            'target': {'host': details.get('host')},
        }
        # Reporting LLM (provider/model) — same format as investigation findings.
        provider = details.get('provider')
        if provider:
            payload['provider'] = str(provider)
        repo = str(details.get('repo') or '').strip()
        if repo:
            payload['repo'] = repo
        source = str(details.get('source') or 'deep-investigation')
        if details.get('source'):
            payload['source'] = source
        if dedupe_key:
            payload['dedupe_key'] = dedupe_key
        # CFOP-80: structured FIX rides beside target.host, never replaces it.
        for key in ('targets', 'observed', 'steps', 'verify', 'rejected'):
            if details.get(key) is not None:
                payload[key] = details[key]
        if details.get('opened_prs'):
            # More than one PR opened in one run: pr_url carries the last,
            # the rest stay visible in the drawer's payload.
            payload['opened_prs'] = list(details['opened_prs'])
        # Which models actually made the call, recorded on the row. Before
        # CFOP-70 the only LLM identity on a remediation was the
        # *investigation's* provider, so reconstructing which model decided to
        # open a PR took a code read of the provider chain. 'provider' keeps
        # its existing meaning (the reporting LLM) so the console row does not
        # silently change referent.
        decided_by: Dict[str, Any] = {}
        if details.get('classifier_model'):
            decided_by['classifier'] = {
                'backend': str(details.get('classifier_backend') or ''),
                'model': str(details.get('classifier_model') or ''),
            }

        # CFOP-70: the frontier-model gate. Fires only on a row that would
        # otherwise auto-execute — remediation_is_auto_eligible IS the risk
        # surface, so it is the whole condition. Testing membership of
        # _SUMMARY_MUTATION_CLASSES as well would add nothing (the auto classes
        # are a subset) while giving the two tuples a way to drift apart.
        nclass, nrisk = normalize_remediation_fields(str(rclass), risk)
        verdict = None
        if remediation_is_auto_eligible(nclass, nrisk, confidence):
            try:
                verdict = self._judge_mutation_remediation(details, nclass, nrisk, confidence)
            except Exception as e:
                # _judge_mutation_remediation catches its own transport errors,
                # so reaching here means a bug in the gate itself. Park the row:
                # a broken gate must not become an open gate, and the caller in
                # _queue_needs_action_remediation does not wrap this call, so
                # letting it escape would abort the enqueue and lose the row.
                logger.error(f"Mutation judge raised, parking for human review: {e}",
                             exc_info=True)
                REMEDIATION_JUDGE.labels(verdict='unavailable').inc()
                verdict = {'verdict': 'downgrade', 'backend': None, 'model': None,
                           'reason': f"judge error ({e}); parked rather than executed unattended"}
            if not isinstance(verdict, dict):
                # The gate must not trust its own return shape either. A
                # non-dict here (a stray early return, a refactor, a test
                # monkeypatch) would AttributeError out of this method and,
                # since the needs_action caller does not wrap it, lose the row
                # — the same escape the raise path above already had to fix.
                logger.error(f"Mutation judge returned {type(verdict).__name__}, "
                             "not a verdict; parking for human review")
                REMEDIATION_JUDGE.labels(verdict='unparseable').inc()
                verdict = {'verdict': 'downgrade', 'backend': None, 'model': None,
                           'reason': "judge returned no verdict; parked rather than "
                                     "executed unattended"}
            decided_by['judge'] = {'backend': verdict.get('backend'),
                                   'model': verdict.get('model'),
                                   'verdict': verdict.get('verdict'),
                                   'reason': verdict.get('reason')}
            if verdict.get('verdict') != 'confirm':
                # Null the confidence rather than inflate the risk: it is the
                # one field that can never clear the gate (the eligibility test
                # requires confidence is not None), and it leaves the
                # classifier's honest risk assessment visible on the row
                # instead of overwriting it with a fiction.
                logger.warning(
                    f"Mutation judge {verdict.get('verdict')}: {nclass}/{nrisk} "
                    f"parked for human review — {verdict.get('reason')}")
                confidence = None
                payload['judge_reason'] = str(verdict.get('reason') or '')
        if decided_by:
            payload['decided_by'] = decided_by
        try:
            rid = self.kb.queue_remediation(
                remediation_class=str(rclass),
                payload=payload,
                investigation_id=investigation_id,
                # String(64) column — a long k8s resource name would reject the
                # INSERT, dropping the row after classification succeeded.
                host_id=str(details.get('host') or 'default')[:64],
                risk=risk,
                confidence=confidence,
                dedupe_key=dedupe_key,
                pr_url=pr_url,
            )
            if rid:
                self._count_enqueued(source, str(rclass), risk, confidence)
                if verdict is not None and verdict.get('verdict') == 'reject':
                    # Recorded, then closed. Dropping it silently would hide the
                    # fact that something proposed a wrong change; 'rejected' is
                    # terminal, so the dedupe key stops matching and a genuine
                    # recurrence is judged afresh rather than suppressed forever
                    # by one rejection.
                    try:
                        self.kb.update_remediation_status(
                            rid, 'rejected',
                            last_error=f"mutation judge rejected: {verdict.get('reason')}")
                    except Exception as e:
                        logger.error(f"Could not close judge-rejected remediation #{rid}: {e}")
            return rid
        except Exception as e:
            logger.error(f"Failed to queue remediation from investigation #{investigation_id}: {e}",
                         exc_info=True)
            return None

    # The judge answers a DIFFERENT question than the classifier. The classifier
    # asks "what shape is this fix?"; the judge asks "should this change be made,
    # unattended, right now?" — which is the question nothing in the pipeline was
    # asking when it opened three PRs to un-pin immich-kiosk from the very node
    # that drives the TV it displays on (CFOP-70).
    _JUDGE_SYSTEM_PROMPT = (
        "You are the last gate before an infrastructure change is made "
        "WITHOUT human review. A smaller model has already classified a "
        "recommendation as safe to execute unattended; it will open a pull "
        "request that a GitOps controller syncs to a live cluster.\n\n"
        "You are NOT asked to improve the fix, and NOT asked to re-classify "
        "it. You are asked one question: should this change be made, "
        "unattended, right now?\n\n"
        "Reasons to refuse are not limited to danger. Refuse also when:\n"
        "- the configuration being changed looks DELIBERATE (a pin, a "
        "nodeSelector, an affinity, a replica count of 0, a resource limit) "
        "and nothing in the evidence explains why it is there. Removing a "
        "constraint someone chose is not a fix; it is a guess.\n"
        "- the recommendation treats a SYMPTOM of a larger failure. If a node "
        "is down, its pods being unschedulable is the node's problem, not the "
        "workloads' — moving them is a decision a human makes.\n"
        "- the evidence does not actually support the recommendation, or the "
        "recommendation names something the evidence never mentions.\n"
        "- the change is irreversible, or its blast radius is wider than the "
        "problem it solves.\n\n"
        "Respond ONLY with a SINGLE JSON object, no other text:\n"
        '{"verdict": "confirm|downgrade|reject", "reason": "one sentence"}\n\n'
        "- confirm: the change is correct, proportionate, and safe to make "
        "unattended.\n"
        "- downgrade: it may well be right, but a human should look first. "
        "The row is parked for review; nothing is lost.\n"
        "- reject: the recommendation is WRONG, not merely risky — doing it "
        "would make things worse.\n\n"
        "When you are unsure, downgrade. A parked row costs a human two "
        "minutes; a wrong unattended change costs an outage."
    )

    def _judge_mutation_remediation(self, details: Dict[str, Any], rclass: str,
                                    risk: str, confidence) -> Dict[str, Any]:
        """Frontier-model verdict on a remediation that would auto-execute (CFOP-70).

        Each backend is pinned to its own model in ``_JUDGE_MODEL_FLOOR``, so a
        cost downgrade of ``remediation.executor.llm.model`` cannot quietly
        demote the model holding the veto: config picks which vendors are
        eligible, never which model they run. That is the whole point of the
        gate — the classifier deciding to open a PR ran on the cheap local
        primary, and its self-reported confidence of 1.0 on three wrong calls
        is exactly why a higher confidence bar would have bought nothing.

        Fails CLOSED. Unavailable, unparseable after one nudge, or raising —
        all return ``downgrade``, which parks the row for a human. This
        deliberately inverts the CFOP-48 escalate-before-parking instinct:
        there, the cost of parking was an operator's attention; here, the cost
        of *not* parking is an unreviewed mutation of a live cluster.

        The one escalation rung that DOES exist is peer failover, and only on
        unreachability. CFOP-70 refused a cross-provider rung because the rung
        it had in mind reached the cheap local primary whose judgement is the
        thing under review; reaching another vendor's frontier model keeps the
        tier and only changes who serves it, and it is what stops one missing
        API key from parking every remediation. A model that WAS reached and
        answered badly does not advance to the next peer — see the loop.
        """
        labels = ((details.get('alert_labels') or {})
                  if isinstance(details.get('alert_labels'), dict) else {})
        # The prompt tells the judge to refuse "the node is down, so its pods
        # being unschedulable is the node's problem" — but it was never given
        # the Ready state to apply that with, so it had to infer a dead node
        # from a report that might only say "immich-kiosk has 0/1 ready". The
        # collapse already pays for this call, and the DELIBERATE-constraint
        # rule would not have caught a rec that looks like ordinary
        # rescheduling.
        try:
            down = sorted(self._notready_nodes())
        except Exception:
            down = []
        node_line = (f"Nodes NOT Ready right now: {', '.join(down)}\n" if down
                     else "All nodes are Ready right now.\n")
        user_msg = (
            f"Alert / trigger: {str(details.get('trigger') or '')[:300]}\n"
            f"Labels: {json.dumps(labels, default=str)[:300]}\n"
            f"Affected host: {str(details.get('host') or 'unknown')}\n"
            f"{node_line}"
            f"Target repo: {str(details.get('repo') or 'unknown')}\n"
            f"Investigation findings:\n{str(details.get('report') or '')[:2000]}\n\n"
            f"Proposed remediation: {str(details.get('recommendation') or '')[:800]}\n"
            f"Classified by a smaller model as: {rclass} / risk={risk} / "
            f"confidence={confidence}\n\n"
            "Should this be done unattended?"
        )
        providers = self._judge_providers()
        if not providers:
            logger.warning("No mutation judge provider is configured or keyed, "
                           "parking for human review")
            REMEDIATION_JUDGE.labels(verdict='unavailable').inc()
            return {'verdict': 'downgrade', 'backend': None, 'model': None,
                    'reason': ("no frontier judge available (no API key for any of "
                               + ', '.join(_JUDGE_DEFAULT_ORDER) +
                               "); parked rather than executed unattended")}

        # The vendor that WROTE this recommendation must not also rule on it.
        # CFOP-70 rejected letting the implementer hold the veto, and CFOP-121
        # made that reachable in practice: deepseek-v4-pro is both a judge rung
        # and the backend the console selects for investigations, so a
        # DeepSeek-reported row would otherwise be judged by itself. The
        # reporter is payload.provider — see _judge_is_self_review for why the
        # match is on the vendor rather than the exact id.
        reporter = str(details.get('provider') or '').strip()
        last_error = None
        self_reviewed: List[str] = []
        for backend in providers:
            model = self._judge_model(backend)
            if _judge_is_self_review(reporter, backend, model):
                logger.info(f"Mutation judge {backend}/{model} is the vendor that "
                            f"wrote this recommendation ({reporter}), skipping to "
                            f"the next peer")
                REMEDIATION_JUDGE.labels(verdict='self-review-skipped').inc()
                self_reviewed.append(f"{backend}/{model}")
                continue
            try:
                reply = self._complete_judge(self._JUDGE_SYSTEM_PROMPT, user_msg,
                                             backend, model)
            except Exception as e:
                # AVAILABILITY failure — this vendor could not be reached at
                # all, so nothing was judged. Trying the next peer is failover,
                # not answer-shopping.
                logger.warning(f"Mutation judge {backend}/{model} unavailable: {e}")
                last_error = e
                continue

            parsed = self._parse_judge_verdict(reply)
            if parsed is None:
                # One nudge, same ladder shape as the classifier (PR #76).
                try:
                    reply2 = self._complete_judge(
                        self._JUDGE_SYSTEM_PROMPT,
                        user_msg + "\n\nYour previous reply was not the required "
                        "format:\n" + str(reply)[:1000] + "\n\nReply again with ONLY "
                        'the single JSON object {"verdict": ..., "reason": ...}.',
                        backend, model)
                    parsed = self._parse_judge_verdict(reply2)
                except Exception as e:
                    logger.warning(f"Mutation judge {backend} nudge failed: {e}")

            if parsed is None:
                # SUBSTANTIVE failure — this model was reached and answered, it
                # just answered badly. Deliberately NOT retried on the next
                # provider: cycling vendors until one returns a parseable
                # verdict is shopping for a permissive answer, and the only
                # answer that unblocks the row is 'confirm'. Park instead.
                logger.warning(f"Mutation judge {backend}/{model} output unparseable, "
                               f"parking for human review: {str(reply)[:200]}")
                REMEDIATION_JUDGE.labels(verdict='unparseable').inc()
                return {'verdict': 'downgrade', 'backend': backend, 'model': model,
                        'reason': "judge verdict unparseable; parked rather than "
                                  "executed unattended"}

            parsed['backend'] = backend
            parsed['model'] = model
            REMEDIATION_JUDGE.labels(verdict=parsed['verdict']).inc()
            return parsed

        # Every configured peer was unreachable, or was the reporter itself.
        REMEDIATION_JUDGE.labels(verdict='unavailable').inc()
        if self_reviewed and last_error is None:
            logger.warning("Every eligible mutation judge wrote the recommendation "
                           "under review, parking for human review")
            return {'verdict': 'downgrade', 'backend': None, 'model': None,
                    'reason': ("the only available judge (" + ', '.join(self_reviewed) +
                               ") is the model that wrote this recommendation; parked "
                               "rather than letting it review its own work")}
        logger.warning("Every mutation judge provider was unavailable, parking for human review")
        return {'verdict': 'downgrade', 'backend': None, 'model': None,
                'reason': f"judge unavailable ({last_error}); parked rather than "
                          "executed unattended"}

    def _judge_providers(self) -> List[str]:
        """Judge backends to try, in order, that actually have a key present.

        Order comes from ``remediation.judge.providers`` (default
        _JUDGE_DEFAULT_ORDER). Names outside _JUDGE_MODEL_FLOOR are dropped
        with a warning rather than accepted — a typo must not silently produce
        a judge-less gate, and it must not be treated as a new frontier tier.

        Providers with no API key are skipped here rather than being allowed to
        fail in the ladder, so a missing key costs nothing and the log says
        which vendors were actually eligible.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        jcfg = rcfg.get('judge') if isinstance(rcfg.get('judge'), dict) else {}
        configured = jcfg.get('providers')
        if isinstance(configured, str):
            configured = [configured]
        if not isinstance(configured, (list, tuple)) or not configured:
            configured = list(_JUDGE_DEFAULT_ORDER)

        out: List[str] = []
        for name in configured:
            backend = str(name or '').strip().lower()
            if backend not in _JUDGE_MODEL_FLOOR:
                logger.warning(f"Ignoring unknown mutation judge provider '{name}' "
                               f"(known: {', '.join(sorted(_JUDGE_MODEL_FLOOR))})")
                continue
            if backend in out:
                continue
            if not self._judge_api_key(backend):
                logger.info(f"Mutation judge provider '{backend}' has no API key, skipping")
                continue
            out.append(backend)
        return out

    @staticmethod
    def _judge_api_key(backend: str) -> str:
        """API key for a judge backend, or '' when unset."""
        if backend == 'anthropic':
            return os.getenv('ANTHROPIC_API_KEY', '').strip()
        cfg = OPENAI_COMPAT_PROVIDERS.get(backend) or {}
        return os.getenv(cfg.get('key_env', ''), '').strip() if cfg else ''

    def _judge_model(self, backend: str) -> str:
        """Model this judge backend runs: DB setting, else config, else floor.

        CFOP-70 pinned the judge outright — "config picks which vendors are
        eligible, never which model they run" — and CFOP-121 relaxes that
        deliberately, because two of the three rungs were pinned to ids the
        vendor had stopped serving and no operator could fix either from
        config.

        What the knob still cannot do is point the veto at a vendor's cheap
        tier: a configured model carrying a known fast-tier marker is refused
        here and the floor is used instead. That is a denylist, not a proof of
        tier — a mid-generation id with no marker is accepted, and beyond the
        obvious demotion the operator is trusted (see _JUDGE_FAST_TIER_TOKENS
        for why an allowlist would be the worse guard). The refusal is logged
        rather than silent: a setting that appears to save and does nothing is
        worse than one that is rejected out loud.

        Precedence mirrors _triage_model (DB over config) so the console can
        repoint a judge live, and so CFOP-122's per-scope page drives this key
        rather than growing a second mechanism.
        """
        floor = _JUDGE_MODEL_FLOOR[backend]
        for source, value in (('console', self._judge_model_setting(backend)),
                              ('config', self._judge_model_config(backend))):
            if not value:
                continue
            if _is_fast_tier_model(value):
                logger.warning(
                    f"Ignoring {source} judge model '{value}' for {backend}: a "
                    f"fast-tier model cannot hold the mutation veto, using {floor}")
                continue
            return value
        return floor

    def _judge_model_setting(self, backend: str) -> str:
        """``judge_model_<backend>`` from the knowledge base, or ''."""
        try:
            return str(self.kb.get_setting(f'judge_model_{backend}', '') or '').strip()
        except Exception as e:
            logger.debug(f"Could not read judge_model_{backend} from DB: {e}")
            return ''

    def _judge_model_config(self, backend: str) -> str:
        """``remediation.judge.models.<backend>`` from config.yaml, or ''."""
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        jcfg = rcfg.get('judge') if isinstance(rcfg.get('judge'), dict) else {}
        models = jcfg.get('models') if isinstance(jcfg.get('models'), dict) else {}
        return str(models.get(backend) or '').strip()

    def _complete_judge(self, system_prompt: str, user_msg: str,
                        backend: str, model: str) -> str:
        """One completion from a frontier judge backend. Raises if unreachable.

        Separate from _complete_node_action_plan because that one resolves its
        model from config (with the floor as a default) while this one is
        floor-pinned outright — there is no config key that can lower it.

        Deliberately a plain single-shot completion rather than
        _chat_with_tools: the judge must see exactly the evidence the pipeline
        already gathered and rule on it. Giving it tools would let it go and
        find different evidence, which makes the verdict unreproducible and the
        gate slow.
        """
        import requests as req
        api_key = self._judge_api_key(backend)
        if not api_key:
            raise RuntimeError(f"no API key for judge backend '{backend}'")

        if backend == 'anthropic':
            resp = req.post(
                'https://api.anthropic.com/v1/messages',
                json={
                    'model': model,
                    'max_tokens': _JUDGE_MAX_TOKENS,
                    # No sampling parameters. Opus 4.7 and later reject
                    # temperature/top_p/top_k with a 400, and that 400 is what
                    # parked every auto-eligible row until CFOP-117. The
                    # OpenAI-compat branch below still pins temperature 0;
                    # here the prompt's "ONLY the JSON object" does the
                    # steadying.
                    'system': system_prompt,
                    'messages': [{'role': 'user', 'content': user_msg}],
                },
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                },
                timeout=120,
            )
            _raise_for_status_with_body(resp)
            return '\n'.join(
                b.get('text', '') for b in resp.json().get('content', [])
                if b.get('type') == 'text'
            )

        # xAI and Gemini both speak the OpenAI chat/completions shape, so one
        # path serves them — the same reason OPENAI_COMPAT_PROVIDERS exists.
        cfg = OPENAI_COMPAT_PROVIDERS.get(backend)
        if not cfg:
            raise RuntimeError(f"unknown judge backend '{backend}'")
        resp = req.post(
            cfg['base_url'].rstrip('/') + '/chat/completions',
            json={
                'model': model,
                'max_tokens': _JUDGE_MAX_TOKENS,
                'temperature': 0,  # a veto should not be sampled differently each run
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_msg},
                ],
            },
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            timeout=120,
        )
        _raise_for_status_with_body(resp)
        choices = resp.json().get('choices') or []
        if not choices:
            return ''
        return str((choices[0].get('message') or {}).get('content') or '')

    @staticmethod
    def _parse_judge_verdict(response_text: str) -> Optional[Dict[str, Any]]:
        """Extract {verdict, reason} from raw judge output, or None.

        Strict in the one way that matters: an unrecognised verdict is None,
        not a coerced 'confirm'. Same posture as
        _parse_remediation_classification — parse or degrade, never salvage —
        except that here degrading means the row parks, so a malformed judge
        can only ever be more conservative than a working one.
        """
        text = re.sub(r'^\s*```(?:json)?|```\s*$', '', (response_text or '').strip()).strip()
        # A findings-array is the classifier's known failure shape, and the
        # brace scan below would happily lift the first object out of one.
        # Doing that here would manufacture a verdict from a malformed reply —
        # and 'confirm' is the direction that opens the gate. Refuse the shape.
        if text.startswith('['):
            return None
        start, end = text.find('{'), text.rfind('}')
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        verdict = str(payload.get('verdict') or '').strip().lower()
        if verdict not in ('confirm', 'downgrade', 'reject'):
            return None
        return {'verdict': verdict, 'reason': str(payload.get('reason') or '').strip()[:500]}

    def _classify_needs_action_recommendation(self, trigger: str, recommendation: str,
                                              alert_info: Dict[str, Any],
                                              report: str = '') -> Dict[str, Any]:
        """Classify a needs_action recommendation into queue hints (CFOP-46/48).

        A separate LLM call over the extracted recommendation — the
        investigation prompt itself stays untouched, so this cannot regress the
        load-bearing STATUS:/RECOMMENDATION: parsing. Shares the class rubric
        with the morning summary (_REMEDIATION_CLASS_RUBRIC) so there is one
        definition of the classes. The classifier sees the trigger, labels,
        recommendation, and an excerpt of the investigation's findings — not
        the tool trace — so the prompt template carries real weight and its
        worked example must never be an auto-eligible object.

        Failure escalates before it parks (CFOP-48 — the operator's direction
        is autonomy, with the human gate at the PR merge button):

          1. one-shot call
          2. unparseable -> nudge-retry: quote the malformed output back with a
             corrective message (the PR #76 pattern — recovered 19/19 there)
          3. still unparseable -> one attempt on the first provider in the
             chain whose (backend, model) has not already answered this ladder
          4. only then degrade to manual/high with no confidence — a
             needs-human row that can never clear the auto gate

        A confidently-classified result is NOT capped: it may clear the
        auto-execute gate (gitops-patch / k8s-action, low risk, >=0.8) and open
        a PR unattended — the mutation path stays a human-merge-gated PR.
        Malformed output is never salvaged into a classification — parse or
        degrade.
        """
        # classifier_backend/classifier_model travel with the hints so the model
        # that made the call is recorded on the row (CFOP-70). The degraded
        # fallback names no model because none of them produced this result —
        # the ladder ran out.
        fallback = {'remediation_class': 'manual', 'risk': 'high',
                    'confidence': None, 'host': None, 'repo': None,
                    'classifier_backend': None, 'classifier_model': None}
        system_prompt = (
            "You classify one infrastructure fix recommendation for a remediation "
            "queue. Respond ONLY with a SINGLE JSON object — never an array, "
            "never a findings list — no other text:\n"
            '{"remediation_class": "gitops-patch|k8s-action|k8s-imperative|'
            'node-action|data-fix|external-system|investigate|manual", '
            '"risk": "low|med|high", "confidence": 0.0, '
            '"host": "affected host or empty", '
            '"repo": "owning GitOps repo slug or empty"}\n'
            "Example of a correctly FORMATTED response (the values here are "
            "placeholders — judge the actual recommendation on its merits):\n"
            f"{json.dumps(_CLASSIFIER_SAFE_EXAMPLE)}\n\n"
            f"{_REMEDIATION_CLASS_RUBRIC}"
            "Be conservative with risk."
        )
        labels = alert_info.get('labels') or (alert_info.get('details') or {}).get('labels') or {}
        findings_part = (f"Investigation findings excerpt:\n{str(report)[:600]}\n"
                         if str(report or '').strip() else "")
        user_msg = (
            f"Alert: {str(trigger)[:300]}\n"
            f"Labels: {json.dumps(labels, default=str)[:300]}\n"
            f"{findings_part}"
            f"Recommendation: {str(recommendation)[:800]}\n\nClassify."
        )
        messages = [{'role': 'user', 'content': user_msg}]
        try:
            result = self._chat_with_tools_with_fallback(
                messages=messages, system_context=system_prompt,
                max_iterations=1,  # one-shot classification — no tool loop
            )
        except Exception as e:
            logger.warning(f"Remediation classifier LLM unavailable, degrading to manual/high: {e}")
            REMEDIATION_CLASSIFIER.labels(result='degraded').inc()
            return fallback
        parsed = self._parse_remediation_classification(result.get('response', ''))
        if parsed is not None:
            REMEDIATION_CLASSIFIER.labels(result='ok').inc()
            return _with_classifier_identity(parsed, result)
        # Every (backend, model) that already answered this ladder with garbage.
        # The fallback wrapper may itself have rotated (transport errors), so
        # rung 3 must skip ALL of them, not just the first (PR #134 review).
        answered = {(result.get('backend'), result.get('model'))}

        # Rung 2 — nudge: quote the malformed output back at the same chain.
        bad = str(result.get('response', ''))
        logger.info(f"Remediation classifier output unparseable, nudging: {bad[:200]}")
        nudge_messages = messages + [
            {'role': 'assistant', 'content': bad[:2000]},
            {'role': 'user', 'content': (
                "That response is not the required format. Reply again with "
                "ONLY the single JSON object described in the instructions — "
                "one object with remediation_class/risk/confidence/host/repo, "
                "not an array, no prose.")},
        ]
        try:
            retry = self._chat_with_tools_with_fallback(
                messages=nudge_messages, system_context=system_prompt,
                max_iterations=1,
            )
            parsed = self._parse_remediation_classification(retry.get('response', ''))
            if parsed is not None:
                REMEDIATION_CLASSIFIER.labels(result='nudged').inc()
                return _with_classifier_identity(parsed, retry)
            answered.add((retry.get('backend'), retry.get('model')))
        except Exception as e:
            logger.warning(f"Remediation classifier nudge retry failed: {e}")

        # Rung 3 — escalate: first provider in the chain that has not already
        # produced garbage on this ladder.
        try:
            other = next(((ptype, url, mname)
                          for ptype, url, mname in self._get_provider_chain('auto')
                          if (ptype, mname) not in answered), None)
        except Exception:
            other = None
        if other is not None:
            ptype, url, mname = other
            try:
                esc = self._chat_with_tools(
                    provider_type=ptype, url=url, model=mname,
                    messages=messages, system_context=system_prompt,
                    max_iterations=1,
                )
                parsed = self._parse_remediation_classification(esc.get('response', ''))
                if parsed is not None:
                    logger.info(f"Remediation classifier escalated to {ptype}/{mname}")
                    REMEDIATION_CLASSIFIER.labels(result='escalated').inc()
                    return _with_classifier_identity(
                        parsed, {'backend': ptype, 'model': mname})
            except Exception as e:
                logger.warning(f"Remediation classifier escalation to {ptype}/{mname} failed: {e}")

        logger.warning("Remediation classifier ladder exhausted, degrading to manual/high: "
                       f"{bad[:200]}")
        REMEDIATION_CLASSIFIER.labels(result='degraded').inc()
        return fallback

    @staticmethod
    def _parse_remediation_classification(response_text: str) -> Optional[Dict[str, Any]]:
        """Extract classification hints from raw classifier output, or None.

        Tolerates markdown fences and surrounding prose (same posture as
        _parse_triage_response). Values are only coerced, never invented:
        an unknown class or risk is passed through for
        normalize_remediation_fields to default conservatively.

        Confidence is NOT capped below the auto gate (CFOP-48): a confident
        gitops-patch/k8s-action at low risk is allowed to auto-queue and become
        a human-merge-gated PR. The summary path keeps its own
        _SUMMARY_CONFIDENCE_CAP — hunches stay capped. A value above 1 means
        the model ignored the 0–1 scale — the opposite of calibrated — so it
        becomes None (cannot clear the gate), not a clamp to certainty
        (PR #134 review). Negatives clamp to 0, which is harmless.
        """
        text = (response_text or '').strip()
        start, end = text.find('{'), text.rfind('}')
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload.get('remediation_class'):
            return None
        conf = payload.get('confidence')
        if isinstance(conf, (int, float)):
            conf = max(float(conf), 0.0) if conf <= 1.0 else None
        else:
            conf = None
        rclass = str(payload.get('remediation_class'))
        host = (str(payload.get('host') or '').strip() or None)
        # A node-action is "a host change over ssh/ansible" — with no host
        # there is nowhere to ssh, so the answer is incoherent rather than
        # merely incomplete. Returning None routes it into the same
        # nudge -> rotate -> degrade ladder that malformed output takes, which
        # buys a real second opinion instead of guessing a class on the
        # model's behalf. Same posture as the >1.0 confidence rule above: an
        # incoherent value is never salvaged into a classification.
        #
        # This MUST stay at parse time. _queue_needs_action_remediation
        # backfills a missing host from the alert's instance/node label, so a
        # host-less node-action reaching that far does NOT fail at enqueue —
        # it inherits an unrelated host and travels the queue as a real
        # node-action. That backfill is how a kubectl-shaped recommendation
        # gets laundered onto the SSH path (live row #49, CFOP-60: died at the
        # executor on "node-action execution not enabled"). Moving this check
        # below the backfill would recreate the bug.
        if rclass == 'node-action' and not host:
            return None
        return {
            'remediation_class': rclass,
            'risk': str(payload.get('risk') or 'high'),
            'confidence': conf,
            'host': host,
            'repo': (str(payload.get('repo') or '').strip() or None),
        }

    @staticmethod
    def _normalize_host(host) -> str:
        """Bare lowercase hostname from a host field or Prometheus label.

        An ``instance`` label is ``raspberrypi4:9100``, so the port has to come
        off before anything is compared against a node name — otherwise the
        node-incident collapse never matches the rows it exists to collapse.
        """
        h = str(host or '').strip().lower()
        if not h:
            return ''
        h = h.split('://', 1)[-1]      # in case a URL slipped into the label
        h = h.split('/', 1)[0]
        if ':' in h:                   # strip :port (never an IPv6 literal here)
            h = h.rsplit(':', 1)[0]
        return h.strip('.')

    def _notready_nodes(self) -> set:
        """Lowercase names of nodes that are not currently Ready.

        Same source _ground_truth_snapshot trusts (tools/k8s.py get_nodes,
        which reports Ready as 'True'/'False'/'Unknown').

        FAILS OPEN — any error yields the empty set, so every row enqueues
        exactly as it did before. A dedupe optimisation must never be the
        reason a real problem goes unrecorded; that is the same posture
        _open_remediation_for_key takes.
        """
        k8s = getattr(getattr(self, 'tools', None), 'k8s_tools', None)
        if not k8s:
            return set()
        try:
            result = k8s.get_nodes()
            if not result.get('success'):
                return set()
            return {str(n.get('name') or '').lower()
                    for n in result.get('nodes', [])
                    if str(n.get('ready')) != 'True' and n.get('name')}
        except Exception as e:
            logger.debug(f"Could not read node readiness for incident collapse: {e}")
            return set()

    @staticmethod
    def _node_incident_dedupe_key(host: str) -> str:
        """The one key every symptom of one dead node folds onto (CFOP-71)."""
        return f"node-down-{CFOperator._normalize_host(host)}"

    def _collapse_key_for_node_incident(self, details: Dict[str, Any]) -> Optional[str]:
        """The node-incident key for this row, or None to enqueue independently.

        One dead node fires many DIFFERENT alerts — node unreachable, deployment
        not ready, four pod-not-ready-30m, ArgoCD metrics absent, promtail
        memory. Each has its own legitimate Alertmanager fingerprint and
        therefore its own dedupe key, which is why _investigation_dedupe_key
        cannot collapse them: it is doing exactly the job CFOP-46 part C built
        it for. The fan-out has to be caught a level up, when the key is chosen.

        Rewriting the key rather than adding a second suppression layer means
        everything downstream keeps working unchanged — queue_remediation's
        existing payload['dedupe_key'] check collapses symptoms two through
        twelve onto row one, and _queue_needs_action_remediation's existing
        _open_remediation_for_key fallback still links each later investigation
        to the surviving row instead of reporting "none proposed".

        Conditional on the node actually being NotReady: a disk-full and a
        crash-looping pod on a HEALTHY host are two problems, not one incident,
        and merging them would lose the second.
        """
        host = self._normalize_host(details.get('host'))
        if not host:
            return None
        down = self._notready_nodes()
        if not down:
            return None
        # An alert's host may be the FQDN of a node registered by short name.
        if host in down or host.split('.', 1)[0] in down:
            return self._node_incident_dedupe_key(host.split('.', 1)[0])
        return None

    @staticmethod
    def _investigation_dedupe_key(alert_info: Dict[str, Any], recommendation: str,
                                  structured_fix=None) -> str:
        """Stable dedupe key for a needs_action enqueue (CFOP-46 part C, CFOP-80).

        Precedence: a dispatch-stamped key (summary/sweep re-dispatch loop
        breaking) > FIX targets (tgt-sha1 of sorted kind/id/repo) when present
        > the Alertmanager fingerprint (fallback when FIX is missing) > a hash
        over normalized (host, recommendation) for manually-triggered
        investigations that have no alert behind them.
        """
        ai = alert_info or {}
        stamped = str(ai.get('dedupe_key') or '').strip()
        if stamped:
            return stamped
        if structured_fix and structured_fix.get('targets'):
            return _fix_targets_dedupe_key(structured_fix)
        fingerprint = str(ai.get('fingerprint') or '').strip()
        if fingerprint:
            return f"alert-{fingerprint}"
        labels = ai.get('labels') or (ai.get('details') or {}).get('labels') or {}
        host = str(ai.get('host') or labels.get('instance') or labels.get('node') or '')
        basis = re.sub(r'\s+', ' ', f"{host}|{recommendation}".lower()).strip()
        return 'inv-' + hashlib.sha1(basis.encode('utf-8')).hexdigest()[:16]

    def _queue_needs_action_remediation(self, investigation_id: int, trigger: str,
                                        alert_info: Dict[str, Any], recommendation: str,
                                        response_text: str, provider: str,
                                        proposal=None, structured_fix=None,
                                        opened_prs=None) -> Optional[int]:
        """Route a needs_action investigation's recommendation into the queue.

        The missing feed from CFOP-46: classify the recommendation (cheap
        one-shot call), then enqueue through the existing gates. Skipped when
        the inline unschedulable-pod proposer already opened a PR for this
        investigation — one fix must not get two drivers; a decline still
        enqueues. Empty / "no action" recommendations enqueue nothing.

        CFOP-80: a valid FIX skips the classifier (class from target.kind).
        Missing/invalid FIX still classifies. Parse is module-level so a
        MagicMock self cannot intercept it.

        CFOP-116: ``opened_prs`` are PRs the model opened itself during the
        run — tool-loop evidence, not prose. The last one becomes the row's
        ``pr_url`` and the row enqueues as pr-open (see
        _maybe_queue_remediation), so the reconciler owns it and the executor
        never gets to open a second one.
        """
        if not self._remediation_flag('queue_feed'):
            return None
        rec = str(recommendation or '').strip()
        if not rec or rec.lower().startswith('no action') or rec.lower() in ('none', 'n/a', 'nothing'):
            return None
        if proposal is not None and (getattr(proposal, 'pr_result', None) or {}).get('status') == 'opened':
            logger.info(f"Investigation #{investigation_id}: inline proposer already opened a PR; "
                        "not enqueuing a second driver for the same fix")
            return None
        # CFOP-78: a fork-shaped recommendation commits to one action BEFORE
        # anything downstream reads it — the classifier then classifies the
        # committed action, and the dedupe key hashes it, instead of both
        # reverse-engineering a fork.
        # Capture the fork shape on the *incoming* rec. A successful commit
        # returns still_forked=False and a rewritten sentence; the FIX object
        # was parsed from the original report and still describes the unchosen
        # alternative. Trusting that FIX's gitops+low 0.8 is live row #72
        # again (PR #173).
        was_forked = bool(_FORK_SHAPED.search(rec))
        rec, still_forked = self._commit_forked_recommendation(trigger, rec,
                                                               report=response_text)
        # Re-validate even when the caller passed structured_fix: a truthy
        # invalid dict must not IndexError in _hints_from_structured_fix and
        # fail the investigation (CFOP-80: invalid FIX degrades to classifier).
        # The registry goes to BOTH calls. _ensure_structured_fix rejecting a
        # FIX does not remove it from response_text, so this re-parse sees the
        # same object again; without the registry it re-accepts exactly what
        # ensure just refused, and the unresolvable repo lands on the payload.
        # That made the CFOP-85 check inert for the only production path that
        # enqueues a row.
        repos = self.git_repos()
        candidate = (structured_fix if structured_fix is not None
                     else _parse_structured_fix(response_text, repos))
        fix = (_validate_structured_fix(candidate, repos)
               if isinstance(candidate, dict) else None)
        if fix:
            # Mutation check: drop this branch and test_valid_fix_skips_classifier fails.
            hints = _hints_from_structured_fix(fix)
        else:
            hints = self._classify_needs_action_recommendation(trigger, rec, alert_info,
                                                               report=response_text)
        details = dict(hints)
        if still_forked or was_forked:
            # Stuck fork (retry would not commit) OR a FIX that was parsed
            # before the rewrite: neither may clear the auto gate. Class stays
            # honest (target.kind / classifier); only the gate is barred.
            # Mutation check: drop `or was_forked` and
            # test_fork_commit_does_not_keep_fix_auto_confidence fails.
            details['confidence'] = None
        if not details.get('host'):
            ai = alert_info or {}
            labels = ai.get('labels') or (ai.get('details') or {}).get('labels') or {}
            details['host'] = ai.get('host') or labels.get('instance') or labels.get('node')
        details.update({
            'recommendation': rec,
            'report': response_text,
            'provider': provider,
            'source': 'investigation',
            'dedupe_key': self._investigation_dedupe_key(alert_info, rec, fix),
            # Context the mutation judge reads (CFOP-70). Not persisted as-is:
            # _maybe_queue_remediation picks what belongs in the payload.
            'trigger': trigger,
            'alert_labels': (alert_info or {}).get('labels')
                            or ((alert_info or {}).get('details') or {}).get('labels') or {},
        })
        opened = [str(u) for u in (opened_prs or []) if u]
        if opened:
            details['pr_url'] = opened[-1]
            if len(opened) > 1:
                details['opened_prs'] = opened
        # CFOP-108: a checklist is not a fix. One follow-up pass (guarded by
        # followup_of on the alert) instead of a row nobody can execute. After
        # the classifier on purpose: details['host'] is what lets the node-
        # incident collapse veto this for a NotReady host. Never when a PR is
        # already open (CFOP-116): a follow-up enqueues nothing, and the PR
        # would have no row to be tracked from.
        if not opened and _dispatch_checklist_followup(self, investigation_id, trigger,
                                                       alert_info, details, fix):
            return None
        rid = self._maybe_queue_remediation(investigation_id, details)
        if rid:
            logger.info(f"Investigation #{investigation_id} needs_action -> remediation #{rid} "
                        f"({details.get('remediation_class')}, risk={details.get('risk')})")
            return rid
        # Deduped (or enqueue failed): a repeat firing must still link to the
        # open row covering it, or the console shows "none proposed" while the
        # row sits on the worklist under the first investigation's id.
        existing = self._open_remediation_for_key(details['dedupe_key'])
        if existing:
            logger.info(f"Investigation #{investigation_id} needs_action deduped onto "
                        f"open remediation #{existing.get('id')}")
            # CFOP-116: the first run proposed, this run opened the PR. The
            # open row is the one the operator is looking at, so it is the
            # one that must carry the link and leave the executor's reach.
            self._stamp_opened_pr(existing, details.get('pr_url'))
            return existing.get('id')
        return None

    _SEVERITY_RISK = {'critical': 'high', 'warning': 'med', 'info': 'low'}

    @staticmethod
    def _dispatch_dedupe_key(host: str, title: str) -> str:
        """Stable key for a summary/sweep-dispatched investigation (CFOP-46 D).

        Slug over (host, finding title) — the two fields that identify the
        underlying problem across mornings, unlike the per-run sweep finding id
        or the reworded recommendation text. Stamped into the dispatched alert
        so the eventual needs_action enqueue carries it, which is what lets the
        next morning's feed see "already handled" and stop re-investigating.
        """
        slug = re.sub(r'[^a-z0-9]+', '-', f"{host} {title}".lower()).strip('-')[:80]
        return f"inv-dispatch-{slug}"

    @staticmethod
    def _extract_remediation_identifiers(text) -> frozenset:
        """The hard identifiers a recommendation names, as a set.

        Empty means "this text names nothing matchable" — and the caller must
        treat that as "do not fold", never as "matches everything".
        """
        raw = str(text or '')
        ids = set()
        for match in _IDENT_IP.finditer(raw):
            ids.add(match.group(1))
        for match in _IDENT_ENV.finditer(raw):
            ids.add(match.group(0))
        for match in _IDENT_ROW.finditer(raw):
            ids.add(f"{match.group(1).lower()}:{match.group(2)}")
        return frozenset(ids)

    def _absorb_repeat_remediation(self, details: Dict[str, Any]) -> Optional[int]:
        """Fold a re-found problem onto the open row already covering it.

        The dedupe key's prose-hash tier misses any rewording — six live rows
        for three problems on 2026-08-23, across two models — so this matches
        on what rewordings reliably share: the hard identifiers the
        recommendation names. Containment either way counts as the same
        problem; disjoint or empty sets never fold. Same posture as the
        CFOP-71 node collapse this sits beside: the fold happens ABOVE the
        key, is recorded visibly on the surviving row, and FAILS OPEN — worst
        case a duplicate row, which is today's behaviour.
        """
        try:
            new_ids = self._extract_remediation_identifiers(details.get('recommendation'))
            if not new_ids:
                return None
            for row in self.kb.list_open_remediations(limit=100):
                payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
                # Exact-key repeats already have a working path (the KB's
                # enqueue-side suppression + _open_remediation_for_key); this
                # fold exists for the rows that path cannot see.
                if payload.get('dedupe_key') and \
                        payload.get('dedupe_key') == details.get('dedupe_key'):
                    continue
                row_ids = self._extract_remediation_identifiers(payload.get('recommendation'))
                if row_ids and (new_ids <= row_ids or row_ids <= new_ids):
                    rid = row.get('id')
                    summary = str(details.get('trigger')
                                  or details.get('recommendation') or '')[:200]
                    logger.info(f"Folding repeat remediation onto open #{rid} "
                                f"(shared identifiers: {sorted(new_ids & row_ids)})")
                    self.kb.record_remediation_absorbed(rid, summary)
                    REMEDIATION_FOLDED.labels(reason='repeat').inc()
                    return rid
        except Exception as e:
            logger.warning(f"Repeat-remediation fold failed (enqueuing as-is): {e}")
        return None

    def _commit_forked_recommendation(self, trigger: str, recommendation: str,
                                      report: str = '') -> tuple:
        """Make a fork-shaped recommendation commit to one action.

        "Truncate the row, or update the config to a larger model" is not
        executable by anything — the dedupe key hashes it, the classifier
        keyword-matches it, and a human is handed the fork (live row #72,
        whose casual second option would have invalidated every stored
        embedding). One retry while the model is still in the loop beats
        parking it: quote the fork back and require a single action.

        Returns ``(text, still_forked)``. On any failure the original text
        comes back with ``still_forked=True`` — the caller caps that row out
        of the auto gate rather than trusting a fork to the executor.
        """
        rec = str(recommendation or '').strip()
        if not _FORK_SHAPED.search(rec):
            return rec, False
        findings_part = (f"Investigation findings excerpt:\n{str(report)[:600]}\n"
                         if str(report or '').strip() else "")
        messages = [{'role': 'user', 'content': (
            f"Alert: {str(trigger)[:300]}\n"
            f"{findings_part}"
            f"Recommendation offering alternatives: {rec[:800]}\n\n"
            "Commit to one action.")}]
        system_prompt = (
            "An infrastructure investigation produced a recommendation that "
            "offers MORE THAN ONE alternative fix. A remediation queue can "
            "execute exactly one action, so a fork is not actionable by "
            "anything downstream. Choose the single safest, most easily "
            "reversed of the alternatives ALREADY OFFERED — never invent a "
            "new one — and restate it as ONE imperative recommendation of at "
            "most three sentences, naming the concrete target (the file, key, "
            "resource or row to change). Reply with ONLY the rewritten "
            "recommendation text: no preamble, no alternatives, no 'or'."
        )
        try:
            result = self._chat_with_tools_with_fallback(
                messages=messages, system_context=system_prompt,
                max_iterations=1,
            )
            committed = str(result.get('response') or '').strip()
            if committed and len(committed) <= 1200 \
                    and not _FORK_SHAPED.search(committed):
                logger.info(f"Forked recommendation committed to one action: "
                            f"{committed[:160]}")
                REMEDIATION_FOLDED.labels(reason='fork_committed').inc()
                return committed, False
            logger.info(f"Fork commit retry still not a single action: "
                        f"{committed[:160]}")
        except Exception as e:
            logger.warning(f"Fork commit retry failed: {e}")
        REMEDIATION_FOLDED.labels(reason='fork_stuck').inc()
        return rec, True

    def _record_absorbed_symptom(self, node_key: str,
                                 details: Dict[str, Any]) -> Optional[int]:
        """Note a folded symptom on the open node-incident row; its id, or None.

        None means no incident row exists yet, so the caller enqueues one. The
        row keeps the list because a collapse that silently discards its inputs
        reads as "one arbitrary symptom won the race" — the operator should be
        able to see that twelve alerts were one dead node, which is the whole
        finding of CFOP-71. Fails open (None) like every other read on this
        path: worst case a second row is created, which is today's behaviour.
        """
        try:
            existing = self.kb.find_open_remediation_by_dedupe_key(node_key)
            if not existing:
                return None
            rid = existing.get('id')
            summary = str(details.get('trigger') or details.get('recommendation') or '')[:200]
            if rid and summary:
                self.kb.record_remediation_absorbed(rid, summary)
            return rid
        except Exception as e:
            logger.warning(f"Could not fold symptom onto node incident {node_key}: {e}")
            return None

    def _open_remediation_for_key(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        """Non-terminal remediation row carrying this dedupe key, or None.

        Fails open (None) on any KB error: dispatching an investigation is the
        long-standing behavior, suppression is the optimization.
        """
        try:
            return self.kb.find_open_remediation_by_dedupe_key(dedupe_key)
        except Exception as e:
            logger.warning(f"Could not check open remediations for '{dedupe_key}': {e}")
            return None

    def _stamp_opened_pr(self, row, pr_url) -> bool:
        """Carry a PR the model opened onto the open row that absorbed its enqueue.

        CFOP-116. Only a row the executor has not touched: queued or
        needs-human moves to pr-open with the URL, the state its own
        completion would have produced. A claimed/executing row already has
        a driver whose completion will report its own PR — log the collision,
        change nothing. A row that already carries a PR keeps it. Returns
        True when the row was stamped; fails open, the row stays what it was.
        """
        pr_url = str(pr_url or '').strip()
        if not pr_url or not isinstance(row, dict) or not row.get('id'):
            return False
        # The column. A row shaped like #85 — URL only in prose, surfaced as
        # named_pr_url — has no tracked PR and is exactly what this promotes.
        if row.get('pr_url'):
            return False
        if row.get('status') not in ('queued', 'needs-human'):
            logger.warning(f"PR {pr_url} opened for remediation #{row['id']} while it is "
                           f"{row.get('status')}; not stamped — that row has a driver")
            return False
        try:
            self.kb.update_remediation_status(row['id'], 'pr-open', pr_url=pr_url)
        except Exception as e:
            logger.warning(f"Could not stamp PR {pr_url} on remediation #{row['id']}: {e}")
            return False
        logger.info(f"Remediation #{row['id']} -> pr-open: investigation opened {pr_url}")
        return True

    @staticmethod
    def _recommendation_is_investigate_shaped(text: str) -> bool:
        """True when the next step is evidence-gathering the agent can do itself.

        Matches the morning-summary prompt's investigate vocabulary
        (check/verify/confirm/investigate/monitor). Human-only cues
        (physically, hardware, power supply, …) stay on the manual queue even
        when a check/verify verb is also present.
        """
        if not text or _HUMAN_ONLY_SHAPED.search(text):
            return False
        return bool(_INVESTIGATE_SHAPED.search(text))

    def _feed_remediations_from_sweeps(self, reports: List[Dict[str, Any]]) -> int:
        """Feed overnight sweep findings into investigation or the remediation queue.

        Off unless ``remediation.queue_feed`` is set. Investigate-shaped
        recommendations (check/verify/monitor/…) are dispatched as autonomous
        investigations — the agent gathers evidence itself rather than parking
        them as needs-human. Mutation-shaped recs (concrete "change this")
        go through the CFOP-48 classifier + auto-queue gates, same as a
        needs_action investigation — they are the recs *most* like executor
        work, and hardcoding them ``manual`` dead-parked them with no
        class/confidence stamp (CFOP-53, live row #43). Only genuinely
        human-shaped recs enqueue directly as ``manual``; classifier
        degrade/failure falls back to that same path so a finding is never
        dropped because classification hiccupped. Deduped by finding id on
        every non-investigate path.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return 0
        handled = 0
        dispatched = 0
        enq = 0
        for rep in reports or []:
            for f in (rep.get('findings') or []):
                rec = str(f.get('remediation') or '').strip()
                if not rec or rec.lower().startswith('no action') or rec.lower() in ('none', 'n/a', 'nothing'):
                    continue
                key = f"sweep-{f.get('id') or rec[:80]}"
                risk = self._SEVERITY_RISK.get(str(f.get('severity') or 'info'), 'high')
                finding = str(f.get('finding') or '').strip()
                title = finding or rec[:80]
                if self._recommendation_is_investigate_shaped(rec):
                    # CFOP-46 D: investigate once. If an earlier dispatch of
                    # this problem landed needs_action, its remediation row is
                    # still open under this key — re-gathering the same
                    # evidence IS the loop. Terminal rows (resolved/rejected)
                    # re-admit a genuine recurrence.
                    host = f.get('resource_name') or f.get('namespace') or ''
                    dispatch_key = self._dispatch_dedupe_key(host, title)
                    open_row = self._open_remediation_for_key(dispatch_key)
                    if open_row:
                        logger.info(
                            f"Skipping re-investigation of '{title}': remediation "
                            f"#{open_row.get('id')} (investigation "
                            f"#{open_row.get('investigation_id')}) is still open")
                        handled += 1
                        continue
                    try:
                        self.enqueue_investigation({
                            'summary': f"{title}: {rec}"[:300],
                            'source': 'sweep-investigate',
                            'host': host,
                            'dedupe_key': dispatch_key,
                        })
                        dispatched += 1
                        handled += 1
                    except Exception as e:
                        logger.warning(f"could not dispatch sweep investigation for '{title}': {e}")
                    continue
                provider = _llm_provider_tag(rep.get('sweep_meta') or {})
                if not _HUMAN_ONLY_SHAPED.search(rec):
                    # CFOP-53: mutation-shaped recs get the same classify →
                    # gate lane as needs_action investigations. A confident
                    # gitops-patch/k8s-action at low risk auto-queues; parked
                    # rows still carry class/confidence/provider so they say
                    # why. Degrade (manual + no confidence) or a classifier
                    # error falls through to the legacy manual enqueue below.
                    try:
                        hints = self._classify_needs_action_recommendation(
                            title, rec,
                            {'labels': {k: v for k, v in (
                                ('namespace', f.get('namespace')),
                                ('resource', f.get('resource_name'))) if v}},
                            report=str(f.get('evidence') or ''))
                        if hints.get('remediation_class') == 'investigate':
                            # The rubric prefers investigate for borderline
                            # recs the _INVESTIGATE_SHAPED regex missed. Honor
                            # it like the shaped branch above — enqueuing would
                            # coerce the unknown class to 'manual' (not in
                            # _REMEDIATION_CLASSES) and park the row.
                            host = f.get('resource_name') or f.get('namespace') or ''
                            dispatch_key = self._dispatch_dedupe_key(host, title)
                            if not self._open_remediation_for_key(dispatch_key):
                                self.enqueue_investigation({
                                    'summary': f"{title}: {rec}"[:300],
                                    'source': 'sweep-investigate',
                                    'host': host,
                                    'dedupe_key': dispatch_key,
                                })
                                dispatched += 1
                            handled += 1
                            continue
                        degraded = (hints.get('remediation_class') == 'manual'
                                    and hints.get('confidence') is None)
                        if not degraded:
                            details = dict(hints)
                            if not details.get('host'):
                                details['host'] = (f.get('resource_name')
                                                   or f.get('namespace'))
                            details.update({
                                'recommendation': rec,
                                'report': '\n'.join(
                                    p for p in (finding, str(f.get('evidence') or ''))
                                    if p),
                                'source': 'morning-summary/sweep',
                                'dedupe_key': key,
                                'trigger': title,
                                'alert_labels': {k: v for k, v in (
                                    ('namespace', f.get('namespace')),
                                    ('resource', f.get('resource_name'))) if v},
                            })
                            if provider:
                                details['provider'] = provider
                            rid = self._maybe_queue_remediation(None, details)
                            if rid:
                                enq += 1
                                handled += 1
                            continue
                    except Exception as e:
                        logger.warning(
                            f"sweep rec classification failed for '{title}', "
                            f"falling back to manual enqueue: {e}")
                try:
                    payload = {
                        'recommendation': rec,
                        'finding': f.get('finding'),
                        'evidence': f.get('evidence'),
                        'resource': {'type': f.get('resource_type'),
                                     'name': f.get('resource_name'),
                                     'namespace': f.get('namespace')},
                        'source': 'morning-summary/sweep',
                        'dedupe_key': key,
                    }
                    if provider:
                        payload['provider'] = provider
                    # This path enqueues directly rather than through
                    # _maybe_queue_remediation, so it would otherwise miss the
                    # CFOP-71 collapse: a morning-summary echo of a node that is
                    # already down ("physically check the power cable") would
                    # open its own row beside the incident. Same rewrite, same
                    # fail-open behaviour.
                    sweep_host = f.get('resource_name') or f.get('namespace')
                    node_key = self._collapse_key_for_node_incident({'host': sweep_host})
                    if node_key and node_key != key:
                        absorbed = self._record_absorbed_symptom(
                            node_key, {'trigger': title, 'recommendation': rec})
                        if absorbed:
                            handled += 1
                            continue
                        key = node_key
                        payload['dedupe_key'] = node_key
                    rid = self.kb.queue_remediation(
                        remediation_class='manual',
                        payload=payload,
                        host_id=str(f.get('resource_name') or f.get('namespace') or 'default')[:64],
                        risk=risk,
                        confidence=None,
                        dedupe_key=key,
                    )
                    if rid:
                        enq += 1
                        handled += 1
                        self._count_enqueued('morning-summary/sweep', 'manual', risk, None)
                except Exception as e:
                    logger.error(f"sweep->remediation enqueue failed: {e}", exc_info=True)
        if handled:
            logger.info(f"Fed {handled} sweep finding(s): "
                        f"{dispatched} investigation(s), {enq} queue row(s)")
        return handled

    def feed_remediations_from_recent_sweeps(self, limit: int = 10) -> int:
        """On-demand: enqueue remediations from the most recent sweep reports."""
        return self._feed_remediations_from_sweeps(self.kb.get_recent_sweep_reports(limit=limit))

    @staticmethod
    def _parse_summary_recommendations(summary_text: str) -> List[Dict[str, Any]]:
        """Extract recs from the summary's ```json {"recommendations":[...]} block."""
        m = re.search(r"```json\s*(\{.*?\})\s*```", summary_text or "", re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return []
        recs = data.get('recommendations') if isinstance(data, dict) else None
        return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []

    @staticmethod
    def _strip_summary_recommendations_block(summary_text: str) -> str:
        """Remove the machine-readable ```json recommendations block from the
        summary so it never reaches operator-facing channels (Slack/ntfy).

        The block is emitted by the LLM purely to feed the remediation queue
        (see _feed_remediations_from_summary); humans see the prose table above
        it. Strip only after the queue has consumed it.
        """
        if not summary_text:
            return summary_text
        stripped = re.sub(r"\n*```json\s*\{.*?\}\s*```\n*", "\n", summary_text,
                          flags=re.DOTALL)
        return stripped.rstrip() + "\n" if stripped != summary_text else summary_text

    def _feed_remediations_from_summary(self, summary_text: str,
                                        overnight_reports: Optional[List[Dict[str, Any]]] = None,
                                        provider: Optional[str] = None) -> int:
        """Feed the queue from the summary's structured recommendations block.

        Captures the operator-facing 'Issues & Recommendations' (LLM synthesis),
        which raw sweep findings don't contain. Falls back to structured sweep
        findings when the LLM emits no usable block. Gated by queue_feed; the
        per-item remediation_class/risk/confidence drive the auto-execute gate,
        so a low-risk gitops-patch can become auto-eligible. Deduped by title.

        ``provider`` is the summary LLM tag (``backend/model``) stamped onto
        queued rows as ``payload.provider`` — not applied on the sweep fallback,
        which has its own ``sweep_meta``.
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not self._remediation_flag('queue_feed'):
            return 0
        recs = self._parse_summary_recommendations(summary_text)
        if not recs:
            return self._feed_remediations_from_sweeps(overnight_reports or [])
        enq = 0
        dispatched = 0  # investigate-class findings sent to the investigation pipeline
        for r in recs:
            rec = str(r.get('recommendation') or '').strip()
            if not rec or rec.lower().startswith('no action') or rec.lower() in ('none', 'n/a', 'nothing'):
                continue
            title = str(r.get('title') or rec[:80]).strip()
            key = "summary-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
            rclass = str(r.get('remediation_class') or 'manual')
            risk = str(r.get('risk') or 'med')
            conf = r.get('confidence') if isinstance(r.get('confidence'), (int, float)) else None
            # Clamp the cheap model's self-reported confidence: a confident
            # hallucination must not surface as a high-confidence queue row.
            if conf is not None:
                conf = min(conf, _SUMMARY_CONFIDENCE_CAP)
            # 'investigate' findings are evidence-gathering the agent does itself.
            # A mutation-class rec from the summary is an UNVERIFIED hypothesis
            # (cheap model, no enforced grounding), so route it the same way:
            # the deep tier verifies it and only a grounded finding becomes a
            # remediation — never a high-confidence host action straight from a
            # summary hunch. Mislabelled 'manual' with investigate-shaped text
            # (check/verify/monitor/…) is also dispatched — the cheap model
            # often defaults to manual for "check CoreDNS" style items.
            # Only genuinely human-only manuals still queue.
            #
            # Deliberate policy split with the sweep path (CFOP-53): the sweep
            # feed runs mutation-shaped recs through the dedicated CFOP-48
            # classifier (fresh call, shared rubric, escalation ladder) and its
            # verdict can auto-queue. The class labels HERE are the summary
            # model's own JSON self-labels — no second opinion — which is why
            # they are dispatch-only. Trust the classifier's classification,
            # not the summarizer's; do not "unify" the two feeds by loosening
            # this one.
            route_investigate = (
                rclass == 'investigate'
                or rclass in _SUMMARY_MUTATION_CLASSES
                or (rclass == 'manual' and self._recommendation_is_investigate_shaped(rec))
            )
            if route_investigate:
                # CFOP-46 D: same loop-break as the sweep path — an open
                # remediation row under this key means an earlier dispatch
                # already landed needs_action; don't re-gather the evidence.
                dispatch_key = self._dispatch_dedupe_key(str(r.get('host') or ''), title)
                open_row = self._open_remediation_for_key(dispatch_key)
                if open_row:
                    logger.info(
                        f"Skipping re-investigation of '{title}': remediation "
                        f"#{open_row.get('id')} (investigation "
                        f"#{open_row.get('investigation_id')}) is still open")
                    continue
                try:
                    # Preserve mutation-class proposals for the investigator;
                    # investigate / mislabelled-manual get no suffix.
                    suffix = (f" [proposed: {rclass}]"
                              if rclass in _SUMMARY_MUTATION_CLASSES else '')
                    self.enqueue_investigation({'summary': f"{title}: {rec}{suffix}"[:300],
                                                'source': 'summary-investigate',
                                                'host': r.get('host'),
                                                'dedupe_key': dispatch_key})
                    dispatched += 1
                except Exception as e:
                    logger.warning(f"could not dispatch investigation for '{title}': {e}")
                continue
            try:
                payload = {'recommendation': rec, 'title': title,
                           'target': {'host': r.get('host')},
                           'repo': (str(r.get('repo') or '').strip() or None),
                           'source': 'morning-summary', 'dedupe_key': key}
                if provider:
                    payload['provider'] = provider
                rid = self.kb.queue_remediation(
                    remediation_class=rclass,
                    payload=payload,
                    host_id=str(r.get('host') or 'default')[:64],
                    risk=risk,
                    confidence=conf,
                    dedupe_key=key,
                )
                if rid:
                    enq += 1
                    self._count_enqueued('morning-summary', rclass, risk, conf)
            except Exception as e:
                logger.error(f"summary->remediation enqueue failed: {e}", exc_info=True)
        if enq or dispatched:
            logger.info(f"Morning summary: queued {enq} remediation(s), "
                        f"dispatched {dispatched} investigation(s)")
        return enq

    def _maybe_open_pr_from_deep_diff(self, alert: Dict[str, Any], details: Dict[str, Any],
                                      diff_text: str) -> Optional[Dict[str, Any]]:
        """Route a deep-investigation diff through the remediation PR gates.

        Off unless ``remediation.deep_open_prs`` is set — until then the diff
        only travels inside the report/notification (dry-run, like Phase B
        before open_prs went live).
        """
        rcfg = self.config.get('remediation', {}) if isinstance(self.config, dict) else {}
        if not rcfg.get('deep_open_prs'):
            return None
        try:
            proposer = RemediationProposer(
                getattr(self.tools, 'k8s_tools', None),
                repos=self.config.get('git', {}).get('repos', []),
                open_prs=True,
                default_repo_name=rcfg.get('default_repo', 'homelab-infra'),
                github=self._github_write_client(),
                max_open_prs=int(rcfg.get('max_open_prs', 3)),
            )
            host = str(details.get('host') or 'unknown')
            alertname = str((alert.get('details') or {}).get('alertname') or 'finding')
            title = f"cfoperator deep-investigation fix: {alertname} on {host}"
            body = (
                f"Proposed by a deep-investigation run for alert: {alert.get('summary')}\n\n"
                f"Recommendation: {details.get('recommendation') or '(see report)'}\n\n"
                "Generated from host forensics; review before merging.\n"
                f"Report excerpt:\n\n{str(details.get('report') or '')[:2000]}"
            )
            return proposer.open_pr_from_diff(
                diff_text=diff_text, title=title, body=body,
                dedupe_key=f"{host}-{alertname}",
            )
        except Exception as e:
            logger.warning(f"Deep-investigation PR path failed: {e}")
            return {'status': 'error', 'detail': str(e)[:200]}

    def _verify_investigation_outcome(self, outcome: str, alert_info: Dict[str, Any],
                                      trigger: str) -> tuple:
        """Deterministically check a 'resolved' verdict against live cluster state.

        The investigation LLM can claim 'resolved' while the resource is still
        broken (it only *recommended* a fix). When the alert pins to a specific
        k8s pod, re-query its real status; if it isn't actually healthy,
        downgrade 'resolved' -> 'needs_action'. Conservative: only downgrades,
        and no-ops when the resource can't be identified, the pod is gone, or
        K8sTools is unavailable. Returns (outcome, note_or_None).
        """
        if outcome != 'resolved':
            return outcome, None
        k8s = getattr(self.tools, 'k8s_tools', None)
        if not k8s:
            return outcome, None
        ident = self._identify_pod(alert_info, trigger)
        if not ident:
            return outcome, None
        namespace, pod_name = ident
        try:
            status = k8s.get_pod_status(namespace, pod_name)
        except Exception as e:
            logger.debug(f"Outcome verify skipped (status query failed): {e}")
            return outcome, None
        # Pod not found could mean it was replaced/cleaned up — don't assume broken.
        if not status.get('success') or self._pod_is_healthy(status):
            return outcome, None
        note = f"claimed resolved but {namespace}/{pod_name} is {status.get('phase', 'unknown')}"
        logger.info(f"Outcome verify downgraded resolved -> needs_action: {note}")
        return 'needs_action', note

    @staticmethod
    def _action_message(outcome: str, trigger: str, duration: float, tool_calls: int) -> str:
        """One-line ActionResult.message summarising an investigation outcome."""
        verb = {
            'resolved': 'Resolved',
            'needs_action': 'Action needed',
            'escalated': 'Escalated',
            'monitoring': 'Monitoring',
            'failed': 'Investigation failed',
        }.get(outcome, outcome.title())
        return f"{verb}: {trigger[:160]} ({duration:.1f}s, {tool_calls} tool calls)"

    def _build_action_result(self, *, success: bool, message: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a result dict matching event_runtime.models.ActionResult.to_dict()."""
        return {
            'action': 'investigate',
            'success': bool(success),
            'message': str(message),
            'details': dict(details),
            'executed_at': datetime.now(timezone.utc).isoformat(),
        }

    def _deep_system_sweep(self):
        """
        Proactive mode: Comprehensive system analysis.

        Every N minutes, systematically:
        1. Query ALL metrics and look for trends
        2. Scan ALL logs for patterns
        3. Check ALL containers/services
        4. Compare to baselines
        5. Search for slow degradation
        6. Consolidate learnings
        7. Generate summary report
        """
        logger.info("Starting deep system sweep")
        sweep_start = time.time()

        findings = []
        sweep_config = self.config['ooda']['sweep']

        # Parallel sweep: if pool has 2+ instances, fan out LLM phases concurrently
        if self.ollama_pool and self.ollama_pool.available_count() >= 2:
            logger.info(f"Using parallel sweep ({self.ollama_pool.available_count()} instances available)")
            try:
                from sweep_graph import run_parallel_sweep
                parallel_findings = run_parallel_sweep(self, self.ollama_pool, sweep_config)
                findings.extend(parallel_findings)
            except Exception as e:
                logger.error(f"Parallel sweep failed, falling back to sequential: {e}")
                findings.extend(self._sequential_sweep(sweep_config))
        else:
            if self.ollama_pool:
                logger.info("Pool has <2 available instances, using sequential sweep")
            findings.extend(self._sequential_sweep(sweep_config))

        # 4. Baseline drift detection
        if sweep_config.get('baseline_drift'):
            logger.info("Checking baseline drift...")
            drift_findings = self._check_baseline_drift()
            findings.extend(drift_findings)
            logger.info(f"Baseline drift check found {len(drift_findings)} findings")

        # 5. Learning consolidation - merge similar learnings
        if sweep_config.get('learning_consolidation'):
            logger.info("Consolidating learnings...")
            self._consolidate_learnings()

        # 5b. Backfill embeddings for unindexed investigations and learnings
        try:
            if self.embeddings.is_available():
                result = self.embeddings.batch_index_investigations(
                    kb=self.kb._kb,
                    batch_size=10,
                    max_total=50
                )
                if result.get('success', 0) > 0:
                    logger.info(f"Embedding backfill (investigations): {result['success']} indexed, {result.get('remaining', 0)} remaining")

                lr = self.embeddings.batch_index_learnings(
                    kb=self.kb._kb,
                    batch_size=10,
                    max_total=50
                )
                if lr.get('success', 0) > 0:
                    logger.info(f"Embedding backfill (learnings): {lr['success']} indexed, {lr.get('remaining', 0)} remaining")
        except Exception as e:
            logger.debug(f"Embedding backfill skipped: {e}")

        # 6. Deduplicate findings across phases
        findings = self._dedup_findings(findings)

        # 6b. LLM judge — filter hallucinated/unsupported findings
        findings = self._verify_findings(findings)

        # 6c. Post findings to event runtime (if configured)
        if findings:
            try:
                self._post_findings_to_event_runtime(findings)
            except Exception as e:
                logger.debug(f"Could not post findings to event runtime: {e}")

        # 6d. Emit resolutions for findings that cleared since last sweep
        # so Slack/Discord see explicit "Resolved: …" notifications instead
        # of silently dropping previously-fired alerts.
        try:
            resolved = self._get_resolved_findings(findings)
            if resolved:
                logger.info(f"Sweep: {len(resolved)} finding(s) resolved since last sweep")
                self._post_resolutions_to_event_runtime(resolved)
        except Exception as e:
            logger.debug(f"Could not emit resolutions: {e}")

        # 7. Generate sweep report
        if findings:
            logger.info(f"Sweep found {len(findings)} total issues")
            report = self._generate_sweep_report(findings)

            # Only notify if findings changed since last sweep
            if report['severity'] in ['warning', 'critical']:
                new_findings = self._get_new_findings(report['findings'])
                if new_findings:
                    logger.warning(f"New findings in sweep ({len(new_findings)} new): {report['summary'][:200]}")
                    # Build a notification-only report with just the new stuff
                    notif_report = self._generate_sweep_report(new_findings)
                    notif_report['summary'] = f"[{len(new_findings)} new of {len(findings)} total] " + notif_report['summary']
                    self._notify_sweep_findings(notif_report)
                else:
                    logger.info(f"Sweep found {len(findings)} issues (all known from previous sweep, skipping notification)")

            # Add timing and mode info to sweep_meta
            sweep_duration = time.time() - sweep_start
            sweep_mode = 'parallel' if (self.ollama_pool and self.ollama_pool.available_count() >= 0) else 'sequential'
            if report.get('sweep_meta'):
                report['sweep_meta']['duration_seconds'] = round(sweep_duration, 1)
                report['sweep_meta']['mode'] = sweep_mode

            # Always store the full report in DB
            try:
                self.kb.store_sweep_report(
                    severity=report['severity'],
                    findings=report['findings'],
                    summary=report['summary'],
                    sweep_meta=report.get('sweep_meta')
                )
            except Exception as e:
                logger.warning(f"Could not store sweep report (DB down?): {e}")
        else:
            logger.info("Sweep complete - no findings")

        # 7b. Capture metric snapshot for correlation baseline
        try:
            snapshot_metrics = self._capture_metric_snapshot()
            if snapshot_metrics:
                self.kb._kb.record_metric_snapshot(
                    metrics=snapshot_metrics,
                    snapshot_type='sweep'
                )
        except Exception as e:
            logger.debug(f"Metric snapshot skipped: {e}")

        # 8. Correlation analysis — detect patterns AND have LLM analyze them
        logger.info("Starting correlation analysis...")
        try:
            # Keep ephemeral Job/CronJob services out of correlations: clean any
            # previously-persisted false rows (recorded before the baseline
            # filter), and guard against recording new ones.
            ephemeral = self._ephemeral_service_names()
            if ephemeral:
                purged = self.kb._kb.purge_correlations_for_services(ephemeral)
                if purged:
                    logger.info(f"Purged {purged} false correlation(s) for ephemeral job services")
            patterns = self.kb._kb.find_service_failure_patterns(days=30)
            if patterns:
                for p in patterns:
                    svc_a = p.get('service_a', '')
                    svc_b = p.get('service_b', '')
                    if svc_a and svc_b and svc_a not in ephemeral and svc_b not in ephemeral:
                        ctype = p.get('correlation_type', 'co_failure')
                        self.kb._kb.record_service_correlation(
                            service_a=svc_a,
                            service_b=svc_b,
                            correlation_type=ctype,
                            time_delta_seconds=p.get('avg_time_delta_seconds'),
                            details={'co_failure_count': p.get('co_failure_count', 0)}
                        )
                logger.info(f"Correlation analysis: {len(patterns)} service failure patterns found")

            # Persist event correlations (investigation<->drift, investigation<->investigation)
            correlated = self.kb._kb.find_correlated_events(window_seconds=300, hours=168)
            persisted = 0
            for ce in correlated:
                try:
                    self.kb._kb.record_event_correlation(
                        event_a_type=ce['event_a']['type'],
                        event_a_id=ce['event_a']['id'],
                        event_b_type=ce['event_b']['type'],
                        event_b_id=ce['event_b']['id'],
                        time_delta_seconds=ce['time_delta_seconds'],
                        root_cause_candidate='event_a' if ce['time_delta_seconds'] > 0 else 'event_b',
                        analysis_notes=f"{ce['event_a'].get('trigger', '')} <-> {ce['event_b'].get('trigger', ce['event_b'].get('drift_type', ''))}"
                    )
                    persisted += 1
                except Exception as e:
                    logger.debug(f"Could not persist event correlation: {e}")
            if persisted:
                logger.info(f"Correlation analysis: persisted {persisted} event correlations")

            # LLM analysis of operational data + correlations
            self._analyze_correlations(findings, patterns or [])
        except Exception as e:
            logger.warning(f"Correlation analysis failed: {e}", exc_info=True)

    def _analyze_correlations(self, sweep_findings: list, failure_patterns: list):
        """Have the LLM analyze operational data and correlations to produce insights."""
        import requests as req

        # Gather operational context
        try:
            ops = self.kb.get_operational_summary(hours=24)
        except Exception:
            ops = {}

        correlated_events = []
        learned_correlations = []
        try:
            correlated_events = self.kb._kb.find_correlated_events(hours=168)[:10]
            learned_correlations = self.kb._kb.get_service_correlations(min_count=2)
        except Exception as e:
            logger.debug(f"Could not load correlations for analysis: {e}")

        # Skip if there's nothing interesting to analyze
        has_data = (
            sweep_findings
            or failure_patterns
            or correlated_events
            or ops.get('investigations', {}).get('total', 0) > 0
        )
        if not has_data:
            logger.info("Correlation analysis: no data to analyze, skipping")
            return

        resolved = self._resolve_provider()
        if not resolved:
            logger.info("Correlation analysis: no LLM provider available, skipping")
            return

        provider_type, url, model = resolved
        logger.info(f"Correlation analysis: sending to {provider_type}/{model} (findings={len(sweep_findings)}, patterns={len(failure_patterns)}, correlated={len(correlated_events)})")

        # Only feed actionable findings into the learning pipeline. Info-severity
        # findings are typically healthy-state restatements ("X is running fine")
        # and turning them into "patterns" just pollutes the knowledge base.
        actionable_findings = [
            f for f in sweep_findings
            if isinstance(f, dict) and str(f.get('severity', '')).lower() in ('warning', 'critical')
        ]

        prompt = f"""Analyze this operational data from the last 24 hours and identify patterns, root causes, or concerns.

SWEEP FINDINGS (this cycle):
{json.dumps(actionable_findings[:10], default=str)[:1500]}

OPERATIONAL SUMMARY:
- Sweeps: {ops.get('sweeps', {}).get('total', 0)} total, avg {ops.get('sweeps', {}).get('avg_findings', 0)} findings/sweep
- Severity breakdown: {json.dumps(ops.get('sweeps', {}).get('by_severity', {}))}
- Investigations: {ops.get('investigations', {}).get('total', 0)} total, outcomes: {json.dumps(ops.get('investigations', {}).get('by_outcome', {}))}
- Learnings extracted: {ops.get('learnings', {}).get('total', 0)}

SERVICE FAILURE PATTERNS (7-day window):
{json.dumps(failure_patterns[:5], default=str)[:800]}

CORRELATED EVENTS (same time window):
{json.dumps(correlated_events[:5], default=str)[:800]}

KNOWN SERVICE CORRELATIONS:
{json.dumps(learned_correlations[:5], default=str)[:500]}

Return ONLY valid JSON:
{{"insights": [
  {{
    "learning_type": "pattern",
    "title": "Brief title (max 100 chars)",
    "description": "What pattern was detected and what it means",
    "applies_when": "The concrete, observable condition under which this learning is relevant (e.g. 'pod X is OOMKilled', 'service Y and Z fail within 5 min of each other'). REQUIRED — an insight with no trigger condition is useless.",
    "services": ["service1"],
    "category": "resource"
  }}
]}}

learning_type must be one of: solution, pattern, root_cause, antipattern, insight
category must be one of: resource, network, config, dependency

Focus on:
- Services that fail together (dependency chains)
- Recurring issues across multiple sweeps
- Escalation patterns (info → warning → critical over time)
- Issues that investigations failed to resolve

Do NOT emit an insight for a healthy/normal state, a one-off transient blip, or a
restatement of a single finding. Only genuine cross-event patterns worth remembering.
Every insight MUST have a non-empty, specific `applies_when`. Omit any insight you
cannot give a real trigger condition for.
Return empty array if nothing notable: {{"insights": []}}"""

        messages = [
            {'role': 'system', 'content': 'You are an SRE analyst. Analyze operational data for patterns. Return ONLY valid JSON.'},
            {'role': 'user', 'content': prompt}
        ]

        try:
            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.3,
                    'format': 'json'
                }
                resp = req.post(f"{url}/api/chat", json=payload, timeout=self.llm_timeout)
                text = resp.json().get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                if not api_key:
                    return
                payload = {
                    'model': model,
                    'messages': messages,
                    'temperature': 0.3,
                    'max_tokens': 2048,
                    'response_format': {'type': 'json_object'}
                }
                resp = req.post(
                    endpoint,
                    json=payload,
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                    timeout=60
                )
                text = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                if not api_key:
                    return
                payload = {
                    'model': model,
                    'max_tokens': 2048,
                    'system': 'You are an SRE analyst. Analyze operational data for patterns. Return ONLY valid JSON.',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                }
                resp = req.post(
                    'https://api.anthropic.com/v1/messages',
                    json=payload,
                    headers={'Content-Type': 'application/json', 'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
                    timeout=60
                )
                text = '\n'.join(
                    b.get('text', '') for b in resp.json().get('content', [])
                    if b.get('type') == 'text'
                )
            else:
                return

            result = json.loads(text)
            insights = result.get('insights', [])

            stored = 0
            skipped = 0
            for insight in insights[:3]:
                if not insight.get('title') or not insight.get('description'):
                    continue
                # Drop insights with no concrete trigger condition — they can
                # never be retrieved on relevance, so they are pure noise.
                if not learning_has_trigger_condition(insight):
                    skipped += 1
                    logger.info(f"Skipping correlation insight without applies_when: {insight.get('title','')[:60]}")
                    continue
                insight.setdefault('learning_type', 'insight')
                insight.setdefault('tags', ['correlation', 'automated'])
                valid_types = {'pattern', 'solution', 'root_cause', 'antipattern', 'insight'}
                if insight['learning_type'] not in valid_types:
                    logger.warning(f"Invalid learning_type '{insight['learning_type']}', defaulting to 'insight'")
                    insight['learning_type'] = 'insight'
                try:
                    lid = self.kb.store_learning(insight)
                    stored += 1
                    if lid and lid > 0:
                        search_text = ' '.join(filter(None, [
                            insight.get('title', ''),
                            insight.get('description', ''),
                            str(insight.get('applies_when', '')),
                        ]))
                        self._embed_learning(lid, search_text)
                except Exception as e:
                    logger.warning(f"Failed to store correlation insight: {e}")
            if skipped:
                logger.info(f"Correlation analysis: skipped {skipped} insight(s) lacking a trigger condition")

            # Tier-2 noise routing: correlation insights are informational, not
            # actionable-now. By default they're stored as learnings (and rolled
            # into the morning summary) rather than paged real-time. Set
            # notifications.realtime_correlation_insights=true to page them.
            realtime_insights = bool(
                (self.config.get('notifications', {}) or {}).get('realtime_correlation_insights', False)
            ) if isinstance(self.config, dict) else False
            if stored and not realtime_insights:
                logger.info(f"Correlation analysis: {stored} insight(s) stored as learnings; "
                            f"real-time notification suppressed (digest)")
            elif stored:
                logger.info(f"Correlation analysis: {stored} insights stored as learnings")
                # Notify about correlation insights
                titles = [i.get('title', '') for i in insights[:3] if i.get('title')]
                summary = f"[Correlation] {stored} insight(s): " + "; ".join(titles)
                # Attribute which LLM produced these insights so operators can
                # tell whether they came from the cheap local model or a paid
                # fallback (cost attribution + debugging when models disagree).
                summary = f"{summary}\n_Generated by: {provider_type}/{model}_"
                for notif in self.notifications:
                    success = False
                    error_msg = None
                    try:
                        notif.send(summary, severity='info')
                        success = True
                    except Exception as e:
                        error_msg = str(e)
                        logger.warning(f"Correlation notification failed: {e}")
                    try:
                        channel_type = getattr(notif, 'channel_type', 'slack')
                        self.kb._kb.record_notification_history(
                            channel_id=0,
                            channel_type=channel_type,
                            severity='info',
                            title=summary[:200],
                            message=summary,
                            success=success,
                            context={'insights_count': stored},
                            error_message=error_msg
                        )
                    except Exception as e:
                        logger.debug(f"Could not record notification history: {e}")
            else:
                logger.info(f"Correlation analysis: LLM returned {len(insights)} insights (0 stored)")

        except json.JSONDecodeError as e:
            logger.warning(f"Correlation analysis LLM response not valid JSON: {e}")
        except Exception as e:
            logger.warning(f"Correlation analysis LLM call failed: {e}")

    def _get_infra_summary(self) -> str:
        """Build a concise summary of the infrastructure from config for LLM context."""
        hosts = self.config.get('infrastructure', {}).get('hosts', {})
        lines = []
        for name, info in hosts.items():
            addr = info.get('address', '?')
            role = info.get('role', '?')
            services = [s.get('name', '?') for s in info.get('services', [])]
            lines.append(f"  {name} ({addr}, {role}): {', '.join(services)}")

        if lines:
            summary = "Infrastructure hosts:\n" + "\n".join(lines)
        else:
            # No static inventory is the normal case for a fresh install: the
            # fleet is discovered from the cluster and from Prometheus instead.
            # Saying so beats emitting an empty "Infrastructure hosts:" header,
            # which reads to the model as "there are no hosts".
            summary = (
                "Infrastructure hosts: not statically configured — discover them "
                "from the live cluster and from Prometheus targets."
            )

        # Append active container runtimes
        if self.containers and hasattr(self.containers, 'runtime_names'):
            runtimes = ', '.join(self.containers.runtime_names)
            summary += f"\nContainer runtimes: {runtimes}."
            if 'kubernetes' in runtimes:
                summary += " Use k8s_* tools for pods/deployments and k8s_get_events for recent BackOff/readiness failures."
                k8s_summary = self._get_k8s_observation_summary()
                if k8s_summary:
                    summary += f"\n{k8s_summary}"

        return summary

    def _get_k8s_observation_summary(self) -> str:
        """Summarize recent Kubernetes signals so recovered failures remain visible to sweeps."""
        if not getattr(self.tools, 'k8s_tools', None):
            return ""

        lines = []

        try:
            ns_result = self.tools.k8s_tools.get_namespaces()
            if ns_result.get('success') and ns_result.get('namespaces'):
                namespace_names = [n.get('name') for n in ns_result['namespaces'] if n.get('name')]
                if namespace_names:
                    lines.append(f"Kubernetes namespaces: {', '.join(namespace_names)}")
        except Exception as e:
            logger.debug(f"Could not summarize Kubernetes namespaces: {e}")

        try:
            events_result = self.tools.k8s_tools.get_events(all_namespaces=True)
            if events_result.get('success') and events_result.get('events'):
                warning_events = [e for e in events_result['events'] if e.get('type') == 'Warning']
                if warning_events:
                    lines.append(
                        "Recent Kubernetes warning events (important: a pod can be Running now but still have recent BackOff/Unhealthy history):"
                    )
                    for event in warning_events[-8:]:
                        obj = event.get('object', 'unknown')
                        reason = event.get('reason', 'unknown')
                        message = str(event.get('message', '')).replace('\n', ' ').strip()
                        if len(message) > 180:
                            message = message[:177] + '...'
                        lines.append(f"  - {obj}: {reason} — {message}")
        except Exception as e:
            logger.debug(f"Could not summarize Kubernetes events: {e}")

        return "\n".join(lines)

    def _build_sweep_system_prompt(self, task: str, skill_name: Optional[str] = None) -> str:
        """Shared system prompt for sweep phases. Rules here apply to every phase.

        The 'no positive observations' rule is load-bearing — without it, models
        emit "[INFO] All nodes Ready" / "No errors in container X" lines as
        findings, which produces notification noise even when nothing is wrong.

        When `skill_name` names a loaded skill, that skill's procedure is
        injected as the step-by-step playbook for the phase. Sweep phases left
        to improvise re-list cluster state dozens of times and over-fetch logs;
        an explicit ordered procedure keeps the investigation bounded.
        """
        infra = self._get_infra_summary()

        procedure = ""
        skill = (self.skills or {}).get(skill_name) if skill_name else None
        if skill:
            procedure = f"""

PROCEDURE — follow these steps in order, and do not repeat a step whose data you already have:
{skill['instructions']}
"""
        elif skill_name:
            logger.warning(f"Sweep requested skill '{skill_name}' but it is not loaded")

        return f"""You are CFOperator performing a proactive infrastructure sweep.

{infra}

{task}
{procedure}

A "finding" is a problem that requires attention or action. Healthy state, "no errors found", "all nodes Ready", "no warnings in container X", and similar status statements are NOT findings — they are the expected default. If a sweep phase finds nothing wrong, the correct response is the empty array [].

Severity rules:
- "critical": active outage, data loss risk, security breach, or imminent failure.
- "warning": real degradation, recoverable failure, or risk that warrants action soon.
- "info": ONLY for genuine actionable observations the operator should know about (e.g. a deprecated config still in use, an unusual but non-failing pattern). Do NOT emit "info" for healthy state, absence of errors, or "everything looks fine" reports.

After investigating, respond with your findings as a JSON array:
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "exact tool output or data supporting this finding", "remediation": "suggested fix or action"}}]

The "evidence" field is REQUIRED — paste the specific metric value, log line, container name, or tool output that proves the finding. Do not make claims without evidence. If your evidence is "no problems detected" or "queries returned no errors", do NOT emit a finding — return [] for that phase instead.

If everything looks healthy, return an empty array: []
Only return the JSON array, no other text."""

    def _sweep_with_llm(self, task: str, max_iterations: int = None,
                        skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase. The LLM gets the task description,
        infrastructure context, and access to all tools (prometheus_query,
        loki_query, docker_list, ssh_execute, etc).

        `skill_name` optionally injects a loaded skill's procedure as the
        ordered playbook for the phase (see _build_sweep_system_prompt).

        Returns list of findings: [{'severity': ..., 'finding': ...}]
        """
        if max_iterations is None:
            max_iterations = self._get_sweep_max_iterations()

        # Check for sweep-specific backend/model override (DB settings)
        sweep_backend = self.kb.get_setting('sweep_backend', '')
        sweep_model = self.kb.get_setting('sweep_model', '')

        system_prompt = self._build_sweep_system_prompt(task, skill_name=skill_name)

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': task}],
                system_context=system_prompt,
                backend=sweep_backend or 'auto',
                model=sweep_model or None,
                max_iterations=max_iterations,
            )

            provider_type = result.get('backend', 'unknown')
            model = result.get('model', 'unknown')
            response_text = result.get('response', '')
            tool_calls = result.get('tool_calls', 0)
            input_tokens = result.get('input_tokens', 0)
            output_tokens = result.get('output_tokens', 0)
            cached_hits = result.get('cached_tool_hits', 0)
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
                f"{cached_hits} cached | "
                f"{len(response_text)} chars | "
                f"tokens: {input_tokens}in/{output_tokens}out"
            )

            # Parse findings from response
            return self._parse_sweep_findings(response_text)

        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                logger.warning("No LLM provider available for sweep — skipping")
                return []
            logger.error(f"Sweep LLM failed: {e}")
            ERROR_RATE.inc()
            return []
        except Exception as e:
            # All providers in the fallback chain exhausted with this exception.
            logger.error(f"Sweep LLM failed (all providers exhausted): {e}")
            ERROR_RATE.inc()
            return []

    # Patterns that indicate the LLM is reporting its own tool failures, not real
    # infrastructure issues.  Case-insensitive substring match on finding text.
    _SELF_REFERENTIAL_PATTERNS = [
        'unable to query',
        'could not query',
        'failed to query',
        'syntax error',
        'query syntax is invalid',
        'no logs could be retrieved',
        'loki query parser is failing',
        'literal not terminated',
        'could not retrieve logs',
        'unable to retrieve logs',
        'query failed due to',
        'logql query error',
        'errors prevent log analysis',
        'prevent log retrieval',
        'invalid logql',
        'logql queries',
        'log aggregation fail',
        'monitoring system is compromised',
        'monitoring tools',
        'query configuration',
    ]

    def _is_self_referential(self, finding_text: str) -> bool:
        """Return True if a finding is about the agent's own tool failures."""
        lower = finding_text.lower()
        return any(p in lower for p in self._SELF_REFERENTIAL_PATTERNS)

    def _parse_sweep_findings(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse LLM response into structured findings."""
        # Try to extract JSON array from the response
        text = response_text.strip()

        # Find JSON array in the response (may be wrapped in markdown code blocks)
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            try:
                findings = json.loads(json_match.group())
                if isinstance(findings, list):
                    # Validate each finding has required keys
                    valid = []
                    # Patterns that indicate tool errors, not infrastructure issues
                    tool_error_patterns = [
                        'not found', 'failed with',
                        'returned empty', 'no such', 'could not find',
                    ]
                    for f in findings:
                        if isinstance(f, dict) and 'finding' in f:
                            finding_text = str(f['finding'])
                            evidence_text = str(f.get('evidence', ''))
                            if self._is_self_referential(finding_text):
                                logger.info(f"Filtered self-referential finding: {finding_text[:120]}")
                                continue
                            # Filter findings with no evidence — likely hallucinated
                            if not evidence_text.strip():
                                logger.info(f"Filtered no-evidence finding: {finding_text[:120]}")
                                continue
                            # Filter findings that are tool/query errors, not real issues
                            finding_lower = finding_text.lower()
                            if any(p in finding_lower for p in tool_error_patterns):
                                logger.info(f"Filtered tool-error finding: {finding_text[:120]}")
                                continue
                            parsed = {
                                'severity': f.get('severity', 'info'),
                                'finding': finding_text
                            }
                            if evidence_text.strip():
                                parsed['evidence'] = evidence_text
                            if f.get('remediation'):
                                parsed['remediation'] = str(f['remediation'])
                            valid.append(parsed)
                    return valid
            except json.JSONDecodeError:
                pass

        # If JSON parsing failed but response has content, treat it as a single info finding
        # Filter out iteration-limit messages and self-referential tool failures
        if text and text != '[]' and 'Maximum tool iterations' not in text:
            if not self._is_self_referential(text):
                return [{'severity': 'info', 'finding': text[:500]}]

        return []

    def _sweep_metrics(self) -> List[Dict[str, Any]]:
        """Sweep metrics across the infrastructure using LLM analysis."""
        logger.info("Starting LLM-driven metric sweep")
        return self._sweep_with_llm(
            "Check the health of all infrastructure hosts and services by examining metrics. "
            "Look at resource usage, scrape targets, container health, and anything that looks off."
        )

    def _sweep_logs(self) -> List[Dict[str, Any]]:
        """Sweep logs across all services using LLM pattern detection."""
        logger.info("Starting LLM-driven log sweep")
        return self._sweep_with_llm(
            "Check recent logs across infrastructure services for errors, warnings, or concerning patterns. "
            "Use loki_query with correct LogQL syntax. "
            "CORRECT examples: "
            '(1) {namespace="apps"} |= "error"  '
            '(2) {namespace=~"apps|monitoring"} |~ "error|warning"  '
            '(3) {pod=~"cfoperator.*"} |= "error"  '
            '(4) {namespace="monitoring", container="prometheus"} |= "error".  '
            "Use =~ for multi-value matching. NEVER use || or -- between {} selectors. "
            "Each loki_query call must contain exactly ONE stream selector {}."
        )

    def _sweep_containers(self) -> List[Dict[str, Any]]:
        """Check all containers/pods across configured backends + LLM review."""
        findings = []
        containers = []

        # Determine active runtime names for LLM context
        runtime_label = "all configured backends"
        if hasattr(self.containers, 'runtime_names'):
            runtime_label = ', '.join(self.containers.runtime_names)

        # Direct container status check (fast, no LLM needed)
        try:
            containers = self.containers.list_containers()
            logger.info(f"Found {len(containers)} containers/pods across {runtime_label}")

            running_count = sum(1 for c in containers if c.get('status') == 'running')
            RUNNING_CONTAINERS.set(running_count)

            for container in containers:
                if container.get('status') != 'running':
                    findings.append({
                        'severity': 'warning',
                        'finding': f"{container['name']} on {container['host']}: status={container['status']}"
                    })

        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            ERROR_RATE.inc()

        # LLM review of container health
        container_summary = ""
        if containers:
            container_summary = f"\n\nCurrently running {running_count} of {len(containers)} containers/pods."
            stopped = [c for c in containers if c.get('status') != 'running']
            if stopped:
                container_summary += f"\nStopped/unhealthy: {', '.join(c['name'] for c in stopped)}"

        k8s_context = self._get_k8s_observation_summary()
        if k8s_context:
            container_summary += f"\n\n{k8s_context}"

        llm_findings = self._sweep_with_llm(
            f"Review workload health across the fleet (backends: {runtime_label}).{container_summary} "
            "Use k8s tools (k8s_get_pods, k8s_get_all_unhealthy, k8s_get_events) for Kubernetes workloads across apps, monitoring, data, iot, ai, infrastructure, and kube-system, "
            "loki_query for workload logs, prometheus_query for resource metrics, and ssh_list_services for bare-metal hosts. "
            "Do not rely only on current pod phase: recovered failures may appear only in recent Kubernetes warning events or Loki logs. "
            "Check for BackOff, Unhealthy/readiness failures, CrashLoopBackOff, and other issues. "
            "IMPORTANT: High restart counts alone are NOT findings if the pod is currently healthy and the last restart was hours/days ago. "
            "Only report restarts as issues if they are RECENT (last 2 hours) or ONGOING. Stale restart counts from past node reboots are normal. "
            "IMPORTANT: Identify workloads by their Deployment/StatefulSet/DaemonSet name, NOT by specific pod names. "
            "Pod names include random suffixes (e.g., -7b5b6c8d9f-xyz12) that change on every rollout. "
            "Never report a specific pod name as 'missing' — check the parent Deployment's ready replica count instead.",
            skill_name='k3s-cluster-health',
        )
        findings.extend(llm_findings)

        return findings

    def _sequential_sweep(self, sweep_config: dict) -> List[Dict[str, Any]]:
        """Run sweep phases sequentially (fallback when pool unavailable)."""
        from ollama_pool import SWEEP_DURATION
        start = time.time()
        findings = []

        if sweep_config.get('metrics') and self.metrics:
            logger.info("Sweeping metrics...")
            metric_findings = self._sweep_metrics()
            findings.extend(metric_findings)
            logger.info(f"Metric sweep found {len(metric_findings)} findings")

        if sweep_config.get('logs') and self.logs:
            logger.info("Sweeping logs...")
            log_findings = self._sweep_logs()
            findings.extend(log_findings)
            logger.info(f"Log sweep found {len(log_findings)} findings")

        if sweep_config.get('containers') and self.containers:
            logger.info("Sweeping containers...")
            container_findings = self._sweep_containers()
            findings.extend(container_findings)
            logger.info(f"Container sweep found {len(container_findings)} findings")

        SWEEP_DURATION.labels(mode='sequential').observe(time.time() - start)
        return findings

    def _sweep_with_llm_on_instance(self, task: str, url: str, model: str,
                                     max_iterations: int = None,
                                     skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Run an LLM-driven sweep phase on a specific Ollama instance.

        Like _sweep_with_llm() but takes explicit url/model from pool checkout
        instead of resolving via _resolve_provider().
        """
        if max_iterations is None:
            max_iterations = self._get_sweep_max_iterations()

        provider_type = 'ollama'
        system_prompt = self._build_sweep_system_prompt(task, skill_name=skill_name)

        try:
            result = self._chat_with_tools(
                provider_type=provider_type,
                url=url,
                model=model,
                messages=[{'role': 'user', 'content': task}],
                system_context=system_prompt,
                max_iterations=max_iterations
            )

            response_text = result.get('response', '')
            tool_calls = result.get('tool_calls', 0)
            input_tokens = result.get('input_tokens', 0)
            output_tokens = result.get('output_tokens', 0)
            cached_hits = result.get('cached_tool_hits', 0)
            hit_limit = tool_calls >= max_iterations
            logger.info(
                f"Sweep LLM completed: {provider_type}/{model}@{url} | "
                f"{tool_calls}/{max_iterations} tool calls{'(limit hit)' if hit_limit else ''} | "
                f"{cached_hits} cached | "
                f"{len(response_text)} chars | "
                f"tokens: {input_tokens}in/{output_tokens}out"
            )

            return self._parse_sweep_findings(response_text)

        except Exception as e:
            logger.error(f"Sweep LLM failed on {url}/{model}: {e}")
            ERROR_RATE.inc()
            return []

    def _check_baseline_drift(self) -> List[Dict[str, Any]]:
        """Compare expected infrastructure state to reality."""
        findings = []

        try:
            # Get expected services from config
            hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
            expected_services = {}
            for host_name, host_info in hosts_config.items():
                for svc in host_info.get('services', []):
                    container = svc.get('container')
                    if container:
                        expected_services.setdefault(host_name, []).append({
                            'name': svc['name'],
                            'container': container
                        })

            # Get actually-running containers
            actual_containers = {}
            if self.containers:
                try:
                    for c in self.containers.list_containers():
                        host = c.get('host', 'unknown')
                        actual_containers.setdefault(host, set()).add(c['name'])
                except Exception as e:
                    logger.warning(f"Failed to list containers for drift check: {e}")

            # Compare expected vs actual
            has_docker_backend = any(
                c.get('backend') in ('docker', 'prometheus')
                for c in self._container_configs
            )
            for host_name, services in expected_services.items():
                host_info = hosts_config.get(host_name, {})
                host_addr = host_info.get('address', '')

                # Match Prometheus engine_host to config host by exact name or IP
                actual_names = set()
                for actual_host, containers in actual_containers.items():
                    if (actual_host == host_name or
                            actual_host == host_addr or
                            actual_host.split('.')[0] == host_name):
                        actual_names.update(containers)

                # If no data for this host, try SSH docker ps (only if a Docker-type backend is configured)
                if not actual_names and host_addr and has_docker_backend:
                    ssh_user = host_info.get('ssh', {}).get('user', 'sre')
                    try:
                        result = subprocess.run(
                            ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                             '-o', 'ConnectTimeout=5', f'{ssh_user}@{host_addr}',
                             'docker', 'ps', '--format', '{{.Names}}'],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            actual_names = {name.strip() for name in result.stdout.strip().split('\n') if name.strip()}
                            logger.debug(f"Drift check: SSH fallback for {host_name} found {len(actual_names)} containers")
                    except Exception as e:
                        logger.debug(f"Drift check: SSH fallback failed for {host_name}: {e}")

                for svc in services:
                    container_name = svc['container']
                    if actual_names and container_name not in actual_names:
                        findings.append({
                            'severity': 'warning',
                            'finding': f"Expected service '{svc['name']}' (container: {container_name}) not found running on {host_name}"
                        })

            # Bootstrap/update baselines
            self._update_baselines(actual_containers)

        except Exception as e:
            logger.error(f"Error checking baseline drift: {e}")
            ERROR_RATE.inc()

        return findings

    def _update_baselines(self, actual_containers: Dict[str, set]):
        """Update stored baselines with current state."""
        # Ephemeral Job/CronJob pods churn by schedule — strip them so they
        # never enter the baseline or register as container_change drift (which
        # would surface as false "stopped"/co-failure findings).
        actual_containers = {
            host: {c for c in containers if not is_ephemeral_job_pod(c)}
            for host, containers in (actual_containers or {}).items()
        }
        try:
            stored = self.kb.get_baseline()

            if not stored:
                # No baselines yet — bootstrap from current state
                for host, containers in actual_containers.items():
                    self.kb.update_baseline(
                        service_name=f"host:{host}",
                        expected_state='running',
                        baseline_metrics={
                            'container_count': len(containers),
                            'containers': sorted(containers)
                        }
                    )
                if actual_containers:
                    logger.info(f"Bootstrapped baselines for {len(actual_containers)} hosts")
            else:
                # Compare to stored baselines and record drift
                for host, containers in actual_containers.items():
                    key = f"host:{host}"
                    baseline = stored.get(key, {})
                    if baseline:
                        old_containers = set(baseline.get('baseline_metrics', {}).get('containers', []))
                        new_containers = set(containers)
                        added = new_containers - old_containers
                        removed = old_containers - new_containers

                        if added or removed:
                            desc_parts = []
                            if added:
                                desc_parts.append(f"new: {', '.join(sorted(added))}")
                            if removed:
                                desc_parts.append(f"gone: {', '.join(sorted(removed))}")

                            self.kb.record_drift_event(
                                drift_type='container_change',
                                description=f"{host}: {'; '.join(desc_parts)}",
                                drift_details={
                                    'host': host,
                                    'added': sorted(added),
                                    'removed': sorted(removed),
                                    'current_count': len(containers)
                                }
                            )
                            # Update baseline to current state
                            self.kb.update_baseline(
                                service_name=key,
                                expected_state='running',
                                baseline_metrics={
                                    'container_count': len(containers),
                                    'containers': sorted(containers)
                                }
                            )
                            logger.info(f"Drift detected on {host}: {'; '.join(desc_parts)}")

        except Exception as e:
            logger.warning(f"Baseline update failed: {e}")

    def _consolidate_learnings(self):
        """Periodically consolidate similar learnings by deprecating duplicates."""
        try:
            learnings = self.kb.find_learnings(limit=100)
            if len(learnings) < 10:
                return  # Not enough to consolidate
            logger.info(f"Consolidating {len(learnings)} learnings...")
            # Group by title similarity — deprecate exact title duplicates
            seen_titles = {}
            deprecated_count = 0
            for l in learnings:
                title_key = l['title'].lower().strip()
                if title_key in seen_titles:
                    self.kb._kb.deprecate_learning(l['id'])  # No resilient wrapper needed
                    deprecated_count += 1
                else:
                    seen_titles[title_key] = l['id']
            if deprecated_count:
                logger.info(f"Deprecated {deprecated_count} duplicate learnings")
        except Exception as e:
            logger.warning(f"Learning consolidation failed: {e}")

    def _extract_learnings(self, inv_id: int, trigger: str, findings: Dict[str, Any]):
        """Extract structured learnings from a resolved investigation using LLM."""
        import requests as req

        try:
            resolved = self._resolve_provider()
            if not resolved:
                logger.warning("No LLM provider available for learning extraction")
                return

            provider_type, url, model = resolved

            prompt = f"""Analyze this resolved infrastructure investigation and extract 1-3 reusable learnings.

Investigation trigger: {trigger}
Findings: {json.dumps(findings, default=str)[:2000]}

Return ONLY valid JSON in this exact format:
{{"learnings": [
  {{
    "learning_type": "solution",
    "title": "Brief title (max 100 chars)",
    "description": "What was learned and how it was resolved",
    "applies_when": "The concrete, observable condition that should make a future investigation recall this (e.g. 'pod faster-whisper is OOMKilled', 'cfoperator restart alert with no matching k8s event'). REQUIRED.",
    "services": ["service1"],
    "tags": ["tag1", "tag2"],
    "category": "resource"
  }}
]}}

learning_type must be one of: solution, pattern, root_cause, antipattern, insight
category must be one of: resource, network, config, dependency
Keep learnings specific and actionable. Only extract a learning if there is genuine,
reusable insight — a root cause, a fix, or a non-obvious gotcha. Do NOT extract a
learning that just restates "X was healthy" or describes a one-off transient blip.
Every learning MUST have a non-empty, specific `applies_when`; omit any you cannot
write a real trigger condition for. Return {{"learnings": []}} if nothing qualifies."""

            messages = [
                {'role': 'system', 'content': 'You are a structured data extractor. Return ONLY valid JSON.'},
                {'role': 'user', 'content': prompt}
            ]

            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.3,
                    'format': 'json'
                }
                resp = req.post(f"{url}/api/chat", json=payload, timeout=self.llm_timeout)
                data = resp.json()
                text = data.get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                if not api_key:
                    key_env = OPENAI_COMPAT_PROVIDERS[provider_type]['key_env']
                    logger.warning(f"{key_env} not set for learning extraction")
                    return
                payload = {
                    'model': model,
                    'messages': messages,
                    'temperature': 0.3,
                    'max_tokens': 2048,
                    'response_format': {'type': 'json_object'}
                }
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
                resp = req.post(
                    endpoint,
                    json=payload, headers=headers, timeout=60
                )
                data = resp.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                if not api_key:
                    logger.warning("ANTHROPIC_API_KEY not set for learning extraction")
                    return
                payload = {
                    'model': model,
                    'max_tokens': 2048,
                    'system': 'You are a structured data extractor. Return ONLY valid JSON.',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                }
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01'
                }
                resp = req.post(
                    'https://api.anthropic.com/v1/messages',
                    json=payload, headers=headers, timeout=60
                )
                data = resp.json()
                text = '\n'.join(
                    b.get('text', '') for b in data.get('content', [])
                    if b.get('type') == 'text'
                )
            else:
                logger.warning(f"Learning extraction not implemented for {provider_type}")
                return

            # Parse JSON response
            result = json.loads(text)
            learnings = result.get('learnings', [])

            stored = 0
            skipped = 0
            for learning_data in learnings[:3]:  # Cap at 3
                learning_data['investigation_id'] = inv_id
                if not learning_data.get('learning_type') or not learning_data.get('title'):
                    continue
                if not learning_has_trigger_condition(learning_data):
                    skipped += 1
                    logger.info(f"Skipping extracted learning without applies_when: {learning_data.get('title','')[:60]}")
                    continue
                try:
                    lid = self.kb.store_learning(learning_data)
                    stored += 1
                    logger.info(f"Learning extracted: [{learning_data['learning_type']}] {learning_data['title'][:60]}")
                    # Generate embedding for the learning
                    if lid and lid > 0:
                        search_text = ' '.join(filter(None, [
                            learning_data.get('title', ''),
                            learning_data.get('description', ''),
                            learning_data.get('applies_when', ''),
                        ]))
                        self._embed_learning(lid, search_text)
                except Exception as e:
                    logger.warning(f"Failed to store learning: {e}")

            if stored:
                logger.info(f"Extracted {stored} learnings from investigation #{inv_id}")

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse learning extraction response: {e}")
        except Exception as e:
            logger.warning(f"Learning extraction failed for investigation #{inv_id}: {e}")

    def _embed_investigation(self, inv_id: int, trigger: str, findings: Dict[str, Any], outcome: str):
        """Generate and store embedding for a completed investigation."""
        try:
            if not self.embeddings.is_available():
                return

            investigation_data = {
                'trigger': trigger,
                'findings': findings,
                'outcome': outcome
            }
            embedding_text = self.embeddings.create_investigation_text(investigation_data)
            if not embedding_text or len(embedding_text) < 10:
                return

            embedding = self.embeddings.generate_embedding(embedding_text)
            if not embedding:
                return

            self.kb._kb.store_investigation_embedding(
                investigation_id=inv_id,
                embedding=embedding,
                embedding_model=self.embeddings.model,
                embedding_text=embedding_text
            )
            logger.info(f"Embedding stored for investigation #{inv_id}")
            EMBEDDING_REQUESTS.labels(result='success').inc()
        except Exception as e:
            logger.warning(f"Embedding generation failed for investigation #{inv_id}: {e}")
            EMBEDDING_REQUESTS.labels(result='error').inc()

    def _embed_learning(self, learning_id: int, search_text: str):
        """Generate and store embedding for a learning."""
        try:
            if not self.embeddings.is_available():
                return

            embedding = self.embeddings.generate_embedding(search_text)
            if not embedding:
                return

            from sqlalchemy import text as sql_text
            embedding_str = vector_literal(embedding)
            with self.kb._kb.session_scope() as session:
                session.execute(sql_text("""
                    UPDATE investigation_learnings
                    SET embedding_hash = :hash
                    WHERE id = :lid
                """), {'hash': hashlib.md5(search_text.encode()).hexdigest(), 'lid': learning_id})
                # Store in embedding cache for retrieval during search
                session.execute(sql_text("""
                    INSERT INTO learning_embeddings (learning_id, embedding, embedding_model, embedding_text)
                    VALUES (:lid, :embedding, :model, :text)
                    ON CONFLICT (learning_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_text = EXCLUDED.embedding_text
                """), {
                    'lid': learning_id,
                    'embedding': embedding_str,
                    'model': self.embeddings.model,
                    'text': search_text
                })
                session.commit()
            logger.info(f"Embedding stored for learning #{learning_id}")
        except Exception as e:
            logger.debug(f"Learning embedding failed for #{learning_id}: {e}")

    @staticmethod
    def _finding_key(finding: Dict[str, Any]) -> str:
        """Produce a stable key for dedup by stripping variable parts (numbers, timestamps)."""
        import re
        text = finding.get('finding', '')
        # Strip numbers (counts, ports, timestamps change across sweeps)
        text = re.sub(r'\d+', '#', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip().lower()
        # Take first 120 chars — enough to identify the issue
        return text[:120]

    def _dedup_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate findings across sweep phases.

        When multiple phases report the same issue, keep the one with highest severity.
        """
        severity_rank = {'critical': 3, 'warning': 2, 'info': 1}
        seen = {}  # key -> finding
        for f in findings:
            key = self._finding_key(f)
            existing = seen.get(key)
            if not existing or severity_rank.get(f.get('severity', 'info'), 0) > severity_rank.get(existing.get('severity', 'info'), 0):
                seen[key] = f
        deduped = list(seen.values())
        if len(deduped) < len(findings):
            logger.info(f"Deduplicated {len(findings)} findings to {len(deduped)}")
        return deduped

    # Tokens that look like real workload identifiers but are too generic to
    # match safely against pod/deployment names. Kept narrow on purpose — only
    # words the sweep prompts themselves use as scaffolding, not domain nouns.
    _GROUND_TRUTH_STOPWORDS = frozenset({
        'active', 'apparent', 'apps', 'cluster', 'config', 'configuration',
        'container', 'containers', 'control', 'critical', 'data', 'default',
        'degraded', 'deploy', 'deployed', 'deployment', 'docker', 'evidence',
        'expectations', 'expected', 'failed', 'failing', 'feature', 'finding',
        'found', 'health', 'healthy', 'history', 'image', 'images',
        'infrastructure', 'ingress', 'ingresses', 'install', 'installed',
        'instance', 'issue', 'issues', 'kubelet', 'logs', 'master', 'masters',
        'memory', 'metric', 'metrics', 'missing', 'monitoring', 'name',
        'namespace', 'namespaces', 'network', 'node', 'nodes', 'normal',
        'operator', 'overall', 'plane', 'pod', 'pods', 'pressure', 'primary',
        'production', 'project', 'prometheus', 'ready', 'related', 'remediation',
        'report', 'restart', 'restarts', 'running', 'scrape', 'service',
        'services', 'severity', 'should', 'stability', 'stable', 'status',
        'storage', 'system', 'systems', 'target', 'targets', 'unhealthy',
        'unstable', 'verify', 'warning', 'workload', 'workloads',
    })

    _MISSING_KEYWORDS = (
        'not installed', 'not running', 'not present', 'not deployed',
        'no active', 'no such', 'does not have', "doesn't have",
        'is missing', 'are missing', 'not found',
    )

    _NODE_HEALTH_KEYWORDS = (
        'kubelet', 'service issue', 'service is not', 'service not',
        'unhealthy', 'unstable', 'pressure', 'degraded', 'stability',
        'not running', 'not ready', 'notready', 'ready condition',
        'status=false', 'status="false"', 'condition false', 'down',
    )

    # Pattern 3: metrics sweep reads an empty prometheus_query result and
    # concludes the workload is not being scraped, even though the pod/service
    # exists and is almost certainly a Prometheus target.
    _SCRAPE_TARGET_KEYWORDS = (
        'not scraping', 'not being scraped', 'no scrape target', 'missing scrape target',
        'no metrics for', 'not reporting metrics', 'no active scrape', 'no targets for',
    )

    # Pattern 4: sweep claims a node is absent/unregistered when it is present
    # in kubectl get nodes (metrics sweep may read a stale kube_node_info
    # series as evidence that the node no longer exists).
    _NODE_ABSENT_KEYWORDS = (
        'not in cluster', 'not joined', 'missing from cluster', 'not part of cluster',
        'node missing', 'node not present', 'node not found', 'not registered',
    )

    # Pattern 5: containers sweep uses k8s_get_ingresses (added 2026-04-30)
    # and reports a service as unexposed when the tool returns empty for a
    # name-mismatch query, even though a matching ingress exists.
    _EXPOSURE_KEYWORDS = (
        'not exposed', 'has no ingress', 'no ingress for', 'not publicly accessible',
        'no external access', 'not reachable externally', 'no ingress rule',
        'not accessible externally',
    )

    def _ground_truth_snapshot(self) -> Optional[Dict[str, Any]]:
        """Pull a single cluster snapshot used to disprove obvious false positives.

        Returns None if K8sTools isn't wired up (tests, partial bootstrap),
        which makes the suppressor a no-op.
        """
        k8s = getattr(getattr(self, 'tools', None), 'k8s_tools', None)
        if not k8s:
            return None

        snapshot: Dict[str, Any] = {'nodes': {}, 'workloads': set(), 'ingresses': set()}

        try:
            nodes_result = k8s.get_nodes()
            if nodes_result.get('success'):
                for n in nodes_result.get('nodes', []):
                    name = n.get('name')
                    if name:
                        snapshot['nodes'][name.lower()] = n
        except Exception as e:
            logger.debug(f"Ground truth: could not load nodes: {e}")

        try:
            # Single broad lookup covering everything a sweep might claim is "missing".
            result = k8s._run_kubectl(
                ['get',
                 'pods,deployments,daemonsets,statefulsets,cronjobs,jobs,services,ingresses',
                 '-A', '-o', 'name'],
                timeout=15,
            )
            if result.get('success'):
                for line in result.get('stdout', '').splitlines():
                    # Lines look like "pod/river-history-ingest-29625252-8ltx8"
                    if '/' in line:
                        kind, resource_name = line.split('/', 1)
                        resource_name = resource_name.strip().lower()
                        if resource_name:
                            snapshot['workloads'].add(resource_name)
                            if kind.strip().lower() == 'ingress':
                                snapshot['ingresses'].add(resource_name)
        except Exception as e:
            logger.debug(f"Ground truth: could not load workloads: {e}")

        return snapshot

    def _match_workload_in_text(self, text: str,
                                snapshot: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Find a cluster workload named in ``text``.

        Returns the matching (token, workload_name), or None. Tokens are the
        dashed and ≥4-char words of the text minus the stopword list, so a
        claim naming a real pod/deployment can be disproved by the snapshot.
        """
        tokens: set = set()
        tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
        tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
        tokens -= self._GROUND_TRUTH_STOPWORDS

        for token in tokens:
            if len(token) < 4:
                continue
            for workload in snapshot['workloads']:
                if token == workload or token in workload.split('-'):
                    return token, workload
        return None

    def _ground_truth_suppress(self,
                               finding: Dict[str, Any],
                               snapshot: Dict[str, Any]) -> Optional[str]:
        """Return a reason string if the cluster snapshot disproves the finding."""
        if not snapshot:
            return None

        text = (str(finding.get('finding', '')) + ' '
                + str(finding.get('evidence', ''))).lower()
        if not text.strip():
            return None

        # Pattern 1: claim asserts a node-level health/kubelet problem, but the
        # node is actually Ready with no pressure. k3s embeds the kubelet, so a
        # missing kubelet.service is expected and not a real finding.
        for node_name, node in snapshot['nodes'].items():
            if node_name in text and any(k in text for k in self._NODE_HEALTH_KEYWORDS):
                ready = node.get('ready') == 'True'
                mem_ok = node.get('memoryPressure') in ('False', 'Unknown', None)
                disk_ok = node.get('diskPressure') in ('False', 'Unknown', None)
                if ready and mem_ok and disk_ok:
                    return (
                        f"node {node_name} reports Ready=True with no pressure "
                        f"(kubelet {node.get('kubeletVersion','?')}); "
                        f"k3s embeds the kubelet so a standalone kubelet.service is expected to be absent"
                    )

        # Pattern 2: claim asserts a workload is missing, but a matching pod /
        # deployment / cronjob / service / ingress exists in the cluster.
        if any(k in text for k in self._MISSING_KEYWORDS):
            matched = self._match_workload_in_text(text, snapshot)
            if matched:
                token, workload = matched
                return (
                    f"workload matching '{token}' exists in cluster "
                    f"({workload})"
                )

        # Pattern 3: claim asserts a workload is not being scraped by Prometheus
        # or has no metrics, but the named pod/service exists. The metrics sweep
        # commonly reads an empty prometheus_query result as "target absent"
        # rather than "series has no recent data".
        if any(k in text for k in self._SCRAPE_TARGET_KEYWORDS):
            matched = self._match_workload_in_text(text, snapshot)
            if matched:
                token, workload = matched
                return (
                    f"workload matching '{token}' exists in cluster "
                    f"({workload}); an empty prometheus_query result does not confirm the target is absent"
                )

        # Pattern 4: claim asserts a node is absent from / not registered in
        # the cluster, but the node appears in the snapshot. The metrics sweep
        # may misread a stale kube_node_info series as "node missing".
        if any(k in text for k in self._NODE_ABSENT_KEYWORDS):
            for node_name in snapshot['nodes']:
                if node_name in text:
                    return (
                        f"node '{node_name}' is present in the cluster "
                        f"(confirmed in kubectl get nodes snapshot)"
                    )

        # Pattern 5: claim asserts a service has no ingress / is not externally
        # accessible, but a matching Ingress resource exists. Triggered by
        # k8s_get_ingresses returning empty on a name-mismatch query, causing
        # the sweep to conclude the service is unexposed. Only fires on ingress
        # name matches (not pods/services) to avoid over-suppression.
        if any(k in text for k in self._EXPOSURE_KEYWORDS):
            tokens = set()
            tokens.update(re.findall(r'\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b', text))
            tokens.update(re.findall(r'\b[a-z]{4,}\b', text))
            tokens -= self._GROUND_TRUTH_STOPWORDS

            for token in tokens:
                if len(token) < 4:
                    continue
                for ingress_name in snapshot.get('ingresses', set()):
                    if token == ingress_name or token in ingress_name.split('-'):
                        return (
                            f"ingress matching '{token}' exists in cluster "
                            f"({ingress_name}); service exposure claim is likely a false positive"
                        )

        # Pattern 6: a "container restarted N times" finding where the pod is
        # healthy now with few restarts is recovered noise, not a real finding.
        # (The sweep-path analogue of the Tier-1 alert filter.)
        noise_cfg = (self.config.get('ooda', {}) or {}).get('noise', {}) if isinstance(self.config, dict) else {}
        if noise_cfg.get('enabled', True):
            reason = self._restart_finding_is_noise(text, int(noise_cfg.get('recovered_restart_threshold', 3)))
            if reason:
                return reason

        return None

    def _verify_single_finding(self,
                               finding: Dict[str, Any],
                               max_iterations: int) -> Optional[Dict[str, Any]]:
        """Actively try to disprove a finding before allowing it to be emitted."""
        infra = self._get_infra_summary()
        system_prompt = f"""You are a strict verification agent for infrastructure monitoring findings.

{infra}

Your job is to try to DISPROVE a drafted finding before it is emitted.

Verification procedure:
1. Read the drafted finding and its current evidence.
2. Identify the strongest counter-hypothesis that would make the finding false.
3. Use the available tools to test that counter-hypothesis before deciding. You MUST make at least one tool call before your final answer.
4. Keep the finding only if the fresh tool results still support it.

Rules:
- Prefer direct disproof queries over repeating the original evidence.
- Verify exact Kubernetes namespace, pod, service, ingress, deployment, and container names before trusting a claim.
- For missing exposure or routing claims, inspect Services and Ingresses in the relevant namespace before keeping the finding.
- For log-absence or missing-container claims, resolve the real pod/container identity first, then inspect logs or pod status.
- If the fresh query disproves the claim, if names do not match, if support is ambiguous, or if you cannot verify confidently, return [].
- Never report tool/query failures as findings.

Return ONLY a JSON array:
[]
or
[{{"severity": "info|warning|critical", "finding": "description", "evidence": "fresh evidence from verification", "remediation": "suggested fix or action"}}]

Only return the JSON array, no other text."""

        user_msg = (
            "Actively verify this drafted finding before it can be emitted. "
            "Try to falsify it with fresh tool queries, then return [] if it does not survive verification.\n\n"
            f"Draft finding JSON:\n{json.dumps(finding, default=str)}"
        )

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_msg}],
                system_context=system_prompt,
                max_iterations=max_iterations,
            )
        except Exception as e:
            logger.warning(f"Verification skipped (LLM unavailable): {e}")
            return finding  # don't filter on a failed verification step

        tool_calls = result.get('tool_calls', 0)
        if tool_calls <= 0:
            logger.info(f"Verification dropped finding with no fresh checks: {finding.get('finding', '')[:150]}")
            return None

        verified = self._parse_sweep_findings(result.get('response', ''))
        if not verified:
            return None

        verified_finding = verified[0]
        if not verified_finding.get('remediation') and finding.get('remediation'):
            verified_finding['remediation'] = finding['remediation']
        return verified_finding

    def _verify_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Active verification pass to filter hallucinated or unsupported findings.

        Re-checks each finding individually with tool access and asks the model
        to actively look for the strongest disconfirming signal before keeping it.
        Graceful degradation: returns original findings if verification fails.
        """
        if not findings:
            return findings

        # Stage 1: deterministic ground-truth suppressor. Cheap, cluster-state
        # based, and catches the common LLM hallucinations (k3s embeds-kubelet,
        # CronJob workloads claimed missing). Skipped silently when K8sTools
        # isn't available (e.g. tests).
        snapshot = self._ground_truth_snapshot()
        if snapshot:
            survivors = []
            for f in findings:
                reason = self._ground_truth_suppress(f, snapshot)
                if reason:
                    logger.info(
                        f"Ground-truth suppressed: {str(f.get('finding',''))[:140]} — {reason}"
                    )
                    continue
                survivors.append(f)
            suppressed = len(findings) - len(survivors)
            if suppressed:
                logger.info(
                    f"Ground-truth filter: {len(findings)} → {len(survivors)} ({suppressed} suppressed)"
                )
            findings = survivors
            if not findings:
                return findings

        max_iterations = max(2, min(4, self._get_max_tool_iterations()))

        try:
            verified = []
            for finding in findings:
                verified_finding = self._verify_single_finding(
                    finding=finding,
                    max_iterations=max_iterations,
                )
                if verified_finding:
                    verified.append(verified_finding)

            removed = len(findings) - len(verified)
            logger.info(f"Finding verification: {len(findings)} → {len(verified)} ({removed} filtered)")

            if removed > 0:
                # Log which findings were filtered
                verified_texts = {v['finding'] for v in verified}
                for f in findings:
                    if f['finding'] not in verified_texts:
                        logger.info(f"Judge filtered: {f['finding'][:150]}")

            return verified

        except Exception as e:
            logger.warning(f"Finding verification failed, returning unfiltered: {e}")
            return findings

    def _get_new_findings(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return only findings that weren't in the previous sweep report."""
        try:
            prev_reports = self.kb.get_recent_sweep_reports(limit=1)
            if not prev_reports:
                return findings  # First sweep — everything is new
            prev_keys = {self._finding_key(f) for f in prev_reports[0].get('findings', [])}
            new = [f for f in findings if self._finding_key(f) not in prev_keys]
            return new
        except Exception as e:
            logger.debug(f"Could not check previous sweep for dedup: {e}")
            return findings  # On error, notify for everything

    def _get_resolved_findings(self, current_findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return findings present in the previous sweep that are now gone.

        Used to emit "Resolved: …" notifications so operators see clear
        outcomes, not just the firing edge. Returns empty on first sweep
        (no baseline to diff against) or when the previous-sweep lookup
        fails — both cases are safer than emitting bogus resolutions.
        """
        try:
            prev_reports = self.kb.get_recent_sweep_reports(limit=1)
            if not prev_reports:
                return []
            prev_findings = prev_reports[0].get('findings', []) or []
            current_keys = {self._finding_key(f) for f in current_findings}
            return [f for f in prev_findings if self._finding_key(f) not in current_keys]
        except Exception as e:
            logger.debug(f"Could not compute resolved findings: {e}")
            return []

    def _capture_metric_snapshot(self) -> Optional[Dict[str, Any]]:
        """Capture key cluster metrics for correlation baseline."""
        snapshot = {}
        try:
            if self.metrics:
                # Node resource usage
                cpu_result = self.metrics.query('100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle",job="node"}[5m])) * 100)')
                if cpu_result:
                    snapshot['node_cpu_percent'] = {r['metric'].get('instance', '?'): round(float(r['value'][1]), 1) for r in cpu_result}

                mem_result = self.metrics.query('(1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"}) * 100')
                if mem_result:
                    snapshot['node_memory_percent'] = {r['metric'].get('instance', '?'): round(float(r['value'][1]), 1) for r in mem_result}

                # Pod counts by phase
                phase_result = self.metrics.query('sum by (phase) (kube_pod_status_phase)')
                if phase_result:
                    snapshot['pod_phases'] = {r['metric'].get('phase', '?'): int(float(r['value'][1])) for r in phase_result}

                # Container restart total
                restart_result = self.metrics.query('sum(increase(kube_pod_container_status_restarts_total[30m]))')
                if restart_result:
                    snapshot['restarts_30m'] = round(float(restart_result[0]['value'][1]), 1)

        except Exception as e:
            logger.debug(f"Metric snapshot partial failure: {e}")

        return snapshot if snapshot else None

    def _generate_sweep_report(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate summary report from sweep findings."""
        max_severity = 'info'
        if any(f.get('severity') == 'critical' for f in findings):
            max_severity = 'critical'
        elif any(f.get('severity') == 'warning' for f in findings):
            max_severity = 'warning'

        summary = f"System sweep found {len(findings)} issues:\n"
        for f in findings:
            summary += f"- [{f.get('severity', 'info').upper()}] {f.get('finding', '')}"
            if f.get('remediation'):
                summary += f"\n  -> {f['remediation']}"
            summary += "\n"

        sweep_backend = self.kb.get_setting('sweep_backend', '')
        sweep_model = self.kb.get_setting('sweep_model', '')

        return {
            'timestamp': datetime.now(),
            'findings': findings,
            'summary': summary,
            'severity': max_severity,
            'sweep_meta': {
                'sweep_backend': sweep_backend or 'default',
                'sweep_model': sweep_model or 'default',
            }
        }

    def _post_findings_to_event_runtime(self, findings: List[Dict[str, Any]]) -> None:
        """Post sweep findings as alerts to the event runtime if configured."""
        url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if not url:
            return
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        endpoint = f"{url.rstrip('/')}/alert?mode=async"
        # Honor operator dismissals: don't re-post findings marked
        # acknowledged/false_positive — they'd otherwise recur every sweep.
        try:
            dismissed = self.kb._kb.get_dismissed_finding_keys()
        except Exception:
            dismissed = set()
        for finding in findings:
            summary_text = str(finding.get("finding") or finding.get("summary") or "").strip()
            fid = finding.get("id") or hashlib.md5(
                (finding.get("finding", "") + finding.get("sweep_phase", "")).encode()
            ).hexdigest()[:8]
            sig = 'sig::' + normalize_finding_signature(summary_text)
            if dismissed and (fid in dismissed or sig in dismissed
                              or (summary_text and summary_text in dismissed)):
                logger.info(f"Skipping dismissed finding (acknowledged/false_positive/known-noise): {summary_text[:80]}")
                continue
            severity = str(finding.get("severity") or "info").lower()
            if severity not in ("info", "warning", "critical"):
                severity = "warning"
            payload = {
                "source": "cfoperator-sweep",
                "severity": severity,
                "summary": str(finding.get("finding") or finding.get("summary") or "sweep finding"),
                "namespace": finding.get("namespace"),
                "resource_type": finding.get("resource_type"),
                "resource_name": finding.get("resource_name") or finding.get("resource"),
                "details": {
                    "category": finding.get("category"),
                    "remediation": finding.get("remediation"),
                    "evidence": finding.get("evidence"),
                    "sweep_source": finding.get("source"),
                },
            }
            body = json.dumps(payload, default=str).encode("utf-8")
            try:
                req = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=5) as resp:
                    resp.read()
            except (URLError, TimeoutError, OSError) as exc:
                logger.debug(f"Failed to post finding to event runtime: {exc}")
                return  # Stop trying on first failure

    def _post_resolutions_to_event_runtime(self, resolved: List[Dict[str, Any]]) -> None:
        """Post 'finding cleared' notifications to the event runtime.

        These ride the same /alert path as live findings but are tagged
        with ``details.resolution=True`` so:
          - run_triage short-circuits to action=notify (no LLM spend),
          - the Slack formatter renders ":white_check_mark: Resolved: …"
            instead of the "[severity]" prefix.
        Severity is forced to info — a resolution is by definition not
        a firing alert.
        """
        url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if not url or not resolved:
            return
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        endpoint = f"{url.rstrip('/')}/alert?mode=async"
        for finding in resolved:
            payload = {
                "source": "cfoperator-sweep",
                "severity": "info",
                "summary": str(finding.get("finding") or finding.get("summary") or "sweep finding"),
                "namespace": finding.get("namespace"),
                "resource_type": finding.get("resource_type"),
                "resource_name": finding.get("resource_name") or finding.get("resource"),
                "details": {
                    "resolution": True,
                    "requested_action": "notify",
                    "category": finding.get("category"),
                    "sweep_source": finding.get("source"),
                },
            }
            body = json.dumps(payload, default=str).encode("utf-8")
            try:
                req = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=5) as resp:
                    resp.read()
            except (URLError, TimeoutError, OSError) as exc:
                logger.debug(f"Failed to post resolution to event runtime: {exc}")
                return

    def _notify_sweep_findings(self, report: Dict[str, Any]):
        """Send notifications for sweep findings and record in history.

        When CFOP_EVENT_RUNTIME_URL is set, the event runtime is the sole
        owner of Slack/Discord for sweep findings: each finding is already
        forwarded to /alert by _post_findings_to_event_runtime and triaged
        individually, so emitting a roll-up here would produce duplicate
        (and lower-fidelity) Slack messages. We still record one
        notification_history row so audit/UI counters reflect that the
        sweep produced operator-visible output.
        """
        event_runtime_url = os.getenv("CFOP_EVENT_RUNTIME_URL", "").strip()
        if event_runtime_url:
            try:
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type='event-runtime',
                    severity=report['severity'],
                    title=report['summary'][:200],
                    message=report['summary'],
                    success=True,
                    context={
                        'findings_count': len(report.get('findings', [])),
                        'delegated_to': 'event_runtime',
                    },
                    error_message=None,
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")
            return

        for notif in self.notifications:
            success = False
            error_msg = None
            try:
                notif.send(report['summary'], severity=report['severity'])
                success = True
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending notification: {e}")

            # Record in notification_history
            try:
                channel_type = getattr(notif, 'channel_type', 'slack')
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type=channel_type,
                    severity=report['severity'],
                    title=report['summary'][:200],
                    message=report['summary'],
                    success=success,
                    context={'findings_count': len(report.get('findings', []))},
                    error_message=error_msg
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")

    def _get_alert_check_interval(self) -> int:
        """Get alert check interval: DB setting → config.yaml → default 10."""
        try:
            val = self.kb.get_setting('alert_check_interval', '')
            if val:
                return max(5, min(300, int(val)))
        except Exception as e:
            logger.debug(f"Invalid alert_check_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('alert_check_interval', 10)

    def _get_sweep_interval(self) -> int:
        """Get sweep interval: DB setting → config.yaml → default 1800."""
        try:
            val = self.kb.get_setting('sweep_interval', '')
            if val:
                return max(60, min(86400, int(val)))
        except Exception as e:
            logger.debug(f"Invalid sweep_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('sweep_interval', 1800)

    def _get_reap_interval(self) -> int:
        """Remediation reaper interval: DB setting → config.yaml → default 300."""
        try:
            val = self.kb.get_setting('remediation_reap_interval', '')
            if val:
                return max(60, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_reap_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_reap_interval_seconds', 300)

    def _get_drain_interval(self) -> int:
        """Remediation drainer interval: DB setting → config.yaml → default 60."""
        try:
            val = self.kb.get_setting('remediation_drain_interval', '')
            if val:
                return max(10, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_drain_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_drain_interval_seconds', 60)

    def _get_verify_interval(self) -> int:
        """Remediation PR-reconcile interval: DB setting → config.yaml → default 300."""
        try:
            val = self.kb.get_setting('remediation_verify_interval', '')
            if val:
                return max(30, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid remediation_verify_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('remediation_verify_interval_seconds', 300)

    def _format_heartbeat(self) -> str:
        """Build a one-line OODA heartbeat summary for periodic log emission.

        Single grep target ("OODA heartbeat:") plus the key fields an operator
        wants when checking "is the agent alive between events": uptime, the
        HTTP investigation queue depth (rising → worker saturated), minutes
        since last proactive sweep, and whether the reactive Alertmanager
        poll is on (true means the agent is independent of event_runtime).
        """
        uptime_min = (time.time() - self.start_time) / 60.0
        sweep_age = time.time() - self.last_sweep if self.last_sweep else None
        sweep_label = f"{sweep_age/60:.0f}m ago" if sweep_age is not None else "never"
        queue_depth = self._investigation_queue.qsize() if getattr(self, "_investigation_queue", None) else 0
        return (
            f"OODA heartbeat: uptime={uptime_min:.0f}m "
            f"queue_depth={queue_depth} "
            f"last_sweep={sweep_label} "
            f"reactive_poll={'on' if self._reactive_poll_enabled else 'off'}"
        )

    def _get_heartbeat_interval(self) -> int:
        """Get OODA-loop heartbeat interval: DB setting → config.yaml → default 300 (5 min).

        Heartbeats prove the loop is alive when nothing else is logging — with
        ``reactive_poll: false`` the agent can otherwise go silent for the
        full sweep interval (30 min by default) between HTTP investigations.
        """
        try:
            val = self.kb.get_setting('heartbeat_interval', '')
            if val:
                return max(30, min(3600, int(val)))
        except Exception as e:
            logger.debug(f"Invalid heartbeat_interval setting, using default: {e}")
        return self.config.get('ooda', {}).get('heartbeat_interval_seconds', 300)

    # Slash shortcut expansions — short commands that become natural-language
    # prompts rather than skills. One row per command carries the expansion
    # *and* what the console shows for it (args hint, one-line description):
    # the console used to keep its own copy of this metadata, and the two
    # drifted (CFOP-93). list_slash_commands() reads these rows.
    _SLASH_SHORTCUTS = {
        '/sweeps': {
            'prompt': 'Show me the recent sweep reports with findings summaries.',
            'args': '',
            'description': 'Recent sweep reports',
        },
        '/stats': {
            'prompt': 'Give me the operational summary for the last {0} hours.',
            'args': '[hours]',
            'description': 'Operational summary',
        },
        '/investigations': {
            'prompt': 'List recent investigations with their triggers and outcomes.',
            'args': '',
            'description': 'Recent investigations',
        },
        '/correlations': {
            'prompt': 'Show me correlated events and service failure patterns.',
            'args': '',
            'description': 'Service failure patterns',
        },
    }

    def _expand_slash_shortcut(self, message: str) -> str:
        """Expand slash shortcuts into natural language prompts.
        Returns the original message if not a shortcut."""
        if not message.startswith('/'):
            return message
        parts = message.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''
        shortcut = self._SLASH_SHORTCUTS.get(cmd)
        if shortcut:
            template = shortcut['prompt']
            if '{0}' in template and args:
                return template.format(args)
            elif '{0}' in template:
                return template.format('24')
            return template
        return message

    @staticmethod
    def _one_line_description(description: str) -> str:
        """The first sentence of a skill description.

        Frontmatter descriptions are written for a model choosing a skill: a
        paragraph of when-to-use plus a keyword list. A sidebar entry and an
        autocomplete row want one line, and the first sentence is the one
        that says what the skill does.
        """
        text = ' '.join((description or '').split())
        first = re.split(r'(?<=[.!?])\s', text, maxsplit=1)[0]
        return first.strip()

    def list_slash_commands(self) -> List[Dict[str, str]]:
        """Every ``/command`` the chat path recognises, for the console.

        Two sources, the same two the chat path dispatches on: the skills
        loaded from ``skills/*/SKILL.md`` (``_execute_skill``) and the
        shortcut expansions above. The console renders both its sidebar and
        its slash-autocomplete from this list, so a skill added server-side
        appears in both without the page changing. Nothing is hand-listed
        here — that was the drift CFOP-93 removed.
        """
        commands = []
        for name in sorted(self.skills or {}):
            skill = self.skills[name]
            commands.append({
                'command': f'/{name}',
                'name': name,
                'args': skill.get('args', ''),
                'description': self._one_line_description(skill.get('description', '')),
                'kind': 'skill',
            })
        for cmd, shortcut in self._SLASH_SHORTCUTS.items():
            commands.append({
                'command': cmd,
                'name': cmd[1:],
                'args': shortcut['args'],
                'description': shortcut['description'],
                'kind': 'shortcut',
            })
        return commands

    def _get_max_tool_iterations(self) -> int:
        """Get max tool iterations: DB setting → config.yaml → default 10."""
        try:
            val = self.kb.get_setting('max_tool_iterations', '')
            if val:
                return max(1, min(50, int(val)))
        except Exception as e:
            logger.debug(f"Invalid max_tool_iterations setting, using default: {e}")
        return self.config.get('chat', {}).get('max_tool_iterations', 10)

    def _get_sweep_max_iterations(self) -> int:
        """Iteration cap for sweep phases.

        Sweep phases are bounded data-gathering tasks, not open-ended chat — a
        handful of tool calls is enough to inspect metrics/logs/containers. The
        global `max_tool_iterations` (used for interactive chat) was letting
        sweep phases loop up to 50 times, re-ingesting tool output each turn and
        blowing up token cost. Keep this small and independent.
        """
        try:
            val = self.config.get('ooda', {}).get('sweep', {}).get('max_iterations')
            if val:
                return max(2, min(20, int(val)))
        except Exception as e:
            logger.debug(f"Invalid ooda.sweep.max_iterations config, using default: {e}")
        return 12

    def _max_tool_result_chars(self) -> int:
        """Per-tool-result size cap (chars) before it is appended to context.

        Untrimmed tool output (kubectl dumps, Loki log floods) is re-sent on
        every subsequent iteration, so a single fat result inflates every later
        turn. Cap each result; the model still sees the head plus a marker.
        """
        try:
            val = self.config.get('chat', {}).get('max_tool_result_chars')
            if val:
                return max(500, int(val))
        except Exception as e:
            logger.debug(f"Invalid chat.max_tool_result_chars config, using default: {e}")
        return 6000

    @staticmethod
    def _serialize_tool_result(result: Any, max_chars: int) -> str:
        """JSON-serialize a tool result, truncating to max_chars.

        Truncation keeps the head (most tools put the salient summary first) and
        appends an explicit marker so the model knows output was clipped rather
        than treating a cut-off payload as the whole picture.
        """
        text = json.dumps(result, default=str)
        if len(text) <= max_chars:
            return text
        omitted = len(text) - max_chars
        return text[:max_chars] + f'\n...[truncated {omitted} chars of tool output]'

    @staticmethod
    def _handle_empty_final(empty_nudge_sent: bool, iteration_budget: int,
                            max_iterations: int, full_messages: list,
                            provider_type: str, model: str):
        """The model ended the tool loop with an empty message — no tool
        calls, no text (gemma4:26b does this on nearly every healthy-cluster
        investigation; see benchmarks/empty_response_sim.py).

        First occurrence: append EMPTY_RESPONSE_NUDGE and grant one bonus
        round (the nudge recovered a well-formed answer 19/19 times in the
        benchmark). Second occurrence: raise EmptyLLMResponseError so the
        provider fallback chain rotates — never return '' to the caller,
        where _extract_status('') silently classifies it as 'monitoring'
        (investigations #1880/#1884/#1885/#1889).

        Both branches increment LLM_EMPTY_FINALS, labelled by provider/model,
        because until now the only trace an empty final left was the warning
        below — there was no way to answer "does gemma4 need two attempts on
        40% of investigations, or 4%?". This is the single chokepoint every
        provider branch funnels through, so counting here (rather than at the
        three call sites) is what keeps a provider from quietly stopping
        counting.

        Returns the updated (empty_nudge_sent, iteration_budget).
        """
        if empty_nudge_sent:
            LLM_EMPTY_FINALS.labels(provider=provider_type, model=model,
                                    disposition='exhausted').inc()
            raise EmptyLLMResponseError(
                f"{provider_type}/{model} returned an empty final response "
                f"even after the nudge retry")
        LLM_EMPTY_FINALS.labels(provider=provider_type, model=model,
                                disposition='nudged').inc()
        logger.warning(
            f"[CHAT] empty final response from {provider_type}/{model} — "
            f"nudging once for an answer")
        full_messages.append({'role': 'user', 'content': EMPTY_RESPONSE_NUDGE})
        return True, min(iteration_budget + 1, max_iterations + 1)

    # Read-only inspection tools whose result is stable enough to memoize for
    # the lifetime of one _chat_with_tools call. A repeated identical call
    # returns a short stub instead of re-running the tool and re-dumping the
    # payload — this is what stops sweep phases from re-listing pods/deployments
    # dozens of times. Mutating tools (ssh_execute) are deliberately excluded.
    _MEMOIZABLE_TOOLS = frozenset({
        'k8s_get_pods', 'k8s_get_nodes', 'k8s_get_deployments', 'k8s_get_events',
        'k8s_get_all_unhealthy', 'k8s_get_ingresses', 'k8s_get_services',
        'k8s_get_pod_status', 'k8s_get_pod_logs', 'loki_query', 'prometheus_query',
        'ssh_list_services', 'ping_host',
    })

    def _cached_tool_exec(self, tool_name: str, tool_args: dict, cache: dict,
                          max_chars: int):
        """Execute a tool, memoizing read-only inspection tools within one session.

        On a repeated identical (tool, args) call the tool is NOT re-run — a
        short stub is returned telling the model the result is unchanged and to
        reuse the earlier output. Caps the redundant re-fetching that bloats
        sweep phases. Returns (content_str, result_obj, was_cached).
        """
        key = None
        if tool_name in self._MEMOIZABLE_TOOLS:
            try:
                key = tool_name + '|' + json.dumps(tool_args, sort_keys=True, default=str)
            except Exception:
                key = None
        if key is not None and key in cache:
            prior = cache[key]
            stub = json.dumps({
                'cached': True,
                'note': (f"Identical {tool_name} call already made in this session — "
                         f"result is unchanged. Reuse the earlier {tool_name} output "
                         f"above instead of re-fetching."),
            })
            return stub, prior, True
        result = self.tools.execute(tool_name, tool_args)
        if key is not None:
            cache[key] = result
        return self._serialize_tool_result(result, max_chars), result, False

    @staticmethod
    def _parse_tool_arguments(raw_args) -> dict:
        """Normalize an LLM tool-call ``arguments`` field into a dict.

        Providers disagree: Ollama may send a dict or a JSON string, Groq
        always sends a string (sometimes empty).
        """
        if isinstance(raw_args, str):
            return json.loads(raw_args) if raw_args.strip() else {}
        return raw_args if raw_args else {}

    def _dispatch_tool_call(self, tool_name: str, tool_args: dict, *,
                            stats: '_ToolLoopStats', tool_cache: dict,
                            max_result_chars: int, iteration: int,
                            max_iterations: int, event_callback=None) -> str:
        """Execute one model-requested tool call and return its message content.

        Shared by every provider branch of the tool loop: streams the
        tool_call/tool_result events, memoizes read-only tools, tracks the
        loop counters plus consulted learning ids, and records the metric.
        """
        if event_callback:
            event_callback('tool_call', {
                'tool': tool_name,
                'args': tool_args,
                'iteration': iteration + 1,
                'max': max_iterations
            })

        try:
            content, result, was_cached = self._cached_tool_exec(
                tool_name, tool_args, tool_cache, max_result_chars)
        except Exception:
            # execute() itself does not raise (it returns {error: ...}), but a
            # serialize blow-up still has to hit result="error" or the metric
            # goes silent on the only path that actually crashed.
            TOOL_CALLS.labels(tool_name=tool_name, result='error').inc()
            raise
        stats.tool_calls += 1
        if was_cached:
            stats.cached_hits += 1
            logger.info(f"Tool result reused from cache: {tool_name}")
        else:
            logger.info(f"Executing tool: {tool_name}")

        if tool_name == 'find_learnings' and isinstance(result, list):
            stats.learning_ids.extend(r.get('id') for r in result if isinstance(r, dict) and r.get('id'))
        if tool_name == 'github_create_pr' and not was_cached:
            opened = _opened_pr_url(result)
            if opened:
                stats.opened_prs.append(opened)

        if event_callback:
            event_callback('tool_result', {
                'tool': tool_name,
                'result': json.dumps(result, default=str)[:500],
                'iteration': iteration + 1
            })

        TOOL_CALLS.labels(
            tool_name=tool_name,
            result=_tool_call_result_label(result),
        ).inc()
        return content

    @staticmethod
    def _to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert the internal (Ollama-shaped) message list to Anthropic format.

        Drops system messages (Anthropic takes ``system`` as a top-level
        param) and rewrites tool results as ``tool_result`` content blocks in
        a user message, preferring the stored ``tool_results`` array so
        parallel tool calls survive the round trip.
        """
        converted = []
        for m in messages:
            if m.get('role') == 'system':
                continue
            if m.get('role') == 'tool':
                if m.get('tool_results'):
                    converted.append({'role': 'user', 'content': m['tool_results']})
                else:
                    converted.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': m.get('tool_use_id', 'tool_0'),
                            'content': m.get('content', '')
                        }]
                    })
            elif m.get('role') == 'assistant' and isinstance(m.get('content'), list):
                converted.append(m)
            else:
                converted.append({
                    'role': m.get('role', 'user'),
                    'content': m.get('content', '')
                })
        return converted

    @staticmethod
    def _openai_compat_request_config(provider_type: str):
        """Resolve (api_key, chat_completions_url) for an OpenAI-compatible provider.

        Returns (None, None) for an unknown provider. The api_key may be '' if
        the provider's key env var is unset — callers check and raise.
        """
        cfg = OPENAI_COMPAT_PROVIDERS.get(provider_type)
        if not cfg:
            return None, None
        return os.getenv(cfg['key_env'], ''), cfg['base_url'].rstrip('/') + '/chat/completions'

    def _get_provider_chain(self, backend: str = 'auto', model: str = None) -> List[Tuple[str, str, str]]:
        """
        Get ordered list of providers to try for fallback.

        Returns providers in order: user-selected first, then fallbacks.
        Respects allow_paid_escalation setting for cloud providers.

        Args:
            backend: 'auto', 'ollama', 'groq', 'anthropic'
            model: Optional model override

        Returns:
            List of (provider_type, url, model) tuples to try in order
        """
        providers = []

        # First, add the selected/resolved provider
        primary = self._resolve_provider(backend, model)
        if primary:
            providers.append(primary)

        # Check if fallback is allowed
        allow_fallback = self.kb.get_setting('allow_paid_escalation', 'true')
        if allow_fallback == 'false':
            return providers

        # Define fallback order: ollama -> groq -> xai -> anthropic
        # Gemini (and DeepSeek, for the same reason) is deliberately ABSENT
        # here even though it is a registered provider and selectable by name. Adding it would have put it between
        # xAI and Anthropic for every INVESTIGATION fallback, so a paid
        # escalation that used to reach Opus would reach whatever the config's
        # gemini entry names instead. That is a quality change to the
        # investigation path, not the judge-gate change this was; the judge
        # reaches Gemini through _JUDGE_MODEL_FLOOR, which pins its own model.
        fallback_order = ['ollama', 'groq', 'xai', 'anthropic']

        # Add other providers as fallbacks (skip the primary)
        primary_type = primary[0] if primary else None
        for fb_type in fallback_order:
            if fb_type == primary_type:
                continue  # Skip primary - already added

            fb_provider = self._resolve_provider(fb_type, None)
            if fb_provider and fb_provider not in providers:
                # Verify the provider has required config (API keys, etc.)
                if fb_type in OPENAI_COMPAT_PROVIDERS and not os.getenv(
                        OPENAI_COMPAT_PROVIDERS[fb_type]['key_env']):
                    continue
                if fb_type == 'anthropic' and not os.getenv('ANTHROPIC_API_KEY'):
                    continue
                providers.append(fb_provider)

        return providers

    def _resolve_provider(self, backend: str = 'auto', model: str = None):
        """
        Resolve LLM provider from UI selection.

        Centralizes provider resolution so chat, skills, and OODA all stay in sync.

        Resolution order for 'auto' mode:
        1. DB `selected_backend` (UI provider selection - ollama/groq/anthropic)
        2. Fallback chain if no DB preference set

        For each provider, model resolution order:
        1. Explicit `model` param (caller override)
        2. DB `{provider}_selected_model` (UI model selection)
        3. Config fallback

        Args:
            backend: 'auto', 'ollama', 'groq', 'anthropic'
            model: Explicit model override, or None to resolve from DB/config

        Returns:
            Tuple of (provider_type, url, model) or None if unavailable
        """
        # For 'auto', check if user has selected a preferred backend in UI
        if backend == 'auto':
            db_backend = self.kb.get_setting('selected_backend', '')
            if db_backend and db_backend in ('ollama', 'anthropic', *OPENAI_COMPAT_PROVIDERS):
                backend = db_backend
                logger.info(f"[PROVIDER] Using UI-selected backend: {backend}")
            else:
                # No UI preference - use fallback chain
                provider_info = self.llm.get_next_provider()
                if not provider_info:
                    return None
                provider_type, url, resolved_model = provider_info
                source = 'fallback-chain'
                # If fallback chain selected ollama, override model with user's DB selection
                if provider_type == 'ollama' and not model:
                    db_model = self.kb.get_setting('ollama_selected_model', '')
                    if db_model:
                        resolved_model = db_model
                        source = 'db:ollama_selected_model'
                if model:
                    source = 'explicit-override'
                final = (provider_type, url, model or resolved_model)
                logger.debug(f"Resolved provider: {final[0]}/{final[2]} (source={source})")
                return final

        provider_type = backend
        llm_config = self.config.get('llm', {})

        if backend == 'ollama':
            primary = llm_config.get('primary', {})
            url = primary.get('url', os.getenv('OLLAMA_URL', ''))
            if not model:
                db_model = self.kb.get_setting('ollama_selected_model', '')
                config_model = primary.get('model', '')
                model = db_model or config_model
                source = 'db:ollama_selected_model' if db_model else 'config:llm.primary.model'
            else:
                source = 'explicit-override'
            logger.debug(f"[PROVIDER] Resolved ollama: {model} (source={source})")
            return (provider_type, url, model)
        elif backend in ('anthropic', *OPENAI_COMPAT_PROVIDERS):
            url = None
            if not model:
                # Check DB for user's model selection, fall back to config
                db_model = self.kb.get_setting(f'{backend}_selected_model', '')
                if db_model:
                    model = db_model
                else:
                    for fb in llm_config.get('fallback', []):
                        if fb.get('provider') == backend:
                            model = fb.get('model', '')
                            break
                if not model:
                    # Registry default, when the provider declares one. Without
                    # it a key-only provider resolves to model='' and the
                    # request fails at the vendor instead of here.
                    model = OPENAI_COMPAT_PROVIDERS.get(backend, {}).get('default_model', '')
            logger.debug(f"Resolved provider: {provider_type}/{model}")
            return (provider_type, url, model)
        else:
            return None

    def _chat_with_tools_with_fallback(
        self,
        messages: List[Dict[str, str]],
        system_context: str = '',
        backend: str = 'auto',
        model: str = None,
        max_iterations: int = None,
        event_callback=None,
    ) -> Dict[str, Any]:
        """Run _chat_with_tools across the configured provider fallback chain.

        On ANY exception from one provider, record the failure for cooldown,
        log it, and try the next provider in the chain. The chain is
        primary (user-selected) → other locals → paid escalation, gated by
        the existing ``allow_paid_escalation`` setting.

        Use this for OODA-internal paths (sweep, investigation, morning
        summary, learning extraction, etc.) where a transient Ollama
        timeout (e.g. cold-start after GPU unload) should not fail the
        whole operation. The chat handler also uses this so all paths
        share the same fallback semantics.

        Returns the same shape as _chat_with_tools, plus:
          - ``backend``: provider type that actually succeeded
          - ``model``: model that actually succeeded
          - ``fallback_used``: True iff a non-primary provider was used

        Raises the last provider's exception if all providers in the chain
        fail, or ``RuntimeError`` if the chain is empty.
        """
        provider_chain = self._get_provider_chain(backend, model)
        if not provider_chain:
            raise RuntimeError("No LLM providers available")

        last_error = None
        prev_provider = None
        for idx, (provider_type, url, model_name) in enumerate(provider_chain):
            try:
                if idx > 0 and event_callback and prev_provider:
                    event_callback('fallback', {
                        'from': prev_provider,
                        'to': f"{provider_type}/{model_name}",
                        'reason': str(last_error)[:100] if last_error else 'unknown',
                    })
                logger.info(
                    f"[FALLBACK] Trying provider {idx+1}/{len(provider_chain)}: "
                    f"{provider_type}/{model_name}"
                )
                result = self._chat_with_tools(
                    provider_type=provider_type, url=url, model=model_name,
                    messages=messages, system_context=system_context,
                    max_iterations=max_iterations, event_callback=event_callback,
                )
                provider_key = (
                    f"{provider_type}/{url}/{model_name}" if url
                    else f"{provider_type}/{model_name}"
                )
                self.llm.record_success(provider_key)
                result['backend'] = provider_type
                result['model'] = model_name
                result['fallback_used'] = idx > 0
                return result
            except Exception as e:
                last_error = e
                prev_provider = f"{provider_type}/{model_name}"
                provider_key = (
                    f"{provider_type}/{url}/{model_name}" if url
                    else f"{provider_type}/{model_name}"
                )
                logger.warning(
                    f"[FALLBACK] Provider {provider_type}/{model_name} failed: "
                    f"{type(e).__name__}: {e}"
                )
                self.llm.record_failure(provider_key, self.llm.classify_error(e))
                continue

        logger.error(
            f"[FALLBACK] All {len(provider_chain)} providers failed. "
            f"Last error: {last_error}"
        )
        raise last_error or RuntimeError("All LLM providers exhausted")

    def _chat_with_tools(self, provider_type: str, url: str, model: str,
                         messages: List[Dict[str, str]], system_context: str,
                         max_iterations: int = None, event_callback=None) -> Dict[str, Any]:
        """
        Execute LLM chat with tool calling support.

        Wraps _chat_with_tools_inner with Prometheus metrics tracking.
        """
        start = time.time()
        try:
            result = self._chat_with_tools_inner(
                provider_type, url, model, messages, system_context,
                max_iterations, event_callback
            )
            latency = time.time() - start
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='success').inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)
            if result.get('input_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='input').inc(result['input_tokens'])
            if result.get('output_tokens'):
                LLM_TOKENS.labels(provider=provider_type, model=model, type='output').inc(result['output_tokens'])
            return result
        except Exception as e:
            latency = time.time() - start
            LLM_REQUESTS.labels(provider=provider_type, model=model, result='error').inc()
            LLM_ERRORS.labels(provider=provider_type, error_type=type(e).__name__).inc()
            LLM_LATENCY.labels(provider=provider_type, model=model).observe(latency)
            raise

    def _chat_with_tools_inner(self, provider_type: str, url: str, model: str,
                         messages: List[Dict[str, str]], system_context: str,
                         max_iterations: int = None, event_callback=None) -> Dict[str, Any]:
        """
        Execute LLM chat with tool calling support.

        Args:
            provider_type: 'ollama', 'groq', 'gemini', 'anthropic', etc.
            url: API endpoint URL
            model: Model name
            messages: Chat history
            system_context: System prompt
            max_iterations: Max tool call iterations

        Returns:
            {
                'response': '...',
                'tool_calls': 2,
                'input_tokens': 1234,
                'output_tokens': 567
            }
        """
        import requests

        if max_iterations is None:
            max_iterations = self._get_max_tool_iterations()

        stats = _ToolLoopStats()
        tool_cache = {}  # memoizes read-only tool results for this session
        dispatch_kwargs = {
            'stats': stats,
            'tool_cache': tool_cache,
            'max_result_chars': self._max_tool_result_chars(),
            'max_iterations': max_iterations,
            'event_callback': event_callback,
        }
        final_answer_forced = False  # final-iteration nudge sent only once
        empty_nudge_sent = False  # empty-final-response nudge sent only once

        # Get tool schemas
        tools = self.tools.get_schemas()

        # Build initial messages with system context
        full_messages = [{'role': 'system', 'content': system_context}] + messages

        # while (not for/range) so the empty-response nudge can grant one
        # bonus round past max_iterations — the empty final typically happens
        # ON the last iteration, where a `continue` would otherwise just fall
        # out of the loop.
        iteration = -1
        iteration_budget = max_iterations
        while iteration + 1 < iteration_budget:
            iteration += 1
            try:
                logger.debug(f"[CHAT] iteration {iteration+1}/{max_iterations}, messages count: {len(full_messages)}")
                # Force a final answer on the last iteration instead of falling
                # off the end of the loop empty-handed. Ollama/Anthropic: simply
                # withhold tools — they then return text cleanly. OpenAI-compatible
                # providers (groq, xai) hard-error ("tool_use_failed") if a
                # reasoning model emits a tool call while tools are absent — so
                # for those, keep tools available and nudge with a message.
                is_final_iteration = (iteration == iteration_budget - 1)
                is_openai_compat = provider_type in OPENAI_COMPAT_PROVIDERS
                offered_tools = [] if (is_final_iteration and not is_openai_compat) else tools
                if is_final_iteration and not final_answer_forced:
                    final_answer_forced = True
                    if is_openai_compat:
                        full_messages.append({
                            'role': 'user',
                            'content': ("FINAL STEP — you have gathered enough data. Do NOT "
                                        "call any more tools. Respond now with your findings "
                                        "as the JSON array described above."),
                        })
                    logger.info("[CHAT] final iteration — forcing an answer")
                # Build payload for Ollama (OpenAI-compatible format)
                if provider_type == 'ollama':
                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'stream': False,
                        'temperature': 0.7
                    }
                    if offered_tools:
                        payload['tools'] = offered_tools
                    headers = {'Content-Type': 'application/json'}
                    logger.debug(f"[CHAT] POST to {url}/api/chat, roles={[m.get('role') for m in full_messages]}")
                    response = requests.post(
                        f"{url}/api/chat",
                        json=payload,
                        headers=headers,
                        timeout=self.llm_timeout
                    )
                    # Convert 4xx/5xx into an exception so the fallback chain
                    # picks it up. Without this, Ollama's `{"error": "model
                    # not found"}` 404 body parses cleanly as JSON, data.get(
                    # "message", {}) is empty, and we silently return an
                    # empty response — bypassing fallback entirely.
                    response.raise_for_status()
                    data = response.json()
                    logger.debug(f"[CHAT] LLM status={response.status_code}, tool_calls={bool(data.get('message', {}).get('tool_calls'))}, content_len={len(data.get('message', {}).get('content', ''))}")

                    # Extract tokens
                    stats.input_tokens += data.get('prompt_eval_count', 0)
                    stats.output_tokens += data.get('eval_count', 0)

                    # Check for tool calls
                    message = data.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        # Execute ALL tool calls (not just the first)
                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            tool_args = self._parse_tool_arguments(
                                tool_call['function'].get('arguments', {}))

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            # Append each tool result (size-capped, memoized)
                            full_messages.append({
                                'role': 'tool',
                                'content': content
                            })

                        # Continue loop for next iteration
                        continue

                    # No tool calls, extract text response
                    text = message.get('content', '')
                    if not (text or '').strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                elif provider_type in OPENAI_COMPAT_PROVIDERS:
                    # OpenAI-compatible cloud provider (Groq, xAI Grok) with tool use
                    api_key, endpoint = self._openai_compat_request_config(provider_type)
                    key_env = OPENAI_COMPAT_PROVIDERS[provider_type]['key_env']
                    if not api_key:
                        raise ValueError(f"{key_env} not set")

                    payload = {
                        'model': model,
                        'messages': full_messages,
                        'temperature': 0.7,
                        'max_tokens': 4096
                    }
                    if offered_tools:
                        payload['tools'] = offered_tools
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {api_key}'
                    }

                    response = requests.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    response.raise_for_status()
                    data = response.json()

                    if data.get('error'):
                        label = OPENAI_COMPAT_PROVIDERS[provider_type]['label']
                        raise ValueError(f"{label} API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    stats.input_tokens += usage.get('prompt_tokens', 0)
                    stats.output_tokens += usage.get('completion_tokens', 0)

                    # Check for tool calls
                    choice = data.get('choices', [{}])[0]
                    message = choice.get('message', {})
                    tool_calls = message.get('tool_calls', [])

                    if tool_calls:
                        # Append the assistant message (with all tool_calls) once
                        full_messages.append(message)

                        for tool_call in tool_calls:
                            tool_name = tool_call['function']['name']
                            tool_args = self._parse_tool_arguments(
                                tool_call['function'].get('arguments', '{}'))
                            tool_call_id = tool_call.get('id', f'call_{iteration}')

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            # Append each tool result as a separate message (size-capped, memoized)
                            full_messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call_id,
                                'content': content
                            })

                        continue

                    # No tool calls — return text response
                    text = message.get('content', '')
                    if not (text or '').strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                elif provider_type == 'anthropic':
                    # Anthropic Messages API with tool use
                    api_key = os.getenv('ANTHROPIC_API_KEY', '')
                    if not api_key:
                        raise ValueError("ANTHROPIC_API_KEY not set")

                    # Convert OpenAI tool schemas to Anthropic format
                    anthropic_tools = []
                    for t in tools:
                        func = t.get('function', {})
                        anthropic_tools.append({
                            'name': func['name'],
                            'description': func.get('description', ''),
                            'input_schema': func.get('parameters', {'type': 'object', 'properties': {}})
                        })

                    # Anthropic uses system as a top-level param, not a message
                    converted_messages = self._to_anthropic_messages(full_messages)

                    payload = {
                        'model': model,
                        'max_tokens': 4096,
                        'system': system_context,
                        'messages': converted_messages,
                        'temperature': 0.7
                    }
                    if offered_tools:
                        payload['tools'] = anthropic_tools
                    headers = {
                        'Content-Type': 'application/json',
                        'x-api-key': api_key,
                        'anthropic-version': '2023-06-01'
                    }

                    response = requests.post(
                        'https://api.anthropic.com/v1/messages',
                        json=payload,
                        headers=headers,
                        timeout=120
                    )
                    response.raise_for_status()
                    data = response.json()

                    if data.get('error'):
                        raise ValueError(f"Anthropic API error: {data['error']}")

                    # Extract tokens
                    usage = data.get('usage', {})
                    stats.input_tokens += usage.get('input_tokens', 0)
                    stats.output_tokens += usage.get('output_tokens', 0)

                    # Check for tool use in content blocks
                    # Anthropic can return multiple tool_use blocks in parallel
                    content_blocks = data.get('content', [])
                    tool_use_blocks = [b for b in content_blocks if b.get('type') == 'tool_use']
                    text_parts = [b.get('text', '') for b in content_blocks if b.get('type') == 'text']

                    if tool_use_blocks:
                        # Execute ALL tool calls and collect results
                        tool_results = []
                        for tool_block in tool_use_blocks:
                            tool_name = tool_block['name']
                            tool_args = tool_block.get('input', {})
                            tool_use_id = tool_block.get('id', f'tool_{iteration}')

                            content = self._dispatch_tool_call(
                                tool_name, tool_args, iteration=iteration, **dispatch_kwargs)

                            tool_results.append({
                                'type': 'tool_result',
                                'tool_use_id': tool_use_id,
                                'content': content
                            })

                        # Append assistant message with all tool uses
                        full_messages.append({
                            'role': 'assistant',
                            'content': content_blocks
                        })
                        # Append all tool results in a single user message
                        full_messages.append({
                            'role': 'tool',
                            'tool_use_id': tool_results[0]['tool_use_id'],
                            'tool_results': tool_results,
                            'content': json.dumps([tr['content'] for tr in tool_results])
                        })
                        continue

                    # No tool calls — return text response
                    text = '\n'.join(text_parts)
                    if not text.strip():
                        empty_nudge_sent, iteration_budget = self._handle_empty_final(
                            empty_nudge_sent, iteration_budget, max_iterations,
                            full_messages, provider_type, model)
                        continue
                    return stats.result(text)

                else:
                    raise NotImplementedError(f"Provider {provider_type} not yet implemented for chat")

            except EmptyLLMResponseError:
                # Must reach _chat_with_tools_with_fallback so the next
                # provider gets a shot — a synthetic error-text response
                # would be stored as findings and misread as a verdict.
                raise
            except Exception as e:
                logger.error(f"Chat iteration {iteration} failed: {e}", exc_info=True)
                if iteration == 0:
                    # First failure, raise immediately
                    raise
                # Subsequent failure during tool loop, return what we have
                return stats.result(f"Error during tool execution: {str(e)}")

        # Hit max iterations — do one final no-tools call to get a summary.
        # Extract tool results from conversation to provide as context.
        logger.info(f"Hit iteration limit ({max_iterations}), attempting summary call")
        try:
            # Collect tool results from the conversation for context
            tool_summaries = []
            for msg in full_messages:
                if msg.get('role') == 'tool':
                    content = msg.get('content', '')
                    # Truncate long tool results
                    if len(content) > 500:
                        content = content[:500] + '...'
                    tool_summaries.append(content)

            tool_context = "\n---\n".join(tool_summaries[-6:])  # Last 6 tool results

            summary_messages = [
                {'role': 'system', 'content': system_context},
                {'role': 'user', 'content': (
                    f'You investigated the infrastructure using {stats.tool_calls} tool calls. '
                    f'Here are the key results from your tool calls:\n\n{tool_context}\n\n'
                    f'Based on these results, provide your findings as a JSON array:\n'
                    f'[{{"severity": "info|warning|critical", "finding": "description", '
                    f'"remediation": "suggested fix"}}]\n'
                    f'If everything looks healthy, return: []\n'
                    f'Only return the JSON array, no other text.'
                )}
            ]

            if provider_type == 'ollama':
                payload = {
                    'model': model,
                    'messages': summary_messages,
                    'stream': False,
                    'temperature': 0.7
                }
                response = requests.post(
                    f"{url}/api/chat",
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=self.llm_timeout
                )
                response.raise_for_status()
                data = response.json()
                stats.input_tokens += data.get('prompt_eval_count', 0)
                stats.output_tokens += data.get('eval_count', 0)
                summary_text = data.get('message', {}).get('content', '')
            elif provider_type in OPENAI_COMPAT_PROVIDERS:
                api_key, endpoint = self._openai_compat_request_config(provider_type)
                payload = {
                    'model': model,
                    'messages': summary_messages,
                    'temperature': 0.7,
                    'max_tokens': 4096
                }
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()
                usage = data.get('usage', {})
                stats.input_tokens += usage.get('prompt_tokens', 0)
                stats.output_tokens += usage.get('completion_tokens', 0)
                summary_text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            elif provider_type == 'anthropic':
                api_key = os.getenv('ANTHROPIC_API_KEY', '')
                converted = self._to_anthropic_messages(full_messages)
                payload = {
                    'model': model, 'max_tokens': 4096,
                    'system': system_context,
                    'messages': converted, 'temperature': 0.7
                }
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01'
                }
                response = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, timeout=120)
                response.raise_for_status()
                data = response.json()
                usage = data.get('usage', {})
                stats.input_tokens += usage.get('input_tokens', 0)
                stats.output_tokens += usage.get('output_tokens', 0)
                summary_text = '\n'.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
            else:
                summary_text = ''

            if summary_text.strip():
                logger.info(f"Got {len(summary_text)} char summary after hitting iteration limit")
                return stats.result(summary_text)
            else:
                logger.warning("Summary call returned empty response after iteration limit")
        except Exception as e:
            logger.warning(f"Failed to get summary after iteration limit: {e}", exc_info=True)

        # Fallback if summary call also failed
        return stats.result(
            "Maximum tool iterations reached. Please simplify your request.")

    def _build_chat_system_context(self, mention_skills: bool = False) -> str:
        """Build the chat system prompt: infra state, capabilities, recent learnings.

        Shared by the buffered and streaming chat paths. ``mention_skills``
        adds the slash-command bullet, which only the buffered path (the one
        that routes ``/skill`` messages itself) advertises.
        """
        hosts_config = self.config.get('infrastructure', {}).get('hosts', {})
        host_list = ', '.join(f"{name} ({info.get('address', '?')}, {info.get('role', 'unknown')})"
                              for name, info in hosts_config.items())
        skills_line = "- Execute skills when requested (e.g., /investigate-container)\n" if mention_skills else ""

        # Capability list is derived from the live tool registry so it cannot
        # drift behind newly registered tools (CFOP-22 C). One line per schema.
        tool_lines = []
        try:
            for schema in (self.tools.get_schemas() if self.tools else []):
                fn = (schema.get('function') or {}) if isinstance(schema, dict) else {}
                name = fn.get('name') or ''
                desc = str(fn.get('description') or '').strip()
                if not name:
                    continue
                # Keep the prompt compact: first sentence / ~100 chars of desc.
                short = desc.split('. ')[0].strip()
                if len(short) > 100:
                    short = short[:97] + '...'
                tool_lines.append(f"- {name}: {short}" if short else f"- {name}")
        except Exception as e:
            logger.debug(f"Could not enumerate tool schemas for chat prompt: {e}")
        tools_block = '\n'.join(tool_lines) if tool_lines else '- (tool registry unavailable)'

        system_context = f"""You are CFOperator, an autonomous infrastructure monitoring agent.

Current System State:
- Active investigation: {self.current_investigation is not None}
- Last sweep: {int(time.time() - self.last_sweep)}s ago
- Monitoring {len(hosts_config)} hosts: {host_list}

You have access to these tools (use them — do not claim you lack a capability that appears here):
{tools_block}

Important: Some services run as systemd units (e.g., ollama on ollama-gpu), not containers.
Use ssh_list_services to see BOTH containers and systemd services on a host.

Your role:
- Answer infrastructure-specific questions
- Investigate issues using available tools
{skills_line}- ALWAYS use store_learning to save solutions when you or the user resolves an issue
- Use find_learnings to check for known solutions before investigating
- NOT general system administration (user has Claude Code CLI for that)

Be concise and infrastructure-focused.
"""

        # Surface recent verified learnings so LLM knows what's available
        try:
            recent_learnings = self.kb.find_learnings(limit=5, verified_only=False)
            if recent_learnings:
                system_context += "\n\nRecent learnings from past investigations:\n"
                for l in recent_learnings[:3]:
                    rate = f" ({l.get('success_rate', 0):.0%} success)" if l.get('times_applied', 0) > 0 else ""
                    system_context += f"- [{l.get('learning_type', '?')}] {l.get('title', '?')}{rate}\n"
                system_context += "Use find_learnings tool for more details on any of these.\n"
        except Exception:
            pass  # Don't break chat if KB is down

        return system_context

    def _prepare_skill_invocation(self, message: str):
        """Resolve a ``/skill args`` message into its LLM inputs.

        Returns ``(system_context, user_message, skill_name)``, or
        ``(None, None, skill_name)`` when the skill is unknown — callers turn
        that into the "Unknown skill" response.
        """
        parts = message.split(maxsplit=1)
        skill_name = parts[0][1:]  # Remove leading /
        skill_args = parts[1] if len(parts) > 1 else ''

        if skill_name not in self.skills:
            return None, None, skill_name

        skill = self.skills[skill_name]
        system_context = f"""You are CFOperator executing the "{skill['name']}" skill.

SKILL DESCRIPTION:
{skill['description']}

SKILL INSTRUCTIONS:
{skill['instructions']}

USER REQUEST:
{message}

IMPORTANT:
- Follow the skill instructions exactly as written
- Use the tools in the suggested sequence
- Provide structured output as described in the skill
- Be thorough but concise
"""
        user_message = f"Execute {skill_name} for: {skill_args}" if skill_args else f"Execute {skill_name}"
        logger.info(f"Executing skill: {skill_name} with args: {skill_args}")
        return system_context, user_message, skill_name

    def _unknown_skill_response(self, skill_name: str) -> Dict[str, Any]:
        available = ', '.join(self.skills.keys())
        return {
            'response': f"Unknown skill: {skill_name}\n\nAvailable skills: {available}",
            'backend': 'N/A',
            'model': 'N/A',
            'tool_calls': 0
        }

    def handle_chat_message(self, message: str, history: List[Dict[str, str]], backend: str = 'auto', model: str = None) -> Dict[str, Any]:
        """
        Handle chat message from user (via web UI).

        This is for infrastructure-specific questions like:
        - "Why did immich restart?"
        - "Show me Pi2 container status"
        - "What's using memory on Pi3?"
        - "/investigate-container immich-ml"

        NOT for general system administration (that's Claude Code CLI).

        Args:
            message: User's message
            history: Chat history
            backend: LLM backend to use (auto, ollama, groq, gemini, anthropic)
            model: Specific model to use (overrides default for the backend)

        Returns:
            {
                'response': '...',
                'backend': 'ollama',
                'model': 'qwen3:14b',
                'tool_calls': 2
            }
        """
        logger.info(f"Handling chat message: {message[:100]}")

        system_context = self._build_chat_system_context(mention_skills=True)

        # Expand shortcut slash commands into natural language prompts
        message = self._expand_slash_shortcut(message)

        # Check for skill/command invocation
        if message.startswith('/'):
            return self._execute_skill(message, backend=backend, model=model)

        # Check for explicit summary request (must be the primary intent, not just containing the word)
        msg_lower = message.lower().strip()
        if msg_lower in ('summary', 'report', 'status', 'tps report', 'morning summary', 'give me a summary', 'show summary'):
            summary = self._generate_morning_summary()
            return {
                'response': summary['text'],
                'backend': 'N/A',
                'model': 'N/A',
                'tool_calls': 0
            }

        # Call LLM with tools + metrics tracking
        start_time = time.time()
        tool_calls_count = 0

        try:
            # Build messages
            messages = list(history) + [{'role': 'user', 'content': message}]

            result = self._chat_with_tools_with_fallback(
                messages=messages,
                system_context=system_context,
                backend=backend,
                model=model,
            )

            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
                'learning_ids': result.get('learning_ids', []),
            }

        except Exception as e:
            # Track failed LLM request
            latency = time.time() - start_time
            provider = provider_type if 'provider_type' in locals() else 'unknown'
            model_name = model if 'model' in locals() else 'unknown'

            LLM_REQUESTS.labels(provider=provider, model=model_name, result='error').inc()
            LLM_ERRORS.labels(provider=provider, error_type=type(e).__name__).inc()
            LLM_LATENCY.labels(provider=provider, model=model_name).observe(latency)

            # Record failure in fallback manager
            if 'provider_key' in locals():
                error_type = self.llm.classify_error(e)
                self.llm.record_failure(provider_key, error_type)

            logger.error(f"Chat failed: {e}", exc_info=True)

            return {
                'response': f"Error processing request: {str(e)}",
                'backend': provider,
                'model': model_name,
                'tool_calls': tool_calls_count,
                'learning_ids': []
            }

    def handle_chat_message_stream(self, message: str, history: List[Dict[str, str]], backend: str = 'auto', model: str = None):
        """
        Streaming version of handle_chat_message. Yields SSE event dicts.

        Events yielded:
            {'event': 'tool_call', 'data': {'tool': ..., 'args': ..., 'iteration': ..., 'max': ...}}
            {'event': 'tool_result', 'data': {'tool': ..., 'result': ..., 'iteration': ...}}
            {'event': 'done', 'data': {'response': ..., 'backend': ..., 'model': ..., 'tool_calls': ...}}
            {'event': 'error', 'data': {'error': ...}}
        """
        event_queue = queue.Queue()

        def event_callback(event_type, data):
            event_queue.put({'event': event_type, 'data': data})

        def run_chat():
            try:
                # Expand shortcut slash commands
                nonlocal message
                message = self._expand_slash_shortcut(message)

                # Check for skill/command invocation
                if message.startswith('/'):
                    result = self._execute_skill_stream(message, backend=backend, model=model, event_callback=event_callback)
                elif message.lower().strip() in ('summary', 'report', 'status', 'tps report', 'morning summary', 'give me a summary', 'show summary'):
                    summary = self._generate_morning_summary()
                    result = {'response': summary['text'], 'backend': 'N/A', 'model': 'N/A', 'tool_calls': 0}
                else:
                    result = self._handle_chat_with_stream(message, history, backend, model, event_callback)
                event_queue.put({'event': 'done', 'data': result})
            except Exception as e:
                logger.error(f"Stream chat failed: {e}", exc_info=True)
                event_queue.put({'event': 'error', 'data': {'error': str(e)}})

        # Run the chat in a background thread
        import threading
        thread = threading.Thread(target=run_chat, daemon=True)
        thread.start()

        # Yield events as they arrive
        while True:
            try:
                evt = event_queue.get(timeout=180)
                yield evt
                if evt['event'] in ('done', 'error'):
                    break
            except queue.Empty:
                yield {'event': 'error', 'data': {'error': 'Timeout waiting for response'}}
                break

    def _handle_chat_with_stream(self, message, history, backend, model, event_callback):
        """Internal: runs handle_chat_message logic but passes event_callback to _chat_with_tools."""
        system_context = self._build_chat_system_context()

        messages = list(history) + [{'role': 'user', 'content': message}]

        try:
            result = self._chat_with_tools_with_fallback(
                messages=messages,
                system_context=system_context,
                backend=backend,
                model=model,
                event_callback=event_callback,
            )
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {'response': 'No LLM providers available', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            raise

        return {
            'response': result.get('response', ''),
            'backend': result.get('backend', 'unknown'),
            'model': result.get('model', 'unknown'),
            'tool_calls': result.get('tool_calls', 0),
            'learning_ids': result.get('learning_ids', []),
            'fallback_used': result.get('fallback_used', False),
        }

    def _execute_skill_stream(self, message: str, backend: str = 'auto', model: str = None, event_callback=None) -> Dict[str, Any]:
        """Execute a skill with streaming events."""
        system_context, user_message, skill_name = self._prepare_skill_invocation(message)
        if system_context is None:
            return self._unknown_skill_response(skill_name)

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                backend=backend,
                model=model,
                event_callback=event_callback,
            )
            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
            }
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {'response': f'LLM provider unavailable: {backend}', 'backend': 'none', 'model': 'none', 'tool_calls': 0}
            logger.error(f"Skill execution (stream) failed (all providers exhausted): {e}", exc_info=True)
            return {'response': f"Skill execution failed: {str(e)}", 'backend': 'error', 'model': 'N/A', 'tool_calls': 0}
        except Exception as e:
            logger.error(f"Skill execution (stream) failed: {e}", exc_info=True)
            return {'response': f"Skill execution failed: {str(e)}", 'backend': 'error', 'model': 'N/A', 'tool_calls': 0}

    def _execute_skill(self, message: str, backend: str = 'auto', model: str = None) -> Dict[str, Any]:
        """
        Execute a skill command (e.g., /investigate-container immich-ml).

        Skills are structured LLM prompts with:
        - Clear instructions for what to do
        - Tool calling sequence
        - Expected output format

        The skill instructions are injected into the system context,
        and the LLM executes the skill using available tools.
        """
        system_context, user_message, skill_name = self._prepare_skill_invocation(message)
        if system_context is None:
            return self._unknown_skill_response(skill_name)

        # Execute with LLM + tools
        start_time = time.time()

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': user_message}],
                system_context=system_context,
                backend=backend,
                model=model,
            )
            return {
                'response': result.get('response', ''),
                'backend': result.get('backend', 'unknown'),
                'model': result.get('model', 'unknown'),
                'tool_calls': result.get('tool_calls', 0),
            }
        except RuntimeError as e:
            if "No LLM providers available" in str(e):
                return {
                    'response': f'LLM provider unavailable: {backend}',
                    'backend': 'none',
                    'model': 'none',
                    'tool_calls': 0
                }
            logger.error(f"Skill execution failed (all providers exhausted): {e}", exc_info=True)
            return {
                'response': f"Skill execution failed: {str(e)}",
                'backend': 'error',
                'model': 'N/A',
                'tool_calls': 0
            }
        except Exception as e:
            logger.error(f"Skill execution failed: {e}", exc_info=True)
            return {
                'response': f"Skill execution failed: {str(e)}",
                'backend': 'error',
                'model': 'N/A',
                'tool_calls': 0
            }

    def answer_question(self, question_id: int, answer: str):
        """
        User answered a pending question.

        This unblocks an investigation that was waiting for input.
        """
        logger.info(f"Received answer for question {question_id}: {answer[:100]}")

        # TODO: Store answer in DB
        # TODO: Signal waiting investigation to continue

        # For now, just log
        logger.info(f"Answer handling not yet fully implemented")

    def _settings_readable(self) -> bool:
        """Whether a settings read just now can be believed.

        ``ResilientKnowledgeBase.get_setting`` degrades silently: when the
        health monitor says the connection is down it returns the *default*
        instead of raising, so an empty read is ambiguous — no marker, or no
        database. ``is_online()`` disambiguates. A plain KnowledgeBase (and the
        test doubles) has no such probe; those raise on failure, so absent a
        probe the read is taken at face value.
        """
        probe = getattr(self.kb, 'is_online', None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:
            return False

    def _check_morning_summary(self):
        """
        Generate morning summary (TPS report style).

        Checks if it's morning (e.g., 7-9 AM) and we haven't sent today's report yet.
        If yes, generates summary of overnight events and patterns.

        Summary includes:
        - Investigations resolved overnight
        - Alerts that fired (and auto-resolved)
        - Container restarts
        - Notable metric trends
        - Log patterns detected
        - Learnings extracted
        - Recommendations for the day

        Sent to:
        - Slack (if configured)
        - Stored in DB as sweep_report type
        """
        from datetime import datetime as dt

        # Check if morning summary is enabled
        summary_config = self.config.get('ooda', {}).get('morning_summary', {})
        if not summary_config.get('enabled', True):
            return

        # Check if it's morning time
        now = dt.now()
        summary_hour_start = summary_config.get('hour_start', 7)
        summary_hour_end = summary_config.get('hour_end', 9)

        if not (summary_hour_start <= now.hour < summary_hour_end):
            return

        # Check if we already sent today's summary. The in-memory mark is the
        # fast path; the DB setting is what survives a pod restart.
        #
        # The read fails *closed*: generating feeds the remediation queue, so a
        # read that cannot be trusted must skip the tick rather than fall
        # through to a second run (that is the 2026-08-28 duplicate). The next
        # loop retries, and if the database stays down for the whole window we
        # lose the day's summary — the cheaper miss, and one the log names.
        today = now.date()
        if getattr(self, 'last_summary_date', None) == today:
            return
        try:
            sent_on = self.kb.get_setting(_MORNING_SUMMARY_SENT_SETTING, '') or ''
        except Exception as e:
            logger.warning(f"Could not read {_MORNING_SUMMARY_SENT_SETTING} ({e}); "
                           "skipping this tick rather than risk a duplicate summary")
            return
        if not sent_on and not self._settings_readable():
            logger.warning(
                "Database is offline; cannot tell whether today's morning summary "
                "already ran. Skipping this tick rather than risk a duplicate.")
            return
        if sent_on == today.isoformat():
            logger.info(f"Morning summary already sent today ({sent_on}) per "
                        "agent settings; not re-running after restart")
            self.last_summary_date = today
            return

        logger.info("="*60)
        logger.info("MORNING SUMMARY: Generating overnight report")
        logger.info("="*60)

        # Generate the summary
        summary = self._generate_morning_summary()

        # Mark in memory now: whatever happens to the delivery below, this
        # process must not re-enter _generate_morning_summary today — that is
        # the call that feeds the remediation queue. The durable marker waits
        # until the digest has actually landed (below).
        self.last_summary_date = today

        # Send to Slack
        for notif in self.notifications:
            success = False
            error_msg = None
            try:
                notif.send(summary['text'], severity='info')
                success = True
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending morning summary: {e}")
            try:
                channel_type = getattr(notif, 'channel_type', 'slack')
                self.kb._kb.record_notification_history(
                    channel_id=0,
                    channel_type=channel_type,
                    severity='info',
                    title='Morning Summary',
                    message=summary['text'][:2000],
                    success=success,
                    error_message=error_msg
                )
            except Exception as e:
                logger.debug(f"Could not record notification history: {e}")

        # Store in DB as a sweep report
        stored = False
        try:
            self.kb.store_sweep_report(
                severity=summary.get('severity', 'info'),
                findings=[{'severity': 'info', 'finding': summary['text'][:500]}],
                summary=f"Morning summary - {now.strftime('%Y-%m-%d')}",
                # full_text: the cfop://digest/morning MCP resource serves the
                # complete summary; findings stay truncated for the console.
                sweep_meta={'type': 'morning_summary', 'full_text': summary['text']}
            )
            stored = True
        except Exception as e:
            logger.warning(f"Could not store morning summary in DB: {e}")

        # Persist "sent today" only once the digest has landed. Marking before
        # the store would make a failed store sticky: the restart that used to
        # retry the day would see the marker and skip, and the digest would
        # never exist. The write itself is still best-effort — an offline DB
        # costs restart-safety for today, not the summary.
        if stored:
            try:
                self.kb.set_setting(_MORNING_SUMMARY_SENT_SETTING, today.isoformat())
            except Exception as e:
                logger.warning(f"Could not persist {_MORNING_SUMMARY_SENT_SETTING}: {e}")
            logger.info("Morning summary sent")
        else:
            logger.warning("Morning summary sent but not stored; leaving "
                           f"{_MORNING_SUMMARY_SENT_SETTING} unset so a restart "
                           "retries today's digest")

    def _generate_morning_summary(self) -> Dict[str, Any]:
        """
        Generate morning summary by gathering overnight data from DB
        and having the LLM synthesize it with live infrastructure checks.
        """
        from datetime import datetime as dt, timedelta

        midnight = dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        now = dt.now()

        # Gather overnight data from DB
        context_parts = []

        # 1. Sweep reports since midnight
        overnight_reports = []
        try:
            reports = self.kb.get_recent_sweep_reports(limit=10)
            overnight_reports = [r for r in reports
                                if r.get('swept_at', '') >= midnight.isoformat()]
            if overnight_reports:
                context_parts.append(f"## Overnight Sweep Reports ({len(overnight_reports)} sweeps)")
                for r in overnight_reports:
                    context_parts.append(
                        f"- {r['swept_at']}: {r['severity'].upper()} - "
                        f"{r['finding_count']} findings: {r.get('summary', '')[:200]}"
                    )
                    for f in (r.get('findings') or [])[:5]:
                        sev = f.get('severity', 'info')
                        finding = f.get('finding', '')[:150]
                        remediation = f.get('remediation', '')
                        context_parts.append(f"  [{sev}] {finding}")
                        if remediation:
                            context_parts.append(f"    -> {remediation[:150]}")
            else:
                context_parts.append("## No sweep reports since midnight")
        except Exception as e:
            context_parts.append(f"## Sweep reports unavailable: {e}")

        # 2. Investigations since midnight
        try:
            investigations = self.kb.get_recent_investigations(limit=20)
            overnight_inv = [i for i in investigations
                            if i.get('started_at', '') >= midnight.isoformat()]
            if overnight_inv:
                context_parts.append(f"\n## Overnight Investigations ({len(overnight_inv)})")
                for inv in overnight_inv:
                    outcome = inv.get('outcome', 'unknown')
                    trigger = inv.get('trigger', '')[:100]
                    duration = inv.get('duration_seconds', 0) or 0
                    tools = inv.get('tool_calls_count', 0) or 0
                    context_parts.append(
                        f"- [{outcome}] {trigger} ({duration}s, {tools} tool calls)"
                    )
            else:
                context_parts.append("\n## No investigations since midnight")
        except Exception as e:
            context_parts.append(f"\n## Investigations unavailable: {e}")

        # 3. New learnings since midnight
        try:
            learnings = self.kb.get_learnings_since(midnight, limit=20)
            if learnings:
                context_parts.append(f"\n## New Learnings ({len(learnings)})")
                for l in learnings:
                    context_parts.append(f"- {l.get('title', 'untitled')}: {l.get('description', '')[:150]}")
            else:
                context_parts.append("\n## No new learnings since midnight")
        except Exception as e:
            context_parts.append(f"\n## Learnings unavailable: {e}")

        overnight_data = "\n".join(context_parts)
        infra = self._get_infra_summary()

        # Ask LLM to synthesize + do live checks
        task = (
            f"Generate a concise morning infrastructure summary for "
            f"{now.strftime('%Y-%m-%d %H:%M')}.\n\n"
            f"{infra}\n\n"
            f"Here is overnight activity data from the database:\n{overnight_data}\n\n"
            f"Do a quick live check: ping each host, check key metrics (CPU, memory, disk), "
            f"and verify critical services are running. Then produce a summary covering:\n"
            f"1. Overnight activity highlights\n"
            f"2. Current system health status\n"
            f"3. Any issues or recommendations\n\n"
            f"Be concise and practical. Use markdown formatting.\n\n"
            f"AFTER the markdown, append the actionable items as EXACTLY one fenced "
            f"json block (use [] if none) so they can be tracked/remediated:\n"
            f"```json\n"
            f'{{"recommendations": [{{"title": "short label", '
            f'"recommendation": "the concrete next step", "host": "affected host or empty", '
            f'"remediation_class": "gitops-patch|k8s-action|k8s-imperative|'
            f'node-action|data-fix|external-system|investigate|manual", '
            f'"risk": "low|med|high", "confidence": 0.0, '
            f'"repo": "owning GitOps repo slug or empty"}}]}}\n'
            f"```\n"
            f"{_REMEDIATION_CLASS_RUBRIC}"
            f"Be conservative with risk."
        )

        try:
            result = self._chat_with_tools_with_fallback(
                messages=[{'role': 'user', 'content': task}],
                system_context=(
                    f"You are CFOperator generating a morning infrastructure summary. "
                    f"You have tools to check live infrastructure. Be concise and actionable.\n\n"
                    f"{infra}"
                ),
                max_iterations=15,
            )
            summary_text = result.get('response', '')
            if summary_text and 'Maximum tool iterations' not in summary_text:
                # Attribute which LLM produced this so operators can correlate
                # summary quality with the served model (the fallback chain may
                # have rolled over to a different provider than the configured
                # primary; without this line nobody can tell after the fact).
                summary_text = _append_llm_attribution(summary_text, result)
                # Feed the queue from the summary's structured recommendations
                # (captures the prose 'Issues & Recommendations' the operator
                # sees); falls back to raw sweep findings if no block emitted.
                self._feed_remediations_from_summary(
                    summary_text, overnight_reports,
                    provider=_llm_provider_tag(result),
                )
                # Strip the machine-readable JSON block now that the queue has
                # consumed it — operators only need the prose table above it,
                # not the raw recommendations block leaking into Slack/ntfy.
                summary_text = self._strip_summary_recommendations_block(summary_text)
                return {
                    'text': summary_text,
                    'timestamp': now,
                    'severity': 'info'
                }
        except RuntimeError as e:
            if "No LLM providers available" not in str(e):
                logger.error(f"LLM morning summary failed (all providers exhausted): {e}")
        except Exception as e:
            logger.error(f"LLM morning summary failed: {e}")

        # Fallback: return the raw data if LLM is unavailable
        summary_text = (
            f"## Infrastructure Summary - {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{overnight_data}\n\n"
            f"*LLM unavailable — raw data shown above*\n\n"
            f"_Generated by: fallback (no LLM available)_"
        )

        return {
            'text': summary_text,
            'timestamp': now,
            'severity': 'info'
        }

    def _get_agent_settings(self) -> Dict[str, Any]:
        """
        Get agent settings relevant to LLM fallback.

        Returns dict with:
        - enable_local_ollama: Whether to use local Ollama instances
        - llm_fallback_chain: List of Ollama provider keys in priority order
        - paid_llm_escalation: Single paid provider key
        - allow_paid_escalation: Boolean flag to enable/disable paid LLM usage
        """
        settings = {}

        # Get enable_local_ollama flag (default: True)
        enable_local = self.kb.get_setting("enable_local_ollama", "true")
        settings["enable_local_ollama"] = enable_local.lower() == "true" if isinstance(enable_local, str) else enable_local

        # Get fallback chain (newline-separated string or JSON array)
        chain_raw = self.kb.get_setting("llm_fallback_chain", "")
        if chain_raw:
            try:
                # Try JSON array first
                settings["llm_fallback_chain"] = json.loads(chain_raw)
            except json.JSONDecodeError:
                # Treat as newline-separated
                settings["llm_fallback_chain"] = [line.strip() for line in chain_raw.split('\n') if line.strip()]
        else:
            settings["llm_fallback_chain"] = []

        # Get paid LLM escalation provider
        settings["paid_llm_escalation"] = self.kb.get_setting("paid_llm_escalation", "")

        # Get allow paid flag (default: False for safety)
        allow_paid = self.kb.get_setting("allow_paid_escalation", "false")
        settings["allow_paid_escalation"] = allow_paid.lower() == "true" if isinstance(allow_paid, str) else allow_paid

        # Get Ollama instances configuration (used by fallback manager to get URLs)
        ollama_instances = self.kb.get_setting("ollama_instances", "{}")
        try:
            settings["ollama_instances"] = json.loads(ollama_instances)
        except json.JSONDecodeError:
            settings["ollama_instances"] = {}

        return settings

def main():
    """Main entry point."""
    logger.info("="*60)
    logger.info("CFOperator - Continuous Feedback Operator")
    logger.info(f"Version: {build_version()}")
    logger.info("="*60)

    # Load a .env file if present, so API keys (XAI_API_KEY, GROQ_API_KEY, ...)
    # can live in .env. override=False — real environment variables (e.g. k8s
    # secrets injected into the pod) take precedence; .env only fills the gaps.
    # CFOP_NO_DOTENV opts the whole process out (see cfshared.config.
    # load_env_file). Honoured here too: this is a second, independent reader,
    # and a switch that means "do not read .env" while one code path still
    # does is worse than no switch at all.
    if os.getenv("CFOP_NO_DOTENV", "").strip():
        logger.debug("CFOP_NO_DOTENV set — not reading .env")
    else:
        try:
            from dotenv import load_dotenv
            if load_dotenv(override=False):
                logger.info("Loaded environment from .env")
        except ImportError:
            logger.debug("python-dotenv not installed — skipping .env load")

    config_path = os.getenv('CONFIG_PATH', 'config.yaml')
    operator = CFOperator(config_path=config_path)
    operator.run()

if __name__ == '__main__':
    main()
