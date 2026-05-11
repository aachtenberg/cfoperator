"""
Git Tools for Code Change Investigation
========================================

Provides tools for CFOperator to inspect local or SSH-accessible git
repositories: recent commits, diffs, blame, and file history.
"""

import logging
import os
import shlex
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cfoperator.tools.git")


class GitTools:
    """Git operations on local or SSH-accessible repositories.

    Configured via the ``git.repos`` section of config.yaml.  Each repo entry
    includes an optional ``ssh`` block (user/address/key_path) for remote
    access and a ``path`` pointing to the repository directory on that host.
    """

    def __init__(self, repos_config: List[Dict[str, Any]]):
        self.repos: Dict[str, Dict[str, Any]] = {}
        for entry in repos_config:
            name = entry.get("name") or entry.get("github", "unknown")
            self.repos[name] = entry
        logger.info("Git tools initialized for %d repos", len(self.repos))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_repo(self, repo: str) -> Optional[Dict[str, Any]]:
        """Resolve a repo name to its config entry."""
        if repo in self.repos:
            return self.repos[repo]
        # Fallback: try matching by github slug
        for cfg in self.repos.values():
            if cfg.get("github") == repo:
                return cfg
        return None

    def _run_git(self, repo_cfg: Dict[str, Any], git_args: str, timeout: int = 30) -> Dict[str, Any]:
        """Run a git command locally or over SSH.

        Returns an error result when no ``path`` is configured — callers
        should prefer the GitHub API tools when repos aren't cloned locally.
        """
        path = repo_cfg.get("path")
        if not path:
            return {"success": False, "error": "No local path configured for this repo. Use the github_* tools instead."}
        git_cmd = f"git -C {shlex.quote(path)} {git_args}"

        ssh_cfg = repo_cfg.get("ssh")
        if ssh_cfg:
            ssh_user = ssh_cfg["user"]
            ssh_address = ssh_cfg["address"]
            ssh_key = ssh_cfg.get("key_path")
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
            if ssh_key:
                parts.extend(["-i", os.path.expanduser(str(ssh_key))])
            port = ssh_cfg.get("port")
            if port:
                parts.extend(["-p", str(port)])
            parts.append(f"{ssh_user}@{ssh_address}")
            parts.append(git_cmd)
        else:
            parts = ["sh", "-c", git_cmd]

        try:
            result = subprocess.run(parts, capture_output=True, text=True, timeout=timeout)
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    def recent_commits(
        self,
        repo: str,
        count: int = 10,
        since: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return recent commits from a repository."""
        repo_cfg = self._resolve_repo(repo)
        if repo_cfg is None:
            return {"success": False, "error": f"Unknown repo: {repo}", "available": list(self.repos.keys())}

        fmt = "--format=%H|%an|%ai|%s"
        args = f"log {fmt} --no-merges -n {int(count)}"
        if since:
            args += f" --since={shlex.quote(since)}"
        if branch:
            args += f" {shlex.quote(branch)}"

        raw = self._run_git(repo_cfg, args)
        if not raw["success"]:
            return raw
        commits = []
        for line in raw["stdout"].strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return {"success": True, "repo": repo, "commits": commits}

    def diff_summary(self, repo: str, ref_from: str, ref_to: str = "HEAD") -> Dict[str, Any]:
        """Return ``git diff --stat`` between two refs."""
        repo_cfg = self._resolve_repo(repo)
        if repo_cfg is None:
            return {"success": False, "error": f"Unknown repo: {repo}"}
        raw = self._run_git(repo_cfg, f"diff --stat {shlex.quote(ref_from)}..{shlex.quote(ref_to)}")
        if not raw["success"]:
            return raw
        return {"success": True, "repo": repo, "ref_from": ref_from, "ref_to": ref_to, "diff_stat": raw["stdout"]}

    def show_file(self, repo: str, path: str, ref: str = "HEAD") -> Dict[str, Any]:
        """Return file contents at a specific revision."""
        repo_cfg = self._resolve_repo(repo)
        if repo_cfg is None:
            return {"success": False, "error": f"Unknown repo: {repo}"}
        raw = self._run_git(repo_cfg, f"show {shlex.quote(ref)}:{shlex.quote(path)}")
        if not raw["success"]:
            return raw
        return {"success": True, "repo": repo, "path": path, "ref": ref, "content": raw["stdout"]}

    def blame(self, repo: str, path: str, lines: Optional[str] = None) -> Dict[str, Any]:
        """Return ``git blame`` for a file, optionally scoped to a line range (e.g. ``10,20``)."""
        repo_cfg = self._resolve_repo(repo)
        if repo_cfg is None:
            return {"success": False, "error": f"Unknown repo: {repo}"}
        args = f"blame {shlex.quote(path)}"
        if lines:
            args += f" -L {shlex.quote(lines)}"
        raw = self._run_git(repo_cfg, args)
        if not raw["success"]:
            return raw
        return {"success": True, "repo": repo, "path": path, "blame": raw["stdout"]}

    def log_path(self, repo: str, path: str, count: int = 10) -> Dict[str, Any]:
        """Return commit history scoped to a specific file or directory."""
        repo_cfg = self._resolve_repo(repo)
        if repo_cfg is None:
            return {"success": False, "error": f"Unknown repo: {repo}"}
        fmt = "--format=%H|%an|%ai|%s"
        raw = self._run_git(repo_cfg, f"log {fmt} -n {int(count)} -- {shlex.quote(path)}")
        if not raw["success"]:
            return raw
        commits = []
        for line in raw["stdout"].strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return {"success": True, "repo": repo, "path": path, "commits": commits}

    # ------------------------------------------------------------------
    # LLM tool schemas
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Return JSON schemas for LLM function-calling registration."""
        repo_names = ", ".join(self.repos.keys())
        note = "Requires a local clone (path configured). Prefer github_* tools when no clone is available."
        return [
            {
                "name": "git_recent_commits",
                "description": f"List recent commits from a LOCAL git clone. {note} Repos: {repo_names}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": f"Repository name ({repo_names})"},
                        "count": {"type": "integer", "description": "Number of commits (default 10)", "default": 10},
                        "since": {"type": "string", "description": "Only commits after this date (e.g. '3 days ago', '2024-01-01')"},
                        "branch": {"type": "string", "description": "Branch to inspect (default: repo default branch)"},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "git_diff_summary",
                "description": f"Show file-level diff stats between two git refs from a LOCAL clone. {note}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"},
                        "ref_from": {"type": "string", "description": "Starting ref (commit SHA, tag, branch)"},
                        "ref_to": {"type": "string", "description": "Ending ref (default HEAD)", "default": "HEAD"},
                    },
                    "required": ["repo", "ref_from"],
                },
            },
            {
                "name": "git_show_file",
                "description": f"Read file contents at a specific git revision from a LOCAL clone. {note}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"},
                        "path": {"type": "string", "description": "File path relative to repo root"},
                        "ref": {"type": "string", "description": "Git ref (commit, tag, branch). Default HEAD.", "default": "HEAD"},
                    },
                    "required": ["repo", "path"],
                },
            },
            {
                "name": "git_blame",
                "description": f"Show git blame annotation for a file from a LOCAL clone. {note}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"},
                        "path": {"type": "string", "description": "File path relative to repo root"},
                        "lines": {"type": "string", "description": "Line range (e.g. '10,20'). Omit for whole file."},
                    },
                    "required": ["repo", "path"],
                },
            },
            {
                "name": "git_log_path",
                "description": f"Show commit history for a specific file/directory from a LOCAL clone. {note}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name"},
                        "path": {"type": "string", "description": "File or directory path relative to repo root"},
                        "count": {"type": "integer", "description": "Number of commits (default 10)", "default": 10},
                    },
                    "required": ["repo", "path"],
                },
            },
        ]
