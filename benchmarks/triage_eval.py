#!/usr/bin/env python3
"""
Triage classification benchmark for CFOperator model candidates.

Axis 2 of the model-suitability methodology (see
`benchmarks/gemma4-26b-vs-qwen3.6-27b.md`): score a candidate model on the
*production* triage prompt with the *production* parser, so the number means
"how well would this model triage real alerts", not "how well does it do on a
benchmark we invented".

The July 2026 gemma4-vs-qwen3.6 round ran this as a throwaway script and
committed only the JSON output, so the next candidate had to rebuild it. Hence
this file.

Fidelity to production (`CFOperator.run_triage`), by construction (CFOP-314):
  - the system prompt IS `CFOperator._triage_system_prompt()` and the user
    message IS `CFOperator._triage_user_message(...)` -- the same two
    functions run_triage calls. This harness used to regex the prompt out of
    run_triage's source; when CFOP-313 turned the literal into an expression
    (framing), the regex captured its first line, and every score between
    2026-10-09 and this change was of a one-line prompt. agent/
    test_triage_eval_contract.py now asserts the harness sends what
    run_triage sends.
  - the request body IS `CFOperator._ollama_chat_payload(...)`, the one place
    an Ollama /api/chat body is assembled: same temperature, same `options`
    rule (CFOP-279: sampling options are sent only when llm.num_ctx is set,
    on both sides). `--num-ctx` mirrors a deployment that sets it.
  - NO tools: run_triage passes max_iterations=1, which makes the tool loop's
    final-iteration branch withhold tools on the very first call.
  - responses are scored by `CFOperator._parse_triage_response` itself.

Usage:
    PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
        --model cfop-triage-ministral3:v5-q4 --url http://192.168.0.232:11434 \
        --output benchmarks/triage_eval_cfop_triage_ministral3_v5_q4.json

    # Re-grade a committed result file, no model needed:
    .venv/bin/python benchmarks/triage_eval.py --replay benchmarks/<file>.json

Scores are only comparable across models run on the SAME ollama version and
the SAME rubric -- record both in the output JSON (this script does).

STATISTICAL POWER -- read before trusting a 100% score
-----------------------------------------------------
The failure mode this suite is looking for is usually RARE, not systematic: a
model that takes a rubric shortcut on some fraction of runs rather than every
run. Three runs per case cannot see that.

Measured example (2026-08-16): qwen3.8:27b answers `precedent-monitoring`
correctly ~92% of the time and takes the notify shortcut the other ~8%. At
3 runs/case the chance of catching it at least once is only 1 - 0.92^3 = 22%,
so the suite reported a clean 42/42. At 12 runs it showed up. gemma4:26b was
12/12 on the same case.

So the default is 36 runs per case (docs/triage-eval-v2-plan.md, Tier 1): a
fault firing on 8% of runs is seen at least once 95% of the time, and at the
deployed model's ~1 s per call the full suite is about eight minutes. The
result JSON records the run count and that power. `--runs 3` is still a fast
screen: a miss is meaningful, a clean sheet is NOT proof, and the summary says
so.

THE GATE
--------
A run passes when every case is at 100% of its `expected` list, every reason
is alert-grounded and none is a v1 canned string or cites an object absent
from the prompt, confidence is not a pure function of action, and every
case's p95 latency is under the deployed triage timeout
(`CFOP_TRIAGE_TIMEOUT_SECONDS`, read exactly as event_runtime reads it: 5 s
when unset). The process exits non-zero on a failed gate so a CI job fails.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import urllib.error

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Order matters and is the reverse of the usual per-directory test invocation:
# `agent/` must be importable (knowledge_base and friends use bare imports),
# but the REPO ROOT has to win priority or `agent` binds to agent/agent.py as a
# bare module and `from agent.agent import ...` fails with "not a package".
sys.path.insert(0, os.path.join(_REPO_ROOT, "agent"))
sys.path.insert(0, _REPO_ROOT)

from agent.agent import CFOperator  # noqa: E402


# ── Production prompt and request, by construction ────────────────────────────

# What run_triage passes to the tool loop for the triage-model call; the
# contract test pins this against the body the production path really sends.
TRIAGE_TEMPERATURE = 0.7


def production_system_prompt() -> str:
    """The system prompt run_triage sends, from the same function."""
    return CFOperator._triage_system_prompt()


def build_user_message(case: dict) -> str:
    """The user message run_triage would build for this case, from the same
    function. `similar` is (outcome, trigger, similarity) per precedent."""
    return CFOperator._triage_user_message(
        case["severity"], case["summary"], case["labels"],
        [tuple(s) for s in (case.get("similar") or [])])


def build_payload(model: str, system_prompt: str, user_msg: str,
                  num_ctx=None) -> dict:
    """The /api/chat body, from the one place production assembles it.

    A bare operator with only the config key _ollama_chat_payload reads:
    `llm.primary.num_ctx` decides whether sampling options are sent at all
    (CFOP-279), so `--num-ctx` is how to score the way a deployment that sets
    it runs.
    """
    op = CFOperator.__new__(CFOperator)
    op.config = {"llm": {"primary": ({"num_ctx": int(num_ctx)} if num_ctx else {})}}
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    return op._ollama_chat_payload(model, messages, TRIAGE_TEMPERATURE)


# ── Ground-truth cases ────────────────────────────────────────────────────────
#
# Fourteen cases spanning the four actions, drawn from this homelab's real alert
# vocabulary.
#
# The first eight are the original 2026-07 set. Both gemma4:26b and qwen3.8:27b
# scored 24/24 on them, i.e. the suite had saturated and could no longer tell
# two candidates apart. Cases 9-14 were added 2026-08-16 to restore
# discriminating power. They are HARDER BY CONSTRUCTION, not merely more
# numerous: each one is built so that the obvious surface reading of the alert
# gives the WRONG answer, and only actually applying the rubric gives the right
# one.
#
# Every case therefore carries two extra fields:
#
#   rubric — the clause of run_triage's rubric that fixes the ground truth.
#            If a case cannot cite one, it does not belong here: the suite
#            measures adherence to the deployed rubric, not the taste of
#            whoever wrote the case.
#   trap   — the wrong answer a model lands on by reading the alert's surface
#            features (severity, "pod failure", "precedent present") instead
#            of the rubric. Printed on failure so a red line says *why* it is
#            wrong, not just that it is.
#
# The four traps cases 9-14 probe:
#
#   * precedent OUTCOME vs presence — `precedent-monitoring` lists similar past
#     investigations whose outcome was 'monitoring'. Presence of precedent says
#     notify; the rubric says a 'monitoring' precedent means investigate.
#   * blanket pod-failure reflex — `precedent-resolved-oom` is a pod failure
#     WITH resolved precedent, where notify is correct. A model that learned
#     "pod failure -> investigate" from cases 5-6 fails here.
#   * severity as a proxy for breadth — `critical-narrow` (critical, one hobby
#     service) must not escalate; `warning-correlated` (broad, but warning)
#     must not escalate either. Escalate needs critical AND broad, together.
#   * noise rules outrank severity — `tmp-pod-critical` is a `tmp-*` pod at
#     critical severity, which is still log_only.
#
# `expected` holds MORE THAN ONE action where the rubric genuinely permits more
# than one (the info-severity cases), so a model is not marked wrong for a
# defensible reading. Any listed action counts as correct.

# Rate used for the power line and the low-run-count warning: the measured
# frequency at which qwen3.8:27b took the notify shortcut on
# `precedent-monitoring` (1 miss in 12 runs, 2026-08-16). It is a reference
# point for "how rare a real shortcut can be", not a property of any
# particular model under test.
REFERENCE_SHORTCUT_RATE = 0.08
# Runs per case that see an 8%-rate fault at least once 95% of the time
# (docs/triage-eval-v2-plan.md, Tier 1).
DEFAULT_RUNS = 36

# The four reason strings the v1 fine-tune emitted for everything (CFOP-153):
# a frozen historical constant, kept as the thing a regression would look
# like. The dataset builder no longer has these to export, so they live here
# and tests/test_build_triage_dataset.py imports them from here.
V1_CANNED_REASONS = frozenset({
    "no resolved precedent for this pattern",
    "similar past investigation resolved with little effort",
    "critical with broad impact, operator should page in",
    "known noise pattern (test pod or watchdog heartbeat)",
})

CASES = [
    {
        "name": "watchdog",
        "severity": "none",
        "summary": "Watchdog: This is an alert meant to ensure that the entire "
                   "alerting pipeline is functional.",
        "labels": {"alertname": "Watchdog", "severity": "none",
                   "namespace": "monitoring"},
        "similar": [],
        "rubric": 'log_only: "Known noise. ... Alertmanager Watchdog"',
        "trap": "escalate/notify — it is an alert-shaped object, but it is the pipeline's own heartbeat",
        "expected": ["log_only"],
    },
    {
        "name": "smoke-test-pod",
        "severity": "warning",
        "summary": "Pod smoke-test-runner-2xk4f in namespace default is "
                   "crash-looping (6 restarts in 20 minutes).",
        "labels": {"alertname": "KubePodCrashLooping", "severity": "warning",
                   "namespace": "default", "pod": "smoke-test-runner-2xk4f"},
        "similar": [],
        "rubric": 'log_only: "Known noise. Test pods (smoke-test-*, tmp-*)"',
        "trap": 'investigate — reads as a CrashLoop pod failure with no precedent',
        "expected": ["log_only"],
    },
    {
        "name": "known-sdcard",
        "severity": "warning",
        "summary": "Filesystem read-only errors on raspberrypi3: SD card "
                   "reporting I/O errors on /dev/mmcblk0p2.",
        "labels": {"alertname": "NodeFilesystemErrors", "severity": "warning",
                   "instance": "raspberrypi3", "device": "/dev/mmcblk0p2"},
        "similar": [
            ("resolved", "raspberrypi3 SD card I/O errors after power loss — "
                         "known failing card, replacement scheduled", 0.94),
            ("resolved", "raspberrypi3 filesystem remounted read-only, "
                         "mmcblk0 errors in dmesg", 0.89),
            ("monitoring", "raspberrypi3 SD card wear warnings", 0.81),
        ],
        "rubric": 'notify: "the pattern is one the operator already understands (e.g. raspberrypi SD-card warning)"; precedent is listed and resolved',
        "trap": 'investigate — a hardware fault sounds urgent if the precedent list is ignored',
        "expected": ["notify"],
    },
    {
        "name": "info-severity",
        "severity": "info",
        "summary": "Nightly PV backup completed with 1 volume skipped "
                   "(prometheus-data, excluded by policy).",
        "labels": {"alertname": "InfraBackupCompleted", "severity": "info",
                   "job": "pv-backup"},
        "similar": [
            ("resolved", "nightly backup skipped prometheus-data — expected, "
                         "regenerable TSDB excluded by policy", 0.92),
        ],
        "rubric": 'notify: "when severity=info"',
        "trap": 'investigate — a skipped backup volume sounds like a failure',
        "expected": ["log_only", "notify"],
    },
    {
        "name": "novel-oom",
        "severity": "warning",
        "summary": "Pod paperless-ngx-7d9c4b8f5-nq2wm in namespace apps was "
                   "OOMKilled (exit code 137), 3 restarts in 10 minutes.",
        "labels": {"alertname": "KubePodOOMKilled", "severity": "warning",
                   "namespace": "apps", "pod": "paperless-ngx-7d9c4b8f5-nq2wm"},
        "similar": [],
        "rubric": 'investigate: "A pod failure with no similar past investigation listed is novel by definition"',
        "trap": 'notify — the #75 rubric explicitly forbids notify here',
        "expected": ["investigate"],
    },
    {
        "name": "novel-imagepull",
        "severity": "warning",
        "summary": "Pod changerecord-5f7b9d6c4-hk8pq in namespace apps is in "
                   "ImagePullBackOff: failed to pull image "
                   "ghcr.io/aachten/changerecord:v0.3.1.",
        "labels": {"alertname": "KubePodImagePullBackOff", "severity": "warning",
                   "namespace": "apps", "pod": "changerecord-5f7b9d6c4-hk8pq"},
        "similar": [],
        "rubric": 'investigate: "A pod failure with no similar past investigation listed is novel by definition"',
        "trap": 'notify — an image tag typo feels like a known, cheap fix',
        "expected": ["investigate"],
    },
    {
        "name": "controlplane-down",
        "severity": "critical",
        "summary": "Node ubuntu-cm5-01 (control plane) has been NotReady for "
                   "8 minutes; kubelet is not posting status.",
        "labels": {"alertname": "KubeNodeNotReady", "severity": "critical",
                   "node": "ubuntu-cm5-01", "role": "control-plane"},
        "similar": [],
        "rubric": 'escalate: "Severity=critical AND impact is broad (NodeNotReady on a control plane)"',
        "trap": 'investigate — under-escalation on a genuine page-worthy event',
        "expected": ["escalate"],
    },
    {
        "name": "correlated-outage",
        "severity": "critical",
        "summary": "Multiple services unreachable: ingress-nginx, postgres and "
                   "authentik all reporting endpoints down within 90 seconds.",
        "labels": {"alertname": "MultipleServicesDown", "severity": "critical",
                   "namespaces": "ingress-nginx,apps,auth"},
        "similar": [],
        "rubric": 'escalate: "Severity=critical AND impact is broad (multiple correlated services down)"',
        "trap": 'investigate — under-escalation on a genuine page-worthy event',
        "expected": ["escalate"],
    },

    # ── Added 2026-08-16: harder cases, after both candidates scored 24/24 ────

    {
        # The precedent list is present and similar, so surface-reading says
        # notify. But every listed outcome is 'monitoring' — the rubric names
        # that case explicitly and sends it the other way.
        "name": "precedent-monitoring",
        "severity": "warning",
        "summary": "Pod immich-machine-learning-6b8d7f9c4-t2ppq in namespace "
                   "apps is crash-looping (5 restarts in 15 minutes).",
        "labels": {"alertname": "KubePodCrashLooping", "severity": "warning",
                   "namespace": "apps",
                   "pod": "immich-machine-learning-6b8d7f9c4-t2ppq"},
        "similar": [
            ("monitoring", "immich-machine-learning restarts after model load — "
                           "cause not established, watching", 0.93),
            ("monitoring", "immich ML pod OOM/restart loop, no fix applied yet", 0.88),
            ("monitoring", "immich-machine-learning intermittent restarts", 0.84),
        ],
        "rubric": 'investigate: "or pattern that previous investigations classified as \'monitoring\'"',
        "trap": "notify — precedent is present, but its OUTCOME was never a fix",
        "expected": ["investigate"],
    },
    {
        # Mirror of novel-oom: same alert class, but precedent IS listed and
        # resolved, which is exactly the exemption the #75 rubric carves out.
        # A model that over-learned "pod failure -> investigate" fails here.
        "name": "precedent-resolved-oom",
        "severity": "warning",
        "summary": "Pod jellyfin-5c9d8b7f6-xk4mn in namespace apps was OOMKilled "
                   "(exit code 137) during a library scan.",
        "labels": {"alertname": "KubePodOOMKilled", "severity": "warning",
                   "namespace": "apps", "pod": "jellyfin-5c9d8b7f6-xk4mn"},
        "similar": [
            ("resolved", "jellyfin OOMKilled during library scan — memory limit "
                         "raised to 4Gi, fixed", 0.95),
            ("resolved", "jellyfin transcode OOM, limit bump applied", 0.90),
        ],
        "rubric": 'notify: pod-failure exemption — "unless a similar past investigation is listed"; here two resolved ones are',
        "trap": "investigate — blanket 'pod failure' reflex ignoring the exemption",
        "expected": ["notify"],
    },
    {
        # critical, but one replica of one hobby service. escalate requires
        # critical AND broad; breadth is absent, and no precedent rules out
        # notify for a pod failure.
        "name": "critical-narrow",
        "severity": "critical",
        "summary": "Pod wallabag-7f6c5d4b3-mq8wz in namespace apps has been "
                   "unavailable for 5 minutes (single-replica bookmarking "
                   "service, no other workload depends on it).",
        "labels": {"alertname": "KubePodNotReady", "severity": "critical",
                   "namespace": "apps", "pod": "wallabag-7f6c5d4b3-mq8wz"},
        "similar": [],
        "rubric": 'escalate needs "Severity=critical AND impact is broad" — breadth absent; no precedent, so investigate',
        "trap": "escalate — treating the severity label alone as sufficient",
        "expected": ["investigate"],
    },
    {
        # Broad, correlated, and genuinely novel — but warning-severity, and
        # escalate needs critical AND broad. Tests the other half of the
        # conjunction from critical-narrow.
        "name": "warning-correlated",
        "severity": "warning",
        "summary": "Elevated p99 latency on three services simultaneously "
                   "(immich, paperless-ngx, nextcloud) — all above 2s for "
                   "10 minutes, no errors logged.",
        "labels": {"alertname": "ServiceLatencyHigh", "severity": "warning",
                   "namespace": "apps",
                   "services": "immich,paperless-ngx,nextcloud"},
        "similar": [],
        "rubric": 'escalate needs "Severity=critical AND impact is broad" — critical absent; novel pattern, so investigate',
        "trap": "escalate — breadth alone, without critical severity",
        "expected": ["investigate"],
    },
    {
        # tmp-* is named in the log_only clause. Severity critical is the
        # distractor: the noise rule is about WHAT the pod is, not how loudly
        # it failed.
        "name": "tmp-pod-critical",
        "severity": "critical",
        "summary": "Pod tmp-restore-verify-9x2kd in namespace default exited "
                   "non-zero (restore drill container, exit code 1).",
        "labels": {"alertname": "KubeJobFailed", "severity": "critical",
                   "namespace": "default", "pod": "tmp-restore-verify-9x2kd"},
        "similar": [],
        "rubric": 'log_only: "Known noise. Test pods (smoke-test-*, tmp-*)" — the clause names tmp-* regardless of severity',
        "trap": "escalate/investigate — critical severity overriding the noise rule",
        "expected": ["log_only"],
    },
    {
        # severity=info is named in the notify clause; absence of precedent is
        # the distractor. Not a pod failure, so the notify restriction that
        # governs novel-oom does not apply here.
        "name": "info-novel-cert",
        "severity": "info",
        "summary": "TLS certificate for grafana.ai renews in 21 days "
                   "(cert-manager scheduled renewal notice).",
        "labels": {"alertname": "CertExpiryNotice", "severity": "info",
                   "namespace": "monitoring", "host": "grafana.ai"},
        "similar": [],
        "rubric": 'notify: "when severity=info". Not a pod failure, so the no-precedent restriction does not apply',
        "trap": "investigate — 'no precedent listed' reflex, ignoring severity=info",
        "expected": ["log_only", "notify"],
    },
]

CASES_BY_NAME = {c["name"]: c for c in CASES}


# ── Ollama call (the production request body) ─────────────────────────────────

def call_ollama(url: str, payload: dict, timeout: int) -> tuple:
    """POST `payload` (from build_payload) to /api/chat.
    Return (response_text, latency_seconds, error_or_None)."""
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # monotonic, not time(): a wall-clock step (NTP slew) mid-call would
    # otherwise land as a fake latency outlier, and the tail is exactly what
    # this benchmark exists to measure against CFOP_TRIAGE_TIMEOUT_SECONDS.
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", round(time.monotonic() - start, 2), f"{type(e).__name__}: {e}"
    elapsed = round(time.monotonic() - start, 2)
    return data.get("message", {}).get("content", ""), elapsed, None


def ollama_version(url: str) -> str:
    try:
        with urllib.request.urlopen(f"{url}/api/version", timeout=5) as r:
            return json.loads(r.read().decode()).get("version", "unknown")
    except Exception:
        return "unknown"


# --- Tier 3: grade the reason, not just the action -------------------------
#
# The v2 fine-tune scored 36/36 on novel-oom while every reason it produced
# cited a precedent that does not exist ("nearest was
# paperless-ngx-7ccf888b4-85484", a different invented pod each sample, on a
# case with no precedents at all). This harness graded the action, so it passed.
#
# Note that BOTH reason checks docs/triage-eval-v2-plan.md specifies would also
# have passed it: the reason is not a v1 canned string, and it does contain a
# token from the alert -- it names the real pod first, then appends a fictional
# one. Grounding is not the absence of fabrication. The check below is the
# converse and it is the one that fails v2: every object-name-shaped token in
# the reason must actually appear in the prompt.

def _citation_tokens(text: str) -> set:
    """Tokens in `text` that read as a Kubernetes object name.

    Deliberately narrow: a digit plus a letter and at least four characters.
    That covers pod names (always carrying a hash), node names like
    raspberrypi3, and namespaced refs, while sparing durations ("30m"),
    exit codes ("137"), cosines ("0.94") and hyphenated English the frames
    legitimately use ("known-noise"). A fabricated name with no digit in it
    would slip through; that is the accepted trade for a gate that does not
    cry wolf.
    """
    out = set()
    # Curly quotes too: the 8B v4 wrote “the “2xk4f” suffix” and the quoted
    # token, kept with its quotes, read as an invented object.
    for raw in re.split(r"[\s,;:()\[\]{}\"'“”‘’]+", text or ""):
        tok = raw.strip(".").strip("'\"“”‘’")
        if len(tok) < 4:
            continue
        # Dotted-quad IPs are object identifiers here (node addresses) and
        # carry no letters, so the alphanumeric rule below would skip them --
        # leaving an invented IP invisible to the gate.
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", tok):
            out.add(tok.lower())
            continue
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            out.add(tok.lower())
    return out


# Phrases that assert a precedent exists. Kept to the builder's own frame
# vocabulary plus the paraphrases the 8B produced; "no precedent" / "unlike
# anything" are the no-precedent frames and must NOT match.
_PRECEDENT_CLAIM_RE = re.compile(
    r"\b(closest earlier|nearest (?:was|match)|repeats an earlier|"
    r"earlier investigation (?:I found|that|which)|ended (?:monitoring|resolved|needs_action)|"
    r"similar past investigation|matches an earlier|precedent (?:that|which) (?:ended|resolved))\b",
    re.IGNORECASE)


def grade_reason(reason: str, prompt: str):
    """(grounded, fabricated) for one reason against the prompt it answered.

    `grounded` is the planned Tier 3 check -- the reason names something from
    the alert. `fabricated` is the list of object names it cites that appear
    nowhere in the prompt, which is the check that catches an invented
    precedent. A reason can be grounded and fabricating at the same time.
    """
    if not reason:
        return False, []
    hay = (prompt or "").lower()
    cited = _citation_tokens(reason)
    fabricated = sorted(t for t in cited if t not in hay)
    # An UNNAMED precedent is still an invention when the prompt offered none.
    # The 8B v3 said "the closest earlier investigation I found ended
    # monitoring" on cases whose prompt has no similar-past block at all; no
    # pod name, so the token check above cannot see it. Same defect as v2,
    # one rung down.
    if "similar past investigations" not in hay and _PRECEDENT_CLAIM_RE.search(reason):
        fabricated.append("<unnamed precedent>")
    grounded = any(t in hay for t in cited) or any(
        w in hay for w in re.findall(r"[a-z]{5,}", reason.lower()))
    return grounded, fabricated


def _normalise_reason(reason) -> str:
    return re.sub(r"\s+", " ", str(reason or "")).strip().strip(".").lower()


_CANNED_NORMALISED = frozenset(_normalise_reason(c) for c in V1_CANNED_REASONS)


def is_canned(reason) -> bool:
    """True when the reason is one of the v1 fine-tune's four templates
    (whitespace, case and a trailing full stop ignored)."""
    return _normalise_reason(reason) in _CANNED_NORMALISED


# ── The gate (CFOP-314): shared by a live run and --replay ────────────────────

def detection_power(runs_per_case: int, fault_rate: float = REFERENCE_SHORTCUT_RATE) -> float:
    """P(a fault firing independently on `fault_rate` of runs is seen at
    least once in `runs_per_case` runs)."""
    return 1 - (1 - fault_rate) ** max(0, int(runs_per_case))


def triage_timeout_seconds() -> tuple:
    """(seconds, source): CFOP_TRIAGE_TIMEOUT_SECONDS read the way
    event_runtime's build_http_triage_engine reads it -- 5 s when unset or
    unparseable. The source is printed so a result says which world it was
    graded in: the homelab sets 120 s; a chart or compose install sets nothing."""
    raw = os.getenv("CFOP_TRIAGE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 5.0, "default (CFOP_TRIAGE_TIMEOUT_SECONDS unset)"
    try:
        return float(raw), "CFOP_TRIAGE_TIMEOUT_SECONDS"
    except ValueError:
        return 5.0, f"default (CFOP_TRIAGE_TIMEOUT_SECONDS={raw!r} is not a number)"


def deep_confidence_threshold() -> float:
    """The deep-investigation reroute band's upper bound, as event_runtime
    reads it (CFOP_DEEP_CONFIDENCE_THRESHOLD, default 0.4)."""
    raw = os.getenv("CFOP_DEEP_CONFIDENCE_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else 0.4
    except ValueError:
        return 0.4


def _percentile(values, pct: float):
    """Nearest-rank percentile; None on no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered) + 0.4999)))
    return round(ordered[min(rank, len(ordered)) - 1], 2)


