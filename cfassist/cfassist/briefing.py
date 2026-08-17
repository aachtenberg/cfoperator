"""Turn a CFOperator investigation into a briefing a terminal agent can use.

This is the product of CFOP-29. CFOperator's most valuable output is not the
console page, it is the *briefing*: what it observed, what it concluded, what it
already queued. `attach` seeds that into an LLM session so the operator's first
question is "what do we do" rather than "what happened".

Pure functions over plain dicts — no HTTP, no click, no rich — so the assembly
can be tested against deliberately broken payloads.
"""

import re

# The cockpit handoff verb. `event_runtime/notifications.py` prints this command
# into every investigation-bearing Slack/Discord/ntfy notification and lives in a
# different deploy artifact, so the two are kept honest by
# cfassist/tests/test_attach_contract.py rather than by a shared import.
ATTACH_VERB = "attach"

# Appended to the system prompt (not shown in the terminal — the operator can
# already see the briefing). States the two things a model gets wrong when handed
# a snapshot: that it is current, and that it can act on CFOperator through it.
ATTACH_GUIDANCE = """\
You are attached to a CFOperator investigation as the operator's terminal
copilot. The briefing below is CFOperator's own account of the incident: what it
observed, what it concluded, and what it queued as a result.

Two things about it:

- It is a snapshot taken when this session started. Anything time-sensitive
  (pod state, disk usage, whether a service recovered) must be re-checked with
  your own tools before you act on it or report it as current.
- This session has read-only access to CFOperator. Approving, rejecting or
  queueing a remediation happens in the console or through the MCP server, not
  here. Recommend those actions; do not claim to have taken them.

You do have real hands on this machine via your own shell and file tools. Use
them to verify and to fix, and say plainly when the briefing and reality differ.
"""

# The id must be the whole string or the tail of a path/query, never just the
# trailing digits of arbitrary text: a bare `\d+$` accepted "-4" (as 4) and
# would accept "release-8" too, which is how you end up attached to the wrong
# incident.
_REF_PATTERN = re.compile(r"(?:^|[/=?#])(\d+)\s*$")


def attach_command(investigation_id):
    """The copy-pasteable one-liner. Single source of truth for the verb."""
    return f"cfassist {ATTACH_VERB} {investigation_id}"


def parse_investigation_ref(raw):
    """Coerce an operator-supplied reference to an investigation id.

    Accepts what people actually paste: ``1889``, ``#1889``, and a console URL
    or any other string ending in the id (``http://cfop/investigations/1889``).
    Raises ValueError otherwise rather than guessing, because attaching to the
    wrong incident is worse than a retype.
    """
    text = str(raw or "").strip().lstrip("#").strip()
    if not text:
        raise ValueError("no investigation id given")
    match = _REF_PATTERN.search(text)
    if not match:
        raise ValueError(f"not an investigation id: {raw!r}")
    value = int(match.group(1))
    if value <= 0:
        raise ValueError(f"not an investigation id: {raw!r}")
    return value


def investigation_facts(investigation):
    """Flatten an investigation into the fields the briefing renders.

    Exists because of a real API trap: ``/api/investigations`` returns
    ``outcome`` at the top level and no findings, while
    ``/api/investigations/<id>`` nests ``provider``, ``response`` and
    ``recommendation`` *inside* ``findings``. Reading the top level for a report
    yields an empty string and a briefing that says nothing, which is exactly
    the failure this function makes impossible to reintroduce quietly.
    """
    inv = investigation or {}
    findings = inv.get("findings")
    if not isinstance(findings, dict):
        # Older/degraded rows store findings as a bare string.
        findings = {"response": findings} if isinstance(findings, str) else {}

    return {
        "id": inv.get("id"),
        "outcome": str(inv.get("outcome") or "").strip(),
        "trigger": str(inv.get("trigger") or "").strip(),
        "host_id": str(inv.get("host_id") or "").strip(),
        "started_at": str(inv.get("started_at") or "").strip(),
        "completed_at": str(inv.get("completed_at") or "").strip(),
        "duration_seconds": inv.get("duration_seconds"),
        "tool_calls": inv.get("tool_calls_count"),
        "parent_investigation_id": inv.get("parent_investigation_id"),
        "triage_action": str(inv.get("triage_action") or "").strip(),
        "operator_notes": str(inv.get("operator_notes") or "").strip(),
        "provider": str(findings.get("provider") or "").strip(),
        "recommendation": str(findings.get("recommendation") or "").strip(),
        "report": str(findings.get("response") or "").strip(),
        "deep": bool(findings.get("deep")),
        "verification": str(findings.get("outcome_verification") or "").strip(),
    }


def _truncate(text, limit):
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "\n… [truncated; full text in the console]"
    return text


def _indent(text, prefix="  "):
    return "\n".join(prefix + line if line else "" for line in text.splitlines())


def _format_duration(seconds):
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if value >= 60:
        return f"{int(value // 60)}m{int(value % 60):02d}s"
    return f"{value:.1f}s"


