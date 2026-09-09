"""Jira adapter against a fake transport — request shapes only; no live Jira exists here."""

from __future__ import annotations

import base64

import pytest

import backends
from backend_jira import JiraBackend, adf, make_jira
from shapes import Item, decode_ref


class FakeHttp:
    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        key = (method, path.split("?", 1)[0])
        if key in self.routes:
            return self.routes[key]
        if key == ("POST", "/rest/api/3/issue"):
            return {"success": True, "status": 201, "data": {"key": "OPS-9"}}
        if key == ("POST", "/rest/api/3/issue/OPS-9/comment"):
            return {"success": True, "status": 201, "data": {}}
        if key == ("GET", "/rest/api/3/issue/OPS-9/transitions"):
            return {"success": True, "status": 200,
                    "data": {"transitions": [{"id": "31", "name": "Done"}, {"id": "41", "name": "Won't Do"}]}}
        if key == ("POST", "/rest/api/3/issue/OPS-9/transitions"):
            return {"success": True, "status": 204, "data": {}}
        if key == ("GET", "/rest/api/3/issue/OPS-9"):
            return {"success": True, "status": 200,
                    "data": {"fields": {"status": {"name": "To Do", "statusCategory": {"key": "new"}},
                                        "updated": "t"}}}
        return {"success": False, "status": 404, "data": {"errorMessages": ["gone"]}}


def _backend(http=None, **kw):
    http = http or FakeHttp()
    return JiraBackend(http, base_url="https://j.example", project_key="OPS", **kw), http


def test_create_shape_priority_and_labels():
    be, http = _backend(labels=["cf operator"])
    ref = be.create(Item(remediation_id=1, title="t", body_markdown="a\n\n```\ncode\n```",
                         priority="high", labels=["needs human"]))
    fields = http.calls[-1][2]["fields"]
    assert fields["project"] == {"key": "OPS"} and fields["issuetype"] == {"name": "Task"}
    assert fields["priority"] == {"name": "High"}
    assert fields["labels"] == ["cf-operator", "needs-human"]
    assert fields["description"]["type"] == "doc"
    assert fields["description"]["content"][1]["type"] == "codeBlock"
    assert ref.key == "OPS-9" and ref.url == "https://j.example/browse/OPS-9"
    assert decode_ref(ref.ref) == {"backend": "jira", "key": "OPS-9"}


def test_transition_looks_up_the_named_transition_and_fails_when_not_offered():
    be, http = _backend()
    meta = {"backend": "jira", "key": "OPS-9"}
    be.transition(meta, "rejected", "not needed")
    assert http.calls[-1] == ("POST", "/rest/api/3/issue/OPS-9/transitions", {"transition": {"id": "41"}})
    assert http.calls[-2][1] == "/rest/api/3/issue/OPS-9/comment"
    be2, _ = _backend(resolved_transition="Closed")
    with pytest.raises(backends.TrackerError) as e:
        be2.transition(meta, "resolved", "")
    assert "Closed" in str(e.value) and "Done" in str(e.value)


@pytest.mark.parametrize("status, want", [
    ({"name": "To Do", "statusCategory": {"key": "new"}}, "open"),
    ({"name": "Done", "statusCategory": {"key": "done"}}, "resolved"),
    ({"name": "Won't Do", "statusCategory": {"key": "done"}}, "rejected"),
])
def test_get_maps_status(status, want):
    http = FakeHttp({("GET", "/rest/api/3/issue/OPS-9"): {
        "success": True, "status": 200, "data": {"fields": {"status": status, "updated": "t"}}}})
    be, _ = _backend(http)
    assert be.get({"backend": "jira", "key": "OPS-9"}).state == want


def test_make_jira_uses_basic_auth_and_env_overrides():
    be = make_jira({"JIRA_BASE_URL": "https://j.example/", "JIRA_EMAIL": "a@b", "JIRA_API_TOKEN": "tok",
                    "JIRA_PROJECT_KEY": "OPS", "JIRA_ISSUE_TYPE": "Bug", "JIRA_PRIORITY_LOW": "Lowest"})
    expected = base64.b64encode(b"a@b:tok").decode()
    assert be.http.headers["Authorization"] == f"Basic {expected}"
    assert be.issue_type == "Bug" and be.priority["low"] == "Lowest"


def test_adf_never_returns_an_empty_document():
    doc = adf("")
    assert doc["content"] and doc["content"][0]["type"] == "paragraph"