def grade(results: list, cases_by_name: dict) -> dict:
    """Everything the gate decides on, from the per-run rows alone, so a
    committed result file can be re-graded without a model (--replay).

    Rows carry: case, action, expected, correct, valid, confidence, reason,
    reason_grounded, reason_fabricated, latency_s, error.
    """
    rows = list(results)
    n = len(rows)
    valid = [r for r in rows if r.get("valid")]
    per_case_counts = {}
    for r in rows:
        per_case_counts[r["case"]] = per_case_counts.get(r["case"], 0) + 1
    runs_per_case = min(per_case_counts.values()) if per_case_counts else 0

    # Action: every case at 100% of its expected list (Tier 1 pass criterion).
    correct = sum(1 for r in rows if r.get("correct"))
    failing_cases = sorted({r["case"] for r in rows if not r.get("correct")})

    # Reason (Tier 3).
    canned = [r for r in valid if is_canned(r.get("reason"))]
    grounded = sum(1 for r in valid if r.get("reason_grounded"))
    fabricating = sum(1 for r in valid if r.get("reason_fabricated"))
    distinct_reasons = len({_normalise_reason(r.get("reason")) for r in valid})

    # Confidence (Tier 3): a label, not a confidence, when every action class
    # with more than one run shows exactly one value.
    by_action = {}
    for r in valid:
        if r.get("confidence") is None:
            continue
        by_action.setdefault(str(r.get("action")), []).append(float(r["confidence"]))
    values_by_action = {a: sorted(set(v)) for a, v in by_action.items()}
    multi = {a: v for a, v in by_action.items() if len(v) >= 2}
    confidence_is_alias = bool(multi) and all(len(set(v)) == 1 for v in multi.values())
    threshold = deep_confidence_threshold()
    reroute = sum(1 for r in valid if r.get("confidence") is not None
                  and 0 < float(r["confidence"]) < threshold)

    # Latency (the SLO), per case, against the configured timeout.
    timeout_s, timeout_source = triage_timeout_seconds()
    latency = {}
    breaches = []
    for name in sorted(per_case_counts):
        lat = [r["latency_s"] for r in rows if r["case"] == name
               and r.get("error") is None and r.get("latency_s") is not None]
        entry = {"n": len(lat), "p50": _percentile(lat, 50), "p95": _percentile(lat, 95),
                 "p99": _percentile(lat, 99), "max": (round(max(lat), 2) if lat else None)}
        latency[name] = entry
        if entry["p95"] is not None and entry["p95"] >= timeout_s:
            breaches.append(name)
    all_lat = [r["latency_s"] for r in rows if r.get("error") is None and r.get("latency_s") is not None]
    # A chart or compose install gets the 5 s default; say when a deployment
    # that set its own timeout would still fail there.
    default_world_warning = (timeout_source == "CFOP_TRIAGE_TIMEOUT_SECONDS"
                             and any(e["p95"] is not None and e["p95"] >= 5.0 for e in latency.values()))

    gate = {
        "action_every_case_100pct": n > 0 and correct == n,
        "json_valid_100pct": n > 0 and len(valid) == n,
        "reason_none_canned": not canned,
        "reason_grounded_90pct": bool(valid) and grounded / len(valid) >= 0.9,
        "reason_none_fabricated": fabricating == 0,
        "confidence_not_action_alias": not confidence_is_alias,
        "latency_p95_under_timeout": not breaches and bool(all_lat),
    }
    gate["pass"] = all(gate.values())

    return {
        "runs": n,
        "runs_per_case": runs_per_case,
        "detection_power_at_reference_rate": round(detection_power(runs_per_case), 4),
        "reference_fault_rate": REFERENCE_SHORTCUT_RATE,
        "json_valid_rate": round(len(valid) / n, 4) if n else 0,
        "action_accuracy": round(correct / n, 4) if n else 0,
        "failing_cases": failing_cases,
        "reason": {
            "canned_runs": len(canned),
            "canned_rate": round(len(canned) / len(valid), 4) if valid else 0,
            "grounded_rate": round(grounded / len(valid), 4) if valid else 0,
            "fabrication_rate": round(fabricating / len(valid), 4) if valid else 0,
            "distinct": distinct_reasons,
        },
        "confidence": {
            "values_by_action": values_by_action,
            "is_action_alias": confidence_is_alias,
            "deep_reroute_threshold": threshold,
            "deep_reroute_candidates": reroute,
        },
        "latency": {
            "timeout_s": timeout_s,
            "timeout_source": timeout_source,
            "mean_s": round(statistics.mean(all_lat), 2) if all_lat else None,
            "p95_s": _percentile(all_lat, 95),
            "p99_s": _percentile(all_lat, 99),
            "max_s": round(max(all_lat), 2) if all_lat else None,
            "per_case": latency,
            "p95_breaches": breaches,
            "would_breach_unset_default": default_world_warning,
        },
        "errors": sum(1 for r in rows if r.get("error")),
        "gate": gate,
    }


