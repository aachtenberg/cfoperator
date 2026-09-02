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


# CFOP-153. The regexes above take whatever word follows "pod"/"namespace",
# which in prose is usually an English word: "Pod with high memory" yielded
# {"pod": "with"} and "namespace has been" yielded {"namespace": "has"}. That
# was ~19% of populated Labels in the v1 training set, so the model was
# effectively trained on the summary line alone.
#
# The shape test is the fix. Every pod/namespace/node name in this fleet
# carries a digit or a hyphen (promtail-2xvvb, loki-0, raspberrypi3), and no
# English stopword does. Deliberately conservative in the same direction as
# derive_severity: a bare single-word name like "prometheus" is dropped rather
# than guessed at. A missing label is a known gap; a wrong one is training
# noise that looks like signal.
_LABEL_STOPWORDS = frozenset("""
a an and are as at be been by during for from had has have in is it its no not
of on or that the this to was were when where which while with without after
before above below over under
""".split())


# Words that appear in alert prose but never as a whole segment of an object
# name here. Only used to spot compounds like "crash-looping" / "not-ready";
# a name is rejected only when EVERY segment is one of these, so
# node-exporter-zgzxm and kube-state-metrics survive.
_PROSE_SEGMENTS = _LABEL_STOPWORDS | frozenset("""
crash crashing looping loop ready unready memory cpu disk usage high low full
restart restarting restarted pending failed failing error errors down up out
killed unavailable unhealthy timeout timed slow stuck stalled missing lost
degraded oom evicted terminating unreachable
""".split())


def _is_english_compound(tok: str) -> bool:
    """True when every hyphen-separated segment is an ordinary word."""
    segs = [x for x in tok.split("-") if x]
    return len(segs) > 1 and all(x in _PROSE_SEGMENTS for x in segs)


def _plausible_k8s_name(token, *, from_prose: bool = False) -> str | None:
    """A token that could be a real object name here, else None.

    ``from_prose`` is the important distinction. Where the token has no
    structural anchor -- the bare word after "Pod" -- prose supplies things
    like "restarting" that no stopword list will ever fully cover, so the
    name is additionally required to LOOK like a Kubernetes object: a digit
    or a hyphen, which every pod name in this fleet carries (promtail-2xvvb,
    loki-0).

    "and no English word does" was the overstatement, caught in review on
    PR #240: single words do not, but COMPOUNDS do, so "Pod crash-looping"
    still produced {"pod": "crash-looping"} -- and the regression test used
    the unhyphenated "crashlooping", so it did not catch it. Hence the
    second test: a token whose hyphen-separated segments are ALL ordinary
    words is prose, whatever its punctuation.

    Rejecting hyphens outright, or demanding a digit, was measured against
    the corpus first and costs four real names (node-exporter-zgzxm,
    kube-state-metrics, promtail-fdppw, node-exporter-nvtfv) whose random
    suffix happens to be all letters. The segment test costs none of them.

    Where the token IS anchored, that extra test would do damage rather than
    good: the namespace in "Pod apps/camera-api-5f" is fixed by the slash,
    and _NODE_RE already constrains its capture to names containing
    pi/cm5/llm/gpu. Namespaces (`apps`, `argocd`, `monitoring`) and the node
    literally named `raspberrypi` carry neither a digit nor a hyphen, so
    requiring one there silently drops correct labels -- which is the same
    class of error as the bug being fixed, pointed the other way.
    """
    tok = str(token or "").strip().lower().rstrip(".,;:)!?'\"")
    if len(tok) < 3 or tok in _LABEL_STOPWORDS:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", tok):
        return None
    if from_prose:
        if not re.search(r"[-0-9]", tok):
            return None
        if _is_english_compound(tok):
            return None
    return tok


def derive_labels(trigger: str) -> dict:
    labels = {}
    m = _POD_RE.search(trigger)
    if m:
        # The pod name is the one capture with no structural anchor.
        pod = _plausible_k8s_name(m.group(2), from_prose=True)
        if pod:
            labels["pod"] = pod
        # Independently of the pod. Review on PR #240: nesting this under
        # `if pod` meant "Pod apps/prometheus is unavailable" dropped the
        # namespace too, because `prometheus` fails the prose shape test --
        # discarding a slash-anchored fact on account of an unrelated one,
        # and contradicting the contract stated above.
        ns = _plausible_k8s_name(m.group(1)) if m.group(1) else None
        if ns:
            labels["namespace"] = ns
    if "namespace" not in labels:
        m = _NS_RE.search(trigger)
        if m:
            # From prose, but namespaces are plain words, so the stopword
            # list is the only guard available here.
            ns = _plausible_k8s_name(m.group(1))
            if ns:
                labels["namespace"] = ns
    m = _NODE_RE.search(trigger)
    if m:
        # _NODE_RE already requires pi/cm5/llm/gpu in the name.
        node = _plausible_k8s_name(m.group(1))
        if node:
            labels["node"] = node
    return labels


