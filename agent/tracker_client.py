"""HTTP client for the cfop-tracker service (agent side).

Speaks the stable contract:

  POST {base}/items
  POST {base}/items/{ref}/comment
  POST {base}/items/{ref}/transition
  GET  {base}/items/{ref}

When ``CFOP_TRACKER_URL`` is unset the agent never calls this module. When
``CFOP_TRACKER_SHARED_SECRET`` is set, requests send ``X-CFOP-Token``.
Stdlib only — no dependency on the tracker image code. Same shape as
``change_record_client.py``, kept separate because the two contracts are
different things (a gate versus a hand-off) and must not grow into each other.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("cfop.tracker")

AUTH_HEADER = "X-CFOP-Token"
SHARED_SECRET_ENV = "CFOP_TRACKER_SHARED_SECRET"  # noqa: S105 - env var name


class TrackerClientError(RuntimeError):
    """The tracker service could not be reached or refused the request.

    ``status`` is the HTTP status (0 for transport failures) so a caller can
    tell a dead service from an item the backend would not accept.
    """

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _auth_headers() -> Dict[str, str]:
    secret = (os.getenv(SHARED_SECRET_ENV) or "").strip()
    return {AUTH_HEADER: secret} if secret else {}


def _request_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
                  timeout: int = 30) -> Dict[str, Any]:
    data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json", **_auth_headers()}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator URL
            raw = resp.read().decode("utf-8")
            return {"success": True, "status": resp.status, "data": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:500]
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": raw}
        return {"success": False, "status": e.code, "data": payload}
    except urllib.error.URLError as e:
        return {"success": False, "status": 0, "data": {"error": str(e)}}


def _fail(what: str, r: Dict[str, Any]) -> TrackerClientError:
    detail = (r.get("data") or {}).get("error", r.get("data")) if isinstance(r.get("data"), dict) else r.get("data")
    return TrackerClientError(f"{what} failed ({r.get('status')}): {detail}", status=r.get("status"))


def _item_path(base_url: str, ref: str, suffix: str = "") -> str:
    return f"{base_url.rstrip('/')}/items/{urllib.parse.quote(ref, safe='')}{suffix}"


def create_item(base_url: str, item: Dict[str, Any], *, timeout: int = 30) -> Dict[str, Any]:
    """POST /items. Returns {ref, url, key, backend}. Raises on failure."""
    r = _request_json("POST", f"{base_url.rstrip('/')}/items", item, timeout=timeout)
    if not r.get("success"):
        raise _fail("create", r)
    data = r.get("data") or {}
    if not data.get("ref"):
        raise TrackerClientError("create returned no ref", status=r.get("status"))
    return data


def comment_item(base_url: str, ref: str, body_markdown: str, *, timeout: int = 30) -> None:
    """POST /items/{ref}/comment. Raises on failure."""
    r = _request_json("POST", _item_path(base_url, ref, "/comment"),
                      {"body_markdown": body_markdown}, timeout=timeout)
    if not r.get("success"):
        raise _fail("comment", r)


def transition_item(base_url: str, ref: str, state: str, note: str = "", *,
                    timeout: int = 30) -> None:
    """POST /items/{ref}/transition with state resolved | rejected. Raises on failure."""
    r = _request_json("POST", _item_path(base_url, ref, "/transition"),
                      {"state": state, "note": note or ""}, timeout=timeout)
    if not r.get("success"):
        raise _fail("transition", r)


def get_item(base_url: str, ref: str, *, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """GET /items/{ref}. Returns {state, url, key, updated_at}, or None when the
    item no longer exists on the backend (404). Raises on other failures."""
    r = _request_json("GET", _item_path(base_url, ref), timeout=timeout)
    if r.get("status") == 404:
        return None
    if not r.get("success"):
        raise _fail("get", r)
    data = r.get("data") or {}
    if not data.get("state"):
        raise TrackerClientError("get returned no state", status=r.get("status"))
    return data
