"""Unit tests for the github recorder (mocked GitHub client)."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import pytest

from github_recorder import ChangeRecordError, GitHubRecorder
from shapes import Intent, decode_ref, encode_ref


def _intent(**extra) -> Intent:
    base = dict(
        remediation_id=42,
        host="headless-gpu",
        commands=["sudo -n chmod 600 /root/.ssh/config"],
        justification="fix perms",
        image_digest="ghcr.io/aachtenberg/cfoperator-executor@sha256:abc",
        flag_snapshot={"node_action.enabled": True},
        investigation_id=99,
        risk="low",
        confidence=0.9,
    )
    base.update(extra)
    return Intent(**base)


class _FakeGH:
    def __init__(self, responses: Dict[str, Any]):
        self.responses = responses
        self.calls: List[tuple] = []

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        key = f"{method} {path.split('?')[0]}"
        for k, v in self.responses.items():
            if key.startswith(k) or k == key:
                return v
        return {"success": False, "status": 404, "data": {}}

    def get_file(self, repo, path, ref):
        return self.responses.get(f"FILE {ref}:{path}")


def test_github_open_creates_branch_commit_and_pr():
    client = _FakeGH({
        "GET /repos/o/r/git/ref/heads/cfop/change/42": {"success": False, "status": 404, "data": {}},
        "GET /repos/o/r/git/ref/heads/main": {
            "success": True, "status": 200, "data": {"object": {"sha": "abc"}},
        },
        "POST /repos/o/r/git/refs": {"success": True, "status": 201, "data": {}},
        "PUT /repos/o/r/contents/change-records/42.json": {
            "success": True, "status": 201, "data": {},
        },
        "POST /repos/o/r/pulls": {
            "success": True, "status": 201,
            "data": {"number": 7, "html_url": "https://github.com/o/r/pull/7"},
        },
        "FILE main:change-records/42.json": None,
    })
    backend = GitHubRecorder(client, "o/r", "main")
    ref = backend.open(_intent())
    assert ref.url.endswith("/pull/7")
    meta = decode_ref(ref.id)
    assert meta["pr_number"] == 7 and meta["backend"] == "github"
    put = next(c for c in client.calls if c[0] == "PUT")
    content = json.loads(base64.b64decode(put[2]["content"]))
    assert content["executor_image_digest"].startswith("ghcr.io/")
    assert content["flag_snapshot"]["node_action.enabled"] is True
    assert content["commands"] == ["sudo -n chmod 600 /root/.ssh/config"]


def test_github_approval_merged_vs_open_vs_closed():
    client = _FakeGH({
        "GET /repos/o/r/pulls/7": {
            "success": True, "status": 200,
            "data": {"merged": True, "merged_by": {"login": "bob"},
                     "merged_at": "2026-07-30T12:00:00Z", "state": "closed"},
        },
    })
    backend = GitHubRecorder(client, "o/r")
    token = encode_ref({"backend": "github", "repo": "o/r", "pr_number": 7})
    ap = backend.approval(token)
    assert ap is not None and ap.identity == "bob" and ap.state == "merged"

    client.responses["GET /repos/o/r/pulls/7"] = {
        "success": True, "status": 200, "data": {"merged": False, "state": "open"},
    }
    assert backend.approval(token) is None

    client.responses["GET /repos/o/r/pulls/7"] = {
        "success": True, "status": 200, "data": {"merged": False, "state": "closed"},
    }
    with pytest.raises(ChangeRecordError, match="closed without merge"):
        backend.approval(token)


def test_github_close_commits_outcome():
    doc = {"kind": "cfop-change-record", "outcome": None}
    client = _FakeGH({
        "FILE main:change-records/7.json": (json.dumps(doc), "sha1"),
        "PUT /repos/o/r/contents/change-records/7.json": {
            "success": True, "status": 200, "data": {},
        },
    })
    backend = GitHubRecorder(client, "o/r")
    token = encode_ref({
        "backend": "github", "repo": "o/r", "base": "main",
        "path": "change-records/7.json", "pr_number": 7,
    })
    backend.close(token, {"executed": [{"returncode": 0}]})
    put = next(c for c in client.calls if c[0] == "PUT")
    written = json.loads(base64.b64decode(put[2]["content"]))
    assert written["outcome"]["executed"][0]["returncode"] == 0
    assert "closed_at" in written
