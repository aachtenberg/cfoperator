"""GitHub Issues adapter against a fake transport."""

from __future__ import annotations

import pytest

from backend_github import BODY_MAX, GitHubIssuesBackend
from backends import TrackerError, TrackerNotFound
from shapes import Item, decode_ref


class FakeHttp:
    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        key = (method, path)
        if key in self.routes:
            return self.routes[key]
        if key == ("POST", "/repos/o/r/issues"):
            return {"success": True, "status": 201,
                    "data": {"number": 17, "html_url": "https://github.com/o/r/issues/17"}}
        if key == ("POST", "/repos/o/r/issues/17/comments"):
            return {"success": True, "status": 201, "data": {"id": 1}}
        if key == ("PATCH", "/repos/o/r/issues/17"):
            return {"success": True, "status": 200, "data": {}}
        if key == ("GET", "/repos/o/r/issues/17"):
            return {"success": True, "status": 200,
                    "data": {"state": "open", "html_url": "u", "updated_at": "t"}}
        return {"success": False, "status": 404, "data": {"message": "Not Found"}}


def _backend(http=None, **kw):
    http = http or FakeHttp()
    return GitHubIssuesBackend(http, repo="o/r", **kw), http


def test_create_sets_priority_label_and_returns_number_key():
    be, http = _backend(labels=["cfoperator"])
    ref = be.create(Item(remediation_id=42, title="t", body_markdown="b", priority="high",
                         labels=["needs-human", "cfoperator"]))
    method, path, body = http.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues")
    assert body["labels"] == ["cfoperator", "needs-human", "priority:high"]
    assert ref.key == "#17" and ref.url.endswith("/issues/17")
    assert decode_ref(ref.ref) == {"backend": "github", "number": 17}


def test_create_truncates_oversized_body_with_a_marker():
    be, http = _backend()
    be.create(Item(remediation_id=1, title="t", body_markdown="x" * (BODY_MAX + 10)))
    body = http.calls[-1][2]["body"]
    assert len(body) <= BODY_MAX and body.endswith("characters)")


def test_transition_closes_with_state_reason_after_the_note():
    be, http = _backend()
    meta = {"backend": "github", "number": 17}
    be.transition(meta, "resolved", "merged")
    assert http.calls[-2][1] == "/repos/o/r/issues/17/comments"
    assert http.calls[-1][2] == {"state": "closed", "state_reason": "completed"}
    be.transition(meta, "rejected", "")
    assert http.calls[-1][2] == {"state": "closed", "state_reason": "not_planned"}
    assert http.calls[-2][1] != "/repos/o/r/issues/17/comments"


@pytest.mark.parametrize("data, want", [
    ({"state": "open"}, "open"),
    ({"state": "closed", "state_reason": "completed"}, "resolved"),
    ({"state": "closed", "state_reason": None}, "resolved"),
    ({"state": "closed", "state_reason": "not_planned"}, "rejected"),
])
def test_get_maps_state_and_reason(data, want):
    http = FakeHttp({("GET", "/repos/o/r/issues/17"): {"success": True, "status": 200, "data": data}})
    be, _ = _backend(http)
    assert be.get({"backend": "github", "number": 17}).state == want


def test_not_found_and_bad_ref():
    be, _ = _backend()
    with pytest.raises(TrackerNotFound):
        be.get({"backend": "github", "number": 99})
    with pytest.raises(TrackerError):
        be.comment({"backend": "github"}, "x")
