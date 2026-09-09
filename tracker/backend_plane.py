"""Plane backend (Community Edition REST API v1).

Env: PLANE_BASE_URL, PLANE_API_KEY, PLANE_WORKSPACE_SLUG, PLANE_PROJECT_ID;
optional CFOP_TRACKER_RESOLVED_STATE / CFOP_TRACKER_REJECTED_STATE (state
*names*; the default maps by state *group* — completed / cancelled — which
every Plane project has), CFOP_TRACKER_LABELS (comma list of label names).

Everything is by id. This adapter never lists issues: Plane CE ignores PQL
filters silently (HTTP 200 with the unfiltered set — CLAUDE.md), so a
list-and-match would return the wrong item with a straight face. State,
label and project lookups happen once at startup (``warm``), so a wrong
state name fails the pod, not the first transition.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backends import TrackerError, TrackerNotFound, require_env
from http_client import JsonHttp, error_text
from shapes import Item, ItemRef, ItemState, encode_ref, md_to_html_lite

logger = logging.getLogger("cfop-tracker.plane")

NAME = "plane"


class PlaneBackend:
    name = NAME

    def __init__(self, http: JsonHttp, *, base_url: str, slug: str, project_id: str,
                 resolved_state: str = "", rejected_state: str = "",
                 label_names: Iterable[str] = ()):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.slug = slug
        self.project_id = project_id
        self._resolved_name = resolved_state.strip()
        self._rejected_name = rejected_state.strip()
        self._label_names = [x.strip() for x in label_names if x and x.strip()]
        self.identifier = ""
        self.states: Dict[str, Tuple[str, str]] = {}  # id -> (name, group)
        self.resolved_state_id = ""
        self.rejected_state_id = ""
        self.label_ids: List[str] = []
        self._labels_by_name: Dict[str, str] = {}  # lower(name) -> id, grown on demand

    # -- startup ------------------------------------------------------------

    @property
    def _p(self) -> str:
        return f"/api/v1/workspaces/{self.slug}/projects/{self.project_id}"

    def warm(self) -> "PlaneBackend":
        proj = self.http.request("GET", f"{self._p}/")
        if not proj.get("success"):
            raise TrackerError(f"plane: could not read project {self.project_id} ({error_text(proj)})")
        self.identifier = str((proj.get("data") or {}).get("identifier") or "").strip()
        if not self.identifier:
            raise TrackerError("plane: project has no identifier")

        states = self.http.request("GET", f"{self._p}/states/")
        if not states.get("success"):
            raise TrackerError(f"plane: could not list states ({error_text(states)})")
        for s in _results(states):
            self.states[str(s.get("id"))] = (str(s.get("name") or ""), str(s.get("group") or ""))
        self.resolved_state_id = self._pick_state(self._resolved_name, "completed", "resolved")
        self.rejected_state_id = self._pick_state(self._rejected_name, "cancelled", "rejected")

        # Always fetched: the agent sends labels on every item (cfoperator,
        # needs-human / pr-open, the class, risk), not only the env list. A
        # failed fetch fails warm like project/states do — otherwise a
        # permissions error would read as "label not found" for every name.
        labels = self.http.request("GET", f"{self._p}/labels/")
        if not labels.get("success"):
            raise TrackerError(f"plane: could not list labels ({error_text(labels)})")
        self._labels_by_name = {str(l.get("name") or "").lower(): str(l.get("id"))
                                for l in _results(labels) if l.get("id")}
        self.label_ids = self._label_ids(self._label_names)
        logger.info("plane backend ready: %s, resolved=%s rejected=%s labels=%d",
                    self.identifier, self.states[self.resolved_state_id][0],
                    self.states[self.rejected_state_id][0], len(self.label_ids))
        return self

    def _label_ids(self, names: Iterable[str]) -> List[str]:
        """Ids for label names, creating a project label for any name it lacks.

        GitHub creates unknown labels on the fly; Plane wants an id, so this
        does the same explicitly. A create that fails is logged and that one
        label skipped — the item still files, the others still land.
        """
        out: List[str] = []
        for name in names:
            name = str(name or "").strip()
            if not name:
                continue
            lid = self._labels_by_name.get(name.lower())
            if not lid:
                r = self.http.request("POST", f"{self._p}/labels/", body={"name": name})
                lid = str((r.get("data") or {}).get("id") or "") if r.get("success") else ""
                if not lid:
                    logger.warning("plane: could not create label %r (%s); skipped", name, error_text(r))
                    continue
                self._labels_by_name[name.lower()] = lid
            if lid not in out:
                out.append(lid)
        return out

    def _pick_state(self, name: str, group: str, what: str) -> str:
        if name:
            for sid, (sname, _g) in self.states.items():
                if sname.lower() == name.lower():
                    return sid
            offered = ", ".join(sorted(n for n, _ in self.states.values()))
            raise TrackerError(f"plane: {what} state {name!r} not in project (states: {offered})")
        for sid, (_n, g) in self.states.items():
            if g == group:
                return sid
        raise TrackerError(f"plane: no state with group {group!r} for {what}")

    # -- contract -----------------------------------------------------------

    def _url(self, issue_id: str) -> str:
        return f"{self.base_url}/{self.slug}/projects/{self.project_id}/issues/{issue_id}"

    def create(self, item: Item) -> ItemRef:
        body: Dict[str, Any] = {
            "name": item.title[:255],
            "description_html": md_to_html_lite(item.body_markdown),
            "priority": item.priority,
        }
        labels = list(self.label_ids)
        for lid in self._label_ids(item.labels):
            if lid not in labels:
                labels.append(lid)
        if labels:
            body["labels"] = labels
        r = self.http.request("POST", f"{self._p}/issues/", body=body)
        if not r.get("success"):
            raise TrackerError(f"plane: create failed ({error_text(r)})")
        data = r.get("data") or {}
        issue_id = str(data.get("id") or "")
        if not issue_id:
            raise TrackerError("plane: create returned no id")
        meta = {"backend": NAME, "id": issue_id}
        key = f"{self.identifier}-{data.get('sequence_id')}"
        return ItemRef(ref=encode_ref(meta), url=self._url(issue_id), key=key,
                       backend=NAME, meta=meta)

    def comment(self, meta: Dict[str, Any], body_markdown: str) -> None:
        issue_id = _id(meta)
        r = self.http.request("POST", f"{self._p}/issues/{issue_id}/comments/",
                              body={"comment_html": md_to_html_lite(body_markdown)})
        if r.get("status") == 404:
            raise TrackerNotFound(f"plane: issue {issue_id} not found")
        if not r.get("success"):
            raise TrackerError(f"plane: comment failed ({error_text(r)})")

    def transition(self, meta: Dict[str, Any], state: str, note: str) -> None:
        issue_id = _id(meta)
        target = self.resolved_state_id if state == "resolved" else self.rejected_state_id
        # State first, note second: the state change is idempotent on retry,
        # a comment is not. A note lost to a failure after the close is the
        # cheaper miss (the contract says notes are at-least-once).
        r = self.http.request("PATCH", f"{self._p}/issues/{issue_id}/", body={"state": target})
        if r.get("status") == 404:
            raise TrackerNotFound(f"plane: issue {issue_id} not found")
        if not r.get("success"):
            raise TrackerError(f"plane: transition failed ({error_text(r)})")
        if note:
            self.comment(meta, note)

    def get(self, meta: Dict[str, Any]) -> ItemState:
        issue_id = _id(meta)
        r = self.http.request("GET", f"{self._p}/issues/{issue_id}/")
        if r.get("status") == 404:
            raise TrackerNotFound(f"plane: issue {issue_id} not found")
        if not r.get("success"):
            raise TrackerError(f"plane: get failed ({error_text(r)})")
        data = r.get("data") or {}
        _name, group = self.states.get(str(data.get("state")), ("", ""))
        if str(data.get("state")) == self.resolved_state_id or group == "completed":
            state = "resolved"
        elif str(data.get("state")) == self.rejected_state_id or group == "cancelled":
            state = "rejected"
        else:
            state = "open"
        return ItemState(state=state, url=self._url(issue_id),
                         key=f"{self.identifier}-{data.get('sequence_id')}",
                         updated_at=data.get("updated_at"))


def _results(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = resp.get("data")
    if isinstance(data, dict):
        rows = data.get("results")
        return rows if isinstance(rows, list) else []
    return data if isinstance(data, list) else []


def _id(meta: Dict[str, Any]) -> str:
    issue_id = str(meta.get("id") or "").strip()
    if not issue_id:
        raise TrackerError("plane: ref missing id")
    return issue_id


def make_plane(env: Dict[str, str]) -> PlaneBackend:
    base = require_env(env, "PLANE_BASE_URL")
    key = require_env(env, "PLANE_API_KEY")
    slug = require_env(env, "PLANE_WORKSPACE_SLUG")
    project = require_env(env, "PLANE_PROJECT_ID")
    labels = (env.get("CFOP_TRACKER_LABELS") or "").split(",")
    http = JsonHttp(base, {"X-API-Key": key})
    return PlaneBackend(
        http, base_url=base, slug=slug, project_id=project,
        resolved_state=env.get("CFOP_TRACKER_RESOLVED_STATE") or "",
        rejected_state=env.get("CFOP_TRACKER_REJECTED_STATE") or "",
        label_names=labels,
    ).warm()
