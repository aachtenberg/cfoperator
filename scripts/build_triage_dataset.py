#!/usr/bin/env python3
"""
Build a triage fine-tuning dataset from investigation history.

Emits chat-format JSONL (system/user/assistant) for supervised fine-tuning of
a local triage model (first target: mistral 3 14b), where each example is a
historical alert reconstructed into the EXACT production triage prompt and the
assistant turn is the rubric-correct decision derived from how the
investigation actually ended.

Fidelity rules (same philosophy as benchmarks/triage_eval.py):
  - The system prompt is EXTRACTED FROM agent/agent.py AT RUNTIME via the
    benchmark harness's own loader. A model fine-tuned against a paraphrased
    prompt would not transfer to production inference.
  - The user message is assembled by the same format strings run_triage uses.
  - Anything resembling a benchmarks/triage_eval.py CASE is EXCLUDED, so the
    existing eval harness remains a valid held-out test for the fine-tune.
    Train on the eval and the harness can no longer tell you anything.

What the DB can and cannot give us
----------------------------------
Investigations persist trigger text, outcome, findings (final response,
recommendation, similar_past citations) — but NOT the original alert's
severity/labels, and NOT the triage decision itself (it is returned to
event_runtime and never stored). So:

  - The similar-investigations block is reconstructed retrospectively: each
    investigation's trigger is embedded with the SAME model production uses
    (nomic-embed-text via ollama) and matched against STRICTLY EARLIER
    investigations only, so no example's context leaks the future. (The
    persisted findings.similar_past citations would have been preferable,
    but they only exist on rows written after CFOP-31 — 6 of 1770 at the
    time of writing — and their hybrid combined-score sits on a different
    scale than the eval suite's synthetic similarities. Cosine over the
    production embedding model is the consistent reconstruction.)
  - severity/labels are re-derived from the trigger text where a conservative
    pattern allows it, else left "unknown"/sparse. meta.severity_source
    records which. This is the one known fidelity gap.
  - The training label is DERIVED, not observed: every stored investigation
    was, by definition, routed "investigate", so the useful label is the
    retrospective one — what the cheapest correct action would have been,
    per the deployed rubric, given how the investigation ended. The exact
    rule that fired is recorded in meta.label_basis so bad derivations can
    be audited and filtered rather than silently trained on.

Derivation rules (rubric clause in parentheses):
  log_only     trigger matches known noise: smoke-test-*/tmp-* pods,
               Alertmanager Watchdog ("Known noise").
  escalate     the investigation itself concluded escalate.
  notify       this investigation ended cheaply (resolved/monitoring) AND a
               resolved precedent with similarity >= 0.85 was in its context
               ("similar past investigation resolved with little effort").
               needs_action rows never downgrade to notify — they needed the
               investigation.
  investigate  everything else, including resolved-without-precedent (it was
               novel; investigating was correct and it worked).

Usage:
    # From the console API (port-forward or in-cluster; Bearer token for
    # services — see web_server.py console auth):
    CFOP_TOKEN=... PYTHONPATH=agent:. .venv/bin/python \
        scripts/build_triage_dataset.py \
        --base-url http://localhost:8083 --limit 1000 \
        --out-dir benchmarks/datasets

    # Offline, from a JSON dump (list of /api/investigations/<id> objects):
    PYTHONPATH=agent:. .venv/bin/python scripts/build_triage_dataset.py \
        --from-file dump.json --out-dir benchmarks/datasets

Output: triage_train.jsonl / triage_val.jsonl (temporal split — the newest
fraction becomes validation so the split respects time, matching how the
model will actually be used). One JSON object per line:
    {"messages": [...], "meta": {...}}
Most SFT loaders ignore the extra "meta" key; pass --no-meta for ones that
do not.

DO NOT COMMIT THE OUTPUT. The JSONL contains this homelab's real alert and
investigation text and the repo is public. benchmarks/datasets/ is
gitignored for this reason.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Same path dance as benchmarks/triage_eval.py, for the same reason: agent/
# must be importable for its bare imports, repo root must win so
# `from agent.agent import ...` resolves the package, and benchmarks/ gets us
# the harness whose prompt loader and CASES we must stay consistent with.
sys.path.insert(0, os.path.join(_REPO_ROOT, "agent"))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "benchmarks"))

import triage_eval  # noqa: E402  (load_production_system_prompt, CASES)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_investigations(base_url: str, token: str, limit: int) -> list:
    base = base_url.rstrip("/")
    listing = _get(f"{base}/api/investigations?limit={limit}", token)
    rows = listing.get("investigations", [])
    out = []
    for row in rows:
        inv_id = row.get("id")
        if inv_id is None:
            continue
        try:
            detail = _get(f"{base}/api/investigations/{inv_id}", token)
        except urllib.error.HTTPError as e:
            print(f"  skip #{inv_id}: HTTP {e.code}", file=sys.stderr)
            continue
        out.append(detail)
    return out


# ── Reconstruction ────────────────────────────────────────────────────────────

# Investigations whose trigger never passed through run_triage: the OODA
# sweep's internal monitoring_cycle entries, operator-initiated retries and
# re-investigations, sweep tasks, and remediation verification runs. They are
# real investigations but not alert-shaped — training triage on them teaches
# a distribution the classifier never sees in production.
NON_TRIAGE_RE = re.compile(
    r"^(monitoring_cycle\b|Retry of |Context-driven re-investigation"
    r"|CFOperator sweep|verify remediation "
    # Imperative task-shaped triggers (sweep recommendations and operator
    # requests: "Investigate X pattern: ...", "Monitor Loki stability").
    # Alertmanager summaries state conditions; they don't give orders.
    r"|Investigate |Watch |Monitor |Check )")

# Test-pod prefixes are case-insensitive; "Watchdog" is deliberately
# case-sensitive — the rubric's noise clause means the Alertmanager Watchdog
# alert (capitalized alertname), while lowercase "watchdog" in a trigger is
# usually a real hardware/systemd watchdog event (this homelab has had those)
# and must not be labeled log_only.
NOISE_RE = re.compile(r"(?i:smoke-test-|tmp-)|\bWatchdog\b")

# Conservative severity derivation from trigger text. Only patterns whose
# production severity is unambiguous in this homelab's alert vocabulary get a
# value; everything else stays "unknown" rather than teaching the model an
# invented severity/action correlation.
_SEVERITY_PATTERNS = [
    (re.compile(r"control.plane.*NotReady|NotReady.*control.plane", re.I), "critical"),
    (re.compile(r"multiple services|all reporting.*down", re.I), "critical"),
    (re.compile(r"backup completed|completed with.*skipped", re.I), "info"),
    (re.compile(r"crash.?loop|OOMKilled|ImagePullBackOff|restarts", re.I), "warning"),
    (re.compile(r"I/O errors|read-only|filesystem", re.I), "warning"),
]

# Handles both "Pod name-xyz in namespace ns" and "Pod ns/name-xyz" forms.
_POD_RE = re.compile(
    r"\bPod (?:([a-z0-9-]+)/)?([a-z0-9][a-z0-9.-]*)", re.IGNORECASE)
_NS_RE = re.compile(r"\bnamespace ([a-z0-9-]+)", re.IGNORECASE)
_NODE_RE = re.compile(r"\b(?:Node|on|host) ([a-z][a-z0-9-]*(?:pi|cm5|llm|gpu)[a-z0-9-]*)", re.IGNORECASE)


def derive_severity(trigger: str):
    for pattern, sev in _SEVERITY_PATTERNS:
        if pattern.search(trigger):
            return sev, "derived"
    return "unknown", "unknown"


def derive_labels(trigger: str) -> dict:
    labels = {}
    m = _POD_RE.search(trigger)
    if m:
        labels["pod"] = m.group(2)
        if m.group(1):
            labels["namespace"] = m.group(1)
    if "namespace" not in labels:
        m = _NS_RE.search(trigger)
        if m:
            labels["namespace"] = m.group(1)
    m = _NODE_RE.search(trigger)
    if m:
        labels["node"] = m.group(1)
    return labels


def derive_label(trigger: str, outcome: str, similar_past: list):
    """Rubric-correct action from the context VISIBLE AT TRIAGE TIME.

    Returns (action, basis, reason, confidence), or None when the visible
    context and hindsight disagree. Hindsight (the outcome) is used only to
    confirm the rubric's answer or to resolve cases the rubric leaves open —
    never to override it. A resolved-precedent alert that nonetheless ended
    needs_action is a rubric miss, not a training example: labeling it
    "notify" trains the miss, labeling it "investigate" trains the model to
    contradict the deployed rubric the eval harness grades against. Skip
    those (counted as "conflict") and review them by hand — they are the
    interesting ones.

    STATED EXCEPTION — escalate labels from hindsight alone. The rubric's
    escalate clause needs severity AND breadth, but neither is reliably
    reconstructable from a stored trigger (derived severity is usually
    "unknown"), so a visible-context escalate check would drop nearly every
    escalate example and leave the class untrained. An escalate outcome is
    also the one label a human implicitly confirmed (the page happened and
    was warranted), which no other outcome gives us. The risk this accepts —
    training an escalate reflex from surface features — is exactly what the
    eval's critical-narrow and warning-correlated traps measure, so a bad
    reflex is caught at the gate rather than silently deployed.

    Reasons are short and rubric-flavoured — training targets for the style
    of reason run_triage's parser accepts, not incident ground truth.
    """
    if NOISE_RE.search(trigger):
        if outcome in ("needs_action", "escalate", "escalated"):
            return None  # noise-shaped trigger that turned out real
        return ("log_only", "noise-pattern",
                "known noise pattern (test pod or watchdog heartbeat)", 0.95)
    if outcome in ("escalate", "escalated"):
        return ("escalate", "outcome-escalate",
                "critical with broad impact, operator should page in", 0.9)
    resolved_precedent = any(
        (s.get("outcome") == "resolved"
         and float(s.get("similarity") or 0) >= 0.85)
        for s in similar_past
    )
    if resolved_precedent:
        if outcome in ("resolved", "monitoring"):
            return ("notify", "resolved-precedent",
                    "similar past investigation resolved with little effort",
                    0.85)
        return None  # rubric said notify, reality needed action — conflict
    basis = {
        "needs_action": "outcome-needs-action",
        "monitoring": "outcome-monitoring",
        "resolved": "novel-but-resolved",
    }.get(outcome, "default")
    return ("investigate", basis,
            "no resolved precedent for this pattern", 0.8)


def build_user_message(severity: str, trigger: str, labels: dict,
                       similar_past: list) -> str:
    """Mirror of run_triage's user message assembly (agent/agent.py)."""
    similar_context = ""
    if similar_past:
        lines = []
        for s in similar_past[:3]:
            sim = float(s.get("similarity") or 0)
            lines.append(
                f"- [{s.get('outcome', '?'):10}] "
                f"{str(s.get('trigger', ''))[:100]} (similarity: {sim:.2f})"
            )
        similar_context = "\n\nSimilar past investigations:\n" + "\n".join(lines)
    return (
        f"Alert severity: {severity}\n"
        f"Alert summary: {trigger}\n"
        f"Labels: {json.dumps(labels, default=str)[:500]}"
        f"{similar_context}\n\n"
        "Classify."
    )