# Words that show the alert itself claims breadth. Used only to decide
# whether an escalate reason may assert blast radius; absence means the
# reason stays silent about it rather than inventing it.
_BREADTH_RE = re.compile(
    r"\b(quorum|cluster[- ]?wide|multiple|several|all nodes|all pods|"
    r"across \w+|fleet|outage|every |both nodes|control plane)\b",
    re.IGNORECASE)


def _alert_subject(trigger: str, labels: dict) -> str:
    """The most specific thing this alert is about, for use in a reason.

    Prefers a real object name (labels are shape-checked upstream), then any
    distinctive token in the trigger, and only then a generic fallback. A
    reason that says "this alert" is not grounded, so the fallback is a last
    resort and shows up in the grounding test as such.
    """
    for key in ("pod", "node", "namespace"):
        if labels.get(key):
            return str(labels[key])
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]{3,}", trigger or ""):
        if re.search(r"[-0-9]", tok) and tok.lower() not in _LABEL_STOPWORDS:
            return tok
    # Plenty of real alerts name no object at all ("Certificate expiration
    # approaching"). Falling back to "this alert" made 20% of v2 reasons
    # generic -- they passed a token-overlap grounding check only via the
    # similarity number, which is grounding in the letter and not the spirit.
    # The alert's own words are always available and always on-topic.
    #
    # QUOTED, because it is a fragment being slotted into a frame. Unquoted it
    # produced mad-libs -- "Backup did not complete repeats an earlier
    # investigation", "no earlier investigation resembles Certificate
    # expiration approaching". Quoting is what makes a clause behave like a
    # noun in every frame below, without needing a second set of templates.
    clause = _clause(trigger, limit=60)
    return f'"{clause}"' if clause else "this alert"


