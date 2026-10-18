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
        image="ghcr.io/aachtenberg/cfoperator-executor:main",
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
    assert content["executor_image"].startswith("ghcr.io/")
    assert content["flag_snapshot"]["node_action.enabled"] is True
    assert content["commands"] == ["sudo -n chmod 600 /root/.ssh/config"]


def test_github_open_resume_without_pr_creates_pr():
    """Branch exists but PR create crashed — resume must create the PR."""
    client = _FakeGH({
        "GET /repos/o/r/git/ref/heads/cfop/change/42": {
            "success": True, "status": 200, "data": {"object": {"sha": "b"}},
        },
        "GET /repos/o/r/pulls": {"success": True, "status": 200, "data": []},
        "POST /repos/o/r/pulls": {
            "success": True, "status": 201,
            "data": {"number": 11, "html_url": "https://github.com/o/r/pull/11"},
        },
    })
    backend = GitHubRecorder(client, "o/r", "main")
    ref = backend.open(_intent())
    meta = decode_ref(ref.id)
    assert meta["pr_number"] == 11
    assert ref.url.endswith("/pull/11")
    assert any(c[0] == "POST" and c[1].endswith("/pulls") for c in client.calls)


def test_github_approval_ignores_token_repo():
    """Token-supplied repo must not redirect the recorder at another repo."""
    client = _FakeGH({
        "GET /repos/o/r/pulls/7": {
            "success": True, "status": 200,
            "data": {"merged": True, "merged_by": {"login": "bob"},
                     "merged_at": "2026-07-30T12:00:00Z", "state": "closed"},
        },
    })
    backend = GitHubRecorder(client, "o/r")
    token = encode_ref({"backend": "github", "repo": "evil/other", "pr_number": 7})
    ap = backend.approval(token)
    assert ap is not None and ap.identity == "bob"
    assert any(c[1] == "/repos/o/r/pulls/7" for c in client.calls)
    assert not any("evil/other" in c[1] for c in client.calls)


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


def test_github_close_rejects_path_outside_records_dir():
    backend = GitHubRecorder(_FakeGH({}), "o/r")
    token = encode_ref({
        "backend": "github", "repo": "o/r", "base": "main",
        "path": "../secrets/token", "pr_number": 7,
    })
    with pytest.raises(ChangeRecordError, match="path outside records_dir"):
        backend.close(token, {"executed": []})


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
        "backend": "github", "repo": "evil/other", "base": "main",
        "path": "change-records/7.json", "pr_number": 7,
    })
    backend.close(token, {"executed": [{"returncode": 0}]})
    put = next(c for c in client.calls if c[0] == "PUT")
    assert put[1].startswith("/repos/o/r/contents/")
    written = json.loads(base64.b64decode(put[2]["content"]))
    assert written["outcome"]["executed"][0]["returncode"] == 0
    assert "closed_at" in written


# ---- CFOP-442: pre-approved records -----------------------------------------

_CONFIRMED = {"identity": "aachten", "timestamp": "2026-10-18T00:00:00+00:00",
              "state": "console-confirmed", "via": "console-chat", "chat_session_id": 12}


def test_github_open_approved_commits_to_base_and_opens_no_pr():
    """A command an admin already confirmed is recorded, not put up for review:
    one commit on base carrying the approval, no branch, no PR."""
    client = _FakeGH({
        "PUT /repos/o/r/contents/change-records/42.json": {
            "success": True, "status": 201,
            "data": {"content": {"html_url": "https://github.com/o/r/blob/main/change-records/42.json"}},
        },
        "FILE main:change-records/42.json": None,
    })
    backend = GitHubRecorder(client, "o/r", "main")
    ref = backend.open(_intent(commands=["sudo -n docker stop square-redis"], approval=dict(_CONFIRMED)))
    methods = [(m, path) for m, path, _ in client.calls]
    assert not any(m == "POST" for m, _ in methods), methods  # no branch ref, no PR
    put = next(c for c in client.calls if c[0] == "PUT")
    assert put[2]["branch"] == "main"
    assert "approved in the console by aachten" in put[2]["message"]
    doc = json.loads(base64.b64decode(put[2]["content"]))
    assert doc["approval"]["identity"] == "aachten"
    assert doc["approval"]["state"] == "console-confirmed"
    assert doc["commands"] == ["sudo -n docker stop square-redis"]
    assert ref.url.endswith("/blob/main/change-records/42.json")
    assert ref.to_dict()["approval"]["identity"] == "aachten"
    meta = decode_ref(ref.id)
    assert meta["approved"] is True and meta["path"] == "change-records/42.json"


