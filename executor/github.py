"""Minimal, self-contained GitHub REST client for the portable executor.

Stdlib urllib only — the executor opens its own PRs rather than calling back
into the agent's RemediationProposer, so it carries no monolith dependency.
The PR flow and safety gates (single-file diff, secret-path refusal, branch
dedupe, exact-context application) mirror the agent's deep-diff path.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from diff import apply_unified_diff, is_secret_path, parse_unified_diff

_DEFAULT_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, api_url: str = _DEFAULT_API, *, timeout: int = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, *, body: Optional[dict] = None) -> Dict[str, Any]:
        """Return {success, status, data}. Never raises on HTTP errors."""
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "cfoperator-executor",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec
                payload = resp.read().decode("utf-8")
                return {"success": True, "status": resp.status,
                        "data": json.loads(payload) if payload else {}}
        except urllib.error.HTTPError as e:
            return {"success": False, "status": e.code, "data": {}}
        except urllib.error.URLError as e:
            return {"success": False, "status": 0, "data": {"error": str(e)}}

    def _get_file(self, repo: str, path: str, ref: str) -> Optional[Tuple[str, str]]:
        r = self.request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        if not r.get("success"):
            return None
        d = r.get("data") or {}
        if d.get("encoding") != "base64" or "content" not in d:
            return None
        text = base64.b64decode(d["content"]).decode("utf-8")
        return text, d.get("sha", "")


def open_pr_from_diff(client: GitHubClient, *, repo: str, base: str, diff_text: str,
                      title: str, body: str, dedupe_key: str) -> Dict[str, Any]:
    """Open a PR from a single-file unified diff. Returns a status dict.

    Statuses: opened | skipped | declined | refused | error. Human merge stays
    the only path that mutates the cluster (ArgoCD syncs the merged PR).
    """
    parsed = parse_unified_diff(diff_text)
    if not parsed:
        return {"status": "declined", "detail": "not a clean single-file unified diff"}
    path, hunks = parsed
    if is_secret_path(path):
        return {"status": "refused", "detail": f"won't patch secret-bearing file: {path}"}

    slug = re.sub(r"[^a-z0-9-]+", "-", f"remediate-{dedupe_key}".lower()).strip("-")
    branch = f"cfop/{slug}"

    if client.request("GET", f"/repos/{repo}/git/ref/heads/{branch}").get("success"):
        return {"status": "skipped", "detail": "remediation branch already exists", "branch": branch}

    got = client._get_file(repo, path, base)
    if not got:
        return {"status": "declined", "detail": f"could not fetch {path} at {base}"}
    text, file_sha = got
    patched = apply_unified_diff(text, hunks)
    if not patched or patched == text:
        return {"status": "declined", "detail": "diff does not apply cleanly to the current file"}

    head = client.request("GET", f"/repos/{repo}/git/ref/heads/{base}")
    head_sha = ((head.get("data") or {}).get("object") or {}).get("sha") if head.get("success") else None
    if not head_sha:
        return {"status": "error", "detail": f"could not read base ref {base}"}

    cr = client.request("POST", f"/repos/{repo}/git/refs",
                        body={"ref": f"refs/heads/{branch}", "sha": head_sha})
    if not cr.get("success"):
        return {"status": "error", "detail": f"branch create failed ({cr.get('status')})"}

    commit = client.request("PUT", f"/repos/{repo}/contents/{path}", body={
        "message": title,
        "content": base64.b64encode(patched.encode("utf-8")).decode("ascii"),
        "branch": branch,
        "sha": file_sha,
    })
    if not commit.get("success"):
        return {"status": "error", "detail": f"commit failed ({commit.get('status')})"}

    pr = client.request("POST", f"/repos/{repo}/pulls",
                        body={"title": title, "body": body, "head": branch, "base": base})
    if not pr.get("success"):
        return {"status": "error", "detail": f"PR create failed ({pr.get('status')})"}
    data = pr.get("data") or {}
    return {"status": "opened", "pr_number": data.get("number"),
            "html_url": data.get("html_url"), "branch": branch, "path": path}
