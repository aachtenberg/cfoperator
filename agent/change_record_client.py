"""HTTP client for the changerecord microservice (agent side).

Speaks the stable contract:

  POST {base}/open
  GET  {base}/approval/{ref}
  POST {base}/close   (executor uses this; agent typically does not)

When ``CFOP_EXEC_CHANGE_URL`` is unset the agent never calls this module.
When ``CFOP_CHANGERECORD_SHARED_SECRET`` is set, requests send ``X-CFOP-Token``.
Stdlib only — no dependency on the changerecord image code.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("cfop.change_record")

AUTH_HEADER = "X-CFOP-Token"
SHARED_SECRET_ENV = "CFOP_CHANGERECORD_SHARED_SECRET"  # noqa: S105 - env var name


class ChangeRecordClientError(RuntimeError):
    """Raised when the changerecord service cannot be reached or returns an error.

    ``status`` is the HTTP status (0 for transport failures) so callers can
    distinguish hard failures (409 closed-without-merge) from transient errors.
    """

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _auth_headers() -> Dict[str, str]:
    secret = (os.getenv(SHARED_SECRET_ENV) or "").strip()
    if secret:
        return {AUTH_HEADER: secret}
    return {}


def _request_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
                  timeout: int = 30) -> Dict[str, Any]:
    data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_auth_headers(),
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator URL
            raw = resp.read().decode("utf-8")
            return {"success": True, "status": resp.status,
                    "data": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:500]
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": raw}
        return {"success": False, "status": e.code, "data": payload}
    except urllib.error.URLError as e:
        return {"success": False, "status": 0, "data": {"error": str(e)}}


def open_record(base_url: str, intent: Dict[str, Any], *, timeout: int = 30) -> Dict[str, Any]:
    """POST /open. Returns {ref, url}. Raises on failure."""
    base = base_url.rstrip("/")
    r = _request_json("POST", f"{base}/open", intent, timeout=timeout)
    if not r.get("success"):
        raise ChangeRecordClientError(
            f"open failed ({r.get('status')}): {(r.get('data') or {}).get('error', r.get('data'))}",
            status=r.get("status"),
        )
    data = r.get("data") or {}
    if not data.get("ref"):
        raise ChangeRecordClientError("open returned no ref", status=r.get("status"))
    return data


def get_approval(base_url: str, ref: str, *, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """GET /approval/{ref}. Returns {identity, timestamp, state} or None if not-yet.

    Raises on hard failures (closed-without-merge → 409, transport errors).
    """
    base = base_url.rstrip("/")
    quoted = urllib.parse.quote(ref, safe="")
    r = _request_json("GET", f"{base}/approval/{quoted}", timeout=timeout)
    if r.get("status") == 404:
        return None
    if not r.get("success"):
        raise ChangeRecordClientError(
            f"approval failed ({r.get('status')}): {(r.get('data') or {}).get('error', r.get('data'))}",
            status=r.get("status"),
        )
    data = r.get("data") or {}
    if not data.get("identity"):
        raise ChangeRecordClientError("approval response missing identity",
                                      status=r.get("status"))
    return data


def close_record(base_url: str, ref: str, outcome: Dict[str, Any], *,
                 timeout: int = 30) -> None:
    """POST /close. Raises on failure."""
    base = base_url.rstrip("/")
    r = _request_json("POST", f"{base}/close", {"ref": ref, "outcome": outcome},
                      timeout=timeout)
    if not r.get("success"):
        raise ChangeRecordClientError(
            f"close failed ({r.get('status')}): {(r.get('data') or {}).get('error', r.get('data'))}",
            status=r.get("status"),
        )
