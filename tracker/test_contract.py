"""Contract tests against a fake in-process backend over the real HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import pytest

import entrypoint
from backends import TrackerError, TrackerNotFound
from shapes import Item, ItemRef, ItemState, encode_ref


class FakeBackend:
    """In-memory backend that exercises the HTTP contract without a tracker."""

    name = "fake"

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.comments: Dict[str, list] = {}
        self.transitions: Dict[str, list] = {}
        self.boom = False
        self.refuse = ""

    def create(self, item: Item) -> ItemRef:
        if self.boom:
            raise RuntimeError("secret boom detail https://x/?token=abc")
        if self.refuse:
            raise TrackerError(self.refuse)
        meta = {"backend": "fake", "id": str(item.remediation_id)}
        ref = encode_ref(meta)
        self.items[ref] = {"item": item, "state": "open"}
        return ItemRef(ref=ref, url=f"http://fake/{item.remediation_id}",
                       key=f"FAKE-{item.remediation_id}", backend="fake", meta=meta)

    def _ref(self, meta):
        ref = encode_ref(meta)
        if ref not in self.items:
            raise TrackerNotFound("no such item")
        return ref

    def comment(self, meta, body_markdown):
        self.comments.setdefault(self._ref(meta), []).append(body_markdown)

    def transition(self, meta, state, note):
        ref = self._ref(meta)
        self.transitions.setdefault(ref, []).append((state, note))
        self.items[ref]["state"] = state

    def get(self, meta):
        ref = self._ref(meta)
        return ItemState(state=self.items[ref]["state"], url="http://fake/x", key="FAKE-x",
                         updated_at="t")


def _serve(env):
    fake = FakeBackend()
    httpd, _ = entrypoint.make_server(
        {"CFOP_TRACKER_HOST": "127.0.0.1", "CFOP_TRACKER_PORT": "0", **env}, backend=fake)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    return httpd, fake, f"http://{host}:{port}"


@pytest.fixture
def server():
    httpd, fake, base = _serve({})
    yield base, fake
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def authed_server():
    httpd, fake, base = _serve({"CFOP_TRACKER_SHARED_SECRET": "s3cret"})
    yield base, fake
    httpd.shutdown()
    httpd.server_close()


def _json(method: str, url: str, body: Optional[dict] = None,
          headers: Optional[dict] = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": raw}
        return e.code, payload


ITEM = {"remediation_id": 42, "title": "[cfop #42] disk full on pi2", "body_markdown": "## Why\n- x",
        "priority": "low", "labels": ["cfoperator"], "links": {"console_url": "http://c/remediations#42"}}


def test_create_comment_transition_get_contract(server):
    base, fake = server
    status, created = _json("POST", f"{base}/items", ITEM)
    assert status == 201
    assert created["key"] == "FAKE-42" and created["backend"] == "fake"
    ref = created["ref"]
    assert fake.items[ref]["item"].priority == "low"
    assert fake.items[ref]["item"].links["console_url"].endswith("#42")

    status, body = _json("GET", f"{base}/items/{ref}")
    assert status == 200 and body["state"] == "open" and body["key"] == "FAKE-x"

    status, body = _json("POST", f"{base}/items/{ref}/comment", {"body_markdown": "PR opened: http://p"})
    assert status == 200 and body["ok"] is True
    assert fake.comments[ref] == ["PR opened: http://p"]

    status, body = _json("POST", f"{base}/items/{ref}/transition", {"state": "resolved", "note": "fixed"})
    assert status == 200 and fake.transitions[ref] == [("resolved", "fixed")]
    status, body = _json("GET", f"{base}/items/{ref}")
    assert body["state"] == "resolved"


def test_create_validates_body(server):
    base, _ = server
    status, body = _json("POST", f"{base}/items", {"title": "no id"})
    assert status == 400 and "remediation_id" in body["error"]
    status, body = _json("POST", f"{base}/items", {"remediation_id": 1})
    assert status == 400 and "title" in body["error"]
    status, body = _json("POST", f"{base}/items", {**ITEM, "priority": "urgent"})
    assert status == 400 and "priority" in body["error"]
    status, body = _json("POST", f"{base}/items", {**ITEM, "labels": "notalist"})
    assert status == 400 and "labels" in body["error"]


def test_transition_rejects_unknown_state_and_empty_comment(server):
    base, _ = server
    _, created = _json("POST", f"{base}/items", ITEM)
    ref = created["ref"]
    status, body = _json("POST", f"{base}/items/{ref}/transition", {"state": "closed"})
    assert status == 400 and "resolved, rejected" in body["error"]
    status, body = _json("POST", f"{base}/items/{ref}/comment", {"body_markdown": "  "})
    assert status == 400 and "body_markdown" in body["error"]


def test_bad_ref_is_400_and_foreign_backend_ref_is_400(server):
    base, _ = server
    status, body = _json("GET", f"{base}/items/not-a-token!!")
    assert status == 400 and "ref" in body["error"]
    foreign = encode_ref({"backend": "jira", "key": "X-1"})
    status, body = _json("GET", f"{base}/items/{foreign}")
    assert status == 400 and "jira" in body["error"] and "fake" in body["error"]


def test_missing_item_is_404(server):
    base, _ = server
    gone = encode_ref({"backend": "fake", "id": "nope"})
    status, body = _json("GET", f"{base}/items/{gone}")
    assert status == 404
    status, _ = _json("POST", f"{base}/items/{gone}/comment", {"body_markdown": "x"})
    assert status == 404
    status, _ = _json("POST", f"{base}/items/{gone}/transition", {"state": "rejected"})
    assert status == 404


def test_backend_refusal_is_400_with_its_reason(server):
    base, fake = server
    fake.refuse = "plane: no state with group 'completed'"
    status, body = _json("POST", f"{base}/items", ITEM)
    assert status == 400 and "completed" in body["error"]


def test_500_returns_generic_message(server):
    base, fake = server
    fake.boom = True
    status, body = _json("POST", f"{base}/items", ITEM)
    assert status == 500
    assert body["error"] == "internal error"
    assert "token=" not in json.dumps(body)


def test_auth_required_when_secret_set(authed_server):
    base, _ = authed_server
    status, body = _json("POST", f"{base}/items", ITEM)
    assert status == 401 and "Missing" in body["error"]
    status, body = _json("POST", f"{base}/items", ITEM, headers={"X-CFOP-Token": "wrong"})
    assert status == 401 and "Invalid" in body["error"]
    status, created = _json("POST", f"{base}/items", ITEM, headers={"X-CFOP-Token": "s3cret"})
    assert status == 201
    ref = created["ref"]
    status, _ = _json("GET", f"{base}/items/{ref}")
    assert status == 401
    status, _ = _json("POST", f"{base}/items/{ref}/comment", {"body_markdown": "x"})
    assert status == 401
    status, _ = _json("POST", f"{base}/items/{ref}/transition", {"state": "resolved"})
    assert status == 401
    # health stays open
    status, body = _json("GET", f"{base}/healthz")
    assert status == 200 and body["ok"] is True and body["backend"] == "fake"
    status, body = _json("GET", f"{base}/livez")
    assert status == 200


def test_unknown_routes_are_404(server):
    base, _ = server
    assert _json("GET", f"{base}/open")[0] == 404
    assert _json("POST", f"{base}/items/x/y/z", {})[0] == 404
