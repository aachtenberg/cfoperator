"""Grail query client: DQL over the Dynatrace platform API (CFOP-203).

Stdlib only, because it runs inside the event runtime, which keeps a
stdlib-only posture (see ``build_portable_runtime``).

The query API lives on the platform host, ``https://<env>.apps.dynatrace.com``,
and takes a platform token (``dt0s16.``...) or an OAuth bearer token with the
``storage:*:read`` scopes the query touches. The classic
``<env>.live.dynatrace.com`` host does not serve it: it answers an HTML
``403 Request forbidden by administrative rules``, which reads like a
permissions problem, so that host is refused up front instead.

A query starts with ``query:execute``. Grail answers inline when the result is
ready within ``requestTimeoutMilliseconds``; otherwise it hands back a request
token for ``query:poll``, which long-polls until the state is terminal. The
token is single-use: polling it again after success answers 410 QUERY_GONE.

Failures raise ``GrailQueryError`` carrying Grail's own message, and are never
turned into an empty result. Whatever reads the output -- an alert source, or a
model reading attached evidence -- treats an empty read as a finding, so a
failure that looked empty would be a false one.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

API = "/platform/storage/query/v1"
_PENDING_STATES = {"RUNNING", "NOT_STARTED"}
# The longest one execute/poll call asks Grail to hold the request open, so a
# slow query still gets a deadline check between polls.
_LONG_POLL_MS = 10_000
# Socket timeout on top of the time Grail was asked to hold the request.
_SOCKET_GRACE_S = 10.0


class GrailQueryError(RuntimeError):
    """A DQL query failed. The message is Grail's own where Grail gave one."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        query_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.query_id = query_id


@dataclass(frozen=True)
class GrailResult:
    """Records exactly as Grail sent them: note that longs arrive as strings."""

    records: List[Dict[str, Any]]
    types: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "GrailResult":
        result = payload.get("result") or {}
        return cls(
            records=list(result.get("records") or []),
            types=list(result.get("types") or []),
            metadata=dict(result.get("metadata") or {}),
        )

    @property
    def notifications(self) -> List[Dict[str, Any]]:
        return list((self.metadata.get("grail") or {}).get("notifications") or [])

    @property
    def warnings(self) -> List[str]:
        """Grail's notices, such as "Your result has been limited to 2."

        A limited result is a partial one. Whoever summarises it should say so
        rather than present it as complete.
        """
        return [str(n.get("message") or n.get("notificationType")) for n in self.notifications]


class GrailClient:
    """Run DQL against one Dynatrace environment."""

    def __init__(self, url: str, token: str, *, timeout: float = 30.0, max_records: int = 1000) -> None:
        url = (url or "").strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"Dynatrace environment URL must be absolute, got {url!r}")
        if parts.hostname.endswith(".live.dynatrace.com"):
            apps = parts.hostname.replace(".live.", ".apps.", 1)
            raise ValueError(
                f"Grail's query API is on the platform host (https://{apps}); "
                f"{parts.hostname} answers it with an HTML 403"
            )
        if not (token or "").strip():
            raise ValueError("Dynatrace platform token is empty")
        self.url = url
        self._token = token.strip()
        self.timeout = float(timeout)
        self.max_records = int(max_records)

    def __repr__(self) -> str:
        return f"GrailClient(url={self.url!r}, timeout={self.timeout:g})"

    def query(
        self,
        dql: str,
        *,
        timeframe_start: datetime | str | None = None,
        timeframe_end: datetime | str | None = None,
        max_records: int | None = None,
    ) -> GrailResult:
        """Run ``dql`` and return its result, polling until done or ``timeout``.

        The timeframe bounds a query that does not set its own ``from:``/``to:``
        (Grail's default is the last two hours). Raises ``GrailQueryError``.
        """
        deadline = time.monotonic() + self.timeout
        body: Dict[str, Any] = {
            "query": dql,
            "requestTimeoutMilliseconds": _wait_ms(deadline),
            "maxResultRecords": int(max_records or self.max_records),
        }
        if timeframe_start is not None:
            body["defaultTimeframeStart"] = _iso(timeframe_start)
        if timeframe_end is not None:
            body["defaultTimeframeEnd"] = _iso(timeframe_end)

        payload = self._call("POST", f"{API}/query:execute", body, deadline)
        request_token = payload.get("requestToken")
        while True:
            state = str(payload.get("state") or "")
            if state == "SUCCEEDED":
                return GrailResult.from_payload(payload)
            if state not in _PENDING_STATES:
                raise GrailQueryError(f"query ended in state {state or '(none)'}", error_type=state or None)
            if not request_token:
                raise GrailQueryError(f"query is {state} but Grail returned no request token to poll")
            if time.monotonic() >= deadline:
                self._cancel(request_token)
                raise GrailQueryError(f"query did not finish within {self.timeout:g}s and was cancelled")
            payload = self._call(
                "GET",
                f"{API}/query:poll?request-token={quote(request_token, safe='')}"
                f"&request-timeout-milliseconds={_wait_ms(deadline)}",
                None,
                deadline,
            )

    def _call(self, method: str, path: str, body: Dict[str, Any] | None, deadline: float) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, method=method, headers=headers)
        socket_timeout = max(deadline - time.monotonic(), 0.0) + _SOCKET_GRACE_S
        try:
            with urlopen(request, timeout=socket_timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GrailQueryError(f"cannot reach {self.url}: {reason}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GrailQueryError(f"non-JSON answer from {self.url}{path.split('?')[0]}") from exc
        if not isinstance(payload, dict):
            raise GrailQueryError(f"unexpected answer from {self.url}: {str(payload)[:120]}")
        return payload

    def _cancel(self, request_token: str) -> None:
        """Best effort: free the query on Grail's side. Its answer is not needed."""
        try:
            self._call(
                "POST",
                f"{API}/query:cancel?request-token={quote(request_token, safe='')}",
                None,
                time.monotonic() + 5,
            )
        except GrailQueryError as exc:
            logger.debug("Grail query cancel failed: %s", exc)


def _wait_ms(deadline: float) -> int:
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    return max(1, min(_LONG_POLL_MS, remaining_ms))


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _http_error(exc: HTTPError) -> GrailQueryError:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - the status alone still says something
        raw = ""
    try:
        error = json.loads(raw).get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        error = {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    error_type = details.get("exceptionType") or details.get("errorType")
    message = details.get("errorMessage") or error.get("message") or " ".join(raw.split())[:200] or exc.reason
    text = f"HTTP {exc.code}" + (f" {error_type}" if error_type else "") + f": {message}"
    start = (details.get("syntaxErrorPosition") or {}).get("start") or {}
    if "line" in start and "column" in start:
        text += f" (line {start['line']}, column {start['column']})"
    return GrailQueryError(text, status=exc.code, error_type=error_type, query_id=details.get("queryId"))