def print_report(model: str, version: str, g: dict, per_case: dict) -> None:
    n = g["runs"]
    print(f"\n{'='*60}")
    print(f"model            {model} (ollama {version})")
    print(f"runs             {n} = {g['runs_per_case']} per case; a {g['reference_fault_rate']*100:.0f}%-rate "
          f"fault is seen {g['detection_power_at_reference_rate']*100:.0f}% of the time")
    print(f"JSON valid       {g['json_valid_rate']*100:.1f}%")
    print(f"Action accuracy  {g['action_accuracy']*100:.1f}%"
          + (f"  <- FAIL on {', '.join(g['failing_cases'])}" if g['failing_cases'] else ""))
    r = g["reason"]
    print(f"Reason grounded  {r['grounded_rate']*100:.1f}%   canned {r['canned_runs']}"
          + ("  <- FAIL: v1 template strings" if r['canned_runs'] else "")
          + f"   distinct {r['distinct']}")
    print(f"Fabricated cites {r['fabrication_rate']*100:.1f}%"
          + ("  <- FAIL: reasons cite objects absent from the prompt" if r['fabrication_rate'] else ""))
    c = g["confidence"]
    print(f"Confidence       by action {c['values_by_action']}"
          + ("  <- FAIL: one value per action is a label, not a confidence" if c['is_action_alias'] else ""))
    print(f"{'':17}{c['deep_reroute_candidates']} run(s) in the deep-tier reroute band "
          f"(0 < c < {c['deep_reroute_threshold']})")
    lt = g["latency"]
    print(f"Latency          mean {lt['mean_s']}s  p95 {lt['p95_s']}s  p99 {lt['p99_s']}s  max {lt['max_s']}s")
    print(f"{'':17}gate: p95 < {lt['timeout_s']}s ({lt['timeout_source']})"
          + (f"  <- FAIL on {', '.join(lt['p95_breaches'])}" if lt['p95_breaches'] else ""))
    if lt["would_breach_unset_default"]:
        print(f"{'':17}WARNING: a case's p95 is over the 5s an install that never set "
              f"CFOP_TRIAGE_TIMEOUT_SECONDS gets; that install would time out.")
    if g["errors"]:
        print(f"Errors           {g['errors']}")
    if g["action_accuracy"] == 1.0 and g["runs_per_case"] < 10:
        print(f"{'':17}NOTE: clean at {g['runs_per_case']} runs/case. A shortcut firing at "
              f"~{REFERENCE_SHORTCUT_RATE*100:.0f}% would be caught only "
              f"{g['detection_power_at_reference_rate']*100:.0f}% of the time at this run "
              f"count. Re-run with --runs {DEFAULT_RUNS} before concluding a model is clean.")
    print(f"GATE             {'PASS' if g['gate']['pass'] else 'FAIL'}  "
          + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in g["gate"].items() if k != "pass"))
    print(f"{'='*60}")
    for name, pc in per_case.items():
        print(f"  {name:22} {pc['correct']}/{pc['of']}  "
              f"expected={'|'.join(pc['expected']):22} got={','.join(pc['actions'])}")


