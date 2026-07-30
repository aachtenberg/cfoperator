"""Minimal GitHub REST client for the change-record github image.

Copied from executor/github.py (content + refs + pulls only) so this image
stays stdlib-only and has no executor dependency. Stdlib urllib only.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

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
            "User-Agent": "cfoperator-changerecord",
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

    def get_file(self, repo: str, path: str, ref: str) -> Optional[Tuple[str, str]]:
        r = self.request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        if not r.get("success"):
            return None
        d = r.get("data") or {}
        if d.get("encoding") != "base64" or "content" not in d:
            return None
        text = base64.b64decode(d["content"]).decode("utf-8")
        return text, d.get("sha", "")
