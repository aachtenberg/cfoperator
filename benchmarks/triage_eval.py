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

Fidelity to production (`CFOperator.run_triage`):
  - system prompt is EXTRACTED FROM `agent/agent.py` AT RUNTIME, not copied
    here. If the rubric changes, this harness changes with it and old scores
    are known to be non-comparable.
  - the user message is assembled in the same order and format.
  - the call matches the triage path: /api/chat, temperature 0.7, stream off,
    NO tools (run_triage passes max_iterations=1, which makes the tool loop's
    final-iteration branch withhold tools on the very first call).
  - responses are scored by `CFOperator._parse_triage_response` itself.

Usage:
    PYTHONPATH=agent:. .venv/bin/python benchmarks/triage_eval.py \
        --model gemma4:26b --runs 3 \
        --output benchmarks/triage_eval_gemma4_26b.json

Scores are only comparable across models run on the SAME ollama version and
the SAME rubric — record both in the output JSON (this script does).

STATISTICAL POWER — read before trusting a 100% score
-----------------------------------------------------
The failure mode this suite is looking for is usually RARE, not systematic: a
model that takes a rubric shortcut on some fraction of runs rather than every
run. The default 3 runs/case cannot see that.

Measured example (2026-08-16): qwen3.8:27b answers `precedent-monitoring`
correctly ~92% of the time and takes the notify shortcut the other ~8%. At
3 runs/case the chance of catching it at least once is only 1 - 0.92^3 = 22%,
so the suite reported a clean 42/42. At 12 runs it showed up. gemma4:26b was
12/12 on the same case.

So:
  --runs 3     fast screen. A miss is meaningful; a clean sheet is NOT proof.
  --runs 10+   with --only, to characterise a case where a model looks shaky.

