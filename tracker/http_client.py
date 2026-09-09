"""One stdlib JSON client shared by every tracker adapter.

Same return shape as ``changerecord/github_client.py`` — ``{success, status,
data}``, never raising on HTTP errors — so the fake-transport test idiom ports
and an adapter can be exercised without a network. Stdlib urllib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class JsonHttp:
    def __init__(self, base_url: str, headers: Dict[str, str], *, timeout: int = 30,
                 user_agent: str = "cfoperator-tracker"):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.headers.setdefault("Accept", "application/json")
        self.headers.setdefault("User-Agent", user_agent)
        self.timeout = timeout

    def request(self, method: str, path: str, *, body: Optional[dict] = None) -> Dict[str, Any]:
        """Return {success, status, data}. Never raises on HTTP errors.

        ``data`` is the parsed JSON body when there is one; for a non-JSON
        error body it is ``{"error": <first 500 chars>}`` so the adapter can
        surface what the tracker actually said.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec - operator URL
                raw = resp.read().decode("utf-8")
                return {"success": True, "status": resp.status, "data": _parse(raw)}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            return {"success": False, "status": e.code, "data": _parse(raw)}
        except urllib.error.URLError as e:
            return {"success": False, "status": 0, "data": {"error": str(e)}}


def _parse(raw: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": raw[:500]}


def error_text(resp: Dict[str, Any]) -> str:
    """One line describing a failed response, for TrackerError messages."""
    data = resp.get("data")
    detail = ""
    if isinstance(data, dict):
        detail = str(data.get("error") or data.get("message") or data.get("detail")
                     or data.get("errorMessages") or "")[:200]
    elif data:
        detail = str(data)[:200]
    return f"HTTP {resp.get('status')}" + (f": {detail}" if detail else "")
