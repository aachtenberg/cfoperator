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
# Eight cases spanning the four actions, drawn from this homelab's real alert
# vocabulary. Two axes are deliberately probed:
#
#   * precedent sensitivity — `known-sdcard` carries similar past
#     investigations, `novel-oom` / `novel-imagepull` deliberately carry none.
#     The post-#75 rubric forbids `notify` for a pod failure with no listed
#     precedent, so those two are the rubric's discriminating cases.
#   * over/under-escalation — noise cases must not escalate, and genuine
#     critical+broad cases must.
#
# `expected` is a set: `info-severity` is legitimately either log_only or
# notify under the rubric, so both count as correct.

CASES = [
    {
        "name": "watchdog",
        "severity": "none",
        "summary": "Watchdog: This is an alert meant to ensure that the entire "
                   "alerting pipeline is functional.",
        "labels": {"alertname": "Watchdog", "severity": "none",
                   "namespace": "monitoring"},
        "similar": [],
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
        "expected": ["escalate"],
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
    args = ap.parse_args()

    system_prompt = load_production_system_prompt()
    version = ollama_version(args.url)

    print(f"Triage eval: {args.model} @ {args.url} (ollama {version})")
    print(f"{len(CASES)} cases x {args.runs} runs, production prompt + parser\n")

    results = []
    for case in CASES:
        user_msg = build_user_message(case)
        for run in range(args.runs):
            text, latency, err = call_ollama(
                args.url, args.model, system_prompt, user_msg, args.timeout)
            decision = CFOperator._parse_triage_response(text) if text else None
            action = decision["action"] if decision else None
            correct = action in case["expected"] if action else False
            results.append({
                "case": case["name"],
                "run": run,
                "valid": decision is not None,
                "action": action,
                "expected": case["expected"],
                "correct": correct,
                "latency_s": latency,
                "confidence": decision["confidence"] if decision else None,
                "error": err,
                "raw": (text[:500] if (args.keep_raw and decision is None)
                        else None),
            })
            mark = "OK " if correct else ("BAD" if decision else "ERR")
            print(f"  [{mark}] {case['name']:20} run{run}  "
                  f"{str(action):12} {latency:6.2f}s"
                  + (f"  {err}" if err else ""))

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
    }

    per_case = {}
    for case in CASES:
        runs = [r for r in results if r["case"] == case["name"]]
        per_case[case["name"]] = {
            "expected": case["expected"],
            "correct": sum(1 for r in runs if r["correct"]),
            "of": len(runs),
            "actions": sorted({str(r["action"]) for r in runs}),
        }

    print(f"\n{'='*60}")
    print(f"model            {summary['model']} (ollama {version})")
    print(f"JSON valid       {valid}/{len(results)} "
          f"({summary['json_valid_rate']*100:.1f}%)")
    print(f"Action accuracy  {correct}/{len(results)} "
          f"({summary['action_accuracy']*100:.1f}%)")
    print(f"Latency          mean {summary['latency_mean_s']}s  "
          f"max {summary['latency_max_s']}s")
    if summary["errors"]:
        print(f"Errors           {summary['errors']}")
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
