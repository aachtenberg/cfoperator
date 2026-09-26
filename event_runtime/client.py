"""The agent's side of the event runtime's HTTP surface (CFOP-214).

Every call the agent makes to the runtime goes through here, so the runtime's
auth contract lives next to the runtime that enforces it instead of being
re-typed at each call site. That is not tidiness. When cfoperator-deploy #18
switched the runtime's bearer gate on (2026-08-23), the agent's two ``POST
/alert`` callers were hand-rolled ``urlopen`` calls that had never sent an
``Authorization`` header, and their failure path logged at DEBUG. Every sweep
finding and every "Resolved" notice was refused for 34 days, and nothing said
so.

Two credentials, because the runtime has two gates:

- ``CFOP_RUNTIME_TOKEN``, sent as ``Authorization: Bearer``, guards ``/alert``
  and the read endpoints (``verify_runtime_auth``). The runtime and the agent
  must both be given it; the runtime cannot tell a caller that forgot it from
  one that was never meant to call.
- ``CFOP_COMPLETION_SHARED_SECRET``, sent as ``X-CFOP-Token``, guards only the
  completion post-back, which is exempt from the bearer gate
  (``verify_completion_auth``).

``urllib.request.urlopen`` is looked up at call time, not imported by name, so
a test that patches it reaches these calls.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import quote

from .http_actions import COMPLETION_AUTH_HEADER, COMPLETION_SECRET_ENV, RUNTIME_TOKEN_ENV

EVENT_RUNTIME_URL_ENV = "CFOP_EVENT_RUNTIME_URL"

# Outcome of one call, coarse enough to be a metric label. ``unauthorized`` is
# split out from ``http_error`` because it is the one failure the operator
# fixes in deploy config rather than by waiting.
OK = "ok"
UNAUTHORIZED = "unauthorized"
HTTP_ERROR = "http_error"
UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class RuntimeResponse:
    """What came back from one call. Never raises for a transport failure."""

    outcome: str
    http_status: Optional[int] = None
    body: Any = None
    error: str = ""
    # The env var holding the credential this endpoint checks, so a refusal
    # names the one to fix: the bearer gates /alert, the completion secret
    # gates the post-back, and naming the wrong one sends an operator after a
    # variable that is fine.
    credential: str = RUNTIME_TOKEN_ENV

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    @property
    def stops_batch(self) -> bool:
        """True when the rest of a batch would fail the same way.

        A sweep stops forwarding at the first such failure rather than
        hammering a runtime that is down, overloaded or refusing the agent. A
        4xx other than 401/403 is about the one payload, so the rest are still
        worth sending.
        """
        if self.ok:
            return False
        return not (self.outcome == HTTP_ERROR and self.http_status is not None
                    and 400 <= self.http_status < 500)

    def describe(self) -> str:
        if self.outcome == UNAUTHORIZED:
            return (f"event runtime refused the agent ({self.http_status}): "
                    f"is {self.credential} mounted on the agent, and equal to the runtime's?")
        if self.outcome == HTTP_ERROR:
            return f"event runtime returned HTTP {self.http_status}: {self.error}"
        if self.outcome == UNREACHABLE:
            return f"event runtime unreachable: {self.error}"
        return "ok"


class EventRuntimeClient:
    """Calls the runtime with the credentials each endpoint needs."""

    def __init__(self, base_url: str, *, runtime_token: Optional[str] = None,
                 completion_secret: Optional[str] = None, timeout: float = 5.0):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self._runtime_token = (runtime_token or "").strip() or None
        self._completion_secret = (completion_secret or "").strip() or None
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None,
                 *, timeout: float = 5.0) -> Optional["EventRuntimeClient"]:
        """The client for this process, or None when no runtime is configured."""
        env = os.environ if env is None else env
        url = (env.get(EVENT_RUNTIME_URL_ENV) or "").strip()
        if not url:
            return None
        return cls(url, runtime_token=env.get(RUNTIME_TOKEN_ENV),
                   completion_secret=env.get(COMPLETION_SECRET_ENV), timeout=timeout)

    def post_alert(self, payload: Mapping[str, Any]) -> RuntimeResponse:
        """``POST /alert?mode=async`` — queue an alert for triage."""
        return self._request("POST", "/alert?mode=async", payload, self._bearer(), RUNTIME_TOKEN_ENV)

    def post_completion(self, alert_id: str, payload: Mapping[str, Any]) -> RuntimeResponse:
        """``POST /v1/investigations/<id>/complete`` — report an investigation's outcome.

        Carries the completion secret, not the bearer: the path is exempt from
        the bearer gate and has its own.
        """
        headers = {}
        if self._completion_secret:
            headers[COMPLETION_AUTH_HEADER] = self._completion_secret
        path = f"/v1/investigations/{quote(alert_id, safe='')}/complete"
        return self._request("POST", path, payload, headers, COMPLETION_SECRET_ENV)

    def _bearer(self) -> dict:
        return {"Authorization": f"Bearer {self._runtime_token}"} if self._runtime_token else {}

    def _request(self, method: str, path: str, payload: Optional[Mapping[str, Any]],
                 headers: Mapping[str, str], credential: str) -> RuntimeResponse:
        data = None
        all_headers = dict(headers)
        if payload is not None:
            data = json.dumps(payload, default=str).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            outcome = UNAUTHORIZED if exc.code in (401, 403) else HTTP_ERROR
            return RuntimeResponse(outcome, exc.code, error=_error_text(exc), credential=credential)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return RuntimeResponse(UNREACHABLE, error=f"{type(exc).__name__}: {exc}",
                                   credential=credential)
        if status is not None and not 200 <= status < 300:
            return RuntimeResponse(HTTP_ERROR, status, error="non-2xx response", credential=credential)
        return RuntimeResponse(OK, status, body=_decode(raw), credential=credential)


def _decode(raw: Any) -> Any:
    if not raw or not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:
        body = b""
    decoded = _decode(body)
    if isinstance(decoded, dict) and decoded.get("error"):
        return str(decoded["error"])[:200]
    return str(exc.reason or "")[:200]
