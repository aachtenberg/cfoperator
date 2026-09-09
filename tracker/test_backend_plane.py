"""Plane adapter against a fake transport: request shapes, state mapping, and the no-list rule."""

from __future__ import annotations

import pytest

from backend_plane import PlaneBackend
from backends import TrackerError, TrackerNotFound
from shapes import Item, decode_ref

P = "/api/v1/workspaces/ws/projects/pid"
STATES = [
    {"id": "s-backlog", "name": "Backlog", "group": "backlog"},
    {"id": "s-done", "name": "Done", "group": "completed"},
    {"id": "s-cancel", "name": "Cancelled", "group": "cancelled"},
]


class FakeHttp:
    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        key = (method, path.split("?", 1)[0])
        if key in self.routes:
            r = self.routes[key]
            return r(body) if callable(r) else r
        if key == ("GET", f"{P}/"):
            return {"success": True, "status": 200, "data": {"identifier": "CFOP"}}
        if key == ("GET", f"{P}/states/"):
            return {"success": True, "status": 200, "data": {"results": STATES}}
        if key == ("GET", f"{P}/labels/"):
            return {"success": True, "status": 200,
                    "data": {"results": [{"id": "l-1", "name": "cfoperator"}]}}
        if key == ("POST", f"{P}/issues/"):
            return {"success": True, "status": 201, "data": {"id": "iss-1", "sequence_id": 170}}
        if key == ("POST", f"{P}/issues/iss-1/comments/"):
            return {"success": True, "status": 201, "data": {"id": "c-1"}}
        if key == ("PATCH", f"{P}/issues/iss-1/"):
            return {"success": True, "status": 200, "data": {}}
        if key == ("GET", f"{P}/issues/iss-1/"):
            return {"success": True, "status": 200,
                    "data": {"id": "iss-1", "sequence_id": 170, "state": "s-backlog", "updated_at": "t"}}
        return {"success": False, "status": 404, "data": {"error": "nope"}}


def _backend(http=None, **kw):
    http = http or FakeHttp()
    be = PlaneBackend(http, base_url="https://plane.example", slug="ws", project_id="pid", **kw)
    return be.warm(), http


def test_warm_resolves_identifier_states_by_group_and_labels():
    be, http = _backend(label_names=["cfoperator", "missing-label"])
    assert be.identifier == "CFOP"
    assert be.resolved_state_id == "s-done" and be.rejected_state_id == "s-cancel"
    assert be.label_ids == ["l-1"]


def test_warm_honours_state_name_override_and_fails_on_unknown_name():
    be, _ = _backend(resolved_state="done")  # case-insensitive
    assert be.resolved_state_id == "s-done"
    with pytest.raises(TrackerError) as e:
        _backend(rejected_state="Wontfix")
    assert "Wontfix" in str(e.value) and "Cancelled" in str(e.value)


def test_create_posts_html_and_priority_and_returns_key_url_ref():
    be, http = _backend(label_names=["cfoperator"])
    item = Item(remediation_id=42, title="[cfop #42] t", body_markdown="## Why\n- x", priority="high")
    ref = be.create(item)
    method, path, body = http.calls[-1]
    assert (method, path) == ("POST", f"{P}/issues/")
    assert body["name"] == "[cfop #42] t" and body["priority"] == "high"
    assert body["description_html"].startswith("<h3>Why</h3><ul><li>x</li></ul>")
    assert body["labels"] == ["l-1"]
    assert ref.key == "CFOP-170" and ref.backend == "plane"
    assert ref.url == "https://plane.example/ws/projects/pid/issues/iss-1"
    assert decode_ref(ref.ref) == {"backend": "plane", "id": "iss-1"}


def test_create_failure_carries_the_backend_message():
    http = FakeHttp({("POST", f"{P}/issues/"): {"success": False, "status": 400,
                                                  "data": {"error": "name too long"}}})
    be, _ = _backend(http)
    with pytest.raises(TrackerError) as e:
        be.create(Item(remediation_id=1, title="t", body_markdown=""))
    assert "HTTP 400" in str(e.value) and "name too long" in str(e.value)


def test_comment_and_transition_are_by_id_and_transition_comments_first():
    be, http = _backend()
    meta = {"backend": "plane", "id": "iss-1"}
    be.comment(meta, "PR opened")
    assert http.calls[-1][1] == f"{P}/issues/iss-1/comments/"
    assert http.calls[-1][2] == {"comment_html": "<p>PR opened</p>"}
    be.transition(meta, "resolved", "fixed by hand")
    assert [c[1] for c in http.calls[-2:]] == [f"{P}/issues/iss-1/comments/", f"{P}/issues/iss-1/"]
    assert http.calls[-1][0] == "PATCH" and http.calls[-1][2] == {"state": "s-done"}
    be.transition(meta, "rejected", "")
    assert http.calls[-1][2] == {"state": "s-cancel"}
    assert http.calls[-2][1] != f"{P}/issues/iss-1/comments/"  # no empty comment


def test_get_maps_state_group_to_contract_state():
    def issue(state):
        return {"success": True, "status": 200,
                "data": {"id": "iss-1", "sequence_id": 170, "state": state, "updated_at": "t"}}
    for state, want in (("s-backlog", "open"), ("s-done", "resolved"), ("s-cancel", "rejected")):
        http = FakeHttp({("GET", f"{P}/issues/iss-1/"): issue(state)})
        be, _ = _backend(http)
        got = be.get({"backend": "plane", "id": "iss-1"})
        assert got.state == want and got.key == "CFOP-170" and got.updated_at == "t"


def test_missing_issue_is_not_found():
    be, _ = _backend()
    with pytest.raises(TrackerNotFound):
        be.get({"backend": "plane", "id": "gone"})
    with pytest.raises(TrackerNotFound):
        be.comment({"backend": "plane", "id": "gone"}, "x")
    with pytest.raises(TrackerError):
        be.get({"backend": "plane"})  # ref without id


def test_adapter_never_lists_issues():
    """Plane CE ignores PQL filters silently, so a list-and-match would pick the
    wrong item with HTTP 200. Every operation must address an issue by id."""
    be, http = _backend()
    meta = {"backend": "plane", "id": "iss-1"}
    be.create(Item(remediation_id=1, title="t", body_markdown="b"))
    be.comment(meta, "c")
    be.transition(meta, "resolved", "n")
    be.get(meta)
    listing = [c for c in http.calls if c[0] == "GET" and c[1].rstrip("/").endswith("/issues")]
    assert listing == [], listing
