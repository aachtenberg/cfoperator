"""GitHub change-record backend: evidence via git.

open  — commit a record file + open a PR; or, for an intent a person already
        approved (CFOP-442: an admin confirming the command in the console
        chat), commit the record straight to base carrying that approval
approval — merged-status + merger identity (or None / error if closed unmerged);
        for a pre-approved record, the approval read from the committed file
close — commit the execution outcome back onto the record file

Approved/closed state env knobs are reserved for ticket backends (snow/jira);
GitHub treats merge as approval. Stdlib only.

Security: ref tokens are opaque handles only. ``repo`` / ``base`` always come
from the recorder instance config (never from the token). ``path`` must stay
under the configured ``records_dir``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

from github_client import GitHubClient
from shapes import Approval, Intent, RecordRef, encode_ref, record_document, utc_now

logger = logging.getLogger("cfop-changerecord.github")


class ChangeRecordError(RuntimeError):
    """Raised when the recorder cannot proceed safely."""


class GitHubRecorder:
    """Evidence via git: open a record PR; approval = merge; close commits outcome."""

    def __init__(self, client: GitHubClient, repo: str, base: str = "main",
                 records_dir: str = "change-records"):
        self.client = client
        self.repo = repo
        self.base = base
        self.records_dir = records_dir.rstrip("/")

    def _safe_path(self, meta: Dict[str, Any], *, rid_fallback: Any = None) -> str:
        """Resolve a record path from meta, constrained to ``records_dir/``.

        Token-supplied ``path`` is ignored unless it is a file directly under
        the configured records directory (no ``..``, no other prefixes).
        """
        prefix = f"{self.records_dir}/"
        raw = str(meta.get("path") or "").strip()
        if raw:
            # Normalize: reject absolute, parent traversal, and off-prefix paths.
            if raw.startswith("/") or ".." in raw.split("/"):
                raise ChangeRecordError(f"path outside records_dir: {raw}")
            if not raw.startswith(prefix) or raw.rstrip("/") == self.records_dir:
                raise ChangeRecordError(f"path outside records_dir: {raw}")
            # Only allow a single file segment under records_dir.
            rest = raw[len(prefix):]
            if not rest or "/" in rest:
                raise ChangeRecordError(f"path outside records_dir: {raw}")
            return raw
        if rid_fallback is not None:
            return f"{prefix}{rid_fallback}.json"
        pr = meta.get("pr_number")
        if pr is not None:
            return f"{prefix}{pr}.json"
        raise ChangeRecordError("ref missing path")

    def _pr_body(self, intent: Intent) -> str:
        return (
            f"Change record for **node-action** remediation `{intent.remediation_id}`.\n\n"
            f"- **Host:** `{intent.host}`\n"
            f"- **Commands:**\n"
            + "".join(f"  - `{c}`\n" for c in intent.commands)
            + f"- **Justification:** {intent.justification}\n"
            f"- **Executor image:** `{intent.image or 'unset'}`\n"
            f"- **Flag snapshot:** `{json.dumps(intent.flag_snapshot)}`\n\n"
            f"Merge approves execution. The agent blocks executor spawn until this PR merges.\n"
        )

    def _create_pr(self, branch: str, title: str, body: str) -> Dict[str, Any]:
        pr = self.client.request("POST", f"/repos/{self.repo}/pulls",
                                 body={"title": title, "body": body, "head": branch, "base": self.base})
        if not pr.get("success"):
            raise ChangeRecordError(f"change-record PR create failed ({pr.get('status')})")
        return pr.get("data") or {}

    def open(self, intent: Intent) -> RecordRef:
        if intent.approval:
            return self._open_approved(intent)
        doc = record_document(intent)
        rid = intent.remediation_id
        path = f"{self.records_dir}/{rid}.json"
        branch = f"cfop/change/{rid}"
        title = f"cfop: change-record for remediation {rid} on {intent.host}"
        body = self._pr_body(intent)

        if self.client.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}").get("success"):
            # Resume an existing record PR rather than failing.
            owner = self.repo.split("/")[0]
            prs = self.client.request(
                "GET",
                f"/repos/{self.repo}/pulls?head={quote(owner + ':' + branch)}"
                f"&base={quote(self.base)}&state=all",
            )
            pr_list = prs.get("data") if isinstance(prs.get("data"), list) else []
            pr = pr_list[0] if pr_list else {}
            if not pr.get("number"):
                # Branch exists but PR create crashed mid-flight — finish it.
                pr = self._create_pr(branch, title, body)
            meta = {
                "backend": "github",
                "repo": self.repo,
                "base": self.base,
                "branch": branch,
                "path": path,
                "pr_number": pr.get("number"),
            }
            return RecordRef(id=encode_ref(meta), url=pr.get("html_url"), meta=meta)

        head = self.client.request("GET", f"/repos/{self.repo}/git/ref/heads/{self.base}")
        head_sha = ((head.get("data") or {}).get("object") or {}).get("sha") if head.get("success") else None
        if not head_sha:
            raise ChangeRecordError(f"could not read base ref {self.base} on {self.repo}")

        cr = self.client.request("POST", f"/repos/{self.repo}/git/refs",
                                 body={"ref": f"refs/heads/{branch}", "sha": head_sha})
        if not cr.get("success"):
            raise ChangeRecordError(f"branch create failed ({cr.get('status')})")

        content_b64 = base64.b64encode(
            json.dumps(doc, indent=2, default=str).encode("utf-8")
        ).decode("ascii")
        put_body: Dict[str, Any] = {
            "message": title,
            "content": content_b64,
            "branch": branch,
        }
        existing = self.client.get_file(self.repo, path, self.base)
        if existing:
            put_body["sha"] = existing[1]

        commit = self.client.request("PUT", f"/repos/{self.repo}/contents/{path}", body=put_body)
        if not commit.get("success"):
            raise ChangeRecordError(f"record commit failed ({commit.get('status')})")

        data = self._create_pr(branch, title, body)
        meta = {
            "backend": "github",
            "repo": self.repo,
            "base": self.base,
            "branch": branch,
            "path": path,
            "pr_number": data.get("number"),
        }
        return RecordRef(id=encode_ref(meta), url=data.get("html_url"), meta=meta)

    def _open_approved(self, intent: Intent) -> RecordRef:
        """Commit a pre-approved record straight to the base branch (CFOP-442).

        A person already approved these exact commands (an admin confirming
        them in the console chat), so there is nothing left to review: a PR
        here would be a second approval of the same thing, and the command
        would wait on a merge nobody is going to make. The record still
        lands in ``records_dir`` like every other change, carrying who
        approved it, and ``close`` commits the outcome onto it.
        """
        doc = record_document(intent)
        rid = intent.remediation_id
        path = f"{self.records_dir}/{rid}.json"
        who = str((intent.approval or {}).get("identity") or "unknown")
        put_body: Dict[str, Any] = {
            "message": (f"cfop: change-record for remediation {rid} on {intent.host} "
                        f"(approved in the console by {who})"),
            "content": base64.b64encode(
                json.dumps(doc, indent=2, default=str).encode("utf-8")).decode("ascii"),
            "branch": self.base,
        }
        existing = self.client.get_file(self.repo, path, self.base)
        if existing:
            put_body["sha"] = existing[1]
        commit = self.client.request("PUT", f"/repos/{self.repo}/contents/{path}", body=put_body)
        if not commit.get("success"):
            raise ChangeRecordError(f"record commit failed ({commit.get('status')})")
        data = commit.get("data") if isinstance(commit.get("data"), dict) else {}
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        url = content.get("html_url") or f"https://github.com/{self.repo}/blob/{self.base}/{path}"
        meta = {
            "backend": "github",
            "repo": self.repo,
            "base": self.base,
            "path": path,
            "approved": True,
            "approval": intent.approval,
        }
        return RecordRef(id=encode_ref(meta), url=url, meta=meta)

    def approval(self, ref_token: str) -> Optional[Approval]:
        from shapes import decode_ref
        meta = decode_ref(ref_token)
        if meta.get("approved"):
            return self._approval_from_record(meta)
        pr_number = meta.get("pr_number")
        if pr_number is None:
            raise ChangeRecordError("ref missing pr_number")
        # Never trust token-supplied repo — use the instance config only.
        r = self.client.request("GET", f"/repos/{self.repo}/pulls/{pr_number}")
        if not r.get("success"):
            raise ChangeRecordError(f"could not fetch PR #{pr_number} ({r.get('status')})")
        data = r.get("data") or {}
        if data.get("merged"):
            merged_by = (data.get("merged_by") or {}).get("login") or "unknown"
            return Approval(
                identity=str(merged_by),
                timestamp=str(data.get("merged_at") or utc_now()),
                state="merged",
            )
        if data.get("state") == "closed":
            raise ChangeRecordError(f"change-record PR #{pr_number} closed without merge")
        return None

    def _approval_from_record(self, meta: Dict[str, Any]) -> Optional[Approval]:
        """The approval a pre-approved record carries, read from the committed
        file on base, never from the token (CFOP-442).

        The token is a handle the client hands back; the file in git is the
        evidence. A token that claims ``approved`` for a path whose record
        holds no approval answers "not approved", like an unmerged PR.
        """
        path = self._safe_path(meta)
        got = self.client.get_file(self.repo, path, self.base)
        if not got:
            raise ChangeRecordError(f"pre-approved change record {path} is not on {self.base}")
        try:
            doc = json.loads(got[0])
        except ValueError:
            raise ChangeRecordError(f"change record {path} is not JSON")
        approval = doc.get("approval") if isinstance(doc, dict) else None
        if not isinstance(approval, dict) or not str(approval.get("identity") or "").strip():
            return None
        return Approval(
            identity=str(approval["identity"]),
            timestamp=str(approval.get("timestamp") or doc.get("opened_at") or utc_now()),
            state=str(approval.get("state") or "approved"),
        )

    def close(self, ref_token: str, outcome: Dict[str, Any]) -> None:
        from shapes import decode_ref
        meta = decode_ref(ref_token)
        path = self._safe_path(meta)
        # Always write into the configured repo/base; token repo/base are ignored.
        # Prefer the file on base (post-merge); fall back to the feature branch.
        # Note: committing to base fails under branch protection — see REMEDIATION.md.
        got = self.client.get_file(self.repo, path, self.base)
        if not got:
            branch = str(meta.get("branch") or "").strip() or f"cfop/change/unknown"
            got = self.client.get_file(self.repo, path, branch)
            if not got:
                raise ChangeRecordError(f"could not load change record {path} to close")
            target_ref = branch
        else:
            target_ref = self.base
        text, sha = got
        try:
            doc = json.loads(text)
        except ValueError:
            doc = {"raw": text}
        doc["outcome"] = outcome
        doc["closed_at"] = utc_now()
        put = self.client.request("PUT", f"/repos/{self.repo}/contents/{path}", body={
            "message": f"cfop: close change-record {meta.get('pr_number') or path}",
            "content": base64.b64encode(
                json.dumps(doc, indent=2, default=str).encode("utf-8")
            ).decode("ascii"),
            "branch": target_ref,
            "sha": sha,
        })
        if not put.get("success"):
            raise ChangeRecordError(f"close commit failed ({put.get('status')})")


def make_recorder(env: Dict[str, str]) -> GitHubRecorder:
    """Build the github recorder from env (this image's only backend)."""
    token = (env.get("GITHUB_TOKEN") or "").strip()
    repo = (env.get("CFOP_GIT_REPO") or "").strip()
    base = (env.get("CFOP_GIT_BASE") or "main").strip()
    records_dir = (env.get("CFOP_CHANGERECORD_RECORDS_DIR") or "change-records").strip()
    if not token:
        raise ChangeRecordError("GITHUB_TOKEN required")
    if not repo:
        raise ChangeRecordError("CFOP_GIT_REPO required")
    # Approved/closed state names stay on the recorder for ticket images;
    # github ignores them (merge = approved) but we log so deploys stay consistent.
    approved = (env.get("CFOP_EXEC_CHANGE_APPROVED_STATE") or "Authorized").strip()
    closed = (env.get("CFOP_EXEC_CHANGE_CLOSED_STATE") or "Closed").strip()
    logger.info("github recorder ready (approved_state=%s closed_state=%s unused for merge)",
                approved, closed)
    return GitHubRecorder(GitHubClient(token), repo, base, records_dir=records_dir)
