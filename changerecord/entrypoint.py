"""cfoperator-changerecord: stdlib HTTP service for the imperative-lane gate.

Same Service name, swappable Deployment image (github today; snow/jira later).
Contract (stable across images):

  POST /open              create a change record from intent
  GET  /approval/{ref}    named identity + timestamp if approved, else 404
  POST /close             body {ref, outcome}; record per-command results
  GET  /healthz           liveness

Selected entirely by which image the Deployment runs — the agent and executor
only speak this HTTP contract via CFOP_EXEC_CHANGE_URL. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import unquote

from github_recorder import ChangeRecordError, make_recorder
from shapes import intent_from_body

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cfop-changerecord")

_APPROVAL_RE = re.compile(r"^/approval/(.+)$")


class ChangeRecordHandler(BaseHTTPRequestHandler):
    """HTTP front for one recorder instance (injected on the server)."""

    server_version = "cfop-changerecord/1.0"

    @property
    def recorder(self) -> Any:
        return self.server.recorder  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - " + fmt, self.address_string(), *args)

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

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/livez"):
            self._write_json(200, {"ok": True})
            return
        m = _APPROVAL_RE.match(self.path.split("?", 1)[0])
        if not m:
            self._write_json(404, {"error": "not found"})
            return
        ref = unquote(m.group(1))
        try:
            approval = self.recorder.approval(ref)
        except ChangeRecordError as e:
            self._write_json(409, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            logger.error("approval failed: %s", e, exc_info=True)
            self._write_json(500, {"error": str(e)})
            return
        if approval is None:
            self._write_json(404, {"error": "not approved"})
            return
        self._write_json(200, approval.to_dict())

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/open":
            self._handle_open()
            return
        if path == "/close":
            self._handle_close()
            return
        self._write_json(404, {"error": "not found"})

    def _handle_open(self) -> None:
        try:
            body = self._read_json()
            intent = intent_from_body(body)
            ref = self.recorder.open(intent)
        except ChangeRecordError as e:
            self._write_json(400, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            logger.error("open failed: %s", e, exc_info=True)
            self._write_json(500, {"error": str(e)})
            return
        self._write_json(201, ref.to_dict())

    def _handle_close(self) -> None:
        try:
            body = self._read_json()
            ref = str(body.get("ref") or "").strip()
            outcome = body.get("outcome")
            if not ref:
                raise ValueError("ref required")
            if not isinstance(outcome, dict):
                raise ValueError("outcome must be an object")
            self.recorder.close(ref, outcome)
        except ChangeRecordError as e:
            self._write_json(400, {"error": str(e)})
            return
        except ValueError as e:
            self._write_json(400, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            logger.error("close failed: %s", e, exc_info=True)
            self._write_json(500, {"error": str(e)})
            return
        self._write_json(200, {"ok": True})


def make_server(env: Dict[str, str], *, recorder: Any = None
                ) -> Tuple[ThreadingHTTPServer, Any]:
    host = (env.get("CFOP_CHANGERECORD_HOST") or "0.0.0.0").strip()
    port = int(env.get("CFOP_CHANGERECORD_PORT") or "8091")
    rec = recorder if recorder is not None else make_recorder(env)
    httpd = ThreadingHTTPServer((host, port), ChangeRecordHandler)
    httpd.recorder = rec  # type: ignore[attr-defined]
    return httpd, rec


def main() -> int:
    env = dict(os.environ)
    try:
        httpd, _ = make_server(env)
    except ChangeRecordError as e:
        logger.error("recorder init failed: %s", e)
        return 1
    host, port = httpd.server_address[:2]
    logger.info("listening on %s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