def _clause(text: str, limit: int = 90) -> str:
    """First clause of a trigger, trimmed on a word boundary."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "..."


def _best_resolved_precedent(similar_past: list):
    """Highest-similarity RESOLVED precedent at or above the notify threshold.

    Separate from _best_precedent on purpose: this one decides the label, so
    it must keep the original rule's semantics exactly (any resolved hit at
    >=0.85), while _best_precedent only supplies wording for the branches
    where no such hit exists.
    """
    best = None
    for s in similar_past or []:
        if str(s.get("outcome") or "") != "resolved":
            continue
        sim = float(s.get("similarity") or 0)
        if sim < 0.85:
            continue
        if best is None or sim > best["similarity"]:
            best = {"similarity": sim, "outcome": "resolved",
                    "trigger": str(s.get("trigger") or "")}
    return best


def _best_precedent(similar_past: list):
    """Highest-similarity precedent as a plain dict, or None."""
    best = None
    for s in similar_past or []:
        sim = float(s.get("similarity") or 0)
        if best is None or sim > best["similarity"]:
            best = {"similarity": sim,
                    "outcome": str(s.get("outcome") or "?"),
                    "trigger": str(s.get("trigger") or "")}
    return best


def derive_label(trigger: str, outcome: str, similar_past: list,
                 labels: dict | None = None):
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

    REASONS ARE GROUNDED IN THE ALERT (CFOP-153). v1 emitted one of four fixed
    strings, one per action, so `reason` and `confidence` carried no
    information beyond `action` and the fine-tune learned exactly that
    four-way lookup: it shipped as an explainer that cannot explain. Every
    reason here names something from the alert it is describing — the pod,
    node or namespace, the noise token that matched, the precedent it is
    leaning on and how close it was. That is also the property
    docs/triage-eval-v2-plan.md Tier 3 grades ("must contain at least one
    token from the alert"), so the data is built to satisfy the test that
    will judge it.

    Deterministic templating, not an LLM. The goal is grounding, not fluency:
    a generated reason would need its own review pass, and an ungrounded
    fluent sentence is exactly the failure being fixed.

    CONFIDENCE TRACKS EVIDENCE, not the action. It scales with how good the
    precedent actually is, so it varies within an action class instead of
    being a rename of it. Note the floor: nothing here drops below ~0.45, so
    the deep-tier reroute (`0 < confidence < 0.4`) stays unreachable. Making
    it reachable is a live behaviour change for host-shaped alerts and is a
    separate decision, not a side effect of fixing the alias.
    """
    labels = labels or {}
    subject = _alert_subject(trigger, labels)
    best = _best_precedent(similar_past)

    m = NOISE_RE.search(trigger)
    if m:
        if outcome in ("needs_action", "escalate", "escalated"):
            return None  # noise-shaped trigger that turned out real
        token = m.group(0).strip()
        # Cite the token that actually matched. "(test/watchdog)" was a fixed
        # pair on every row, so a tmp- pod was described as watchdog traffic.
        # The other branch already had the token; both use it now.
        reason = (f"{subject} matches the known-noise pattern "
                  f"'{token}' — not a real workload")
        return ("log_only", "noise-pattern", reason, 0.9)

    if outcome in ("escalate", "escalated"):
        clause = _clause(trigger)
        # Don't say "raspberrypi3: Node raspberrypi3 NotReady..." -- the
        # clause usually already names the subject.
        # A quoted-clause subject IS this trigger, so prefixing it repeats the
        # sentence -- and _clause truncates with "...", so a naive containment
        # test misses that and duplicates anyway.
        bare = subject.strip('"').rstrip(".").strip()
        head = clause if bare.lower() in clause.lower() else f"{subject}: {clause}"
        # The escalate LABEL is hindsight -- the investigation ended escalate
        # -- but the reason is a triage-time claim. Asserting "severity and
        # blast radius both present" on every escalate row taught exactly the
        # escalate reflex the eval's critical-narrow and warning-correlated
        # traps exist to catch: on a single-pod page it is simply false, and a
        # canned suffix on the highest-consequence action is the worst place
        # to put an unearned claim. Only say it when the trigger shows it.
        breadth = _BREADTH_RE.search(trigger)
        if breadth:
            tail = (f"— severity and blast radius both present "
                    f"({breadth.group(0).strip().lower()}), page an operator now")
        else:
            tail = "— page an operator now"
        return ("escalate", "outcome-escalate", f"{head} {tail}", 0.88)

    # The notify rule is ANY resolved precedent at >=0.85, not "the closest
    # precedent happens to be a resolved one". Those differ whenever a
    # monitoring precedent outranks a resolved one -- and picking the wrong
    # side of that moves the LABEL, not just the wording. Getting this wrong
    # in development cost 35 notify examples (96 -> 61) and would have
    # retrained the class imbalance harder while looking like a reason-only
    # change.
    cited = _best_resolved_precedent(similar_past)
    if cited:
        if outcome in ("resolved", "monitoring"):
            conf = round(min(0.93, 0.70 + (cited["similarity"] - 0.85) * 1.5), 2)
            return ("notify", "resolved-precedent",
                    f"{subject} repeats an earlier investigation that resolved "
                    f"({cited['similarity']:.2f} similarity): "
                    f"{_clause(cited['trigger'])}", conf)
        return None  # rubric said notify, reality needed action — conflict

    basis = {
        "needs_action": "outcome-needs-action",
        "monitoring": "outcome-monitoring",
        "resolved": "novel-but-resolved",
    }.get(outcome, "default")

    if not best:
        reason = (f"no earlier investigation resembles {subject}; "
                  f"nothing to lean on, needs a first look")
        conf = 0.62
    elif best["similarity"] < 0.70:
        reason = (f"nothing close to {subject} in history "
                  f"(nearest {best['similarity']:.2f}); needs a first look")
        conf = 0.60
    else:
        # A near miss is genuinely more ambiguous than no precedent at all:
        # something similar happened and did NOT resolve cheaply.
        # No cosine here. On notify the number IS the reason you did not
        # investigate, so it earns its place; here the sentence already says
        # the useful thing, and a float in every frame teaches "a good reason
        # contains a float" -- citable-looking and trivially faked.
        reason = (f"closest earlier match to {subject} ended "
                  f"{best['outcome']} — no resolved precedent to lean on")
        conf = round(max(0.45, 0.58 - (best["similarity"] - 0.70) * 0.6), 2)
    return ("investigate", basis, reason, conf)


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
        derived = derive_label(trigger, outcome, similar_past, labels)
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
