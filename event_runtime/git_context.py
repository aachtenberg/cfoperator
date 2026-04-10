"""Git change context provider for the event runtime.

Enriches alerts with recent code changes from repositories mapped to
the alerting host or service.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .github_client import DEFAULT_GITHUB_API_URL, GitHubApiClient, validate_repo_slug

from .models import Alert, ContextEnvelope
from .plugins import ContextProvider

logger = logging.getLogger(__name__)


class GitChangeContextProvider(ContextProvider):
    """Attach recent code changes when an alert maps to a tracked repo."""

    name = "git-change-context"
    capabilities = ("git", "code_changes")

    def __init__(
        self,
        repos: List[Dict[str, Any]],
        github_token: Optional[str] = None,
        github_api_url: str = DEFAULT_GITHUB_API_URL,
        commit_count: int = 10,
    ):
        self._repos = repos
        self._github_api_url = github_api_url.rstrip("/")
        self._commit_count = commit_count
        self._github_client = GitHubApiClient(
            token=github_token,
            api_url=github_api_url,
            timeout=15,
        )

    # ------------------------------------------------------------------
    # Host / service matching
    # ------------------------------------------------------------------

    def _repos_for_alert(self, alert: Alert) -> List[Dict[str, Any]]:
        """Return repo configs whose hosts/services overlap with the alert."""
        targets: set[str] = set()
        # Collect host identifiers from alert
        for key in ("host", "hostname", "address", "instance"):
            val = alert.details.get(key)
            if val:
                targets.add(str(val).split(":")[0])  # strip port from instance
        if alert.resource_name:
            targets.add(alert.resource_name)
        if alert.namespace:
            targets.add(alert.namespace)

        matched: List[Dict[str, Any]] = []
        for repo in self._repos:
            repo_hosts = set(repo.get("hosts") or [])
            repo_services = set(repo.get("services") or [])
            if targets & repo_hosts or targets & repo_services:
                matched.append(repo)

        # If nothing matched by host/service, fall back to all repos (they
        # are likely relevant on a single-host setup).
        if not matched and len(self._repos) == 1:
            matched = list(self._repos)

        return matched

    # ------------------------------------------------------------------
    # Commit fetching
    # ------------------------------------------------------------------

    def _fetch_commits_local(self, repo: Dict[str, Any]) -> List[Dict[str, str]]:
        """Fetch recent commits from a local (or SSH-accessible) repo."""
        import shlex
        import subprocess

        path = repo.get("path")
        if not path:
            return []
        path = os.path.expanduser(path)
        fmt = "--format=%H|%an|%ai|%s"
        git_cmd = f"git -C {shlex.quote(path)} log {fmt} --no-merges -n {self._commit_count}"

        ssh_cfg = repo.get("ssh")
        if ssh_cfg:
            known_hosts_path = ssh_cfg.get("known_hosts_path")
            parts: list[str] = [
                "ssh",
                "-o", f"ConnectTimeout={ssh_cfg.get('connect_timeout', 5)}",
            ]
            if known_hosts_path:
                parts.extend(["-o", "StrictHostKeyChecking=yes"])
                parts.extend(["-o", f"UserKnownHostsFile={os.path.expanduser(str(known_hosts_path))}"])
            else:
                parts.extend(["-o", "StrictHostKeyChecking=accept-new"])
            key = ssh_cfg.get("key_path")
            if key:
                parts.extend(["-i", os.path.expanduser(str(key))])
            port = ssh_cfg.get("port")
            if port:
                parts.extend(["-p", str(port)])
            parts.append(f"{ssh_cfg['user']}@{ssh_cfg['address']}")
            parts.append(git_cmd)
        else:
            parts = ["sh", "-c", git_cmd]

        try:
            result = subprocess.run(parts, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return []
        except Exception:
            logger.debug("Failed to fetch local commits for %s", repo.get("name"), exc_info=True)
            return []

        commits: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            segs = line.split("|", 3)
            if len(segs) == 4:
                commits.append({"hash": segs[0], "author": segs[1], "date": segs[2], "message": segs[3]})
        return commits

    def _fetch_commits_github(self, repo: Dict[str, Any]) -> List[Dict[str, str]]:
        """Fetch recent commits via the GitHub API (stdlib only)."""
        slug = repo.get("github")
        if not slug:
            return []
        try:
            slug = validate_repo_slug(str(slug))
        except ValueError:
            logger.warning("Invalid repo slug: %s", slug)
            return []

        params = {"per_page": str(self._commit_count)}
        branch = repo.get("branch")
        if branch:
            params["sha"] = str(branch)
        response = self._github_client.request(
            "GET",
            f"/repos/{slug}/commits",
            params=params,
            cache_ttl=120,
        )
        if not response["success"]:
            logger.debug("Failed to fetch GitHub commits for %s: %s", slug, response.get("error"))
            return []
        data = response.get("data")
        if not isinstance(data, list):
            return []

        return [
            {
                "hash": c["sha"],
                "author": (c.get("commit", {}).get("author") or {}).get("name", ""),
                "date": (c.get("commit", {}).get("author") or {}).get("date", ""),
                "message": c.get("commit", {}).get("message", "").split("\n", 1)[0],
            }
            for c in data
            if isinstance(c, dict)
        ]

    # ------------------------------------------------------------------
    # ContextProvider interface
    # ------------------------------------------------------------------

    def provide(self, alert: Alert, envelope: ContextEnvelope) -> ContextEnvelope:
        matched_repos = self._repos_for_alert(alert)
        if not matched_repos:
            return envelope

        all_changes: List[Dict[str, Any]] = []
        for repo in matched_repos:
            name = repo.get("name") or repo.get("github", "unknown")
            # GitHub API is the default — repos typically aren't cloned on
            # deployed machines.  Fall back to local git only when the API
            # is unavailable (no token or network issue) and a path exists.
            commits = self._fetch_commits_github(repo)
            source = "github"
            if not commits:
                commits = self._fetch_commits_local(repo)
                source = "local"
            if commits:
                all_changes.append({"repo": name, "source": source, "commits": commits})

        if all_changes:
            envelope.context["recent_changes"] = all_changes
            envelope.notes.append(
                f"Attached recent code changes from {len(all_changes)} repo(s): "
                + ", ".join(c["repo"] for c in all_changes)
            )
        return envelope
