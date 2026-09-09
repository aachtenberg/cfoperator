"""Shared shapes for the issue-tracker HTTP contract.

Backend-agnostic: the plane / github / jira adapters all speak these JSON
shapes, and the agent only ever sees them. Stdlib only.

Kept deliberately separate from ``changerecord/shapes.py``: that contract is an
*approval* workflow (open → approval → close) and a gate in front of shell on a
host. This one is a hand-off — create an item, comment on it, close it, read
its state — and nothing in the agent waits on it.
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: What ``GET /items/{ref}`` may report. ``open`` is everything that is not one
#: of the two terminal states, whatever the backend calls it.
CONTRACT_STATES = ("open", "resolved", "rejected")
#: What ``POST /items/{ref}/transition`` accepts.
TRANSITION_STATES = ("resolved", "rejected")
#: Operator rule (2026-09-09): a row with a PR files high, anything without a
#: PR files low. Derived by the agent from ``pr_url``, not from risk.
PRIORITIES = ("high", "low")


@dataclass
class Item:
    """One remediation row, as the agent presents it to a tracker."""

    remediation_id: Any
    title: str
    body_markdown: str
    priority: str = "low"
    investigation_id: Any = None
    labels: List[str] = field(default_factory=list)
    risk: str = ""
    confidence: Any = None
    host: str = ""
    remediation_class: str = ""
    dedupe_key: str = ""
    links: Dict[str, Any] = field(default_factory=dict)  # {console_url, pr_url}


@dataclass
class ItemRef:
    """Opaque handle returned by create; ``meta`` is what the backend needs."""

    ref: str
    url: Optional[str]
    key: str  # human identifier: CFOP-42 / #17 / PROJ-9
    backend: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ref": self.ref, "url": self.url, "key": self.key, "backend": self.backend}


@dataclass
class ItemState:
    """What ``GET /items/{ref}`` returns."""

    state: str
    url: Optional[str]
    key: str
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "url": self.url, "key": self.key,
                "updated_at": self.updated_at}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def item_from_body(body: Dict[str, Any]) -> Item:
    """Validate a ``POST /items`` body. Raises ``ValueError`` naming the field."""
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    remediation_id = body.get("remediation_id")
    if remediation_id in (None, ""):
        raise ValueError("remediation_id required")
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title required")
    priority = str(body.get("priority") or "low").strip().lower()
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    labels = body.get("labels") or []
    if not isinstance(labels, list):
        raise ValueError("labels must be a list")
    links = body.get("links") or {}
    if not isinstance(links, dict):
        raise ValueError("links must be an object")
    return Item(
        remediation_id=remediation_id,
        title=title,
        body_markdown=str(body.get("body_markdown") or ""),
        priority=priority,
        investigation_id=body.get("investigation_id"),
        labels=[str(x).strip() for x in labels if str(x).strip()],
        risk=str(body.get("risk") or ""),
        confidence=body.get("confidence"),
        host=str(body.get("host") or ""),
        remediation_class=str(body.get("remediation_class") or ""),
        dedupe_key=str(body.get("dedupe_key") or ""),
        links={str(k): v for k, v in links.items()},
    )


def encode_ref(meta: Dict[str, Any]) -> str:
    """Pack backend meta into an opaque URL-safe token (stateless service).

    Security posture as in changerecord: meta carries only the backend name
    and the item's id. Repo, project and workspace always come from the
    instance's own env, never from the token.
    """
    raw = json.dumps(meta, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_ref(token: str) -> Dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        meta = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:  # binascii.Error is a ValueError
        raise ValueError("ref is not a valid token") from e
    if not isinstance(meta, dict):
        raise ValueError("ref must decode to an object")
    return meta


# --- markdown → html, just enough for Plane -------------------------------
#
# Plane's description_html / comment_html want HTML; GitHub and Jira take
# their own formats. This is the smallest renderer that keeps the agent's
# body readable there: headings, paragraphs, bullet lists, fenced code, bold,
# inline code and bare links. Everything is escaped first, so a recommendation
# containing ``<script>`` renders as text.

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _URL_RE.sub(r'<a href="\1">\1</a>', out)
    return out


def md_to_html_lite(md: str) -> str:
    lines = (md or "").splitlines()
    parts: List[str] = []
    para: List[str] = []
    bullets: List[str] = []
    fence: Optional[List[str]] = None

    def flush_para() -> None:
        if para:
            parts.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in bullets) + "</ul>")
            bullets.clear()

    for line in lines:
        if fence is not None:
            if line.strip().startswith("```"):
                parts.append("<pre>" + html.escape("\n".join(fence), quote=False) + "</pre>")
                fence = None
            else:
                fence.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para(); flush_bullets()
            fence = []
            continue
        if not stripped:
            flush_para(); flush_bullets()
            continue
        if stripped.startswith("---") and set(stripped) <= {"-"}:
            flush_para(); flush_bullets()
            parts.append("<hr>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(); flush_bullets()
            level = min(len(m.group(1)) + 1, 4)  # '#' → h2: the item title is h1
            parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_para()
            bullets.append(stripped[2:])
            continue
        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            flush_para()
            bullets.append(m.group(1))
            continue
        flush_bullets()
        para.append(stripped)
    if fence is not None:  # unterminated fence: still show it
        parts.append("<pre>" + html.escape("\n".join(fence), quote=False) + "</pre>")
    flush_para(); flush_bullets()
    return "".join(parts)
