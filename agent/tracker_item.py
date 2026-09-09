"""What a remediation row looks like as an issue-tracker item.

Pure functions, no I/O: the agent builds the item here and the tracker
service stays a dumb transport that knows only title / body / labels /
links, so a Plane item and a Jira item carry identical content and the
template is unit-testable without HTTP.

Two things are deliberate omissions. ``payload.rendered_context`` never goes
out — it is up to 5000 chars of raw tool output, the likeliest place for a
hostname, an IP or an env dump — and everything that does go out passes
through ``scrub`` first. A tracker is another party's system; the docs say
to use a private project, and this module assumes it will not always be.

Operator rule (2026-09-09): priority follows the PR. A row with one files
``high``; anything without a PR files ``low``. Not derived from risk.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

TITLE_MAX = 120
BODY_MAX = 20000
_TRUNCATED = "\n\n… (truncated)"

_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret|authorization)\b(\s*[:=]\s*)\S+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
_TOKEN_SHAPES_RE = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abp]-[A-Za-z0-9-]{10,}"
    r"|sk-[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,})\b")


def scrub(text: Any) -> str:
    """Redact token-shaped strings and ``key=value`` credential pairs."""
    s = str(text or "")
    # Bearer first: "Authorization: Bearer x" must lose x, not the word Bearer.
    s = _BEARER_RE.sub("Bearer <redacted>", s)
    s = _TOKEN_SHAPES_RE.sub("<redacted>", s)
    s = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", s)
    return s


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    p = row.get("payload")
    return p if isinstance(p, dict) else {}


def _result(row: Dict[str, Any]) -> Dict[str, Any]:
    r = row.get("result")
    return r if isinstance(r, dict) else {}


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        s = line.strip()
        s = re.sub(r"^(?:[-*>#]+\s*|\d+[.)]\s*)", "", s).strip()
        if s:
            return s
    return ""


def derive_title(row: Dict[str, Any]) -> str:
    """``payload.title`` → first line of the recommendation → "<class> on <host>"."""
    p = _payload(row)
    text = str(p.get("title") or "").strip() or _first_line(p.get("recommendation") or "")
    if not text:
        text = f"{row.get('remediation_class') or 'remediation'} on {row.get('host_id') or 'unknown host'}"
    text = scrub(text)
    prefix = f"[cfop #{row.get('id')}] "
    room = TITLE_MAX - len(prefix)
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    return prefix + text


def priority_for(row: Dict[str, Any]) -> str:
    return "high" if str(row.get("pr_url") or "").strip() else "low"


def labels_for(row: Dict[str, Any]) -> List[str]:
    out = ["cfoperator", "needs-human" if not str(row.get("pr_url") or "").strip() else "pr-open"]
    for x in (row.get("remediation_class"), f"risk:{row.get('risk')}" if row.get("risk") else ""):
        if x and x not in out:
            out.append(str(x))
    return out


def console_url(console_base_url: str, row: Dict[str, Any]) -> str:
    base = str(console_base_url or "").strip().rstrip("/")
    return f"{base}/remediations#{row.get('id')}" if base else ""


def build_body(row: Dict[str, Any], *, console_url: str = "") -> str:
    p = _payload(row)
    pr_url = str(row.get("pr_url") or "").strip()
    conf = row.get("confidence")
    lines: List[str] = ["## Why this is here"]
    if pr_url:
        lines.append(f"- **Status:** {row.get('status') or '—'} · a PR is open and waits for a human merge")
    else:
        lines.append(f"- **Status:** {row.get('status') or '—'} · parked; nothing automated will act on it")
    lines.append(f"- **Class:** {row.get('remediation_class') or '—'} · **Risk:** {row.get('risk') or '—'}"
                 f" · **Confidence:** {conf if conf is not None else '—'}")
    lines.append(f"- **Host:** `{row.get('host_id') or '—'}`")
    reason = str(row.get("last_error") or "").strip()
    lines.append(f"- **Reason:** {scrub(reason) if reason else 'not auto-eligible (class / risk / confidence gate)'}")
    if p.get("provider"):
        lines.append(f"- **Reporting LLM:** `{p.get('provider')}`")
    if p.get("source"):
        lines.append(f"- **Source:** {p.get('source')}")

    rec = str(p.get("recommendation") or p.get("title") or "").strip()
    lines += ["", "## Recommendation", scrub(rec)[:6000] if rec else "—"]

    steps = p.get("steps")
    if isinstance(steps, list) and steps:
        lines += ["", "## Proposed steps"] + [f"{i}. {scrub(s)}" for i, s in enumerate(steps, 1)]
    observed = p.get("observed")
    if isinstance(observed, list) and observed:
        lines += ["", "## Observed"]
        for o in observed:
            if isinstance(o, dict):
                lines.append(f"- {scrub(o.get('source') or '')} → {scrub(o.get('value') or '')}")
            else:
                lines.append(f"- {scrub(o)}")

    links: List[str] = []
    if console_url:
        links.append(f"- Console: {console_url}")
    if row.get("investigation_id"):
        links.append(f"- Investigation: #{row.get('investigation_id')}")
    if pr_url:
        links.append(f"- PR: {pr_url}")
    if links:
        lines += ["", "## Links"] + links

    lines += ["", "---",
              f"Filed by cfoperator for remediation #{row.get('id')} · dedupe key `{p.get('dedupe_key') or '—'}`"
              f" · investigation {row.get('investigation_id') or '—'}",
              "_Automated: this item mirrors the row. Resolve or cancel it here and the row follows; "
              "resolving the row in the console closes this item. Edits to the text do not flow back._"]
    body = "\n".join(lines)
    if len(body) > BODY_MAX:
        body = body[: BODY_MAX - len(_TRUNCATED)] + _TRUNCATED
    return body


def build_item(row: Dict[str, Any], *, console_base_url: str = "") -> Dict[str, Any]:
    """The ``POST /items`` body for one row."""
    p = _payload(row)
    curl = console_url(console_base_url, row)
    return {
        "remediation_id": row.get("id"),
        "investigation_id": row.get("investigation_id"),
        "title": derive_title(row),
        "body_markdown": build_body(row, console_url=curl),
        "priority": priority_for(row),
        "labels": labels_for(row),
        "risk": row.get("risk") or "",
        "confidence": row.get("confidence"),
        "host": row.get("host_id") or "",
        "remediation_class": row.get("remediation_class") or "",
        "dedupe_key": str(p.get("dedupe_key") or ""),
        "links": {"console_url": curl or None, "pr_url": str(row.get("pr_url") or "").strip() or None},
    }


def build_pr_comment(row: Dict[str, Any]) -> str:
    pr = str(row.get("pr_url") or "").strip()
    named = str(row.get("named_pr_url") or "").strip()
    text = f"PR opened: {pr}" if pr else "The row now tracks a PR."
    if not pr and named:
        text += f" (the recommendation names {named})"
    return text + "\n\nMerging it resolves the row; closing it without merge rejects it."


def build_reparked_comment(row: Dict[str, Any]) -> str:
    reason = scrub(str(row.get("last_error") or "").strip()) or "no reason recorded"
    return f"Parked again at needs-human after an executor attempt: {reason}"


def build_queued_comment(row: Dict[str, Any]) -> str:
    return (f"Handed to the executor (attempt {int(row.get('attempts') or 0) + 1}). "
            "If it opens a PR, the link lands here; if it declines, the row parks again.")


def build_transition_note(row: Dict[str, Any], state: str) -> str:
    res = _result(row)
    if state == "resolved":
        who = res.get("resolved_by") or ("PR merge" if res.get("pr_merged") else "cfoperator")
        note = str(res.get("resolution_note") or "").strip()
        text = f"Resolved by {who}."
        return text + (f" {scrub(note)[:2000]}" if note else "")
    reason = str(row.get("last_error") or "").strip()
    return "Rejected." + (f" {scrub(reason)[:2000]}" if reason else "")