def _headline(facts):
    bits = [f"investigation #{facts['id']}"]
    if facts["outcome"]:
        bits.append(f"outcome={facts['outcome']}")
    if facts["host_id"]:
        bits.append(f"host={facts['host_id']}")
    duration = _format_duration(facts["duration_seconds"])
    if duration:
        bits.append(duration)
    if facts["tool_calls"] not in (None, ""):
        bits.append(f"{facts['tool_calls']} tool calls")
    if facts["deep"]:
        bits.append("deep")
    return " | ".join(bits)


def _format_remediation(row):
    row = row or {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    head = [f"#{row.get('id')}", str(row.get("status") or "?")]
    if row.get("remediation_class"):
        head.append(str(row["remediation_class"]))
    if row.get("risk"):
        head.append(f"risk={row['risk']}")
    confidence = row.get("confidence")
    if isinstance(confidence, (int, float)):
        head.append(f"confidence={confidence:.2f}")

    lines = ["  " + " | ".join(head)]
    title = str(payload.get("title") or "").strip()
    if title:
        lines.append(f"      {title}")
    recommendation = str(payload.get("recommendation") or "").strip()
    if recommendation and recommendation != title:
        lines.append(f"      recommendation: {_truncate(recommendation, 400)}")
    if row.get("pr_url"):
        lines.append(f"      pr: {row['pr_url']}")
    if row.get("last_error"):
        lines.append(f"      last error: {str(row['last_error'])[:300]}")
    return "\n".join(lines)


def _format_learning(row, *, related):
    row = row or {}
    marker = "* " if related else "  "
    head = [f"#{row.get('id')}"]
    if row.get("learning_type"):
        head.append(str(row["learning_type"]))
    if row.get("category"):
        head.append(str(row["category"]))
    lines = [f"  {marker}" + " | ".join(head) + f" — {str(row.get('title') or '').strip()}"]
    description = str(row.get("description") or "").strip()
    if description:
        lines.append(f"      {_truncate(description, 400)}")
    applies = str(row.get("applies_when") or "").strip()
    if applies:
        lines.append(f"      applies when: {_truncate(applies, 200)}")
    return "\n".join(lines)


def _sort_learnings(learnings, investigation_id):
    """Learnings this investigation itself produced come first.

    Only possible when the KB search ran in FTS mode — the hybrid SQL path does
    not select ``investigation_id``. Absent the field nothing is hoisted, which
    is the correct degradation rather than a crash.
    """
    own, other = [], []
    for row in learnings or []:
        target = own if (row or {}).get("investigation_id") == investigation_id else other
        target.append(row)
    return own, other


def build_briefing(context, *, max_report_chars=4000):
    """Render the seeded-session briefing from ``collect_attach_context`` output."""
    ctx = context or {}
    facts = investigation_facts(ctx.get("investigation"))
    console_url = str(ctx.get("console_url") or "").strip().rstrip("/")

    out = [
        "=" * 72,
        f"CFOperator briefing — {_headline(facts)}",
        "=" * 72,
    ]

    if facts["started_at"] or facts["completed_at"]:
        out.append(
            f"started: {facts['started_at'] or '?'}   "
            f"completed: {facts['completed_at'] or '(still running)'}"
        )
    if facts["provider"]:
        out.append(f"investigated by: {facts['provider']}")
    if facts["parent_investigation_id"]:
        out.append(
            f"follow-up to investigation #{facts['parent_investigation_id']}"
        )

    if facts["trigger"]:
        out += ["", "Trigger:", _indent(_truncate(facts["trigger"], 1500))]

    if facts["triage_action"] or facts["operator_notes"]:
        note = facts["triage_action"] or "(no action recorded)"
        if facts["operator_notes"]:
            note += f" — {facts['operator_notes']}"
        out += ["", "Operator triage:", _indent(_truncate(note, 1500))]

    if facts["recommendation"]:
        out += ["", "Recommendation:", _indent(_truncate(facts["recommendation"], 1500))]

    if facts["verification"]:
        out += ["", "Outcome verification:", _indent(_truncate(facts["verification"], 1000))]

    if facts["report"]:
        out += ["", "What the agent found:",
                _indent(_truncate(facts["report"], max_report_chars))]
    else:
        # Worth saying out loud: an empty final response is a known failure mode
        # of the local model (~40% on some builds), and a session that silently
        # shows nothing here looks like a working attach against a boring
        # incident rather than a broken investigation.
        out += ["", "What the agent found:",
                "  (no report recorded — the investigation stored an empty response)"]

    remediations = ctx.get("remediations") or []
    if remediations:
        out += ["", f"Linked remediation queue rows ({len(remediations)}):"]
        out += [_format_remediation(row) for row in remediations]

    learnings = ctx.get("learnings") or []
    if learnings:
        mode = ctx.get("learnings_mode") or "unknown"
        own, other = _sort_learnings(learnings, facts["id"])
        out += ["", f"Related knowledge base learnings (search mode: {mode}"
                    f"{'; * = from this investigation' if own else ''}):"]
        out += [_format_learning(row, related=True) for row in own]
        out += [_format_learning(row, related=False) for row in other]

    warnings = ctx.get("warnings") or []
    if warnings:
        out += ["", "Incomplete briefing:"]
        out += [f"  - {w}" for w in warnings]

    if console_url and facts["id"]:
        out += ["", f"Console: {console_url}/investigations "
                    f"(investigation #{facts['id']})"]

    out.append("=" * 72)
    return "\n".join(out)
