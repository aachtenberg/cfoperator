"""GitHub action handlers for the event runtime.

Provides action handlers that can open PRs, comment on issues, and
investigate recent code changes via the GitHub API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .github_client import DEFAULT_GITHUB_API_URL, GitHubApiClient, validate_repo_slug

from .models import ActionRequest, ActionResult
from .plugins import ActionHandler

logger = logging.getLogger(__name__)


class _GitHubMixin:
    """Shared HTTP helper for GitHub action handlers."""

    def __init__(self, token: str, api_url: str = DEFAULT_GITHUB_API_URL):
        self._client = GitHubApiClient(token=token, api_url=api_url)

    def _validate_slug(self, slug: str) -> str:
        """Validate and return a repo slug, raising ValueError if invalid."""
        return validate_repo_slug(slug)

    def _action_error(self, action: str, message: str, details: Optional[Dict[str, Any]] = None) -> ActionResult:
        return ActionResult(action=action, success=False, message=message, details=details or {})

    def _gh_request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        return self._client.request(method, path, body=body)


class OpenPRActionHandler(_GitHubMixin, ActionHandler):
    """Create a pull request on GitHub."""

    name = "open-pr-action"
    action_name = "open_pr"

    def __init__(self, token: str, api_url: str = DEFAULT_GITHUB_API_URL):
        _GitHubMixin.__init__(self, token, api_url)

    def execute(self, request: ActionRequest) -> ActionResult:
        params = request.decision.params
        repo = params.get("repo", "")
        title = params.get("title", f"[cfoperator] {request.alert.summary}")
        body = params.get("body", request.decision.reasoning)
        head = params.get("head", "")
        base = params.get("base", "main")

        if not repo or not head:
            return self._action_error(self.action_name, "Missing required params: repo, head", {"params": params})

        try:
            slug = self._validate_slug(repo)
        except ValueError as exc:
            return self._action_error(self.action_name, str(exc))

        resp = self._gh_request("POST", f"/repos/{slug}/pulls", body={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        })

        if not resp["success"]:
            return self._action_error(
                self.action_name,
                f"GitHub API error: {resp.get('error', resp.get('status', 'unknown'))}",
                {"response": resp},
            )

        pr = resp["data"]
        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"PR #{pr['number']} created: {pr['html_url']}",
            details={"pr_number": pr["number"], "html_url": pr["html_url"]},
        )


class CommentIssueActionHandler(_GitHubMixin, ActionHandler):
    """Post a comment on a GitHub issue or pull request."""

    name = "comment-issue-action"
    action_name = "comment_issue"

    def __init__(self, token: str, api_url: str = DEFAULT_GITHUB_API_URL):
        _GitHubMixin.__init__(self, token, api_url)

    def execute(self, request: ActionRequest) -> ActionResult:
        params = request.decision.params
        repo = params.get("repo", "")
        issue_number = params.get("issue_number")
        body = params.get("body", request.decision.reasoning)

        if not repo or not issue_number:
            return self._action_error(self.action_name, "Missing required params: repo, issue_number", {"params": params})

        try:
            slug = self._validate_slug(repo)
        except ValueError as exc:
            return self._action_error(self.action_name, str(exc))

        resp = self._gh_request("POST", f"/repos/{slug}/issues/{int(issue_number)}/comments", body={"body": body})

        if not resp["success"]:
            return self._action_error(
                self.action_name,
                f"GitHub API error: {resp.get('error', resp.get('status', 'unknown'))}",
                {"response": resp},
            )

        return ActionResult(
            action=self.action_name,
            success=True,
            message=f"Comment posted on #{issue_number}: {resp['data'].get('html_url', '')}",
            details={"comment_url": resp["data"].get("html_url", "")},
        )


class InvestigateCodeActionHandler(ActionHandler):
    """Investigate recent code changes for an alert.

    This handler does not require a GitHub token — it works with local
    git repos when available and falls back to the GitHub API.
    """

    name = "investigate-code-action"
    action_name = "investigate_code"

    def __init__(
        self,
        repos: list[Dict[str, Any]],
        github_token: Optional[str] = None,
        github_api_url: str = DEFAULT_GITHUB_API_URL,
    ):
        self._repos = repos
        self._github_token = github_token
        self._github_api_url = github_api_url.rstrip("/")

    def execute(self, request: ActionRequest) -> ActionResult:
        # Use recent_changes already enriched by GitChangeContextProvider
        recent = request.context.context.get("recent_changes")
        if recent:
            summary_parts = []
            for entry in recent:
                repo_name = entry.get("repo", "unknown")
                commits = entry.get("commits", [])
                summary_parts.append(f"{repo_name}: {len(commits)} recent commit(s)")
                for c in commits[:5]:
                    summary_parts.append(f"  {c.get('hash', '')[:8]} {c.get('author', '')} — {c.get('message', '')}")
            return ActionResult(
                action=self.action_name,
                success=True,
                message="Code investigation complete",
                details={"recent_changes": recent, "summary": "\n".join(summary_parts)},
            )

        return ActionResult(
            action=self.action_name,
            success=True,
            message="No recent code changes found for this alert context",
            details={},
        )


def build_github_action_handlers(
    repos: list[Dict[str, Any]],
    github_token: Optional[str] = None,
    github_api_url: str = DEFAULT_GITHUB_API_URL,
) -> Dict[str, ActionHandler]:
    """Build GitHub-related action handlers, gated on token availability."""
    handlers: Dict[str, ActionHandler] = {}

    # investigate_code works with or without a token
    handlers["investigate_code"] = InvestigateCodeActionHandler(
        repos=repos,
        github_token=github_token,
        github_api_url=github_api_url,
    )

    # Write handlers require a token
    if github_token:
        handlers["open_pr"] = OpenPRActionHandler(token=github_token, api_url=github_api_url)
        handlers["comment_issue"] = CommentIssueActionHandler(token=github_token, api_url=github_api_url)

    return handlers
