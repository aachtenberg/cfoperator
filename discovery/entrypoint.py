"""Discovery pass entrypoint — one-shot fleet characterization (CFOP-47).

Runs as a ``docker run`` / one-shot Job. Contract only — no imports from the
agent monolith:

  in   env: PROMETHEUS_URL (required); optional k8s access (in-cluster SA or
            CFOP_DISCOVERY_K8S_URL + CFOP_DISCOVERY_K8S_TOKEN);
            CFOP_DISCOVERY_LLM_* (see llm.py);
            CFOP_DISCOVERY_MAX_ROUNDS (extra query rounds, default 2);
            CFOP_DISCOVERY_REPORT_DIR (optional: write report.md/report.json);
            CFOP_AGENT_URL + CFOP_API_TOKEN (OPTIONAL — absent = report-only)
  do   deterministic enumeration (inventory.py) -> bounded LLM characterization
       (the model may request extra instant PromQL queries, then must return
       learnings) -> markdown + JSON report with a what-was-queried appendix
  out  report on stdout (+ files); learnings POSTed to /api/learnings when an
       agent URL + token are configured

Every learning must carry ``applies_when``: the KB auto-deprecates learnings
without a trigger condition (they can never match a search), so seeding them
would be a no-op that looks successful. Validation here enforces that before
anything is pushed. Stdlib only; SSH never.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from inventory import QueryLog, enumerate_fleet, instant_query
from llm import LLM, LLMError, make_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cfop-discovery")

LEARNING_TYPES = ("pattern", "solution", "root_cause", "antipattern", "insight")
MAX_EXTRA_QUERIES_PER_ROUND = 8
MAX_LEARNINGS = 30

_PROMPT = """You are characterizing an infrastructure fleet for an SRE knowledge base.
Below is a facts document enumerated deterministically from Prometheus{k8s_note}.
The facts are the only inventory that exists — do not invent hosts, services, or
metrics that are not in them.

Your job is what a senior SRE does in their first hour on a new fleet: infer
roles ("this is the Postgres primary"), relationships, naming conventions,
exporter types, and a first sketch of normal (baseline restart counts, resource
envelopes). Mark uncertainty honestly via the confidence field.

{action_note}

Respond with ONLY a JSON object, no prose around it. Either:

  {{"queries": ["<promql instant query>", ...]}}     (max {max_queries}; only if specific
                                                    facts are missing)
or the final characterization:

  {{"overview_markdown": "<a readable 'here is your fleet' overview>",
    "learnings": [
      {{"learning_type": "insight",            // one of {types}
        "title": "...",                        // short, specific
        "description": "...",                  // the inferred fact and the evidence for it
        "applies_when": "...",                 // REQUIRED: when a future investigation should surface this
        "services": ["..."],
        "tags": ["..."],
        "confidence": 0.0-1.0}}
    ]}}

