"""Tests for the stdlib GitHub PR-from-diff client."""

from github import get_file, list_repo_files, open_pr_from_diff

_DIFF = """--- a/k3s/base/apps/ollama.yaml
+++ b/k3s/base/apps/ollama.yaml
@@ -1,3 +1,3 @@
 a
-b
+B
 c
"""


class _FakeClient:
    """Routes request()/_get_file() to canned responses and records POSTs."""

    def __init__(self, *, branch_exists=False, file_text="a\nb\nc\n"):
        self.branch_exists = branch_exists
        self.file_text = file_text
        self.posts = []

    def request(self, method, path, *, body=None):
        if method == "GET" and "/git/ref/heads/cfop/" in path:
            return {"success": self.branch_exists, "status": 200 if self.branch_exists else 404, "data": {}}
        if method == "GET" and "/git/ref/heads/" in path:  # base ref
            return {"success": True, "status": 200, "data": {"object": {"sha": "basesha"}}}
        if method == "POST" and path.endswith("/git/refs"):
            self.posts.append(("branch", body))
            return {"success": True, "status": 201, "data": {}}
        if method == "PUT" and "/contents/" in path:
            self.posts.append(("commit", body))
            return {"success": True, "status": 200, "data": {}}
        if method == "POST" and path.endswith("/pulls"):
            self.posts.append(("pr", body))
            return {"success": True, "status": 201,
                    "data": {"number": 42, "html_url": "https://github.com/o/r/pull/42"}}
        return {"success": False, "status": 404, "data": {}}

    def _get_file(self, repo, path, ref):
        return (self.file_text, "filesha")


def _open(client):
    return open_pr_from_diff(client, repo="o/r", base="main", diff_text=_DIFF,
                             title="t", body="b", dedupe_key="inv-1")


def test_open_pr_happy_path():
    client = _FakeClient()
    result = _open(client)
    assert result["status"] == "opened"
    assert result["pr_number"] == 42
    assert result["html_url"].endswith("/pull/42")
    # branch name derived from dedupe key
    assert result["branch"] == "cfop/remediate-inv-1"
    assert [p[0] for p in client.posts] == ["branch", "commit", "pr"]


def test_open_pr_skips_when_branch_exists():
    result = _open(_FakeClient(branch_exists=True))
    assert result["status"] == "skipped"


def test_open_pr_refuses_secret_path():
    secret_diff = _DIFF.replace("ollama.yaml", "secrets/sealed.yaml")
    result = open_pr_from_diff(_FakeClient(), repo="o/r", base="main", diff_text=secret_diff,
                              title="t", body="b", dedupe_key="x")
    assert result["status"] == "refused"


def test_open_pr_declines_non_diff():
    result = open_pr_from_diff(_FakeClient(), repo="o/r", base="main", diff_text="not a diff",
                              title="t", body="b", dedupe_key="x")
    assert result["status"] == "declined"


class _TreeClient:
    """Fake client for list_repo_files: base ref + recursive tree."""
    def request(self, method, path, *, body=None):
        if "/git/ref/heads/" in path:
            return {"success": True, "data": {"object": {"sha": "basesha"}}}
        if "/git/trees/" in path:
            return {"success": True, "data": {"tree": [
                {"type": "blob", "path": "k8s/base/apps/ollama.yaml"},
                {"type": "blob", "path": "k8s/base/secrets/sealed.yaml"},  # secret -> filtered
                {"type": "blob", "path": "README.md"},                     # non-yaml -> filtered
                {"type": "tree", "path": "k8s/base"},                      # dir -> skipped
            ]}}
        return {"success": False, "data": {}}


def test_list_repo_files_filters_yaml_and_secrets():
    files = list_repo_files(_TreeClient(), "o/r", "main")
    assert files == ["k8s/base/apps/ollama.yaml"]


def test_get_file_returns_content():
    assert get_file(_FakeClient(file_text="hello\n"), "o/r", "p.yaml", "main") == "hello\n"


def test_open_pr_declines_on_drift():
    # current file differs from the diff's expected context -> decline, not guess
    result = open_pr_from_diff(_FakeClient(file_text="x\ny\nz\n"), repo="o/r", base="main",
                              diff_text=_DIFF, title="t", body="b", dedupe_key="x")
    assert result["status"] == "declined"
