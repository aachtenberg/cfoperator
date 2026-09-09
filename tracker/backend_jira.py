"""Jira Cloud backend (REST API v3) — SHAPE ONLY, NOT LIVE-TESTED.

No Jira instance exists where this was written, so the adapter is exercised
only against a fake transport. The request shapes follow the v3 reference
(create / comment / transitions / get) and the capability matrix in
docs/infrastructure-config.md says "untested" until a trial runs it.

Env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY; optional
JIRA_ISSUE_TYPE (Task), CFOP_TRACKER_RESOLVED_STATE (Done),
CFOP_TRACKER_REJECTED_STATE (Won't Do) — these are *transition names* as the
project's workflow offers them — JIRA_PRIORITY_HIGH (High), JIRA_PRIORITY_LOW
(Low), CFOP_TRACKER_LABELS.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Iterable, List

from backends import TrackerError, TrackerNotFound, require_env
from http_client import JsonHttp, error_text
from shapes import Item, ItemRef, ItemState, encode_ref

logger = logging.getLogger("cfop-tracker.jira")

NAME = "jira"


class JiraBackend:
    name = NAME

    def __init__(self, http: JsonHttp, *, base_url: str, project_key: str,
                 issue_type: str = "Task", resolved_transition: str = "Done",
                 rejected_transition: str = "Won't Do", priority_high: str = "High",
                 priority_low: str = "Low", labels: Iterable[str] = ()):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.issue_type = issue_type
        self.resolved_transition = resolved_transition
        self.rejected_transition = rejected_transition
        self.priority = {"high": priority_high, "low": priority_low}
        self.labels = [_label(x) for x in labels if x and x.strip()]

    def _url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    def create(self, item: Item) -> ItemRef:
        labels = _dedupe(self.labels + [_label(x) for x in item.labels if x])
        fields: Dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": self.issue_type},
            "summary": item.title[:255],
            "description": adf(item.body_markdown),
            "priority": {"name": self.priority.get(item.priority, self.priority["low"])},
        }
        if labels:
            fields["labels"] = labels
        r = self.http.request("POST", "/rest/api/3/issue", body={"fields": fields})
        if not r.get("success"):
            raise TrackerError(f"jira: create failed ({error_text(r)})")
        data = r.get("data") or {}
        key = str(data.get("key") or "")
        if not key:
            raise TrackerError("jira: create returned no key")
        meta = {"backend": NAME, "key": key}
        return ItemRef(ref=encode_ref(meta), url=self._url(key), key=key, backend=NAME, meta=meta)

    def comment(self, meta: Dict[str, Any], body_markdown: str) -> None:
        key = _key(meta)
        r = self.http.request("POST", f"/rest/api/3/issue/{key}/comment", body={"body": adf(body_markdown)})
        if r.get("status") == 404:
            raise TrackerNotFound(f"jira: issue {key} not found")
        if not r.get("success"):
            raise TrackerError(f"jira: comment failed ({error_text(r)})")

    def transition(self, meta: Dict[str, Any], state: str, note: str) -> None:
        key = _key(meta)
        want = self.resolved_transition if state == "resolved" else self.rejected_transition
        r = self.http.request("GET", f"/rest/api/3/issue/{key}/transitions")
        if r.get("status") == 404:
            raise TrackerNotFound(f"jira: issue {key} not found")
        if not r.get("success"):
            raise TrackerError(f"jira: could not list transitions ({error_text(r)})")
        offered = (r.get("data") or {}).get("transitions") or []
        match = next((t for t in offered if str(t.get("name") or "").lower() == want.lower()), None)
        if match is None:
            names = ", ".join(str(t.get("name")) for t in offered) or "none"
            raise TrackerError(f"jira: transition {want!r} not offered for {key} (offered: {names})")
        if note:
            self.comment(meta, note)
        r = self.http.request("POST", f"/rest/api/3/issue/{key}/transitions",
                              body={"transition": {"id": str(match.get("id"))}})
        if not r.get("success"):
            raise TrackerError(f"jira: transition failed ({error_text(r)})")

    def get(self, meta: Dict[str, Any]) -> ItemState:
        key = _key(meta)
        r = self.http.request("GET", f"/rest/api/3/issue/{key}?fields=status,updated")
        if r.get("status") == 404:
            raise TrackerNotFound(f"jira: issue {key} not found")
        if not r.get("success"):
            raise TrackerError(f"jira: get failed ({error_text(r)})")
        fields = (r.get("data") or {}).get("fields") or {}
        status = fields.get("status") or {}
        sname = str(status.get("name") or "")
        category = str((status.get("statusCategory") or {}).get("key") or "")
        if sname.lower() == self.rejected_transition.lower():
            state = "rejected"
        elif category == "done" or sname.lower() == self.resolved_transition.lower():
            state = "resolved"
        else:
            state = "open"
        return ItemState(state=state, url=self._url(key), key=key, updated_at=fields.get("updated"))


def adf(text: str) -> Dict[str, Any]:
    """Atlassian Document Format: one paragraph per blank-line block, fences as code."""
    content: List[Dict[str, Any]] = []
    para: List[str] = []
    fence: Any = None

    def flush() -> None:
        if para:
            content.append({"type": "paragraph",
                            "content": [{"type": "text", "text": "\n".join(para)}]})
            para.clear()

    for line in (text or "").splitlines():
        if fence is not None:
            if line.strip().startswith("```"):
                content.append({"type": "codeBlock",
                                "content": [{"type": "text", "text": "\n".join(fence) or " "}]})
                fence = None
            else:
                fence.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            fence = []
            continue
        if not line.strip():
            flush()
            continue
        para.append(line.lstrip("#").strip() if line.startswith("#") else line)
    if fence is not None:
        content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(fence) or " "}]})
    flush()
    if not content:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
    return {"type": "doc", "version": 1, "content": content}


def _label(x: str) -> str:
    return x.strip().replace(" ", "-")  # Jira labels cannot contain spaces


def _dedupe(labels: Iterable[str]) -> List[str]:
    out: List[str] = []
    for x in labels:
        if x and x not in out:
            out.append(x)
    return out


def _key(meta: Dict[str, Any]) -> str:
    key = str(meta.get("key") or "").strip()
    if not key:
        raise TrackerError("jira: ref missing key")
    return key


def make_jira(env: Dict[str, str]) -> JiraBackend:
    base = require_env(env, "JIRA_BASE_URL")
    email = require_env(env, "JIRA_EMAIL")
    token = require_env(env, "JIRA_API_TOKEN")
    project = require_env(env, "JIRA_PROJECT_KEY")
    basic = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    http = JsonHttp(base, {"Authorization": f"Basic {basic}"})
    logger.warning("jira backend ready: %s — this adapter is shape-only and has not been "
                   "exercised against a live Jira", project)
    return JiraBackend(
        http, base_url=base, project_key=project,
        issue_type=(env.get("JIRA_ISSUE_TYPE") or "Task").strip(),
        resolved_transition=(env.get("CFOP_TRACKER_RESOLVED_STATE") or "Done").strip(),
        rejected_transition=(env.get("CFOP_TRACKER_REJECTED_STATE") or "Won't Do").strip(),
        priority_high=(env.get("JIRA_PRIORITY_HIGH") or "High").strip(),
        priority_low=(env.get("JIRA_PRIORITY_LOW") or "Low").strip(),
        labels=(env.get("CFOP_TRACKER_LABELS") or "").split(","),
    )
