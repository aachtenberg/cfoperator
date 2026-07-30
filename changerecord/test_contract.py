"""Contract tests against a fake in-process recorder over the real HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import pytest

import entrypoint
from shapes import Approval, Intent, RecordRef, encode_ref


class FakeRecorder:
    """In-memory backend that exercises the HTTP contract without GitHub."""

    def __init__(self):
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.approved: Dict[str, Approval] = {}
        self.closed: Dict[str, Dict[str, Any]] = {}
        self.reject_closed = False
        self.boom = False

    def open(self, intent: Intent) -> RecordRef:
        if self.boom:
            raise RuntimeError("secret boom detail")
        meta = {"backend": "fake", "rid": intent.remediation_id,
                "image": intent.image, "flags": intent.flag_snapshot}
        ref = encode_ref(meta)
        self.docs[ref] = {
            "remediation_id": intent.remediation_id,
            "host": intent.host,
            "commands": list(intent.commands),
            "image": intent.image,
            "flag_snapshot": dict(intent.flag_snapshot),
        }
        return RecordRef(id=ref, url=f"http://fake/{intent.remediation_id}", meta=meta)

    def approval(self, ref_token: str) -> Optional[Approval]:
        if self.reject_closed and ref_token in self.docs:
            from github_recorder import ChangeRecordError
            raise ChangeRecordError("closed without merge")
        return self.approved.get(ref_token)

    def close(self, ref_token: str, outcome: Dict[str, Any]) -> None:
        if ref_token not in self.docs:
            from github_recorder import ChangeRecordError
            raise ChangeRecordError("unknown ref")
        self.closed[ref_token] = outcome


@pytest.fixture
def server():
    fake = FakeRecorder()
    httpd, _ = entrypoint.make_server(
        {"CFOP_CHANGERECORD_HOST": "127.0.0.1", "CFOP_CHANGERECORD_PORT": "0"},
        recorder=fake,
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    base = f"http://{host}:{port}"
    yield base, fake
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def authed_server():
    fake = FakeRecorder()
    httpd, _ = entrypoint.make_server(
        {
            "CFOP_CHANGERECORD_HOST": "127.0.0.1",
            "CFOP_CHANGERECORD_PORT": "0",
            "CFOP_CHANGERECORD_SHARED_SECRET": "s3cret",
        },
        recorder=fake,
    )
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    base = f"http://{host}:{port}"
    yield base, fake
    httpd.shutdown()
    httpd.server_close()


def _json(method: str, url: str, body: Optional[dict] = None,
          headers: Optional[dict] = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        **(headers or {}),
    })
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


def test_open_approval_close_contract(server):
    base, fake = server
    status, opened = _json("POST", f"{base}/open", {
        "remediation_id": 42,
        "host": "controller",
        "commands": ["sudo -n chmod 600 /root/.ssh/config"],
        "justification": "fix perms",
        "image": "img:main",
        "flag_snapshot": {"node_action.enabled": True},
        "investigation_id": 9,
    })
    assert status == 201
    ref = opened["ref"]
    assert opened["url"].endswith("/42")
    assert fake.docs[ref]["image"] == "img:main"
    assert fake.docs[ref]["flag_snapshot"]["node_action.enabled"] is True
    assert fake.docs[ref]["commands"] == ["sudo -n chmod 600 /root/.ssh/config"]

    # Not yet approved -> 404
    status, body = _json("GET", f"{base}/approval/{ref}")
    assert status == 404 and "not approved" in body["error"]

    fake.approved[ref] = Approval(identity="alice", timestamp="t", state="merged")
    status, body = _json("GET", f"{base}/approval/{ref}")
    assert status == 200
    assert body == {"identity": "alice", "timestamp": "t", "state": "merged"}

    status, body = _json("POST", f"{base}/close", {
        "ref": ref,
        "outcome": {"executed": [{"returncode": 0}]},
    })
    assert status == 200 and body["ok"] is True
    assert fake.closed[ref]["executed"][0]["returncode"] == 0


def test_approval_closed_without_merge_is_409(server):
    base, fake = server
    status, opened = _json("POST", f"{base}/open", {
        "remediation_id": 1, "host": "h", "commands": ["chmod 600 /a"],
        "justification": "j", "image": "i",
    })
    assert status == 201
    fake.reject_closed = True
    status, body = _json("GET", f"{base}/approval/{opened['ref']}")
    assert status == 409
    assert "closed without merge" in body["error"]


def test_auth_required_when_secret_set(authed_server):
    base, _ = authed_server
    status, body = _json("POST", f"{base}/open", {
        "remediation_id": 1, "host": "h", "commands": ["chmod 600 /a"],
        "justification": "j", "image": "i",
    })
    assert status == 401 and "Missing" in body["error"]

    status, body = _json("POST", f"{base}/open", {
        "remediation_id": 1, "host": "h", "commands": ["chmod 600 /a"],
        "justification": "j", "image": "i",
    }, headers={"X-CFOP-Token": "wrong"})
    assert status == 401 and "Invalid" in body["error"]

    status, opened = _json("POST", f"{base}/open", {
        "remediation_id": 1, "host": "h", "commands": ["chmod 600 /a"],
        "justification": "j", "image": "i",
    }, headers={"X-CFOP-Token": "s3cret"})
    assert status == 201 and opened.get("ref")

    # healthz stays open
    status, body = _json("GET", f"{base}/healthz")
    assert status == 200 and body["ok"] is True


def test_500_returns_generic_message(server):
    base, fake = server
    fake.boom = True
    status, body = _json("POST", f"{base}/open", {
        "remediation_id": 1, "host": "h", "commands": ["chmod 600 /a"],
        "justification": "j", "image": "i",
    })
    assert status == 500
    assert body["error"] == "internal error"
    assert "secret boom" not in body["error"]


def test_close_requires_ref_and_outcome(server):
    base, _ = server
    status, body = _json("POST", f"{base}/close", {"outcome": {}})
    assert status == 400 and "ref" in body["error"]
    status, body = _json("POST", f"{base}/close", {"ref": "x", "outcome": "nope"})
    assert status == 400 and "outcome" in body["error"]


def test_healthz(server):
    base, _ = server
    status, body = _json("GET", f"{base}/healthz")
    assert status == 200 and body["ok"] is True


def test_intent_accepts_legacy_image_digest():
    from shapes import intent_from_body
    intent = intent_from_body({"host": "h", "commands": [], "image_digest": "old"})
    assert intent.image == "old"