A clean run at low --runs means "no systematic error found", never "correct".
The summary prints this caveat rather than leaving the headline to mislead.
"""

import argparse
import inspect
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


# ── Production prompt extraction ──────────────────────────────────────────────

def load_production_system_prompt() -> str:
    """Pull the triage system prompt out of run_triage's source.

    Copying the prompt into this file would let the two drift silently, and a
    drifted prompt makes every score in this benchmark a measurement of
    something that is not running in production. Extracting it means the
    harness cannot lie about what it tested.
    """
    src = inspect.getsource(CFOperator.run_triage)
    match = re.search(r'system_prompt = """(.*?)"""', src, re.DOTALL)
    if not match:
        raise RuntimeError(
            "Could not find the `system_prompt = \"\"\"...\"\"\"` literal in "
            "CFOperator.run_triage. The triage prompt was refactored — update "
            "load_production_system_prompt() to match, and treat previously "
            "recorded scores as non-comparable."
        )
    return match.group(1)


def build_user_message(case: dict) -> str:
    """Assemble the user message exactly as run_triage does."""
    similar_context = ""
    if case.get("similar"):
        lines = [
            f"- [{outcome:10}] {trigger[:100]} (similarity: {sim:.2f})"
            for outcome, trigger, sim in case["similar"]
        ]
        similar_context = "\n\nSimilar past investigations:\n" + "\n".join(lines)

    return (
        f"Alert severity: {case['severity']}\n"
        f"Alert summary: {case['summary']}\n"
        f"Labels: {json.dumps(case['labels'], default=str)[:500]}"
        f"{similar_context}\n\n"
        "Classify."
    )


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

# Rate used only for the low-run-count warning below: the measured frequency at
# which qwen3.8:27b took the notify shortcut on `precedent-monitoring`
# (1 miss in 12 runs, 2026-08-16). It is a reference point for "how rare a real
# shortcut can be", not a property of any particular model under test.
REFERENCE_SHORTCUT_RATE = 0.08

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


# ── Ollama call (matches the production triage request) ───────────────────────

def call_ollama(url: str, model: str, system_prompt: str, user_msg: str,
                timeout: int) -> tuple:
    """Return (response_text, latency_seconds, error_or_None)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        # run_triage goes through _chat_with_tools_with_fallback, whose ollama
        # branch uses 0.7 — NOT the 0.3 used by the insights path. The July
        # round's throwaway script used 0.3, so its latencies/accuracies are
        # very slightly off-production; noted here so the discrepancy is not
        # rediscovered as a mystery.
        "temperature": 0.7,
    }
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
    for raw in re.split(r"[\s,;:()\[\]{}\"']+", text or ""):
        tok = raw.strip(".").strip("'\"")
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
    grounded = any(t in hay for t in cited) or any(
        w in hay for w in re.findall(r"[a-z]{5,}", reason.lower()))
    return grounded, fabricated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--runs", type=int, default=3,
                    help="runs per case (default 3)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="per-call timeout; deliberately far above the "
                         "production CFOP_TRIAGE_TIMEOUT_SECONDS so that a "
                         "timeout-breaching model is MEASURED rather than "
                         "truncated (see the qwen3.6 157s finding)")
    ap.add_argument("--output", default=None)
    ap.add_argument("--keep-raw", action="store_true",
                    help="store raw text for runs that failed to parse")
    ap.add_argument("--only", default=None,
                    help="comma-separated case names to run. For characterising "
                         "one case at high run-count — a latency tail seen once "
                         "in 3 runs is an anecdote, not a measurement")
    args = ap.parse_args()

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

    system_prompt = load_production_system_prompt()
    version = ollama_version(args.url)

    print(f"Triage eval: {args.model} @ {args.url} (ollama {version})")
    print(f"{len(cases)} cases x {args.runs} runs, production prompt + parser\n")

    results = []
    for case in cases:
        user_msg = build_user_message(case)
        for run in range(args.runs):
            text, latency, err = call_ollama(
                args.url, args.model, system_prompt, user_msg, args.timeout)
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

    latencies = [r["latency_s"] for r in results if r["error"] is None]
    valid = sum(1 for r in results if r["valid"])
    correct = sum(1 for r in results if r["correct"])

    summary = {
        "model": args.model,
        "ollama_version": version,
        "runs": len(results),
        "json_valid_rate": round(valid / len(results), 4) if results else 0,
        "action_accuracy": round(correct / len(results), 4) if results else 0,
        "latency_mean_s": round(statistics.mean(latencies), 2) if latencies else None,
        "latency_max_s": round(max(latencies), 2) if latencies else None,
        "errors": sum(1 for r in results if r["error"]),
        "reason_grounded_rate": (
            round(sum(1 for r in results if r["reason_grounded"]) / len(results), 4)
            if results else 0),
        "reason_fabrication_rate": (
            round(sum(1 for r in results if r["reason_fabricated"]) / len(results), 4)
            if results else 0),
    }

    per_case = {}
    for case in cases:
        runs = [r for r in results if r["case"] == case["name"]]
        per_case[case["name"]] = {
            "expected": case["expected"],
            "correct": sum(1 for r in runs if r["correct"]),
            "of": len(runs),
            "actions": sorted({str(r["action"]) for r in runs}),
            "fabricating_runs": sum(1 for r in runs if r["reason_fabricated"]),
        }

    print(f"\n{'='*60}")
    print(f"model            {summary['model']} (ollama {version})")
    print(f"JSON valid       {valid}/{len(results)} "
          f"({summary['json_valid_rate']*100:.1f}%)")
    print(f"Action accuracy  {correct}/{len(results)} "
          f"({summary['action_accuracy']*100:.1f}%)")
    print(f"Latency          mean {summary['latency_mean_s']}s  "
          f"max {summary['latency_max_s']}s")
    fab = sum(1 for r in results if r["reason_fabricated"])
    print(f"Reason grounded  {sum(1 for r in results if r['reason_grounded'])}"
          f"/{len(results)} ({summary['reason_grounded_rate']*100:.1f}%)")
    print(f"Fabricated cites {fab}/{len(results)} "
          f"({summary['reason_fabrication_rate']*100:.1f}%)"
          + ("  <- FAIL: reasons cite objects absent from the prompt" if fab else ""))
    if summary["errors"]:
        print(f"Errors           {summary['errors']}")
    # A perfect score at low run-count is the suite's most misleading output:
    # the shortcuts these cases probe show up on a FRACTION of runs, so a clean
    # sheet here means "no systematic error", not "correct". Say so inline.
    if correct == len(results) and args.runs < 10:
        # P(at least one miss observed) for a shortcut that fires at
        # REFERENCE_SHORTCUT_RATE on each independent run.
        detection_prob = 1 - (1 - REFERENCE_SHORTCUT_RATE) ** args.runs
        print(f"{'':17}NOTE: clean at {args.runs} runs/case. A shortcut firing at "
              f"~{REFERENCE_SHORTCUT_RATE*100:.0f}%\n{'':23}(the observed qwen3.8 "
              f"precedent-monitoring rate) would be caught\n{'':23}only "
              f"{detection_prob*100:.0f}% of the time at this run count. Re-run "
              f"with\n{'':23}--only <case> --runs 12+ before concluding a model "
              f"is clean.")
    print(f"{'='*60}")
    for name, pc in per_case.items():
        print(f"  {name:20} {pc['correct']}/{pc['of']}  "
              f"expected={'|'.join(pc['expected']):22} got={','.join(pc['actions'])}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"summary": summary, "per_case": per_case,
                       "results": results}, f, indent=1)
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
