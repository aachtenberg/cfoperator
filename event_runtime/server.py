"""Portable stdlib HTTP server for the event runtime."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .engine import EventRuntime
from .models import Alert, AlertSeverity
from .worker import BackgroundAlertWorker


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _parse_alert(payload: Dict[str, Any]) -> Alert:
    severity_value = str(payload.get("severity") or "info").lower()
    try:
        severity = AlertSeverity(severity_value)
    except ValueError as exc:
        raise ValueError(f"Invalid severity: {severity_value}") from exc

    summary = payload.get("summary")
    if not summary:
        raise ValueError("Missing required field: summary")

    return Alert(
        source=str(payload.get("source") or "manual"),
        severity=severity,
        summary=str(summary),
        details=dict(payload.get("details") or {}),
        namespace=payload.get("namespace"),
        resource_type=payload.get("resource_type"),
        resource_name=payload.get("resource_name"),
        fingerprint=payload.get("fingerprint"),
    )


def make_handler(runtime: EventRuntime, worker: BackgroundAlertWorker | None = None):
    """Create a request handler bound to the provided runtime."""

    class EventRuntimeHandler(BaseHTTPRequestHandler):
        server_version = "CFOperatorEventRuntime/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                payload = runtime.health()
                if worker is not None:
                    payload["worker"] = worker.health()
                _json_response(self, HTTPStatus.OK, payload)
                return
            if parsed.path == "/history":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["50"])[0])
                _json_response(self, HTTPStatus.OK, {"events": runtime.recent_events(limit=limit)})
                return
            if parsed.path.startswith("/jobs/"):
                if worker is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Worker not enabled"})
                    return
                job_id = parsed.path.rsplit("/", 1)[-1]
                job = worker.get_job(job_id)
                if job is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                    return
                _json_response(self, HTTPStatus.OK, job)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.path != "/alert":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return

            try:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                alert = _parse_alert(payload)
                query = parse_qs(parsed.query)
                mode = query.get("mode", ["async" if worker else "sync"])[0]
                if worker is not None and mode != "sync":
                    job = worker.enqueue(alert)
                    _json_response(self, HTTPStatus.ACCEPTED, {"status": "queued", "job": job})
                else:
                    result = runtime.handle_alert(alert)
                    _json_response(self, HTTPStatus.OK, result)
            except ValueError as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except json.JSONDecodeError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: Tuple[Any, ...]) -> None:
            return None

    return EventRuntimeHandler


def serve(
    runtime: EventRuntime,
    host: str = "0.0.0.0",
    port: int = 8080,
    worker: BackgroundAlertWorker | None = None,
) -> None:
    """Start the portable threaded HTTP server."""
    if worker is not None:
        worker.start()
    handler = make_handler(runtime, worker=worker)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if worker is not None:
            worker.stop()