def summarise_per_case(results: list, case_names: list) -> dict:
    per_case = {}
    for name in case_names:
        runs = [r for r in results if r["case"] == name]
        if not runs:
            continue
        per_case[name] = {
            "expected": runs[0].get("expected"),
            "correct": sum(1 for r in runs if r.get("correct")),
            "of": len(runs),
            "actions": sorted({str(r.get("action")) for r in runs}),
            "fabricating_runs": sum(1 for r in runs if r.get("reason_fabricated")),
        }
    return per_case


def replay(path: str) -> dict:
    """Re-grade a result file this harness wrote. Grounding and fabrication
    come from the persisted per-run fields (the prompt is not stored); the
    rest is recomputed. Returns the summary, with `gate`."""
    with open(path) as f:
        doc = json.load(f)
    results = doc.get("results") or []
    old = doc.get("summary") or {}
    g = grade(results, CASES_BY_NAME)
    g["model"] = old.get("model", "?")
    g["ollama_version"] = old.get("ollama_version", "?")
    g["replayed_from"] = path
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="Ollama tag to score (required unless --replay)")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"runs per case (default {DEFAULT_RUNS}: 95%% power against an "
                         f"{REFERENCE_SHORTCUT_RATE*100:.0f}%%-rate fault)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="per-call timeout; deliberately far above the "
                         "production CFOP_TRIAGE_TIMEOUT_SECONDS so that a "
                         "timeout-breaching model is MEASURED rather than "
                         "truncated (see the qwen3.6 157s finding)")
    ap.add_argument("--num-ctx", type=int, default=None,
                    help="mirror a deployment that sets llm.num_ctx: sampling "
                         "options then ride in `options`, as in production (CFOP-279)")
    ap.add_argument("--output", default=None)
    ap.add_argument("--keep-raw", action="store_true",
                    help="store raw text for runs that failed to parse")
    ap.add_argument("--only", default=None,
                    help="comma-separated case names to run. For characterising "
                         "one case at high run-count — a latency tail seen once "
                         "in 3 runs is an anecdote, not a measurement")
    ap.add_argument("--replay", default=None, metavar="RESULTS_JSON",
                    help="re-grade a result file this harness wrote; no model is called")
    ap.add_argument("--no-gate", action="store_true",
                    help="exit 0 even when the gate fails (exploratory runs)")
    args = ap.parse_args()

    if args.replay:
        g = replay(args.replay)
        with open(args.replay) as f:
            results = (json.load(f).get("results") or [])
        per_case = summarise_per_case(results, sorted({r["case"] for r in results}))
        print(f"Triage eval replay of {args.replay}")
        print_report(g["model"], g["ollama_version"], g, per_case)
        if args.output:
            with open(args.output, "w") as f:
                json.dump({"summary": g, "per_case": per_case, "results": results}, f, indent=1)
        sys.exit(0 if (g["gate"]["pass"] or args.no_gate) else 1)

    if not args.model:
        ap.error("--model is required unless --replay is given")

    cases = CASES
    if args.only:
        # Drop empties first: a trailing comma would otherwise reach the
        # unknown-name check as "" and report a blank name as unrecognised.
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        if not wanted:
            ap.error("--only was given no case names")
        unknown = wanted - {c["name"] for c in CASES}
        if unknown:
            ap.error(f"unknown case name(s): {', '.join(sorted(unknown))}")
        cases = [c for c in CASES if c["name"] in wanted]

    system_prompt = production_system_prompt()
    version = ollama_version(args.url)
    timeout_s, timeout_source = triage_timeout_seconds()

    print(f"Triage eval: {args.model} @ {args.url} (ollama {version})")
    print(f"{len(cases)} cases x {args.runs} runs, production prompt + parser; "
          f"latency gate p95 < {timeout_s}s ({timeout_source})\n")

    results = []
    for case in cases:
        user_msg = build_user_message(case)
        payload = build_payload(args.model, system_prompt, user_msg, args.num_ctx)
        for run in range(args.runs):
            text, latency, err = call_ollama(args.url, payload, args.timeout)
            decision = CFOperator._parse_triage_response(text) if text else None
            action = decision["action"] if decision else None
            correct = action in case["expected"] if action else False
            reason = decision.get("reason") if decision else None
            grounded, fabricated = grade_reason(reason, user_msg)
            results.append({
                "case": case["name"],
                "run": run,
                "valid": decision is not None,
                "action": action,
                "expected": case["expected"],
                "correct": correct,
                "latency_s": latency,
                "confidence": decision["confidence"] if decision else None,
                # Persisted so a reason audit can be retroactive. Without this
                # the v2 fabrication had to be re-queried live against a model
                # that might since have been deleted.
                "reason": reason,
                "reason_grounded": grounded,
                "reason_fabricated": fabricated,
                "reason_canned": is_canned(reason) if decision else False,
                "error": err,
                "raw": (text[:500] if (args.keep_raw and decision is None)
                        else None),
            })
            mark = "OK " if correct else ("BAD" if decision else "ERR")
            print(f"  [{mark}] {case['name']:22} run{run}  "
                  f"{str(action):12} {latency:6.2f}s"
                  + (f"  {err}" if err else ""))
            # A bare "BAD" says a model missed; the trap says which shortcut it
            # took, which is the part worth acting on.
            if not correct and decision:
                print(f"         expected {'|'.join(case['expected'])} — "
                      f"trap: {case['trap']}")
            # A fabricated citation is worth surfacing even on a run whose
            # action was right: that is exactly how v2 passed this harness.
            if fabricated:
                print(f"         FABRICATED {', '.join(fabricated)} — "
                      f"cited but absent from the prompt")
            if decision and is_canned(reason):
                print(f"         CANNED reason — a v1 template string")

    g = grade(results, CASES_BY_NAME)
    g["model"] = args.model
    g["ollama_version"] = version
    g["num_ctx"] = args.num_ctx
    per_case = summarise_per_case(results, [c["name"] for c in cases])
    print_report(args.model, version, g, per_case)

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"summary": g, "per_case": per_case, "results": results}, f, indent=1)
        print(f"\nWrote {args.output}")

    sys.exit(0 if (g["gate"]["pass"] or args.no_gate) else 1)


if __name__ == "__main__":
    main()