# ── Retrospective retrieval context ──────────────────────────────────────────

def embed_triggers(triggers: list, embed_url: str, embed_model: str,
                   cache_path: str) -> dict:
    """Embed unique trigger strings via ollama, with a JSON disk cache.

    Uses the same embedding model as production so 'similar' here means what
    it means at triage time. ~10ms per call on the GPU box; the cache makes
    re-runs free.
    """
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)
    todo = [t for t in set(triggers) if t not in cache]
    for n, text in enumerate(todo):
        body = json.dumps({"model": embed_model,
                           "prompt": text or " "}).encode()
        req = urllib.request.Request(
            embed_url.rstrip("/") + "/api/embeddings", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            cache[text] = json.loads(resp.read())["embedding"]
        if n and n % 200 == 0:
            print(f"  embedded {n}/{len(todo)}", file=sys.stderr)
    if todo:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    return cache


def retrospective_similar(investigations: list, embeddings: dict,
                          limit: int = 3, floor: float = 0.5) -> dict:
    """Top-`limit` similar STRICTLY EARLIER investigations per row.

    Returns {row_index: [citation, ...]} in findings.similar_past shape.
    Temporal ordering is by started_at (the caller must pass rows sorted
    that way); an investigation can only cite ones that already existed,
    matching what the production hybrid search could have returned. The
    floor drops junk matches the search would not have surfaced.
    """
    import numpy as np
    vecs = np.array([embeddings[i.get("trigger") or ""] for i in investigations])
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vecs / norms
    out = {}
    for i in range(len(investigations)):
        if i == 0:
            out[i] = []
            continue
        sims = unit[:i] @ unit[i]
        top = sims.argsort()[::-1][:limit]
        out[i] = [
            {
                "id": investigations[j]["id"],
                "trigger": investigations[j]["trigger"],
                "outcome": investigations[j]["outcome"],
                "similarity": float(sims[j]),
            }
            for j in top if sims[j] >= floor
        ]
    return out


# ── Benchmark exclusion ───────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def overlaps_benchmark(trigger: str, case_token_sets: list) -> bool:
    """True when a trigger is close enough to an eval case to be held out.

    Jaccard >= 0.5 on word tokens, or containment either way. Deliberately
    aggressive: losing a few training examples is cheap, contaminating the
    only eval that can grade the fine-tune is not.
    """
    trig_tokens = _tokens(trigger)
    if not trig_tokens:
        return False
    for case_tokens in case_token_sets:
        inter = len(trig_tokens & case_tokens)
        union = len(trig_tokens | case_tokens)
        if union and inter / union >= 0.5:
            return True
        if inter == len(case_tokens) or inter == len(trig_tokens):
            return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def build_examples(investigations: list, system_prompt: str,
                   max_per_trigger: int, include_meta: bool,
                   similar_by_index: dict):
    case_token_sets = [_tokens(c["summary"]) for c in triage_eval.CASES]
    examples = []
    skipped = {"unfinished": 0, "non_triage": 0, "benchmark": 0,
               "dedup": 0, "conflict": 0}
    conflicts = []
    per_trigger = {}

    for idx, inv in enumerate(investigations):
        outcome = inv.get("outcome")
        if outcome in (None, "in_progress", "failed"):
            skipped["unfinished"] += 1
            continue
        trigger = (inv.get("trigger") or "").strip()
        is_deep = trigger.startswith("[deep] ")
        if is_deep:
            trigger = trigger[len("[deep] "):]
        if not trigger:
            skipped["unfinished"] += 1
            continue
        if NON_TRIAGE_RE.match(trigger):
            skipped["non_triage"] += 1
            continue
        if overlaps_benchmark(trigger, case_token_sets):
            skipped["benchmark"] += 1
            continue

        similar_past = similar_by_index.get(idx, [])
        severity, severity_source = derive_severity(trigger)
        labels = derive_labels(trigger)
        derived = derive_label(trigger, outcome, similar_past)
        if derived is None:
            skipped["conflict"] += 1
            conflicts.append({"investigation_id": inv.get("id"),
                              "trigger": trigger, "outcome": outcome})
            continue
        action, basis, reason, confidence = derived

        # Alerts refire; without a cap the dataset is dominated by whichever
        # alert flapped hardest. Dedup by (trigger, label), NOT trigger
        # alone: a refiring alert's early occurrences are investigate
        # examples and its later refires (resolved precedent now visible)
        # are the notify class — trigger-only dedup would delete the notify
        # class entirely.
        key = (" ".join(sorted(_tokens(trigger)))[:300], action)
        per_trigger[key] = per_trigger.get(key, 0) + 1
        if per_trigger[key] > max_per_trigger:
            skipped["dedup"] += 1
            continue

        example = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": build_user_message(
                    severity, trigger, labels, similar_past)},
                {"role": "assistant", "content": json.dumps({
                    "action": action,
                    "reason": reason,
                    "confidence": confidence,
                }, indent=2)},
            ],
        }
        if include_meta:
            example["meta"] = {
                "investigation_id": inv.get("id"),
                "started_at": inv.get("started_at"),
                "outcome": outcome,
                "deep": is_deep,
                "label": action,
                "label_basis": basis,
                "severity_source": severity_source,
                "tool_calls_count": inv.get("tool_calls_count"),
            }
        examples.append(example)

    return examples, skipped, conflicts


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--base-url", default="http://localhost:8083",
                     help="console base URL (default: %(default)s)")
    src.add_argument("--from-file",
                     help="JSON file with a list of investigation detail dicts "
                          "(offline mode / testing)")
    ap.add_argument("--limit", type=int, default=1000,
                    help="max investigations to fetch (default: %(default)s)")
    ap.add_argument("--max-per-trigger", type=int, default=3,
                    help="cap examples per distinct trigger (default: %(default)s)")
    ap.add_argument("--eval-frac", type=float, default=0.1,
                    help="newest fraction reserved for validation (default: %(default)s)")
    ap.add_argument("--out-dir", default=os.path.join("benchmarks", "datasets"))
    ap.add_argument("--no-meta", action="store_true",
                    help="omit the meta key for strict SFT loaders")
    ap.add_argument("--embed-url", default="http://localhost:11434",
                    help="ollama endpoint for the production embedding model "
                         "(default: %(default)s)")
    ap.add_argument("--embed-model", default="nomic-embed-text",
                    help="embedding model — keep the production one "
                         "(default: %(default)s)")
    args = ap.parse_args()

    system_prompt = triage_eval.load_production_system_prompt()

    if args.from_file:
        with open(args.from_file) as f:
            investigations = json.load(f)
    else:
        token = os.getenv("CFOP_TOKEN", "")
        if not token:
            print("warning: CFOP_TOKEN not set — console auth will likely "
                  "reject the request", file=sys.stderr)
        investigations = fetch_investigations(args.base_url, token, args.limit)

    print(f"fetched {len(investigations)} investigations")
    # Temporal order is load-bearing: retrospective retrieval may only cite
    # strictly earlier rows, and the train/val split is by time.
    investigations = sorted(
        investigations, key=lambda i: i.get("started_at") or "")

    os.makedirs(args.out_dir, exist_ok=True)
    embeddings = embed_triggers(
        [i.get("trigger") or "" for i in investigations],
        args.embed_url, args.embed_model,
        cache_path=os.path.join(args.out_dir, "trigger_embeddings.json"))
    similar_by_index = retrospective_similar(investigations, embeddings)

    examples, skipped, conflicts = build_examples(
        investigations, system_prompt,
        max_per_trigger=args.max_per_trigger,
        include_meta=not args.no_meta,
        similar_by_index=similar_by_index)

    # Temporal split: newest slice is validation, so evaluation always looks
    # forward in time relative to training — the deployment condition.
    n_val = int(len(examples) * args.eval_frac)
    train = examples[:len(examples) - n_val] if n_val else examples
    val = examples[len(examples) - n_val:] if n_val else []

    os.makedirs(args.out_dir, exist_ok=True)
    for name, rows in (("triage_train.jsonl", train), ("triage_val.jsonl", val)):
        path = os.path.join(args.out_dir, name)
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"wrote {len(rows):4d} -> {path}")

    labels = {}
    for e in examples:
        a = json.loads(e["messages"][2]["content"])["action"]
        labels[a] = labels.get(a, 0) + 1
    print(f"labels: {labels}")
    print(f"skipped: {skipped}")
    if conflicts:
        # Rubric/hindsight disagreements are worth human eyes: each one is an
        # alert the deployed rubric would have under- or over-triaged.
        path = os.path.join(args.out_dir, "triage_conflicts.json")
        with open(path, "w") as f:
            json.dump(conflicts, f, indent=2)
        print(f"wrote {len(conflicts):4d} rubric/hindsight conflicts -> {path} "
              "(excluded from training; review by hand)")
    print("reminder: output contains real homelab data — do not commit; "
          "benchmarks/triage_eval.py remains the held-out eval.")


if __name__ == "__main__":
    main()
