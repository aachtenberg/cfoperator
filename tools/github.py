"""
GitHub API Tools for Code Investigation and PR/Issue Automation
===============================================================

Stdlib-only (urllib.request) GitHub REST API client so the event
runtime can use it without extra dependencies.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

from event_runtime.github_client import DEFAULT_GITHUB_API_URL, GitHubApiClient, validate_repo_slug

logger = logging.getLogger("cfoperator.tools.github")

_DEFAULT_API_URL = DEFAULT_GITHUB_API_URL


class GitHubTools:
    """GitHub REST API wrapper using only the Python standard library."""

    def __init__(
        self,
        token: str,
        api_url: str = _DEFAULT_API_URL,
        repos_config: Optional[List[Dict[str, Any]]] = None,
    ):
        self._client = GitHubApiClient(token=token, api_url=api_url)
        self._api_url = api_url.rstrip("/")
        self.repos: Dict[str, Dict[str, Any]] = {}
        for entry in (repos_config or []):
            name = entry.get("name") or entry.get("github", "unknown")
            self.repos[name] = entry
        logger.info("GitHub tools initialized (api=%s, repos=%d)", self._api_url, len(self.repos))

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[Dict[str, str]] = None,
        cache_ttl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated GitHub API request."""
        return self._client.request(method, path, body=body, params=params, cache_ttl=cache_ttl)

    def _validate_slug(self, slug: str) -> str:
        return validate_repo_slug(slug)

    def _resolve_slug(self, repo: str) -> Optional[str]:
        """Return the ``owner/repo`` slug for a configured repo name."""
        if "/" in repo:
            return self._validate_slug(repo)
        cfg = self.repos.get(repo)
        if cfg and cfg.get("github"):
            return self._validate_slug(cfg["github"])
        for cfg in self.repos.values():
            if cfg.get("github") == repo:
                return self._validate_slug(cfg["github"])
        return None

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_recent_prs(self, repo: str, state: str = "all", count: int = 10) -> Dict[str, Any]:
        """List recent pull requests."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("GET", f"/repos/{slug}/pulls", params={"state": state, "per_page": str(count), "sort": "updated", "direction": "desc"}, cache_ttl=120)
        if not resp["success"]:
            return resp
        prs = [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "user": pr["user"]["login"],
                "created_at": pr["created_at"],
                "updated_at": pr["updated_at"],
                "merged_at": pr.get("merged_at"),
                "html_url": pr["html_url"],
            }
            for pr in resp["data"]
        ]
        return {"success": True, "repo": slug, "pull_requests": prs}

    def get_pr(self, repo: str, pr_number: int) -> Dict[str, Any]:
        """Get details for a specific pull request."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("GET", f"/repos/{slug}/pulls/{int(pr_number)}", cache_ttl=120)
        if not resp["success"]:
            return resp
        pr = resp["data"]
        pr_files = []
        files_resp = self._client.get_paginated(
            f"/repos/{slug}/pulls/{int(pr_number)}/files",
            cache_ttl=120,
        )
        if files_resp["success"]:
            pr_files = [
                {
                    "filename": file["filename"],
                    "status": file.get("status", "modified"),
                    "additions": file.get("additions", 0),
                    "deletions": file.get("deletions", 0),
                    "changes": file.get("changes", 0),
                    "previous_filename": file.get("previous_filename"),
                }
                for file in files_resp["data"]
            ]
        return {
            "success": True,
            "repo": slug,
            "pr": {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "user": pr["user"]["login"],
                "body": pr.get("body", ""),
                "created_at": pr["created_at"],
                "updated_at": pr["updated_at"],
                "merged_at": pr.get("merged_at"),
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "changed_files": pr.get("changed_files", 0),
                "html_url": pr["html_url"],
                "head": pr["head"]["ref"],
                "base": pr["base"]["ref"],
                "files": pr_files,
            },
        }

    def list_recent_commits(self, repo: str, branch: Optional[str] = None, count: int = 10) -> Dict[str, Any]:
        """List recent commits on a branch via the GitHub API."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        params: Dict[str, str] = {"per_page": str(count)}
        if branch:
            params["sha"] = branch
        resp = self._request("GET", f"/repos/{slug}/commits", params=params, cache_ttl=120)
        if not resp["success"]:
            return resp
        commits = [
            {
                "sha": c["sha"],
                "author": (c.get("commit", {}).get("author") or {}).get("name", ""),
                "date": (c.get("commit", {}).get("author") or {}).get("date", ""),
                "message": c.get("commit", {}).get("message", "").split("\n", 1)[0],
                "html_url": c.get("html_url", ""),
            }
            for c in resp["data"]
        ]
        return {"success": True, "repo": slug, "branch": branch, "commits": commits}

    def get_issue(self, repo: str, issue_number: int) -> Dict[str, Any]:
        """Get issue details."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("GET", f"/repos/{slug}/issues/{int(issue_number)}", cache_ttl=120)
        if not resp["success"]:
            return resp
        issue = resp["data"]
        return {
            "success": True,
            "repo": slug,
            "issue": {
                "number": issue["number"],
                "title": issue["title"],
                "state": issue["state"],
                "user": issue["user"]["login"],
                "body": issue.get("body", ""),
                "labels": [l["name"] for l in issue.get("labels", [])],
                "created_at": issue["created_at"],
                "updated_at": issue["updated_at"],
                "html_url": issue["html_url"],
            },
        }

    def search_issues(self, repo: str, query: str) -> Dict[str, Any]:
        """Search issues and pull requests in a repository."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        q = f"repo:{slug} {query}"
        resp = self._request("GET", "/search/issues", params={"q": q, "per_page": "10", "sort": "updated"}, cache_ttl=60)
        if not resp["success"]:
            return resp
        items = [
            {
                "number": item["number"],
                "title": item["title"],
                "state": item["state"],
                "is_pr": "pull_request" in item,
                "html_url": item["html_url"],
                "updated_at": item["updated_at"],
            }
            for item in resp["data"].get("items", [])
        ]
        return {"success": True, "repo": slug, "query": query, "results": items, "total_count": resp["data"].get("total_count", 0)}

    def get_file_contents(self, repo: str, path: str, ref: Optional[str] = None) -> Dict[str, Any]:
        """Read a file from the repository via the Contents API."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        params = {}
        if ref:
            params["ref"] = ref
        resp = self._request("GET", f"/repos/{slug}/contents/{path}", params=params or None, cache_ttl=300)
        if not resp["success"]:
            return resp
        data = resp["data"]
        content = ""
        if data.get("encoding") == "base64" and data.get("content"):
            content = base64.b64decode(data["content"]).decode(errors="replace")
        return {"success": True, "repo": slug, "path": path, "ref": ref, "content": content, "sha": data.get("sha", "")}

    def compare_commits(self, repo: str, base: str, head: str) -> Dict[str, Any]:
        """Compare two refs and return file list and stats."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("GET", f"/repos/{slug}/compare/{base}...{head}", cache_ttl=120)
        if not resp["success"]:
            return resp
        data = resp["data"]
        files = [
            {"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"]}
            for f in data.get("files", [])
        ]
        return {
            "success": True,
            "repo": slug,
            "base": base,
            "head": head,
            "ahead_by": data.get("ahead_by", 0),
            "behind_by": data.get("behind_by", 0),
            "total_commits": data.get("total_commits", 0),
            "files": files,
        }

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> Dict[str, Any]:
        """Create a pull request."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("POST", f"/repos/{slug}/pulls", body={"title": title, "body": body, "head": head, "base": base})
        if not resp["success"]:
            return resp
        pr = resp["data"]
        return {
            "success": True,
            "repo": slug,
            "pr_number": pr["number"],
            "html_url": pr["html_url"],
            "message": f"PR #{pr['number']} created: {pr['html_url']}",
        }

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> Dict[str, Any]:
        """Post a comment on an issue or pull request."""
        slug = self._resolve_slug(repo)
        if not slug:
            return {"success": False, "error": f"Cannot resolve GitHub repo for: {repo}"}
        resp = self._request("POST", f"/repos/{slug}/issues/{int(issue_number)}/comments", body={"body": body})
        if not resp["success"]:
            return resp
        return {
            "success": True,
            "repo": slug,
            "issue_number": issue_number,
            "comment_url": resp["data"].get("html_url", ""),
            "message": f"Comment posted on #{issue_number}",
        }

    # ------------------------------------------------------------------
    # LLM tool schemas
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Return JSON schemas for LLM function-calling registration."""
        repo_names = ", ".join(self.repos.keys()) if self.repos else "any owner/repo slug"
        return [
            {
                "name": "github_list_recent_prs",
                "description": f"List recent pull requests from a GitHub repository. Repos: {repo_names}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "state": {"type": "string", "description": "PR state filter: open, closed, all", "default": "all", "enum": ["open", "closed", "all"]},
                        "count": {"type": "integer", "description": "Number of PRs (default 10)", "default": 10},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "github_get_pr",
                "description": "Get details for a specific pull request including diff stats, changed files, and review status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "pr_number": {"type": "integer", "description": "Pull request number"},
                    },
                    "required": ["repo", "pr_number"],
                },
            },
            {
                "name": "github_list_recent_commits",
                "description": "List recent commits on a branch from GitHub (no local clone needed).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "branch": {"type": "string", "description": "Branch name (default: repo default branch)"},
                        "count": {"type": "integer", "description": "Number of commits (default 10)", "default": 10},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "github_get_issue",
                "description": "Get details for a specific GitHub issue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "issue_number": {"type": "integer", "description": "Issue number"},
                    },
                    "required": ["repo", "issue_number"],
                },
            },
            {
                "name": "github_search_issues",
                "description": "Search issues and pull requests in a GitHub repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "query": {"type": "string", "description": "Search query (e.g. 'is:open label:bug')"},
                    },
                    "required": ["repo", "query"],
                },
            },
            {
                "name": "github_get_file_contents",
                "description": "Read a file from a GitHub repository at a specific ref.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "path": {"type": "string", "description": "File path relative to repo root"},
                        "ref": {"type": "string", "description": "Git ref (branch, tag, SHA). Default: repo default branch."},
                    },
                    "required": ["repo", "path"],
                },
            },
            {
                "name": "github_compare_commits",
                "description": "Compare two git refs on GitHub — shows files changed, additions, deletions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "base": {"type": "string", "description": "Base ref (commit, tag, branch)"},
                        "head": {"type": "string", "description": "Head ref (commit, tag, branch)"},
                    },
                    "required": ["repo", "base", "head"],
                },
            },
            {
                "name": "github_create_pr",
                "description": "Create a new pull request on GitHub. Requires an existing branch with commits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "title": {"type": "string", "description": "PR title"},
                        "body": {"type": "string", "description": "PR description (markdown)"},
                        "head": {"type": "string", "description": "Source branch name"},
                        "base": {"type": "string", "description": "Target branch name (e.g. main)"},
                    },
                    "required": ["repo", "title", "body", "head", "base"],
                },
            },
            {
                "name": "github_create_issue_comment",
                "description": "Post a comment on a GitHub issue or pull request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name or owner/repo slug"},
                        "issue_number": {"type": "integer", "description": "Issue or PR number"},
                        "body": {"type": "string", "description": "Comment body (markdown)"},
                    },
                    "required": ["repo", "issue_number", "body"],
                },
            },
        ]
