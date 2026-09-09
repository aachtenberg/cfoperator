"""cfoperator-tracker: stdlib HTTP service that hands remediation rows to an issue tracker.

One ClusterIP Service (``cfop-tracker``); the backend is chosen by
``CFOP_TRACKER_BACKEND`` (plane | github | jira) on this Deployment, and the
tracker's credential lives only here. The agent speaks this contract via
``CFOP_TRACKER_URL`` and never learns which tracker is behind it.

Contract (stable across backends):

  POST /items                    create an item          -> 201 {ref, url, key, backend}
  POST /items/{ref}/comment      body {body_markdown}    -> 200 {ok}
  POST /items/{ref}/transition   body {state, note}      -> 200 {ok}   state: resolved | rejected
  GET  /items/{ref}              current state           -> 200 {state, url, key, updated_at}
  GET  /healthz | /livez         liveness (unauthenticated)

When ``CFOP_TRACKER_SHARED_SECRET`` is set, everything but the health routes
requires ``X-CFOP-Token`` (same idiom as changerecord and completion auth).

Unlike changerecord this is not a gate: nothing in the agent blocks on it. A
row whose item cannot be filed keeps its status and records the error.
Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

from backends import TrackerError, TrackerNotFound, make_backend
from shapes import TRANSITION_STATES, decode_ref, item_from_body

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cfop-tracker")

_ITEM_RE = re.compile(r"^/items/([^/]+)$")
_COMMENT_RE = re.compile(r"^/items/([^/]+)/comment$")
_TRANSITION_RE = re.compile(r"^/items/([^/]+)/transition$")
AUTH_HEADER = "X-CFOP-Token"
SHARED_SECRET_ENV = "CFOP_TRACKER_SHARED_SECRET"  # noqa: S105 - env var name


def _expected_secret(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    src = env if env is not None else os.environ
    value = (src.get(SHARED_SECRET_ENV) or "").strip()
    return value or None


def verify_tracker_auth(token_header: Optional[str], expected: Optional[str]) -> Optional[str]:
    """Return an error string if unauthorized; None if ok.

    When ``expected`` is None (secret unset), requests are accepted — portable
    local tests / early deploys stay runnable. The server logs that once.
    """
    if expected is None:
        return None
    if not token_header:
        return f"Missing {AUTH_HEADER} header"
    if not secrets.compare_digest(token_header, expected):
        return f"Invalid {AUTH_HEADER} header"
    return None


class TrackerHandler(BaseHTTPRequestHandler):
    """HTTP front for one backend instance (injected on the server)."""

    server_version = "cfop-tracker/1.0"

    @property
    def backend(self) -> Any:
        return self.server.backend  # type: ignore[attr-defined]

    @property
    def shared_secret(self) -> Optional[str]:
        return getattr(self.server, "shared_secret", None)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - " + fmt, self.address_string(), *args)

    # -- plumbing -----------------------------------------------------------

    def _require_auth(self) -> bool:
        err = verify_tracker_auth(self.headers.get(AUTH_HEADER), self.shared_secret)
        if err:
            self._write_json(401, {"error": err})
            return False
        return True

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _write_json(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _meta(self, token: str) -> Optional[Dict[str, Any]]:
        """Decode a ref and check it belongs to the running backend, else 400."""
        try:
            meta = decode_ref(unquote(token))
        except ValueError as e:
            self._write_json(400, {"error": str(e)})
            return None
        if meta.get("backend") != self.backend.name:
            self._write_json(400, {"error": f"ref is for backend {meta.get('backend')!r}, "
                                            f"this service runs {self.backend.name!r}"})
            return None
        return meta

    def _run(self, fn, *, ok_status: int = 200, ok_body: Optional[Dict[str, Any]] = None) -> None:
        """Call a backend operation and translate its outcome to a response.

        400 for TrackerError / ValueError (the message is the backend's own
        reason and safe to show), 404 for TrackerNotFound, and a generic 500 for
        anything else — never the exception text, which may carry a URL with a
        token in it.
        """
        try:
            result = fn()
        except TrackerNotFound as e:
            self._write_json(404, {"error": str(e)})
            return
        except (TrackerError, ValueError) as e:
            self._write_json(400, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            logger.error("backend call failed: %s", e, exc_info=True)
            self._write_json(500, {"error": "internal error"})
            return
        self._write_json(ok_status, ok_body if ok_body is not None else result)

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/livez"):
            self._write_json(200, {"ok": True, "backend": getattr(self.backend, "name", None)})
            return
        m = _ITEM_RE.match(path)
        if not m:
            self._write_json(404, {"error": "not found"})
            return
        if not self._require_auth():
            return
        meta = self._meta(m.group(1))
        if meta is None:
            return
        self._run(lambda: self.backend.get(meta).to_dict())

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/items":
            self._handle_create()
            return
        m = _COMMENT_RE.match(path)
        if m:
            self._handle_comment(m.group(1))
            return
        m = _TRANSITION_RE.match(path)
        if m:
            self._handle_transition(m.group(1))
            return
        self._write_json(404, {"error": "not found"})

    def _handle_create(self) -> None:
        if not self._require_auth():
            return

        def go():
            item = item_from_body(self._read_json())
            return self.backend.create(item).to_dict()

        self._run(go, ok_status=201)

    def _handle_comment(self, token: str) -> None:
        if not self._require_auth():
            return
        meta = self._meta(token)
        if meta is None:
            return

        def go():
            body = self._read_json()
            text = str(body.get("body_markdown") or "").strip()
            if not text:
                raise ValueError("body_markdown required")
            self.backend.comment(meta, text)

        self._run(go, ok_body={"ok": True})

    def _handle_transition(self, token: str) -> None:
        if not self._require_auth():
            return
        meta = self._meta(token)
        if meta is None:
            return

        def go():
            body = self._read_json()
            state = str(body.get("state") or "").strip().lower()
            if state not in TRANSITION_STATES:
                raise ValueError(f"state must be one of {', '.join(TRANSITION_STATES)}")
            note = str(body.get("note") or "").strip()
            self.backend.transition(meta, state, note)

        self._run(go, ok_body={"ok": True})


def make_server(env: Dict[str, str], *, backend: Any = None
                ) -> Tuple[ThreadingHTTPServer, Any]:
    host = (env.get("CFOP_TRACKER_HOST") or "0.0.0.0").strip()
    port = int(env.get("CFOP_TRACKER_PORT") or "8092")
    be = backend if backend is not None else make_backend(env)
    httpd = ThreadingHTTPServer((host, port), TrackerHandler)
    httpd.backend = be  # type: ignore[attr-defined]
    httpd.shared_secret = _expected_secret(env)  # type: ignore[attr-defined]
    if httpd.shared_secret:  # type: ignore[attr-defined]
        logger.info("tracker auth enabled (%s required on item endpoints)", AUTH_HEADER)
    else:
        logger.warning("%s unset — /items endpoints accept unauthenticated requests",
                       SHARED_SECRET_ENV)
    return httpd, be


def main() -> int:
    env = dict(os.environ)
    try:
        httpd, be = make_server(env)
    except TrackerError as e:
        logger.error("backend init failed: %s", e)
        return 1
    host, port = httpd.server_address[:2]
    logger.info("listening on %s:%s (backend=%s)", host, port, getattr(be, "name", "?"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