FACTS:
{facts}
"""

_FINAL_NUDGE = ("No further queries are available. Produce the final characterization "
                "JSON now, from the facts you have.")


def parse_model_json(text: str) -> Dict[str, Any]:
    """Extract the JSON object from a model reply (tolerates ``` fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("model reply contains no JSON object")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("model reply is not a JSON object")
    return obj


def validate_learnings(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Keep well-formed learnings; return (kept, reasons-for-dropped).

    Dropping is loud, not silent: rejects land in the report so the operator
    sees what the model proposed and why it was refused.
    """
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    if not isinstance(raw, list):
        return [], ["learnings is not a list"]
    for i, item in enumerate(raw[:MAX_LEARNINGS]):
        if not isinstance(item, dict):
            dropped.append(f"#{i}: not an object")
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        applies_when = str(item.get("applies_when") or "").strip()
        if not (title and description):
            dropped.append(f"#{i}: missing title or description")
            continue
        if not applies_when:
            # The KB born-deprecates trigger-less learnings — pushing one would
            # be dead weight that looks like success.
            dropped.append(f"#{i} ({title[:40]}): missing applies_when")
            continue
        ltype = str(item.get("learning_type") or "insight").strip()
        if ltype not in LEARNING_TYPES:
            ltype = "insight"
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        kept.append({
            "learning_type": ltype,
            "title": title[:500],
            "description": description[:20000],
            "applies_when": applies_when[:5000],
            "services": [str(s) for s in item.get("services") or [] if str(s).strip()][:20],
            "tags": [str(t) for t in item.get("tags") or [] if str(t).strip()][:20],
            "confidence": confidence,
        })
    if isinstance(raw, list) and len(raw) > MAX_LEARNINGS:
        dropped.append(f"{len(raw) - MAX_LEARNINGS} learnings over the {MAX_LEARNINGS} cap")
    return kept, dropped


def characterize(llm: LLM, facts: Dict[str, Any], prom_url: str, log: QueryLog,
                 max_rounds: int) -> Dict[str, Any]:
    """Bounded characterization loop: at most ``max_rounds`` extra-query rounds,
    then one forced-final call. Total LLM calls <= max_rounds + 2."""
    has_k8s = "kubernetes" in facts
    extra: Dict[str, Any] = {}

    def build_prompt(action_note: str) -> str:
        doc = dict(facts)
        if extra:
            doc["followup_queries"] = extra
        return _PROMPT.format(
            k8s_note=" and the Kubernetes API" if has_k8s else "",
            action_note=action_note,
            max_queries=MAX_EXTRA_QUERIES_PER_ROUND,
            types=list(LEARNING_TYPES),
            facts=json.dumps(doc, indent=1, default=str),
        )

    reply = parse_model_json(llm.complete(build_prompt(
        "You may request extra Prometheus instant queries first if needed.")))
    rounds = 0
    # Continue only for actual query strings with no characterization yet:
    # models emit "queries": [] on a finished reply, or both keys at once —
    # neither should discard the characterization or burn a round.
    while (reply.get("queries") or []) and "learnings" not in reply and rounds < max_rounds:
        rounds += 1
        queries = [str(q) for q in (reply.get("queries") or [])][:MAX_EXTRA_QUERIES_PER_ROUND]
        logger.info("round %d: model requested %d follow-up queries", rounds, len(queries))
        for q in queries:
            extra[q] = instant_query(prom_url, q, log)
        note = ("You may request extra queries once more, or produce the final JSON."
                if rounds < max_rounds else _FINAL_NUDGE)
        reply = parse_model_json(llm.complete(build_prompt(note)))
    if "learnings" not in reply:
        # Budget exhausted and the model still wants queries: force the final.
        reply = parse_model_json(llm.complete(build_prompt(_FINAL_NUDGE)))
    return reply


# ---- report ------------------------------------------------------------------


def render_report(overview: str, learnings: List[Dict[str, Any]], dropped: List[str],
                  log: QueryLog, pushed: Optional[List[int]] = None) -> str:
    lines = ["# Fleet discovery report", "", overview.strip(), "", "## Proposed learnings", ""]
    if not learnings:
        lines.append("_none_")
    for i, l in enumerate(learnings):
        pushed_note = ""
        if pushed is not None and i < len(pushed):
            pushed_note = f" — stored as KB learning #{pushed[i]}" if pushed[i] > 0 else " — push FAILED"
        lines.append(f"- **{l['title']}** ({l['learning_type']}, confidence {l['confidence']:.2f}){pushed_note}")
        lines.append(f"  - {l['description']}")
        lines.append(f"  - applies when: {l['applies_when']}")
    if dropped:
        lines += ["", "## Dropped by validation", ""] + [f"- {d}" for d in dropped]
    lines += ["", "## What was queried", "",
              "Every request this pass made, in order — read-only, no SSH:", ""]
    for e in log.entries:
        lines.append(f"- [{e['source']}] {e['what']} — {e['status']}")
    return "\n".join(lines) + "\n"


# ---- push mode ---------------------------------------------------------------


def push_learnings(agent_url: str, token: str, learnings: List[Dict[str, Any]],
                   timeout: int = 30) -> List[int]:
    """POST each learning to the agent's /api/learnings. Returns ids (-1 = failed).

    This is the only write path the pass has — no DB access, ever."""
    ids: List[int] = []
    url = f"{agent_url.rstrip('/')}/api/learnings"
    for l in learnings:
        body = dict(l)
        body["source"] = "discovery"
        body["inferred"] = True
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator-config URL
                ids.append(int(json.loads(resp.read().decode("utf-8")).get("id", -1)))
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.error("push failed for %r: %s", l["title"][:60], e)
            ids.append(-1)
    return ids


# ---- main --------------------------------------------------------------------


def run(env: Dict[str, str]) -> int:
    log = QueryLog()
    prom_url = (env.get("PROMETHEUS_URL") or "").strip()
    facts = enumerate_fleet(env, log)
    llm = make_llm(env)
    max_rounds = int(env.get("CFOP_DISCOVERY_MAX_ROUNDS", "2") or 2)

    reply = characterize(llm, facts, prom_url, log, max_rounds)
    learnings, dropped = validate_learnings(reply.get("learnings"))
    overview = str(reply.get("overview_markdown") or "").strip() or "(model produced no overview)"

    agent_url = (env.get("CFOP_AGENT_URL") or "").strip()
    token = (env.get("CFOP_API_TOKEN") or "").strip()
    pushed: Optional[List[int]] = None
    if agent_url and token:
        pushed = push_learnings(agent_url, token, learnings)
        logger.info("pushed %d/%d learnings", sum(1 for i in pushed if i > 0), len(learnings))
    else:
        logger.info("report-only mode (no CFOP_AGENT_URL/CFOP_API_TOKEN)")

    report = render_report(overview, learnings, dropped, log, pushed)
    result = {
        "learnings": learnings,
        "dropped": dropped,
        "pushed_ids": pushed,
        "queried": log.entries,
    }
    report_dir = (env.get("CFOP_DISCOVERY_REPORT_DIR") or "").strip()
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "report.md"), "w") as f:
            f.write(report)
        with open(os.path.join(report_dir, "report.json"), "w") as f:
            json.dump(result, f, indent=1, default=str)
    print(report)

    if pushed is not None and learnings and all(i <= 0 for i in pushed):
        return 1  # push mode where nothing landed is a failure, not a report
    return 0


def main() -> int:
    try:
        return run(dict(os.environ))
    except (ValueError, LLMError) as e:
        logger.error("discovery pass failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