def test_github_open_without_approval_still_opens_a_pr():
    """The review path is untouched: no approval in the intent, no echo."""
    client = _FakeGH({
        "GET /repos/o/r/git/ref/heads/cfop/change/42": {"success": False, "status": 404, "data": {}},
        "GET /repos/o/r/git/ref/heads/main": {"success": True, "status": 200,
                                              "data": {"object": {"sha": "abc"}}},
        "POST /repos/o/r/git/refs": {"success": True, "status": 201, "data": {}},
        "PUT /repos/o/r/contents/change-records/42.json": {"success": True, "status": 201, "data": {}},
        "POST /repos/o/r/pulls": {"success": True, "status": 201,
                                  "data": {"number": 7, "html_url": "https://github.com/o/r/pull/7"}},
        "FILE main:change-records/42.json": None,
    })
    ref = GitHubRecorder(client, "o/r", "main").open(_intent())
    assert "approval" not in ref.to_dict()
    assert any(c[0] == "POST" and c[1].endswith("/pulls") for c in client.calls)


def test_github_approval_of_pre_approved_record_reads_the_committed_file():
    doc = {"kind": "cfop-change-record", "approval": dict(_CONFIRMED)}
    client = _FakeGH({"FILE main:change-records/42.json": (json.dumps(doc), "sha1")})
    backend = GitHubRecorder(client, "o/r", "main")
    token = encode_ref({"backend": "github", "path": "change-records/42.json", "approved": True})
    got = backend.approval(token)
    assert got.identity == "aachten" and got.state == "console-confirmed"
    # Never a PR lookup for this kind of record.
    assert not any("/pulls/" in c[1] for c in client.calls)


def test_github_approval_trusts_the_file_not_the_token():
    """A token that claims `approved` for a record holding no approval is not
    an approval: the committed file is the evidence, the token only a handle."""
    doc = {"kind": "cfop-change-record", "approval": None}
    client = _FakeGH({"FILE main:change-records/42.json": (json.dumps(doc), "sha1")})
    token = encode_ref({"backend": "github", "path": "change-records/42.json", "approved": True,
                        "approval": {"identity": "forged"}})
    assert GitHubRecorder(client, "o/r", "main").approval(token) is None


def test_github_approval_of_pre_approved_record_missing_from_base_is_an_error():
    client = _FakeGH({"FILE main:change-records/42.json": None})
    token = encode_ref({"backend": "github", "path": "change-records/42.json", "approved": True})
    with pytest.raises(ChangeRecordError):
        GitHubRecorder(client, "o/r", "main").approval(token)


def test_github_approval_of_pre_approved_record_keeps_the_path_rule():
    token = encode_ref({"backend": "github", "path": "../secrets.json", "approved": True})
    with pytest.raises(ChangeRecordError):
        GitHubRecorder(_FakeGH({}), "o/r", "main").approval(token)


def test_github_close_pre_approved_record_commits_outcome_on_base():
    doc = {"kind": "cfop-change-record", "approval": dict(_CONFIRMED), "outcome": None}
    client = _FakeGH({
        "FILE main:change-records/42.json": (json.dumps(doc), "sha1"),
        "PUT /repos/o/r/contents/change-records/42.json": {"success": True, "status": 200, "data": {}},
    })
    token = encode_ref({"backend": "github", "path": "change-records/42.json", "approved": True})
    GitHubRecorder(client, "o/r", "main").close(token, {"executed": [{"returncode": 0}]})
    put = next(c for c in client.calls if c[0] == "PUT")
    assert put[2]["branch"] == "main" and put[2]["sha"] == "sha1"
    assert "None" not in put[2]["message"]
    written = json.loads(base64.b64decode(put[2]["content"]))
    assert written["outcome"]["executed"][0]["returncode"] == 0
    assert written["approval"]["identity"] == "aachten"
