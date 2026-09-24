"""Write each investigation's result back onto its Dynatrace problem (CFOP-206).

A ``CompletionObserver`` (CFOP-212): when an investigation of an alert from the
Davis problem source completes, it comments on that problem, so a Dynatrace
user sees cfoperator's conclusion on the problem itself rather than in a second
console. An observer rather than a notification sink because the write-back
must also happen for the resolved and monitoring outcomes that the
low-severity digest keeps from sinks.

Comments live on the classic environment API, not the platform one:
``POST {api}/api/v2/problems/{problemId}/comments`` with a classic access token
carrying ``problems.write`` (header ``Api-Token``) and a body of
``{"message", "context"}``. The platform token the rest of the plugin reads
with cannot write there. The problem id is the Grail ``event.id`` carried in
the alert's fingerprint.

Posting a comment is not idempotent, so a failed post is logged and not
retried, and each investigation is written at most once per problem.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from event_runtime.models import ActionResult, Alert
from event_runtime.plugins import CompletionObserver

logger = logging.getLogger(__name__)

FINGERPRINT_PREFIX = "dynatrace:"
_MESSAGE_LIMIT = 2000
_REMEMBERED = 500


class CommentError(RuntimeError):
    """A comment could not be written."""


def classic_api_url(platform_url: str) -> str:
    """``https://abc.apps.dynatrace.com`` -> ``https://abc.live.dynatrace.com`` (SaaS only)."""
    parts = urlsplit((platform_url or "").strip().rstrip("/"))
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(".apps.dynatrace.com"):
        raise ValueError(
            f"cannot derive the classic API host from {platform_url!r}; set DT_API_URL "
            "(e.g. https://<env>.live.dynatrace.com)"
        )
    return f"https://{host.replace('.apps.', '.live.', 1)}"


class DynatraceProblemCommenter(CompletionObserver):
    """Comment each completed investigation onto the Dynatrace problem it came from."""

    name = "dynatrace-problem-comments"

    def __init__(self, api_url: str, token: str, *, timeout: float = 10.0, context: str = "cfoperator") -> None:
        url = (api_url or "").strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"Dynatrace API URL must be absolute, got {api_url!r}")
        token = (token or "").strip()
        if not token:
            raise ValueError("Dynatrace problems token is empty")
        if token.startswith("dt0s16."):
            raise ValueError(
                "DT_PROBLEMS_TOKEN is a platform token (dt0s16...); problem comments need a "
                "classic access token (dt0c01...) with problems.write"
            )
        self.api_url = url
        self._token = token
        self._timeout = float(timeout)
        self._context = context
        # Completions arrive on server threads, so "written once" needs the
        # check and the claim to be one step (as EscalationLedger does). The
        # POST itself runs outside the lock: an in-flight claim stops a second
        # post of the same investigation without serialising other problems.
        self._lock = threading.Lock()
        self._written: Set[Tuple[str, str]] = set()
        self._in_flight: Set[Tuple[str, str]] = set()
        self._order: Deque[Tuple[str, str]] = deque()

    def __repr__(self) -> str:
        return f"DynatraceProblemCommenter(api_url={self.api_url!r})"

    def observe(self, alert: Alert, result: ActionResult) -> None:
        fingerprint = alert.fingerprint or ""
        if alert.source != "dynatrace" or not fingerprint.startswith(FINGERPRINT_PREFIX):
            return
        if alert.details.get("resolution") or result.action != "investigate":
            return
        details: Dict[str, Any] = result.details if isinstance(result.details, dict) else {}
        investigation_id = details.get("investigation_id")
        if investigation_id is None:
            return          # a dispatch or stub result, not an investigation's conclusion
        problem_id = fingerprint[len(FINGERPRINT_PREFIX):]
        key = (problem_id, str(investigation_id))
        with self._lock:
            if key in self._written or key in self._in_flight:
                return
            self._in_flight.add(key)
        display_id = (alert.details.get("dynatrace") or {}).get("display_id") or problem_id
        try:
            self._post(problem_id, self._message(result, details, investigation_id))
        except CommentError as exc:
            logger.warning("Could not write investigation #%s back to Dynatrace problem %s: %s",
                           investigation_id, display_id, exc)
            return
        finally:
            with self._lock:
                self._in_flight.discard(key)
        with self._lock:
            self._remember(key)
        logger.info("Wrote investigation #%s back to Dynatrace problem %s", investigation_id, display_id)

    def _message(self, result: ActionResult, details: Dict[str, Any], investigation_id: Any) -> str:
        outcome = details.get("outcome") or ("completed" if result.success else "failed")
        lines = [f"cfoperator investigated this problem (investigation #{investigation_id}, outcome: {outcome})."]
        if result.message:
            lines.append(result.message)
        recommendation = details.get("remediation") or details.get("recommendation")
        if recommendation:
            lines.append(f"Recommendation: {recommendation}")
        snippet = details.get("findings_snippet") or details.get("error")
        if snippet:
            lines.append(f"Summary: {' '.join(str(snippet).split())}")
        if details.get("provider"):
            lines.append(f"Model: {details['provider']}")
        text = "\n\n".join(str(line) for line in lines)
        return text if len(text) <= _MESSAGE_LIMIT else text[: _MESSAGE_LIMIT - 15] + "\n[... trimmed]"

    def _post(self, problem_id: str, message: str) -> None:
        url = f"{self.api_url}/api/v2/problems/{quote(problem_id, safe='')}/comments"
        body = json.dumps({"message": message, "context": self._context}).encode("utf-8")
        request = Request(url, data=body, method="POST", headers={
            "Authorization": f"Api-Token {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=self._timeout) as response:
                response.read()
        except HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - the status alone still says something
                raw = ""
            raise CommentError(f"HTTP {exc.code}: {' '.join(raw.split())[:200] or exc.reason}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CommentError(f"cannot reach {self.api_url}: {getattr(exc, 'reason', exc)}") from exc

    def _remember(self, key: Tuple[str, str]) -> None:
        """Record a written key, forgetting the oldest past the bound. Caller holds the lock."""
        self._written.add(key)
        self._order.append(key)
        while len(self._order) > _REMEMBERED:
            self._written.discard(self._order.popleft())
