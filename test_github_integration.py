from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import urllib.error

from event_runtime.github_client import GitHubApiClient
from event_runtime.git_context import GitChangeContextProvider


def _load_module(module_name: str, relative_path: str):
    repo_root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(module_name, repo_root / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GitTools = _load_module("git_tools_module", "tools/git.py").GitTools
GitHubTools = _load_module("github_tools_module", "tools/github.py").GitHubTools


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {"X-RateLimit-Remaining": "100"}
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_github_client_retries_retryable_http_errors(monkeypatch):
    attempts = {"count": 0}
    delays: list[float] = []

    def fake_urlopen(req, timeout=0):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                503,
                "Service Unavailable",
                hdrs={"Retry-After": "0"},
                fp=io.BytesIO(b'{"message":"retry"}'),
            )
        return _FakeResponse({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: delays.append(seconds))

    client = GitHubApiClient(token="fake-token")
    response = client.request("GET", "/repos/owner/repo")

    assert response["success"] is True
    assert attempts["count"] == 2
    assert delays == [0.0]


def test_github_client_caches_get_requests(monkeypatch):
    calls = {"count": 0}

    def fake_urlopen(req, timeout=0):
        calls["count"] += 1
        return _FakeResponse({"items": [1]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = GitHubApiClient(token="fake-token")
    first = client.request("GET", "/search/issues", params={"q": "repo:owner/repo bug"}, cache_ttl=60)
    second = client.request("GET", "/search/issues", params={"q": "repo:owner/repo bug"}, cache_ttl=60)

    assert first["success"] is True
    assert second["success"] is True
    assert calls["count"] == 1


def test_github_tools_get_pr_includes_changed_files(monkeypatch):
    tools = GitHubTools(token="fake-token", repos_config=[{"name": "repo", "github": "owner/repo"}])

    def fake_request(method, path, body=None, params=None, cache_ttl=None):
        assert method == "GET"
        assert path == "/repos/owner/repo/pulls/7"
        return {
            "success": True,
            "status": 200,
            "data": {
                "number": 7,
                "title": "Improve retries",
                "state": "open",
                "user": {"login": "octocat"},
                "body": "details",
                "created_at": "2026-04-10T00:00:00Z",
                "updated_at": "2026-04-10T01:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/7",
                "head": {"ref": "feature/retries"},
                "base": {"ref": "main"},
                "additions": 15,
                "deletions": 3,
                "changed_files": 2,
            },
        }

    def fake_get_paginated(path, params=None, per_page=100, max_pages=10, cache_ttl=None):
        assert path == "/repos/owner/repo/pulls/7/files"
        return {
            "success": True,
            "status": 200,
            "data": [
                {
                    "filename": "tools/github.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 2,
                    "changes": 12,
                },
                {
                    "filename": "event_runtime/github_actions.py",
                    "status": "renamed",
                    "additions": 5,
                    "deletions": 1,
                    "changes": 6,
                    "previous_filename": "event_runtime/gh_actions.py",
                },
            ],
        }

    monkeypatch.setattr(tools._client, "request", fake_request)
    monkeypatch.setattr(tools._client, "get_paginated", fake_get_paginated)

    response = tools.get_pr("repo", 7)

    assert response["success"] is True
    assert response["pr"]["changed_files"] == 2
    assert response["pr"]["files"][0]["filename"] == "tools/github.py"
    assert response["pr"]["files"][1]["previous_filename"] == "event_runtime/gh_actions.py"


def test_git_context_provider_uses_known_hosts_for_remote_git(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="abc123|Alice|2026-04-09 10:00:00 +0000|fix\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    provider = GitChangeContextProvider(repos=[])
    provider._fetch_commits_local({
        "name": "cfoperator",
        "path": str(tmp_path),
        "ssh": {
            "user": "deploy",
            "address": "git.example.internal",
            "key_path": "~/.ssh/id_ed25519",
            "known_hosts_path": "~/.ssh/known_hosts",
        },
    })

    cmd = captured["cmd"]
    assert "StrictHostKeyChecking=yes" in cmd
    assert any(str(arg).startswith("UserKnownHostsFile=") for arg in cmd)
    assert os.path.expanduser("~/.ssh/id_ed25519") in cmd


def test_git_tools_uses_known_hosts_for_remote_git(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    git_tools = GitTools([
        {
            "name": "cfoperator",
            "path": "~/repos/cfoperator",
            "ssh": {
                "user": "deploy",
                "address": "git.example.internal",
                "key_path": "~/.ssh/id_ed25519",
                "known_hosts_path": "~/.ssh/known_hosts",
            },
        }
    ])

    result = git_tools.recent_commits("cfoperator", count=1)

    assert result["success"] is True
    cmd = captured["cmd"]
    assert "StrictHostKeyChecking=yes" in cmd
    assert any(str(arg).startswith("UserKnownHostsFile=") for arg in cmd)
    assert os.path.expanduser("~/.ssh/id_ed25519") in cmd