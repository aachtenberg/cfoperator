"""GitHub Issues backend.

Env: GITHUB_TOKEN, CFOP_TRACKER_GITHUB_REPO (owner/repo); optional
GITHUB_API_URL, CFOP_TRACKER_LABELS.

GitHub has no priority field, so the contract's ``high`` / ``low`` becomes a
``priority:<x>`` label. Closing uses ``state_reason`` (completed for resolved,
not_planned for rejected), which ``get`` reads back the same way. Note that a
public repository publishes whatever the agent put in the body; the docs say to
use a private one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from backends import TrackerError, TrackerNotFound, require_env
from http_client import JsonHttp, error_text
from shapes import Item, ItemRef, ItemState, encode_ref

logger = logging.getLogger("cfop-tracker.github")

NAME = "github"
BODY_MAX = 65536
_TRUNCATED = "\n\n… (truncated by cfoperator: GitHub issue bodies are capped at 65,536 characters)"


class GitHubIssuesBackend:
    name = NAME

    def __init__(self, http: JsonHttp, *, repo: str, labels: Iterable[str] = ()):
        self.http = http
        self.repo = repo
        self.labels = [x.strip() for x in labels if x and x.strip()]

    def _path(self, number: Any = None) -> str:
        base = f"/repos/{self.repo}/issues"
        return f"{base}/{number}" if number is not None else base

    def create(self, item: Item) -> ItemRef:
        body = item.body_markdown
        if len(body) > BODY_MAX:
            body = body[: BODY_MAX - len(_TRUNCATED)] + _TRUNCATED
        labels: List[str] = list(self.labels) + [x for x in item.labels if x] + [f"priority:{item.priority}"]
        r = self.http.request("POST", self._path(), body={
            "title": item.title[:256], "body": body, "labels": _dedupe(labels),
        })
        if not r.get("success"):
            raise TrackerError(f"github: create failed ({error_text(r)})")
        data = r.get("data") or {}
        number = data.get("number")
        if number is None:
            raise TrackerError("github: create returned no number")
        meta = {"backend": NAME, "number": int(number)}
        return ItemRef(ref=encode_ref(meta), url=data.get("html_url"), key=f"#{number}",
                       backend=NAME, meta=meta)

    def comment(self, meta: Dict[str, Any], body_markdown: str) -> None:
        number = _number(meta)
        r = self.http.request("POST", f"{self._path(number)}/comments", body={"body": body_markdown})
        if r.get("status") == 404:
            raise TrackerNotFound(f"github: issue #{number} not found")
        if not r.get("success"):
            raise TrackerError(f"github: comment failed ({error_text(r)})")

    def transition(self, meta: Dict[str, Any], state: str, note: str) -> None:
        number = _number(meta)
        if note:
            self.comment(meta, note)
        reason = "completed" if state == "resolved" else "not_planned"
        r = self.http.request("PATCH", self._path(number), body={"state": "closed", "state_reason": reason})
        if r.get("status") == 404:
            raise TrackerNotFound(f"github: issue #{number} not found")
        if not r.get("success"):
            raise TrackerError(f"github: transition failed ({error_text(r)})")

    def get(self, meta: Dict[str, Any]) -> ItemState:
        number = _number(meta)
        r = self.http.request("GET", self._path(number))
        if r.get("status") == 404:
            raise TrackerNotFound(f"github: issue #{number} not found")
        if not r.get("success"):
            raise TrackerError(f"github: get failed ({error_text(r)})")
        data = r.get("data") or {}
        if data.get("state") == "closed":
            state = "rejected" if data.get("state_reason") == "not_planned" else "resolved"
        else:
            state = "open"
        return ItemState(state=state, url=data.get("html_url"), key=f"#{number}",
                         updated_at=data.get("updated_at"))


def _dedupe(labels: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for x in labels:
        if x not in seen:
            seen.append(x)
    return seen


def _number(meta: Dict[str, Any]) -> int:
    try:
        return int(meta.get("number"))
    except (TypeError, ValueError):
        raise TrackerError("github: ref missing number") from None


def make_github(env: Dict[str, str]) -> GitHubIssuesBackend:
    token = require_env(env, "GITHUB_TOKEN")
    repo = require_env(env, "CFOP_TRACKER_GITHUB_REPO")
    if "/" not in repo:
        raise TrackerError("CFOP_TRACKER_GITHUB_REPO must be owner/repo")
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").strip()
    http = JsonHttp(api, {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    labels = (env.get("CFOP_TRACKER_LABELS") or "").split(",")
    logger.info("github backend ready: %s", repo)
    return GitHubIssuesBackend(http, repo=repo, labels=labels)
